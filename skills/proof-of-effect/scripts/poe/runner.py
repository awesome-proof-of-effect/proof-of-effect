"""Running tools, finding them in the project, and the per-run context."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from . import gitio

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
QUIET_ENV = {"NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb",
             "PAGER": "cat", "GIT_PAGER": "cat", "MANPAGER": "cat"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "out", ".next", ".venv", "venv",
             "vendor", "coverage", "__pycache__", ".tox", "target"}
ERRORISH = re.compile(r"error|Error|ERROR|ERR!|failed|Failed|cannot|Cannot|not found|Invalid|invalid")


@dataclass
class RunResult:
    argv: List[str]
    code: Optional[int]
    out: str
    err: str
    timed_out: bool = False
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    @property
    def text(self) -> str:
        return (self.out + "\n" + self.err).strip()


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return ANSI.sub("", data)
    return ANSI.sub("", data.decode("utf-8", "replace"))


def run(argv: Sequence[str], cwd: str, timeout: float = 120,
        env: Optional[Dict[str, str]] = None) -> RunResult:
    full_env = dict(os.environ)
    full_env.update(QUIET_ENV)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, env=full_env)
    except FileNotFoundError:
        return RunResult(list(argv), None, "", f"{argv[0]}: command not found", missing=True)
    except subprocess.TimeoutExpired as exc:
        return RunResult(list(argv), None, _decode(exc.stdout),
                         _decode(exc.stderr) + f"\ntimed out after {timeout:g}s", timed_out=True)
    return RunResult(list(argv), proc.returncode, _decode(proc.stdout), _decode(proc.stderr))


def first_line(text: Optional[str], limit: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def error_line(text: str, limit: int = 160) -> str:
    """The most informative line of a tool's error output."""
    for line in text.splitlines():
        if ERRORISH.search(line) and not line.strip().startswith("Oops!"):
            return first_line(line, limit)
    return first_line(text, limit)


@dataclass
class Context:
    root: str
    git_dir: str
    base: str
    base_label: str
    timeout: float = 120
    tools: Dict[str, str] = field(default_factory=dict)
    samples: List[str] = field(default_factory=list)
    env_files: List[str] = field(default_factory=list)
    allow_cli: Set[str] = field(default_factory=set)
    offline: bool = False
    _base_cache: Dict[str, Optional[bytes]] = field(default_factory=dict)
    _files: Optional[List[str]] = None

    @classmethod
    def create(cls, repo: str, base: str = "HEAD", **kwargs) -> "Context":
        root = gitio.repo_root(repo)
        return cls(root=root, git_dir=gitio.git_dir(root), base=gitio.resolve_commit(root, base),
                   base_label=base, **kwargs)

    def abspath(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def rel(self, path: str) -> str:
        """Repo-relative form of a path given relative to the current directory."""
        return os.path.relpath(os.path.abspath(path), self.root)

    def base_bytes(self, rel: str) -> Optional[bytes]:
        if rel not in self._base_cache:
            self._base_cache[rel] = gitio.show(self.root, self.base, rel)
        return self._base_cache[rel]

    def head_bytes(self, rel: str) -> Optional[bytes]:
        path = self.abspath(rel)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    def files(self) -> List[str]:
        if self._files is None:
            self._files = [p for p in gitio.ls_files(self.root)
                           if not set(p.split("/")[:-1]) & SKIP_DIRS]
        return self._files

    def which(self, name: str, near: str = "") -> Optional[str]:
        """Prefer the project's own copy of a tool (node_modules/.bin, .venv/bin) over PATH."""
        if name in self.tools:
            return self.tools[name]
        dirs = []
        rel = near
        while True:
            dirs.append(os.path.join(self.root, rel, "node_modules", ".bin"))
            if not rel:
                break
            rel = os.path.dirname(rel)
        dirs += [os.path.join(self.root, ".venv", "bin"), os.path.join(self.root, "venv", "bin")]
        for directory in dirs:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return shutil.which(name)

    def python(self) -> str:
        if "python" in self.tools:
            return self.tools["python"]
        for candidate in (".venv/bin/python", "venv/bin/python"):
            path = self.abspath(candidate)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return shutil.which("python3") or shutil.which("python") or sys.executable

    def pick_samples(self, exts: Sequence[str], near: str, changed: Sequence[str]) -> List[str]:
        """A source file to resolve per-file configs (ESLint, Ruff) for."""
        suffixes = tuple(exts)
        if self.samples:
            chosen = [s for s in self.samples if s.endswith(suffixes)]
            return chosen or list(self.samples)
        prefix = (near.rstrip("/") + "/") if near else ""

        def usable(path: str) -> bool:
            return path.endswith(suffixes) and path.startswith(prefix) and os.path.isfile(self.abspath(path))

        candidates = [p for p in changed if usable(p)] or [p for p in self.files() if usable(p)]

        def rank(path: str):
            base = os.path.basename(path)
            top = path[len(prefix):].split("/")[0]
            return (0 if top in ("src", "lib", "app") else 1,
                    1 if ("config" in base or base.startswith(".")) else 0, path.count("/"), path)

        return sorted(candidates, key=rank)[:1]
