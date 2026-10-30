"""Tests for the one-command skill installer (ROADMAP #4).

These are hermetic: they never touch the real home directory, never reach the
network, and need no external tool. Agent detection is redirected at a fake
home via $HOME/$USERPROFILE so the suite is the same on every machine.
"""
import os
import subprocess
import sys

import pytest

from conftest import SCRIPTS

INSTALL = SCRIPTS / "install.py"
SKILL_DIR = SCRIPTS.parent  # skills/proof-of-effect
SKILL_NAME = SKILL_DIR.name


def run_install(*args, home=None):
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows home, so the test is cross-platform
    return subprocess.run(
        [sys.executable, str(INSTALL), *args],
        capture_output=True, text=True, env=env,
    )


def test_installs_into_explicit_dest(tmp_path):
    dest = tmp_path / "skills"
    proc = run_install("--dest", str(dest))
    assert proc.returncode == 0, proc.stderr
    installed = dest / SKILL_NAME
    assert (installed / "SKILL.md").is_file()
    assert (installed / "scripts" / "poe.py").is_file()
    assert (installed / "scripts" / "poe" / "engine.py").is_file()


def test_install_excludes_vcs_and_pycache(tmp_path):
    dest = tmp_path / "skills"
    run_install("--dest", str(dest))
    installed = dest / SKILL_NAME
    # No build junk or VCS metadata should ride along, even if the source
    # checkout has been run against and left __pycache__ behind.
    assert not list(installed.rglob("__pycache__"))
    assert not list(installed.rglob("*.pyc"))
    assert not list(installed.rglob(".git"))


def test_dry_run_copies_nothing(tmp_path):
    dest = tmp_path / "skills"
    proc = run_install("--dest", str(dest), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would install" in proc.stdout
    assert not dest.exists()


def test_refuses_to_overwrite_without_force(tmp_path):
    dest = tmp_path / "skills"
    assert run_install("--dest", str(dest)).returncode == 0
    # A second install without --force must refuse and change nothing.
    marker = dest / SKILL_NAME / "SKILL.md"
    marker.write_text("TOUCHED\n")
    proc = run_install("--dest", str(dest))
    assert proc.returncode == 1
    assert "already exists" in proc.stdout
    assert marker.read_text() == "TOUCHED\n"


def test_force_overwrites_and_restores_source(tmp_path):
    dest = tmp_path / "skills"
    run_install("--dest", str(dest))
    marker = dest / SKILL_NAME / "SKILL.md"
    marker.write_text("TOUCHED\n")
    proc = run_install("--dest", str(dest), "--force")
    assert proc.returncode == 0, proc.stderr
    # The real SKILL.md is back, byte for byte.
    assert marker.read_text() == (SKILL_DIR / "SKILL.md").read_text()


def test_agent_flag_targets_that_agents_folder(tmp_path):
    proc = run_install("--agent", "claude", "--dry-run", home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    expected = str(tmp_path / ".claude" / "skills" / SKILL_NAME)
    assert expected in proc.stdout


def test_autodetect_finds_agent_by_config_dir(tmp_path):
    # Create ~/.kiro so Kiro is "detected", but no other agent.
    (tmp_path / ".kiro").mkdir()
    proc = run_install(home=tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (tmp_path / ".kiro" / "skills" / SKILL_NAME / "SKILL.md").is_file()
    # An undetected agent's folder was not created.
    assert not (tmp_path / ".codex").exists()


def test_no_agent_and_no_dest_fails_cleanly(tmp_path):
    proc = run_install(home=tmp_path)
    assert proc.returncode == 1
    assert "no known agent detected" in proc.stderr


def test_list_shows_known_agents_without_installing(tmp_path):
    proc = run_install("--list", home=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Known agents" in proc.stdout
    assert "claude" in proc.stdout and "kiro" in proc.stdout
    # --list installs nothing.
    assert not any(tmp_path.iterdir())


def test_installed_checker_actually_runs(tmp_path):
    """The copied poe.py runs on its own, proving the install is complete."""
    dest = tmp_path / "skills"
    run_install("--dest", str(dest))
    poe = dest / SKILL_NAME / "scripts" / "poe.py"
    proc = subprocess.run([sys.executable, str(poe), "--help"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "check" in proc.stdout
