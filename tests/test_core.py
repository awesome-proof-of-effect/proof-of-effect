import json
import os
import shutil
import stat

import pytest

from poe import gitio, swap
from poe import structdiff as sd
from poe.swap import StateSwapper


def test_strip_jsonc_keeps_string_contents():
    text = '{\n  // comment\n  "url": "http://x//y/*z*/", /* block */ "a": [1, 2,],\n}\n'
    assert json.loads(sd.strip_jsonc(text)) == {"url": "http://x//y/*z*/", "a": [1, 2]}


def test_scalar_lists_are_leaves_and_mapping_lists_are_indexed():
    assert [c.path for c in sd.diff({"include": ["src"]}, {"include": ["src", "test"]})] == [("include",)]
    changes = sd.diff({"steps": [{"with": {"a": 1}}]}, {"steps": [{"with": {"a": 2}}]})
    assert changes[0].path == ("steps", 0, "with", "a")
    assert sd.fmt_path(changes[0].path) == "steps[0].with.a"


def test_change_kinds_and_bool_is_not_int():
    kinds = {c.path: c.kind for c in sd.diff({"a": 1, "b": 2}, {"b": 3, "c": 4})}
    assert kinds == {("a",): "removed", ("b",): "changed", ("c",): "added"}
    assert sd.diff({"a": True}, {"a": 1})


def test_name_matching_ignores_dashes_and_case():
    resolved = [sd.Change(("linter", "line_length"), "88", "100")]
    assert sd.match_change(("tool", "ruff", "line-length"), resolved) is resolved[0]
    assert sd.match_change(("tool", "ruff", "indent-width"), resolved) is None
    assert sd.match_unknown(("compilerOptions", "strictt"), ["strictt"]) == "strictt"
    assert sd.match_unknown(("compilerOptions", "strict"), ["strictt"]) is None


def test_ini_sections_and_yaml_on_key():
    ok, obj = sd.load_structured("setup.cfg", b"[tool:pytest]\ntestpaths = tests\n")
    assert ok and obj == {"tool:pytest": {"testpaths": "tests"}}
    ok, obj = sd.load_structured("ci.yml", b"on: push\n")
    assert ok and sd.fmt_path(next(iter(sd.flatten(obj)))) == "on"


def _gitdir(repo):
    return gitio.git_dir(str(repo.path))


def test_swap_restores_content_mode_and_mtime(repo):
    repo.write("a.json", '{"x": 1}\n')
    repo.commit()
    a = repo.write("a.json", '{"x": 2}\n')
    os.chmod(a, 0o640)
    os.utime(a, ns=(1_000_000_000, 2_000_000_000))
    added = repo.write("new.json", "{}\n")
    gdir = _gitdir(repo)
    with StateSwapper(str(repo.path), gdir, [("a.json", b'{"x": 1}\n'), ("new.json", None)]):
        assert a.read_text() == '{"x": 1}\n'
        assert not added.exists()
        assert swap.pending(gdir)
    assert a.read_text() == '{"x": 2}\n'
    assert stat.S_IMODE(a.stat().st_mode) == 0o640
    assert a.stat().st_mtime_ns == 2_000_000_000
    assert added.exists() and not swap.pending(gdir)


def test_swap_restores_after_an_exception(repo):
    repo.write("a.txt", "old")
    repo.commit()
    a = repo.write("a.txt", "new")
    with pytest.raises(RuntimeError):
        with StateSwapper(str(repo.path), _gitdir(repo), [("a.txt", b"old")]):
            raise RuntimeError("boom")
    assert a.read_text() == "new"


def test_swap_recreates_deleted_file_and_removes_its_dirs(repo):
    repo.write("cfg/deep/x.ini", "[a]\n")
    repo.commit()
    shutil.rmtree(repo.path / "cfg")
    with StateSwapper(str(repo.path), _gitdir(repo), [("cfg/deep/x.ini", b"[a]\n")]):
        assert (repo.path / "cfg/deep/x.ini").read_text() == "[a]\n"
    assert not (repo.path / "cfg").exists()


def test_interrupted_run_blocks_and_restore_recovers(repo):
    repo.write("a.txt", "old")
    repo.commit()
    a = repo.write("a.txt", "new")
    gdir = _gitdir(repo)
    abandoned = StateSwapper(str(repo.path), gdir, [("a.txt", b"old")])
    abandoned.__enter__()  # simulate a crash: __exit__ never runs
    abandoned._restore_handlers()
    assert a.read_text() == "old"
    with pytest.raises(swap.SwapError):
        StateSwapper(str(repo.path), gdir, []).__enter__()
    proc = repo.poe("check")
    assert proc.returncode == 3 and "restore" in proc.stderr
    proc = repo.poe("restore")
    assert proc.returncode == 0 and "a.txt" in proc.stdout
    assert a.read_text() == "new" and not swap.pending(gdir)


def test_nothing_to_check(repo):
    repo.write("notes.txt", "x")
    repo.commit()
    repo.write("notes.txt", "y")
    proc = repo.poe("check")
    assert proc.returncode == 0 and "no config" in proc.stdout
