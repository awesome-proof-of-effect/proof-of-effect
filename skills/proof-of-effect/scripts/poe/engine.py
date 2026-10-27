"""Plan the resolver checks, run each tool before and after a change, and judge the difference.

Every changed file is judged in isolation: the "after" run sees the whole working tree, the
"before" run reverts only that one file, so two edits in the same change never mask each other.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import structdiff as sd
from .gitio import ChangedFile
from .model import Finding, Verdict
from .resolvers import RESOLVERS, Job, Parsed, Resolver
from .runner import Context, run
from .swap import StateSwapper

# pyproject tables that hold packaging or dependency data rather than tool behaviour.
PACKAGING_TABLES = {"poetry", "setuptools", "setuptools_scm", "hatch", "uv", "pdm", "flit", "maturin",
                    "scikit-build", "cibuildwheel", "rye", "pixi", "briefcase", "commitizen",
                    "towncrier", "bumpversion", "semantic_release", "pyproject-fmt"}
KNOWN_TABLES = ["ruff", "pytest", "black", "isort", "mypy", "pyright", "coverage", "pylint", "bandit",
                "codespell", "pydocstyle", "tox", "poetry", "setuptools", "hatch", "uv", "pdm"]


@dataclass
class Item:
    resolver: Resolver
    rel: str
    changes: Optional[List[sd.Change]]  # None: the file is judged as a whole
    jobs: List[Job]
    reason: Optional[str] = None


def _subjects(item: Item):
    if item.changes is None:
        return [("(whole file)", None, "file")]
    return [(sd.fmt_path(c.path), c, c.kind) for c in item.changes]


def _pyproject_tables(rel: str, changes: Sequence[sd.Change], claimed: Set) -> List[Finding]:
    tables: Dict[str, List[sd.Change]] = {}
    for change in changes:
        if change.path in claimed or len(change.path) < 2 or change.path[0] != "tool":
            continue
        table = str(change.path[1])
        if table not in PACKAGING_TABLES:
            tables.setdefault(table, []).append(change)
    out = []
    for table, items in tables.items():
        kind = items[0].kind if len(items) == 1 else "changed"
        close = [t for t in difflib.get_close_matches(table, KNOWN_TABLES, n=1, cutoff=0.8) if t != table]
        if close:
            out.append(Finding(Verdict.NOT_READ, "pyproject", rel, f"tool.{table}",
                               f"no tool reads [tool.{table}]; did you mean [tool.{close[0]}]?", change=kind))
        else:
            out.append(Finding(Verdict.UNVERIFIABLE, "pyproject", rel, f"tool.{table}",
                               f"no checker for [tool.{table}] ({len(items)} key(s) changed); "
                               "prove the effect with `poe.py probe`", change=kind))
    return out


def plan(ctx: Context, changed: Sequence[ChangedFile], only: Optional[Set[str]] = None,
         run_tools: bool = True) -> Tuple[List[Item], List[Finding]]:
    """Decide which tool runs each changed config file needs. Returns (runnable items, findings)."""
    items: List[Item] = []
    findings: List[Finding] = []
    live = [c.path for c in changed if c.status != "D"]
    for cf in changed:
        if cf.status == "D":
            continue
        resolvers = [r for r in RESOLVERS if r.claims(cf.path) and (not only or r.name in only)]
        is_pyproject = os.path.basename(cf.path) == "pyproject.toml" and not only
        if not resolvers and not is_pyproject:
            continue
        ok_b, before = sd.load_structured(cf.path, ctx.base_bytes(cf.path))
        ok_a, after = sd.load_structured(cf.path, ctx.head_bytes(cf.path))
        changes = sd.diff(before, after) if ok_b and ok_a else None
        if changes == []:
            continue  # only comments or formatting changed
        claimed: Set = set()
        for resolver in resolvers:
            if changes is None:
                if not resolver.whole_file_ok(cf.path):
                    continue
                scoped = None
            else:
                scoped = [c for c in changes if resolver.owns(cf.path, c.path)]
                if not scoped:
                    continue
                claimed.update(c.path for c in scoped)
            orphan = resolver.orphan(ctx, cf.path)
            if orphan:
                findings.append(Finding(Verdict.NOT_READ, resolver.name, cf.path, "(whole file)", orphan,
                                        change="file"))
                continue
            if run_tools:
                jobs, reason = resolver.jobs(ctx, cf.path, live)
            else:
                jobs, reason = [], "skipped (--no-swap)"
            items.append(Item(resolver, cf.path, scoped, jobs, reason))
        if is_pyproject and changes:
            findings += _pyproject_tables(cf.path, changes, claimed)
    runnable = []
    for item in items:
        if item.jobs:
            runnable.append(item)
            continue
        for subject, _, kind in _subjects(item):
            findings.append(Finding(Verdict.UNVERIFIABLE, item.resolver.name, item.rel, subject,
                                    item.reason or "no tool available", change=kind))
    return runnable, findings


def execute(ctx: Context, items: Sequence[Item]) -> List[Finding]:
    if not items:
        return []

    def run_job(job: Job) -> Parsed:
        return job.checker.parse(ctx, job, run(job.argv, job.cwd, ctx.timeout, job.env))

    after: Dict = {}
    for item in items:
        for job in item.jobs:
            if job.key not in after:
                after[job.key] = run_job(job)
    by_file: Dict[str, List[Item]] = {}
    for item in items:
        by_file.setdefault(item.rel, []).append(item)
    before: Dict = {}
    for rel, group in by_file.items():
        jobs: Dict = {}
        for item in group:
            for job in item.jobs:
                jobs.setdefault(job.key, job)
        with StateSwapper(ctx.root, ctx.git_dir, [(rel, ctx.base_bytes(rel))]):
            for key, job in jobs.items():
                before[(rel, key)] = run_job(job)
    findings: List[Finding] = []
    for item in items:
        findings += judge(ctx, item, [(before[(item.rel, j.key)], after[j.key]) for j in item.jobs])
    return findings


def _summary(changes: Sequence[sd.Change], limit: int = 3) -> str:
    names = [sd.fmt_path(c.path) for c in changes[:limit]]
    more = len(changes) - limit
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def judge(ctx: Context, item: Item, pairs: Sequence[Tuple[Parsed, Parsed]]) -> List[Finding]:
    resolver, rel = item.resolver, item.rel
    label = item.jobs[0].label
    subjects = _subjects(item)
    explained = resolver.explain(ctx, rel, pairs)
    if explained:
        verdict, why = explained
        return [Finding(verdict, resolver.name, rel, s, why, change=k) for s, _, k in subjects]

    unknown: List[str] = []
    for _, a in pairs:
        unknown += [u for u in a.unknown if u not in unknown]
    a_err = next((a.error for _, a in pairs if a.error), None)
    b_err = next((b.error for b, _ in pairs if b.error), None)
    resolved: List[sd.Change] = []
    for b, a in pairs:
        if b.data is not None and a.data is not None:
            resolved += sd.diff_flat(b.data, a.data)
    loaded = next((a.loaded for _, a in pairs if a.loaded), None)
    # Attribute resolved changes to source keys by name. A key that matches nothing is a no-op when
    # the other keys of this file explain every resolved change.
    matches = {i: sd.match_all(c.path, resolved) for i, (_, c, _) in enumerate(subjects) if c is not None}
    explained = {j for found in matches.values() for j in found}
    unexplained = [r for j, r in enumerate(resolved) if j not in explained]
    unmatched = [i for i, found in matches.items() if not found]
    out: List[Finding] = []

    def add(verdict: Verdict, subject: str, evidence: str, kind: str) -> None:
        out.append(Finding(verdict, resolver.name, rel, subject, evidence, change=kind))

    if item.changes is None:
        for name in unknown:
            add(Verdict.UNKNOWN_KEY, name, f"{resolver.tool} does not recognise `{name}` (`{label}`)", "file")
    for idx, (subject, change, kind) in enumerate(subjects):
        if change is not None:
            hit = sd.match_unknown(change.path, unknown)
            if hit:
                add(Verdict.UNKNOWN_KEY, subject, f"{resolver.tool} does not recognise `{hit}` (`{label}`)", kind)
                continue
        elif unknown and a_err:
            continue  # the UNKNOWN-KEY findings above already explain the failure
        if a_err and unknown:
            add(Verdict.UNCLEAR, subject, f"{resolver.tool} rejects `{unknown[0]}`, so this change cannot be "
                                          "judged; fix that and re-run", kind)
        elif a_err and not b_err:
            add(Verdict.BREAKS, subject, f"`{label}` worked before this change and now fails: {a_err}", kind)
        elif a_err and b_err:
            add(Verdict.UNVERIFIABLE, subject, f"`{label}` fails before and after: {a_err}", kind)
        elif b_err:
            add(Verdict.EFFECTIVE, subject, f"`{label}` failed before this change and works after it", kind)
        elif not resolved:
            why = f"`{label}` resolves the same config before and after"
            if loaded and loaded != rel:
                why = f"{resolver.tool} loads {loaded}, not {rel}; " + why
            add(Verdict.NO_OP, subject, why, kind)
        elif change is None:
            add(Verdict.EFFECTIVE, subject, "resolved config changed: " + _summary(resolved), kind)
        elif matches.get(idx):
            match = resolved[matches[idx][0]]
            add(Verdict.EFFECTIVE, subject, f"resolved {sd.fmt_path(match.path)}: "
                                            f"{sd.short(match.before)} → {sd.short(match.after)}", kind)
        elif not unexplained:
            add(Verdict.NO_OP, subject, f"`{label}` shows no change for it; the resolved changes "
                                        f"({_summary(resolved)}) all come from this file's other edits", kind)
        elif len(unmatched) == 1:
            add(Verdict.EFFECTIVE, subject, "resolved config changed: " + _summary(unexplained), kind)
        else:
            add(Verdict.UNCLEAR, subject, f"resolved config changed ({_summary(unexplained)}) but in no setting "
                                          "named like this one; isolate it with `poe.py probe`", kind)
    return out
