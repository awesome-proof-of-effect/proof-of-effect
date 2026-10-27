import json
import sys

from conftest import git  # noqa: F401  (keeps the helper importable for future tests)
from poe import gitio, static_checks
from poe.runner import Context

ACTION = """
name: setup
inputs:
  node-version:
    description: version
  cache:
    description: old
    deprecationMessage: use cache-path
runs:
  using: composite
  steps: []
"""


def test_local_action_unknown_input(repo):
    repo.write(".github/actions/setup/action.yml", ACTION)
    repo.write(".github/workflows/ci.yml", """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: ./.github/actions/setup
                with:
                  node-version: 20
    """)
    repo.commit()
    repo.write(".github/workflows/ci.yml", """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: ./.github/actions/setup
                with:
                  node-version: 20
                  node-versio: 22
                  cache: npm
    """)
    code, found = repo.check("--only", "gha", "--offline")
    wf = ".github/workflows/ci.yml"
    assert found[(wf, "jobs.build.steps[0].with.node-versio")]["verdict"] == "UNKNOWN-KEY"
    assert "did you mean `node-version`" in found[(wf, "jobs.build.steps[0].with.node-versio")]["evidence"]
    assert found[(wf, "jobs.build.steps[0].with.cache")]["verdict"] == "EXISTS"
    assert "deprecated" in found[(wf, "jobs.build.steps[0].with.cache")]["evidence"]
    assert (wf, "jobs.build.steps[0].with.node-version") not in found  # unchanged
    assert code == 1


def test_all_mode_audits_existing_workflows(repo):
    repo.write(".github/actions/setup/action.yml", ACTION)
    repo.write(".github/workflows/ci.yml", """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: ./.github/actions/setup
                with:
                  node-version: 20
                  cached: true
    """)
    repo.commit()
    code, found = repo.check("--all", "--only", "gha", "--offline")
    wf = ".github/workflows/ci.yml"
    assert found[(wf, "jobs.build.steps[0].with.cached")]["verdict"] == "UNKNOWN-KEY"
    assert "did you mean `cache`" in found[(wf, "jobs.build.steps[0].with.cached")]["evidence"]
    assert found[(wf, "jobs.build.steps[0].with.node-version")]["verdict"] == "EXISTS"
    assert code == 1
    code, found = repo.check("--only", "gha", "--offline")  # without --all nothing changed, nothing to report
    assert code == 0 and not found


def test_local_reusable_workflow_inputs(repo):
    repo.write(".github/workflows/reuse.yml", """
        on:
          workflow_call:
            inputs:
              env-name:
                type: string
        jobs: {}
    """)
    repo.write(".github/workflows/ci.yml", """
        on: push
        jobs:
          deploy:
            uses: ./.github/workflows/reuse.yml
            with:
              env-name: staging
    """)
    repo.commit()
    repo.write(".github/workflows/ci.yml", """
        on: push
        jobs:
          deploy:
            uses: ./.github/workflows/reuse.yml
            with:
              envname: prod
    """)
    _, found = repo.check("--only", "gha", "--offline")
    assert found[(".github/workflows/ci.yml", "jobs.deploy.with.envname")]["verdict"] == "UNKNOWN-KEY"


def test_remote_action_metadata_is_fetched(repo, monkeypatch):
    repo.write(".github/workflows/ci.yml", """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/setup-node@v4
                with:
                  node-version: 18
    """)
    repo.commit()
    repo.write(".github/workflows/ci.yml", """
        on: push
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/setup-node@v4
                with:
                  node-version: 20
                  cache-dependency-pat: package-lock.json
    """)
    calls = []

    def fake_fetch(url, timeout=15):
        calls.append(url)
        if url.endswith("/action.yml"):
            return "inputs:\n  node-version: {}\n  cache-dependency-path: {}\nruns:\n  using: node20\n  main: x.js\n"
        return None

    monkeypatch.setattr(static_checks, "fetch_text", fake_fetch)
    monkeypatch.setattr(static_checks, "_gh_contents", lambda *a: None)
    ctx = Context.create(str(repo.path))
    found = {f.subject: f for f in static_checks.check_workflows(ctx, gitio.changed_files(ctx.root, ctx.base))}
    assert found["jobs.build.steps[0].with.cache-dependency-pat"].verdict.value == "UNKNOWN-KEY"
    assert found["jobs.build.steps[0].with.node-version"].verdict.value == "EXISTS"
    assert calls == ["https://raw.githubusercontent.com/actions/setup-node/v4/action.yml"]


