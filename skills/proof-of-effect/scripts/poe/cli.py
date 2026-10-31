"""Command line: `poe.py check`, `poe.py probe`, `poe.py restore`."""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__, engine, gitio, report, static_checks, swap
from .probe import probe
from .runner import Context

EXIT_OK, EXIT_PROBLEMS, EXIT_ERROR, EXIT_PENDING = 0, 1, 2, 3
CHECKERS = ["tsconfig", "eslint", "jest", "prettier", "ruff", "pytest", "compose", "gha", "env", "cli"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="poe.py",
                                description="Prove that config, CLI-flag and env-var changes actually take effect.")
    p.add_argument("--version", action="version", version=f"proof-of-effect {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check every config, flag and env-var change against a base commit")
    c.add_argument("--repo", default=".", help="repository to check (default: current directory)")
    c.add_argument("--base", default="HEAD",
                   help="commit to compare with (default HEAD; use origin/main to check a whole branch)")
    c.add_argument("--only", default="", help="comma-separated checkers: " + ", ".join(CHECKERS))
    c.add_argument("--sample", action="append", default=[], metavar="FILE",
                   help="source file to resolve per-file configs (ESLint, Ruff) for; repeatable")
    c.add_argument("--env-file", action="append", default=[], metavar="FILE",
                   help="also check every variable in this env file (for git-ignored .env files); repeatable")
    c.add_argument("--tool", action="append", default=[], metavar="NAME=PATH",
                   help="use this executable for a tool; repeatable")
    c.add_argument("--allow-cli", action="append", default=[], metavar="NAME",
                   help="also read `NAME --help` to check NAME's flags; repeatable")
    c.add_argument("--offline", action="store_true", help="do not fetch GitHub Action metadata")
    c.add_argument("--all", action="store_true", dest="audit_all",
                   help="audit what is already in the repo, not just changes: every workflow input, env var "
                        "and script flag (static checks only; before/after tool runs need a change)")
    c.add_argument("--no-swap", action="store_true", help="never touch files: skip the before/after tool runs")
    c.add_argument("--timeout", type=float, default=120, help="seconds per tool run (default 120)")
    c.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    c.add_argument("--strict", action="store_true", help="also exit 1 on UNCLEAR and UNVERIFIABLE")

    pr = sub.add_parser("probe", help="run a command with the base and the changed files and compare the output")
    pr.add_argument("--repo", default=".")
    pr.add_argument("--base", default="HEAD")
    pr.add_argument("--files", nargs="+", required=True, metavar="FILE",
                    help="changed files to put back to their base version for the 'before' run")
    pr.add_argument("--ignore", action="append", default=[], metavar="REGEX",
                    help="mask output that matches REGEX; repeatable")
    pr.add_argument("--raw", action="store_true", help="do not mask timings and timestamps")
    pr.add_argument("--shell", metavar="COMMAND", help="run COMMAND with /bin/sh -c instead of an argv after --")
    pr.add_argument("--timeout", type=float, default=300)
    pr.add_argument("--json", action="store_true")
    pr.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")

    r = sub.add_parser("restore", help="put back files left swapped by an interrupted run")
    r.add_argument("--repo", default=".")
    return p


def _check(args) -> int:
    tools = {}
    for spec in args.tool:
        name, sep, path = spec.partition("=")
        if not sep or not name or not path:
            print(f"proof-of-effect: --tool expects NAME=PATH, got {spec!r}", file=sys.stderr)
            return EXIT_ERROR
        tools[name] = path
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    unknown = only - set(CHECKERS)
    if unknown:
        print(f"proof-of-effect: unknown checker(s) {', '.join(sorted(unknown))}; "
              f"choose from {', '.join(CHECKERS)}", file=sys.stderr)
        return EXIT_ERROR
    ctx = Context.create(args.repo, args.base, timeout=args.timeout, tools=tools,
                         allow_cli=set(args.allow_cli), offline=args.offline, audit_all=args.audit_all)
    ctx.samples = [ctx.rel(s) for s in args.sample]
    ctx.env_files = [ctx.rel(e) for e in args.env_file]
    findings = []
    if args.audit_all:
        ctx.base_label = "an empty repo (--all audit)"
        changed = [gitio.ChangedFile(p, "A") for p in ctx.files()]
    else:
        changed = gitio.changed_files(ctx.root, ctx.base)
        items, findings = engine.plan(ctx, changed, only=only or None, run_tools=not args.no_swap)
        findings += engine.execute(ctx, items)
    if not only or "gha" in only:
        findings += static_checks.check_workflows(ctx, changed)
    if not only or "env" in only:
        findings += static_checks.check_env(ctx, changed)
    if not only or "cli" in only:
        findings += static_checks.check_cli_flags(ctx, changed)
    print(report.as_json(findings, ctx.base_label, ctx.base) if args.json
          else report.markdown(findings, ctx.base_label, ctx.base))
    failed = any(f.is_problem or (args.strict and f.needs_probe) for f in findings)
    return EXIT_PROBLEMS if failed else EXIT_OK


def _probe(args) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if args.shell:
        command = ["/bin/sh", "-c", args.shell]
    if not command:
        print("proof-of-effect: probe needs a command: `poe.py probe --files F -- CMD ...` "
              "or `--shell 'CMD'`", file=sys.stderr)
        return EXIT_ERROR
    ctx = Context.create(args.repo, args.base, timeout=args.timeout)
    files = [ctx.rel(f) for f in args.files]
    finding = probe(ctx, files, command, args.ignore, args.raw)
    print(report.as_json([finding], ctx.base_label, ctx.base) if args.json
          else report.markdown([finding], ctx.base_label, ctx.base, details=True))
    return EXIT_PROBLEMS if finding.is_problem else EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = gitio.repo_root(args.repo)
        gdir = gitio.git_dir(root)
        if args.cmd == "restore":
            restored = swap.recover(gdir)
            print("restored: " + ", ".join(restored) if restored else "nothing to restore")
            return EXIT_OK
        if swap.pending(gdir):
            print("proof-of-effect: an earlier run was interrupted and may have left files in their base "
                  "state; run `poe.py restore` first", file=sys.stderr)
            return EXIT_PENDING
        return _check(args) if args.cmd == "check" else _probe(args)
    except (gitio.GitError, swap.SwapError) as exc:
        print(f"proof-of-effect: {exc}", file=sys.stderr)
        return EXIT_ERROR
