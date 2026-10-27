"""Markdown and JSON reports."""
from __future__ import annotations

import json
from collections import Counter
from typing import List, Sequence

from .model import ORDER, Finding, Verdict


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _sorted(findings: Sequence[Finding]) -> List[Finding]:
    return sorted(findings, key=lambda f: (ORDER.index(f.verdict), f.file, f.subject))


def markdown(findings: Sequence[Finding], base_label: str, base_sha: str, details: bool = False) -> str:
    title = f"Proof of effect vs {base_label} ({base_sha[:10]})"
    if not findings:
        return f"{title}: no config, flag or env-var changes to check."
    lines = [f"### {title}", "", "| Verdict | File | Setting | Evidence |", "|---|---|---|---|"]
    for f in _sorted(findings):
        verdict = f.verdict.value
        if f.verdict is Verdict.NO_OP and not f.is_problem:
            verdict += " (removal, harmless)"
        subject = f"`{f.subject}`" if "`" not in f.subject else f.subject
        lines.append(f"| {verdict} | {_cell(f.file)} | {_cell(subject)} | {_cell(f.evidence)} |")
    problems = sum(f.is_problem for f in findings)
    probes = sum(f.needs_probe for f in findings)
    lines.append("")
    if problems:
        lines.append(f"**{problems} change(s) do not take effect as written.** Fix or revert them, "
                     "and do not report them as fixes.")
    if probes:
        lines.append(f"{probes} change(s) could not be judged here. Prove them with "
                     "`poe.py probe --files <file> -- <command whose output should change>`.")
    if not problems and not probes:
        lines.append("Every checked change takes effect.")
    if details:
        for f in findings:
            if f.detail:
                lines += ["", f"<details><summary>{_cell(f.subject)}</summary>", "", "```diff",
                          f.detail, "```", "", "</details>"]
    return "\n".join(lines)


def as_json(findings: Sequence[Finding], base_label: str, base_sha: str) -> str:
    counts = Counter(f.verdict.value for f in findings)
    return json.dumps({
        "base": base_label,
        "base_sha": base_sha,
        "summary": {
            "problems": sum(f.is_problem for f in findings),
            "needs_probe": sum(f.needs_probe for f in findings),
            "verdicts": dict(sorted(counts.items())),
        },
        "findings": [f.to_dict() for f in _sorted(findings)],
    }, indent=2)
