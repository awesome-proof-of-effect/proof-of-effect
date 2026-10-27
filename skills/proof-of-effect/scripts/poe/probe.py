"""Run any command with the base and the changed version of some files, and compare."""
from __future__ import annotations

import difflib
import re
from typing import Sequence

from .model import Finding, Verdict
from .runner import Context, RunResult, run
from .swap import StateSwapper

VOLATILE = [
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds)\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b\d{2}:\d{2}:\d{2}\b"),
]


def _render(res: RunResult, extra: Sequence["re.Pattern[str]"], raw: bool) -> str:
    text = f"{res.out}\n--- stderr ---\n{res.err}\n--- exit {res.code} ---"
    if not raw:
        for pattern in VOLATILE:
            text = pattern.sub("<volatile>", text)
    for pattern in extra:
        text = pattern.sub("<ignored>", text)
    return text


def probe(ctx: Context, files: Sequence[str], argv: Sequence[str], ignore: Sequence[str] = (),
          raw: bool = False) -> Finding:
    extra = [re.compile(p) for p in ignore]
    entries = [(rel, ctx.base_bytes(rel)) for rel in files]
    subject = ", ".join(files)
    if all(base == ctx.head_bytes(rel) for rel, base in entries):
        return Finding(Verdict.UNVERIFIABLE, "probe", files[0], subject,
                       f"these files have no changes against {ctx.base_label}", change="file")
    label = " ".join(argv)
    after = run(argv, ctx.root, ctx.timeout)
    if after.missing:
        return Finding(Verdict.UNVERIFIABLE, "probe", files[0], subject, f"{argv[0]}: command not found",
                       change="file")
    with StateSwapper(ctx.root, ctx.git_dir, entries):
        before = run(argv, ctx.root, ctx.timeout)
    b, a = _render(before, extra, raw), _render(after, extra, raw)
    if b == a:
        return Finding(Verdict.NO_OP, "probe", files[0], subject,
                       f"`{label}` prints the same output and exit code with the base and the changed files",
                       change="file")
    lines = list(difflib.unified_diff(b.splitlines(), a.splitlines(), "base", "changed", lineterm="", n=1))
    first = next((l for l in lines if l[:1] in "+-" and not l.startswith(("+++", "---"))), "")
    return Finding(Verdict.EFFECTIVE, "probe", files[0], subject,
                   f"`{label}` output changes (first difference: {first.strip()[:100]})",
                   change="file", detail="\n".join(lines[:80]))