def test_env_vars(repo):
    repo.write("package.json", '{"devDependencies": {"vite": "5.0.0"}}\n')
    repo.write("src/main.ts", "console.log(import.meta.env.VITE_API_URL, import.meta.env.API_URL)\n")
    repo.write("server.py", "import os\nprint(os.environ['DB_HOST'])\n")
    repo.write(".env.example", "VITE_API_URL=\n")
    repo.commit()
    repo.write(".env.example", """
        VITE_API_URL=
        API_URL=
        DB_HOST=
        UNUSED_TOKEN=
        NODE_OPTIONS=--max-old-space-size=4096
    """)
    code, found = repo.check("--only", "env")

    def verdict(name):
        return found[(".env.example", name)]["verdict"]

    assert verdict("API_URL") == "NOT-READ"
    assert "VITE_" in found[(".env.example", "API_URL")]["evidence"]
    assert verdict("DB_HOST") == "EXISTS"
    assert verdict("UNUSED_TOKEN") == "NOT-READ"
    assert verdict("NODE_OPTIONS") == "EXISTS"
    assert (".env.example", "VITE_API_URL") not in found
    assert code == 1


def test_env_file_option_covers_ignored_files(repo):
    repo.write(".gitignore", ".env\n")
    repo.write("app.js", "console.log(process.env.APP_PORT)\n")
    repo.commit()
    repo.write(".env", "APP_PORT=1\nGHOST=2\n")
    _, found = repo.check("--only", "env", "--env-file", ".env")
    assert found[(".env", "GHOST")]["verdict"] == "NOT-READ"
    assert found[(".env", "APP_PORT")]["verdict"] == "EXISTS"


FAKE_TOOL = """
#!/usr/bin/env python3
import sys
if "--help" in sys.argv:
    print("usage: faketool [options]")
    print("  --alpha        do a")
    print("  --[no-]color   colour output")
else:
    print("faketool 1.2.3")
"""


def test_cli_flags(repo):
    repo.write(".gitignore", "node_modules/\n")
    repo.write("node_modules/.bin/faketool", FAKE_TOOL, mode=0o755)
    repo.write("package.json", '{"scripts": {"lint": "faketool --alpha"}}\n')
    repo.commit()
    repo.write("package.json", json.dumps({"scripts": {
        "lint": "faketool --alpha --bravo --no-color",
        "fmt": "NODE_ENV=x npx faketool --alpha=1 && unknowntool --zzz",
    }}))
    code, found = repo.check("--only", "cli", "--allow-cli", "faketool")
    assert found[("package.json", "faketool --bravo")]["verdict"] == "PHANTOM-FLAG"
    assert found[("package.json", "faketool --no-color")]["verdict"] == "EXISTS"
    assert found[("package.json", "faketool --alpha")]["verdict"] == "EXISTS"  # new in the fmt script
    assert not any(subject.startswith("unknowntool") for _, subject in found)
    assert code == 1


def test_closest_input():
    meta = static_checks.ActionMeta("actions/checkout/action.yml@v4", {"fetch-depth": None, "ref": None, "token": None})
    assert static_checks.closest_input(meta, "fetch_depth") == "fetch-depth"
    assert static_checks.closest_input(meta, "fetch-dept") == "fetch-depth"
    assert static_checks.closest_input(meta, "submodules") is None


def test_command_parsing():
    assert static_checks.unwrap(["cross-env", "A=1", "npx", "--yes", "eslint", "--fix"]) == ["eslint", "--fix"]
    assert static_checks.unwrap(["uv", "run", "--with", "x", "pytest", "-q"]) == ["pytest", "-q"]
    assert static_checks.unwrap(["python", "-m", "pytest", "--lf"]) == ["pytest", "--lf"]
    assert static_checks.parse_tool_flags(["docker", "compose", "up", "--build"]) == \
        ("docker", ["compose", "up"], ["--build"])
    assert static_checks.parse_tool_flags(["docker", "run", "--rm", "img", "--app-flag"]) == \
        ("docker", ["run"], ["--rm"])
    assert static_checks.parse_tool_flags(["docker", "--context", "x", "ps"]) is None
    assert [c[0] for c in static_checks.split_simple("a --x && b --y | c; d")] == ["a", "b", "c", "d"]


def test_probe(repo):
    repo.write("config.json", '{"level": 1, "extra": true}\n')
    repo.write("show.py", "import json\nprint(json.load(open('config.json'))['level'])\n")
    repo.commit()
    repo.write("config.json", '{"level": 2, "extra": true}\n')
    proc = repo.poe("probe", "--json", "--files", "config.json", "--", sys.executable, "show.py")
    assert json.loads(proc.stdout)["findings"][0]["verdict"] == "EFFECTIVE"
    repo.write("config.json", '{"level": 1, "extra": false}\n')  # show.py never reads "extra"
    proc = repo.poe("probe", "--json", "--files", "config.json", "--", sys.executable, "show.py")
    assert json.loads(proc.stdout)["findings"][0]["verdict"] == "NO-OP"
    assert proc.returncode == 1
    assert (repo.path / "config.json").read_text() == '{"level": 1, "extra": false}\n'
