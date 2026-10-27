"""Temporarily put files into their base-commit state, then put them back exactly.

Before anything is touched, every file is copied to <git-dir>/poe/backup-*/ and a
manifest is written to <git-dir>/poe/recovery.json. If the process dies mid-run,
`poe.py restore` replays the manifest. Content, permission bits and mtime are restored.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

MANIFEST = "recovery.json"


class SwapError(RuntimeError):
    pass


def state_dir(git_dir: str) -> str:
    return os.path.join(git_dir, "poe")


def manifest_path(git_dir: str) -> str:
    return os.path.join(state_dir(git_dir), MANIFEST)


def pending(git_dir: str) -> bool:
    return os.path.exists(manifest_path(git_dir))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _raise_interrupt(signum, frame):  # pragma: no cover - only reached by a real signal
    raise KeyboardInterrupt(f"signal {signum}")


def _restore_records(root: str, records: List[Dict[str, Any]], created_dirs: List[str]) -> List[str]:
    errors: List[str] = []
    for rec in records:
        path = os.path.join(root, rec["path"])
        try:
            if rec["existed"]:
                with open(rec["backup"], "rb") as fh:
                    data = fh.read()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
                os.chmod(path, rec["mode"])
                os.utime(path, ns=(rec["atime_ns"], rec["mtime_ns"]))
                with open(path, "rb") as fh:
                    if _sha(fh.read()) != rec["sha256"]:
                        errors.append(f"{rec['path']} (content check failed)")
            elif os.path.lexists(path):
                os.remove(path)
        except OSError as exc:
            errors.append(f"{rec['path']} ({exc})")
    for directory in reversed(created_dirs):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    return errors


def _cleanup(git_dir: str, backup_dir: Optional[str]) -> None:
    try:
        os.remove(manifest_path(git_dir))
    except FileNotFoundError:
        pass
    if backup_dir:
        shutil.rmtree(backup_dir, ignore_errors=True)


def recover(git_dir: str) -> List[str]:
    """Undo an interrupted swap. Returns the paths that were put back."""
    path = manifest_path(git_dir)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    errors = _restore_records(manifest["root"], manifest["files"], manifest.get("created_dirs", []))
    if errors:
        raise SwapError("could not restore " + "; ".join(errors))
    _cleanup(git_dir, manifest.get("backup_dir"))
    return [rec["path"] for rec in manifest["files"]]


class StateSwapper:
    """Context manager: inside it, each entry's file holds its base content (None = absent)."""

    def __init__(self, root: str, git_dir: str, entries: Iterable[Tuple[str, Optional[bytes]]]):
        self.root = root
        self.git_dir = git_dir
        self.entries = list(entries)
        self.records: List[Dict[str, Any]] = []
        self.created_dirs: List[str] = []
        self.backup_dir: Optional[str] = None
        self.active = False
        self._handlers: Dict[int, Any] = {}

    def _missing_dirs(self, directory: str) -> List[str]:
        missing = []
        while directory and not os.path.isdir(directory):
            missing.append(directory)
            directory = os.path.dirname(directory)
        return [d for d in reversed(missing) if d not in self.created_dirs]

    def __enter__(self) -> "StateSwapper":
        if pending(self.git_dir):
            raise SwapError("an earlier proof-of-effect run was interrupted; run `poe.py restore` first")
        os.makedirs(state_dir(self.git_dir), exist_ok=True)
        self.backup_dir = tempfile.mkdtemp(prefix="backup-", dir=state_dir(self.git_dir))
        for idx, (rel, base) in enumerate(self.entries):
            path = os.path.join(self.root, rel)
            rec: Dict[str, Any] = {"path": rel, "existed": False}
            if os.path.lexists(path):
                if os.path.islink(path) or not os.path.isfile(path):
                    shutil.rmtree(self.backup_dir, ignore_errors=True)
                    raise SwapError(f"{rel} is not a regular file; refusing to swap it")
                st = os.stat(path)
                with open(path, "rb") as fh:
                    data = fh.read()
                backup = os.path.join(self.backup_dir, f"{idx}.bak")
                with open(backup, "wb") as fh:
                    fh.write(data)
                rec.update(existed=True, backup=backup, mode=st.st_mode & 0o7777,
                           atime_ns=st.st_atime_ns, mtime_ns=st.st_mtime_ns, sha256=_sha(data))
            self.records.append(rec)
            if base is not None:
                self.created_dirs.extend(self._missing_dirs(os.path.dirname(path)))
        _write_json_atomic(manifest_path(self.git_dir), {
            "root": self.root, "backup_dir": self.backup_dir,
            "files": self.records, "created_dirs": self.created_dirs,
        })
        self.active = True
        self._install_handlers()
        try:
            for (rel, base), rec in zip(self.entries, self.records):
                path = os.path.join(self.root, rel)
                if base is None:
                    if os.path.lexists(path):
                        os.remove(path)
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(base)
                if rec["existed"]:
                    os.chmod(path, rec["mode"])
        except BaseException:
            self.restore()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.restore()
        return False

    def restore(self) -> None:
        if not self.active:
            return
        errors = _restore_records(self.root, self.records, self.created_dirs)
        self._restore_handlers()
        self.active = False
        if errors:
            raise SwapError("could not restore " + "; ".join(errors)
                            + f". Backups are in {self.backup_dir}; run `poe.py restore` to retry.")
        _cleanup(self.git_dir, self.backup_dir)

    def _install_handlers(self) -> None:
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self._handlers[sig] = signal.signal(sig, _raise_interrupt)
            except (ValueError, OSError):  # not in the main thread
                pass

    def _restore_handlers(self) -> None:
        for sig, old in self._handlers.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass
        self._handlers.clear()
