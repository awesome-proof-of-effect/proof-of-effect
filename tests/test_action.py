"""Tests for the GitHub Action entrypoint (poe.action)."""
import os

import pytest

from poe import action, cli, gitio


@pytest.fixture
def two_commits(repo):
    """A repo with a base commit and a second commit that adds a no-op tsconfig key."""
    repo.write("tsconfig.json", '{"compilerOptions": {"strict": true}}\n')
    repo.commit("base")
    base_sha = gitio.resolve_commit(str(repo.path), "HEAD")
    repo.write("notes.txt", "hello\n")  # harmless change nothing objects to
    repo.commit("second")
    return repo, base_sha


def _clear_env(monkeypatch):
    for name in ("GITHUB_BASE_REF", "POE_EVENT_BEFORE", "POE_ACTION_BASE",
                 "POE_ACTION_FAIL_ON_PROBLEMS", "POE_ACTION_ARGS", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)


def test_resolve_base_prefers_explicit(two_commits, monkeypatch):
    repo, base_sha = two_commits
    _clear_env(monkeypatch)
    monkeypatch.setenv("GITHUB_BASE_REF", "main")  # should be ignored when explicit wins
    assert action.resolve_base(str(repo.path), explicit=base_sha) == base_sha


def test_resolve_base_uses_origin_base_ref(two_commits, monkeypatch):
    repo, base_sha = two_commits
    _clear_env(monkeypatch)
    # Simulate the base branch having been fetched to origin/release.
    gitio._git(str(repo.path), "update-ref", "refs/remotes/origin/release", base_sha)
    monkeypatch.setenv("GITHUB_BASE_REF", "release")
    assert gitio.resolve_commit(str(repo.path), action.resolve_base(str(repo.path))) == base_sha


def test_resolve_base_skips_unresolvable_and_falls_back(two_commits, monkeypatch):
    repo, base_sha = two_commits
    _clear_env(monkeypatch)
    # Base ref points at a branch that was never fetched -> skipped; before is all-zero -> skipped;
    # falls through to HEAD~1, which is the base commit.
    monkeypatch.setenv("GITHUB_BASE_REF", "never-fetched")
    monkeypatch.setenv("POE_EVENT_BEFORE", "0" * 40)
    assert gitio.resolve_commit(str(repo.path), action.resolve_base(str(repo.path))) == base_sha


def test_resolve_base_errors_when_nothing_resolves(repo, monkeypatch):
    repo.write("only.txt", "x\n")
    repo.commit("only")  # single commit: HEAD~1 does not exist
    _clear_env(monkeypatch)
    with pytest.raises(gitio.GitError):
        action.resolve_base(str(repo.path))


def test_write_summary_appends(tmp_path):
    path = tmp_path / "summary.md"
    assert action.write_summary("first", str(path)) is True
    assert action.write_summary("second", str(path)) is True
    assert path.read_text() == "first\nsecond\n"
    assert action.write_summary("ignored", None) is False


def test_run_writes_summary_and_passes_clean(two_commits, monkeypatch, tmp_path, capsys):
    repo, base_sha = two_commits
    _clear_env(monkeypatch)
    summary = tmp_path / "summary.md"
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("POE_ACTION_BASE", base_sha)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = action.run()
    assert code == cli.EXIT_OK
    text = summary.read_text()
    assert "Proof of effect" in text
    out = capsys.readouterr().out
    assert "Proof of effect" in out  # also echoed to the log


def test_run_problem_exit_honours_fail_on_problems(repo, monkeypatch):
    # The exit-code plumbing: a PROBLEMS result fails by default, but fail-on-problems=false
    # downgrades it to success (report-only mode). The verdict itself comes from cli.main,
    # exercised by the other test modules, so here we stub it to isolate the mapping.
    repo.write("a.txt", "x\n")
    repo.commit("base")
    base_sha = gitio.resolve_commit(str(repo.path), "HEAD")
    _clear_env(monkeypatch)
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("POE_ACTION_BASE", base_sha)
    monkeypatch.setattr(action.cli, "main",
                        lambda argv: (print("### Proof of effect\n\nproblem"), cli.EXIT_PROBLEMS)[1])

    monkeypatch.setenv("POE_ACTION_FAIL_ON_PROBLEMS", "true")
    assert action.run() == cli.EXIT_PROBLEMS
    monkeypatch.setenv("POE_ACTION_FAIL_ON_PROBLEMS", "false")
    assert action.run() == cli.EXIT_OK


def test_run_error_always_fails(repo, monkeypatch):
    repo.write("a.txt", "x\n")
    repo.commit("base")
    base_sha = gitio.resolve_commit(str(repo.path), "HEAD")
    _clear_env(monkeypatch)
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("POE_ACTION_BASE", base_sha)
    monkeypatch.setattr(action.cli, "main", lambda argv: (print("", end=""), cli.EXIT_ERROR)[1])
    monkeypatch.setenv("POE_ACTION_FAIL_ON_PROBLEMS", "false")  # ignored for real errors
    assert action.run() == cli.EXIT_ERROR


def test_truthy_defaults():
    assert action._truthy("", default=True) is True
    assert action._truthy("", default=False) is False
    assert action._truthy("false", default=True) is False
    assert action._truthy("YES", default=False) is True
