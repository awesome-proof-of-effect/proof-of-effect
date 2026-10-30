#!/usr/bin/env python3
"""Install the proof-of-effect skill into an agent's skills folder.

A coding agent loads a skill by reading its `SKILL.md` from a folder the agent
scans. Different agents scan different folders (Claude Code `~/.claude/skills`,
Codex `~/.codex/skills`, Kiro `~/.kiro/skills`, ...), so "install" is just:
copy this skill directory into the right place under a stable name.

This script does exactly that, in one command, with no dependencies beyond the
Python standard library. It copies the directory that contains the skill's
`SKILL.md` (the folder this file lives in) into `<agent skills dir>/proof-of-effect`,
skipping throwaway artefacts (`.git`, `__pycache__`, `*.pyc`) so a checkout that
has been run against still installs clean.

Usage:
    python3 skills/proof-of-effect/scripts/install.py                 # every detected agent
    python3 skills/proof-of-effect/scripts/install.py --agent claude  # one agent
    python3 skills/proof-of-effect/scripts/install.py --dest DIR      # an explicit skills folder
    python3 skills/proof-of-effect/scripts/install.py --list          # show targets, install nothing
    python3 skills/proof-of-effect/scripts/install.py --dry-run       # show what would happen
    python3 skills/proof-of-effect/scripts/install.py --force         # overwrite an existing copy

Exit code 0 on success (including a no-op dry run), 1 when nothing could be
installed (no agent detected and no --dest, or a refused overwrite).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The skill folder is the parent of `scripts/` (this file's grandparent dir).
SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_NAME = SKILL_DIR.name  # "proof-of-effect"

# Known agents and the skills folder each one scans, relative to $HOME. The keys
# are the names accepted by --agent; `label` is what we print.
AGENTS: Dict[str, Dict[str, str]] = {
    "claude": {"label": "Claude Code", "rel": ".claude/skills"},
    "codex": {"label": "Codex", "rel": ".codex/skills"},
    "kiro": {"label": "Kiro", "rel": ".kiro/skills"},
    "cursor": {"label": "Cursor", "rel": ".cursor/skills"},
    "windsurf": {"label": "Windsurf", "rel": ".windsurf/skills"},
}

# What never belongs in an installed copy: VCS metadata and Python build junk the
# tool leaves behind when it has been run from the source checkout.
_IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo")


def home() -> Path:
    return Path(os.path.expanduser("~"))


def agent_dir(rel: str) -> Path:
    return home() / rel


def detected_agents() -> List[str]:
    """Agents whose PARENT config dir exists (so the agent is plausibly installed)."""
    found = []
    for name, info in AGENTS.items():
        # The skills folder itself may not exist yet, but its parent (e.g. ~/.claude)
        # existing is good evidence the agent is set up on this machine.
        if agent_dir(info["rel"]).parent.is_dir():
            found.append(name)
    return found


def _resolve_targets(args: argparse.Namespace) -> Tuple[List[Tuple[str, Path]], Optional[str]]:
    """Return [(label, skills_dir)], or ([], reason) when there is nothing to do."""
    targets: List[Tuple[str, Path]] = []
    if args.dest:
        targets.append(("--dest", Path(os.path.expanduser(args.dest)).resolve()))
    if args.agent:
        info = AGENTS[args.agent]
        targets.append((info["label"], agent_dir(info["rel"])))
    if not targets:
        names = detected_agents()
        if not names:
            return [], ("no known agent detected under your home directory. "
                        "Pass --agent NAME or --dest DIR. Known agents: "
                        + ", ".join(AGENTS))
        for name in names:
            info = AGENTS[name]
            targets.append((info["label"], agent_dir(info["rel"])))
    return targets, None


def _install_one(skills_dir: Path, *, force: bool, dry_run: bool) -> Tuple[bool, str]:
    """Copy the skill into skills_dir/<SKILL_NAME>. Return (ok, message)."""
    dest = skills_dir / SKILL_NAME
    exists = dest.exists()
    if exists and not force:
        return False, f"{dest} already exists (use --force to overwrite)"
    if dry_run:
        verb = "would overwrite" if exists else "would install"
        return True, f"{verb} {dest}"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if exists:
        shutil.rmtree(dest)
    shutil.copytree(SKILL_DIR, dest, ignore=_IGNORE)
    return True, f"{'reinstalled' if exists else 'installed'} {dest}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Install the proof-of-effect skill into an agent's skills folder.")
    ap.add_argument("--agent", choices=sorted(AGENTS),
                    help="install for one named agent (default: every detected agent)")
    ap.add_argument("--dest", metavar="DIR",
                    help="install into this skills folder instead of a detected one")
    ap.add_argument("--list", action="store_true",
                    help="list the agents that would be targeted and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be copied without copying anything")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing proof-of-effect skill in the target")
    args = ap.parse_args(argv)

    if args.list:
        names = detected_agents()
        if names:
            print("Detected agents:")
            for name in names:
                info = AGENTS[name]
                print(f"  {name:10} {info['label']:14} -> {agent_dir(info['rel']) / SKILL_NAME}")
        else:
            print("No known agent detected under your home directory.")
        print("\nKnown agents (use --agent NAME):")
        for name, info in AGENTS.items():
            print(f"  {name:10} {info['label']:14} ~/{info['rel']}")
        return 0

    targets, reason = _resolve_targets(args)
    if reason:
        print(f"Nothing to install: {reason}", file=sys.stderr)
        return 1

    any_ok = False
    any_fail = False
    for label, skills_dir in targets:
        ok, message = _install_one(skills_dir, force=args.force, dry_run=args.dry_run)
        print(f"  {label}: {message}")
        any_ok = any_ok or ok
        any_fail = any_fail or not ok

    if not any_ok:
        return 1
    if not args.dry_run:
        print(f"\nDone. The agent loads `{SKILL_NAME}` when it edits configuration.")
    return 1 if any_fail and not any_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
