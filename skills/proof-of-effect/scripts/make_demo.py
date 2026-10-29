#!/usr/bin/env python3
"""Render the README terminal demo from REAL checker output.

This builds a tiny throwaway git repo that mimics an agent "fix" (one config
key that takes effect, one CLI flag that is a typo of a real one), runs the
actual `poe.py check` against it, and renders the real verdict table as a
self-contained terminal SVG. Nothing in the picture is hand-written: the table
is whatever the checker prints, so the demo can never drift from the truth.

The output is deterministic. Volatile bits (the throwaway repo's commit SHA and
its absolute path) are masked exactly the way `poe.py probe` masks volatile
output, so re-running writes the file back byte-for-byte unchanged. A test runs
this generator and asserts the committed SVG still matches, which is what keeps
the README honest.

Usage:
    python3 skills/proof-of-effect/scripts/make_demo.py            # write the SVG
    python3 skills/proof-of-effect/scripts/make_demo.py --check     # verify it is current
    python3 skills/proof-of-effect/scripts/make_demo.py --print     # print the captured lines
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
POE = HERE / "poe.py"
REPO_ROOT = HERE.parents[2]
SVG_PATH = REPO_ROOT / "research" / "demo" / "demo.svg"

# The command shown in the demo. `check --base HEAD~1` reads naturally for a
# reader looking at a single commit.
SHOWN_COMMAND = "$ poe.py check --base HEAD~1"

# Base tree: a typechecked project with strict mode on.
BASE_FILES = {
    "src/index.ts": "export const greeting: string = 'hi';\n",
    "tsconfig.json": '{\n  "compilerOptions": {"strict": true},\n  "include": ["src"]\n}\n',
    "package.json": '{\n  "name": "demo",\n  "scripts": {"typecheck": "tsc --noEmit"}\n}\n',
}
# The agent's "fix": tighten checks and, it claims, add a null-check flag.
# noUncheckedIndexedAccess really turns on (EFFECTIVE); --strictNullCheck is a
# typo of --strictNullChecks, so tsc never sees it (PHANTOM-FLAG).
CHANGED_FILES = {
    "tsconfig.json": '{\n  "compilerOptions": {"strict": true, "noUncheckedIndexedAccess": true},\n'
                     '  "include": ["src"]\n}\n',
    "package.json": '{\n  "name": "demo",\n  "scripts": {"typecheck": "tsc --noEmit --strictNullCheck"}\n}\n',
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=demo", "-c", "user.email=demo@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def capture_lines() -> List[str]:
    """Build the demo repo, run the real checker, return masked output lines."""
    with tempfile.TemporaryDirectory(prefix="poe-demo-") as tmp:
        root = Path(tmp)
        _git(root, "init", "-q", "-b", "main")
        _write(root, BASE_FILES)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        _write(root, CHANGED_FILES)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "tighten checks")

        # Let poe.py find the tools. Normally they are on PATH (as the README's
        # own instructions assume); the test suite instead points at them with
        # POE_TEST_TOOLPATH, so honour it here too. Unset in production, this is
        # a no-op.
        env = dict(os.environ)
        toolpath = os.environ.get("POE_TEST_TOOLPATH")
        if toolpath:
            env["PATH"] = toolpath + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            [sys.executable, str(POE), "check", "--repo", str(root), "--base", "HEAD~1", "--offline"],
            capture_output=True, text=True, env=env,
        )
        out = proc.stdout
    # Mask volatile output the way `poe.py probe` does: the throwaway repo's SHA
    # and absolute path never belong in a committed picture. The title reads
    # `Proof of effect vs <base-label> (<sha10>)`; keep the readable label, drop
    # the SHA in parentheses so the picture is byte-stable across runs.
    out = re.sub(r"(Proof of effect vs .+?) \([0-9a-f]{7,40}\)", r"\1", out)
    out = out.replace(str(Path(tempfile.gettempdir())), "")
    out = re.sub(r"/poe-demo-[^/ ]+", "", out)
    lines = [SHOWN_COMMAND] + [ln.rstrip() for ln in out.splitlines()]
    return lines


# A tiny, dependency-free terminal renderer. Monospace, dark background, one
# <text> element per line; the prompt line is tinted green.
_FONT = 13
_LINE_H = 20
_PAD = 16
_CHAR_W = 7.8
_BG = "#0d1117"
_FG = "#c9d1d9"
_PROMPT = "#3fb950"
_TITLE = "#58a6ff"


def render_svg(lines: List[str]) -> str:
    width = int(max(len(ln) for ln in lines) * _CHAR_W) + 2 * _PAD
    height = _LINE_H * len(lines) + 2 * _PAD
    rows = []
    y = _PAD + _FONT
    for ln in lines:
        if ln.startswith("$ "):
            fill = _PROMPT
        elif ln.startswith("###"):
            fill = _TITLE
        else:
            fill = _FG
        rows.append(f'<text x="{_PAD}" y="{y}" fill="{fill}" xml:space="preserve">{escape(ln)}</text>')
        y += _LINE_H
    body = "\n  ".join(rows)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
        f'font-size="{_FONT}">\n'
        f'  <rect width="{width}" height="{height}" rx="8" fill="{_BG}"/>\n'
        f'  {body}\n'
        f'</svg>\n'
    )


def build() -> str:
    return render_svg(capture_lines())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render the README terminal demo from real checker output.")
    ap.add_argument("--check", action="store_true", help="verify the committed SVG is up to date (exit 1 if not)")
    ap.add_argument("--print", dest="show", action="store_true", help="print the captured lines and exit")
    args = ap.parse_args(argv)

    if args.show:
        for ln in capture_lines():
            print(ln)
        return 0

    svg = build()
    if args.check:
        current = SVG_PATH.read_text() if SVG_PATH.exists() else ""
        if current != svg:
            print(f"demo.svg is stale; run: python3 {os.path.relpath(__file__, REPO_ROOT)}", file=sys.stderr)
            return 1
        print("demo.svg is up to date")
        return 0

    SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SVG_PATH.write_text(svg)
    print(f"wrote {SVG_PATH.relative_to(REPO_ROOT)} ({len(svg)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
