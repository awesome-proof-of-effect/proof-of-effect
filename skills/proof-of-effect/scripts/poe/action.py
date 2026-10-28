"""GitHub Action entrypoint: check a pull request's config changes against its base.

This is the glue the composite `action.yml` calls. It keeps the logic in Python
(not shell) so the same unit-test harness can drive it. It does three things:

1. Work out the base commit to compare against. On a pull request GitHub gives us
   ``GITHUB_BASE_REF`` (the target branch name); the base commit itself is fetched
   into ``refs/remotes/origin/<base>`` by the action's checkout step. Outside a pull
   request (a push) we fall back to the commit before the push, then to ``HEAD~1``.
2. Run the ordinary ``poe.py check --base <base>`` and capture its Markdown report
   and exit code.
3. Append the Markdown to the job summary (``$GITHUB_STEP_SUMMARY``) so it shows up
   on the workflow run page, echo it to the log, and exit non-zero when a change
   does not take effect -- unless ``fail-on-problems`` is false.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from typing import List, Optional, Sequence

from . import cli, gitio


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _truthy(value: str, default: bool) -> bool:
    value = value.strip().lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off", ""):
        return False if value else default
    return default


def resolve_base(root: str, explicit: str = "") -> str:
    """Pick the ref to compare the working tree against, newest-known first.

    An explicit ``--base`` (the ``base`` action input) always wins. On a pull
    request we use ``origin/<GITHUB_BASE_REF>``; the base branch tip is the right
    comparison point for "what did this PR change". On a push we use the event's
    before-SHA, then ``HEAD~1``. Each candidate is only accepted if it resolves to
    a real commit in this clone, so a shallow checkout that is missing the base
    degrades instead of crashing.
    """
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    base_ref = _env("GITHUB_BASE_REF")
    if base_ref:
        candidates.append(f"origin/{base_ref}")
        candidates.append(base_ref)
    before = _env("POE_EVENT_BEFORE")  # github.event.before, passed through by action.yml
    if before and set(before) != {"0"}:  # all-zero SHA means "no previous commit"
        candidates.append(before)
    candidates.append("HEAD~1")
    for ref in candidates:
        try:
            gitio.resolve_commit(root, ref)
        except gitio.GitError:
            continue
        return ref
    raise gitio.GitError(
        "could not find a base commit to compare against; pass `base:` on the action, "
        "or make sure the base branch was fetched (actions/checkout fetch-depth: 0)"
    )


def _summary_path() -> Optional[str]:
    path = _env("GITHUB_STEP_SUMMARY")
    return path or None


def write_summary(markdown: str, path: Optional[str]) -> bool:
    """Append the report to the job summary file. Returns True when it was written."""
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip("\n") + "\n")
    return True


def run(argv: Optional[Sequence[str]] = None) -> int:
    root = gitio.repo_root(".")
    base = resolve_base(root, _env("POE_ACTION_BASE"))
    fail_on_problems = _truthy(_env("POE_ACTION_FAIL_ON_PROBLEMS"), default=True)

    args = ["check", "--base", base]
    extra = _env("POE_ACTION_ARGS")
    if extra:
        args += extra.split()
    if argv is not None:
        args = list(argv)

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(args)
    markdown = buf.getvalue()

    # The check writes its Markdown report to stdout on success (exit 0 or 1) and an
    # error to stderr on exit 2. Surface whatever we captured either way.
    sys.stdout.write(markdown)
    if not markdown.strip():
        markdown = f"Proof of effect: the checker exited with status {code} and no report."
    write_summary(markdown, _summary_path())

    if code == cli.EXIT_ERROR:
        return code  # a real error (bad git ref, etc.) always fails the step
    if code == cli.EXIT_PROBLEMS and not fail_on_problems:
        return cli.EXIT_OK
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(argv)
    except gitio.GitError as exc:
        print(f"proof-of-effect: {exc}", file=sys.stderr)
        return cli.EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
