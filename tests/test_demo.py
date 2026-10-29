"""The README terminal demo must stay truthful and in sync with real output.

`make_demo.py` renders the demo from the actual checker, so these tests guard
two things: the committed SVG still matches what the generator produces now (it
is not a stale hand-edited picture), and the verdicts in it are the real ones
tsc reports. They need tsc on POE_TEST_TOOLPATH and skip when it is missing,
like the other integration tests.
"""
import subprocess
import sys

import pytest

from conftest import SCRIPTS, have

import make_demo

MAKE_DEMO = SCRIPTS / "make_demo.py"
needs_tsc = pytest.mark.skipif(not have("tsc"), reason="tsc not on POE_TEST_TOOLPATH")


@needs_tsc
def test_committed_svg_is_current():
    """`make_demo.py --check` passes: the committed SVG matches a fresh render."""
    proc = subprocess.run(
        [sys.executable, str(MAKE_DEMO), "--check"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "research/demo/demo.svg is out of date; regenerate it with "
        "`python3 skills/proof-of-effect/scripts/make_demo.py`.\n" + proc.stdout + proc.stderr
    )


@needs_tsc
def test_demo_shows_the_real_verdicts():
    """The demo is built from real output: one phantom flag, one effective change."""
    lines = make_demo.capture_lines()
    text = "\n".join(lines)
    # The command shown is the real check command.
    assert lines[0] == "$ poe.py check --base HEAD~1"
    # The typo'd flag is caught, the real key is confirmed.
    assert "PHANTOM-FLAG" in text
    assert "`tsc --strictNullCheck`" in text
    assert "EFFECTIVE" in text
    assert "compilerOptions.noUncheckedIndexedAccess" in text
    assert "1 change(s) do not take effect as written" in text
    # No volatile data leaked into the picture.
    assert "/tmp" not in text
    assert "poe-demo-" not in text


@needs_tsc
def test_render_is_deterministic():
    """Rendering twice yields byte-identical SVG (so re-running never churns the file)."""
    assert make_demo.build() == make_demo.build()


def test_render_svg_is_wellformed_without_tools():
    """The pure renderer does not need any tool: it turns lines into valid SVG."""
    svg = make_demo.render_svg(["$ poe.py check", "### Proof of effect vs HEAD~1", "ok"])
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert svg.count("<text") == 3
    # The prompt line is tinted; the body line is not.
    assert "#3fb950" in svg  # prompt green
    assert "#58a6ff" in svg  # title blue


def test_render_escapes_markup():
    """A line containing XML metacharacters is escaped, not injected into the SVG."""
    svg = make_demo.render_svg(["a < b & c > d"])
    assert "a &lt; b &amp; c &gt; d" in svg
    assert "<text" in svg and svg.count("<text") == 1
