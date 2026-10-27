"""Thin wrappers around git. Every call here is read-only."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


class GitError(RuntimeError):
    pass


def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", root, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {' '.join(args)}: {message}")
    return proc


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def repo_root(path: str) -> str:
    return _text(_git(path, "rev-parse", "--show-toplevel").stdout).strip()


def git_dir(root: str) -> str:
    return _text(_git(root, "rev-parse", "--absolute-git-dir").stdout).strip()


def resolve_commit(root: str, ref: str) -> str:
    proc = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    sha = _text(proc.stdout).strip()
    if proc.returncode != 0 or not sha:
        raise GitError(f"cannot resolve {ref!r} to a commit (does the repository have any commits?)")
    return sha


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # "A" added, "M" modified, "D" deleted


def changed_files(root: str, base: str) -> List[ChangedFile]:
    """Files that differ between `base` and the working tree: staged, unstaged and untracked."""
    parts = _text(_git(root, "diff", "--name-status", "-z", "--no-renames", base, "--").stdout).split("\0")
    out: List[ChangedFile] = []
    i = 0
    while i + 1 < len(parts) and parts[i]:
        status, path = parts[i][0], parts[i + 1]
        i += 2
        status = "M" if status == "T" else status
        if status in ("A", "M", "D"):
            out.append(ChangedFile(path, status))
    seen = {c.path for c in out}
    untracked = _text(_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout).split("\0")
    out.extend(ChangedFile(p, "A") for p in untracked if p and p not in seen)
    return out


def show(root: str, commit: str, rel: str) -> Optional[bytes]:
    proc = _git(root, "show", f"{commit}:{rel}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def ls_files(root: str) -> List[str]:
    out = _text(_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout)
    return sorted({p for p in out.split("\0") if p})


def _excludes(paths: Sequence[str]) -> List[str]:
    return [f":(exclude,literal){p}" for p in paths]


def grep_word(root: str, word: str, exclude: Sequence[str] = ()) -> List[Tuple[str, int, str]]:
    """Whole-word, fixed-string search over tracked and untracked (not ignored) files."""
    args = ["grep", "-n", "-I", "-w", "-F", "--untracked", "-e", word, "--", ".", *_excludes(exclude)]
    proc = _git(root, *args, check=False)
    if proc.returncode not in (0, 1):
        raise GitError(_text(proc.stderr).strip())
    hits = []
    for line in _text(proc.stdout).splitlines():
        path, _, rest = line.partition(":")
        lineno, _, content = rest.partition(":")
        if lineno.isdigit():
            hits.append((path, int(lineno), content))
    return hits


def grep_fixed(root: str, text: str, exclude: Sequence[str] = ()) -> List[str]:
    """Files (tracked or untracked, not ignored) that contain `text` anywhere."""
    args = ["grep", "-l", "-I", "-F", "--untracked", "-e", text, "--", ".", *_excludes(exclude)]
    proc = _git(root, *args, check=False)
    if proc.returncode not in (0, 1):
        raise GitError(_text(proc.stderr).strip())
    return [p for p in _text(proc.stdout).splitlines() if p]
