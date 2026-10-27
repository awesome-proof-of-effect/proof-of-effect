import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "proof-of-effect" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Extra directories (colon-separated) holding tsc/eslint/jest/ruff for the integration tests.
TOOL_PATH = os.environ.get("POE_TEST_TOOLPATH", "")
GIT_ID = ["-c", "user.name=poe-test", "-c", "user.email=poe-test@example.invalid", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], check=True,
                          capture_output=True, text=True).stdout


def tool_env():
    return {"PATH": TOOL_PATH + os.pathsep + os.environ["PATH"]} if TOOL_PATH else {}


def have(tool):
    return shutil.which(tool, path=TOOL_PATH + os.pathsep + os.environ["PATH"]) is not None


class Repo:
    def __init__(self, path):
        self.path = path

    def write(self, rel, text, mode=None):
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text).lstrip("\n"))
        if mode:
            target.chmod(mode)
        return target

    def commit(self, message="base"):
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message)

    def poe(self, *args, env=None):
        full = dict(os.environ)
        full.update(env or {})
        return subprocess.run([sys.executable, str(SCRIPTS / "poe.py"), *args], cwd=self.path,
                              capture_output=True, text=True, env=full)

    def check(self, *args, env=None):
        proc = self.poe("check", "--json", *args, env=env)
        assert proc.returncode in (0, 1), proc.stderr + proc.stdout
        data = json.loads(proc.stdout)
        return proc.returncode, {(f["file"], f["subject"]): f for f in data["findings"]}


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return Repo(path)
