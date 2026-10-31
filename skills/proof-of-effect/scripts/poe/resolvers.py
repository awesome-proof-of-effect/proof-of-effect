"""Checkers that ask a tool for its resolved configuration, before and after a change."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import gitio
from .model import Verdict
from .runner import Context, RunResult, error_line
from .structdiff import KeyPath, flatten

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pytest_dump")
JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
PY_EXTS = (".py", ".pyi")
# Common file types Prettier formats, so there is a sample to resolve per-file options for.
PRETTIER_EXTS = JS_EXTS + (".json", ".jsonc", ".json5", ".css", ".scss", ".less",
                           ".md", ".mdx", ".yaml", ".yml", ".html", ".vue")

# A tiny program run through the project's own Prettier: it prints one JSON object with the
# options Prettier resolves for a file, the config file it loads, and Prettier's own list of
# known option names. We derive the package from the resolved `prettier` bin so it works
# without PATH or NODE_PATH, matching how the other checkers call a project-local tool.
PRETTIER_PROGRAM = r"""
const pkg = process.argv[1], file = process.argv[2];
const p = require(pkg);
(async () => {
  const [config, configFile, support] = await Promise.all([
    p.resolveConfig(file, { editorconfig: false }),
    p.resolveConfigFile(file),
    p.getSupportInfo(),
  ]);
  const known = (support.options || []).map((o) => o.name);
  process.stdout.write(JSON.stringify({ config: config || {}, configFile, known }));
})().catch((e) => { process.stderr.write("prettier: " + (e && e.message || e)); process.exit(3); });
"""


@dataclass
class Job:
    checker: "Resolver"
    argv: List[str]
    cwd: str
    label: str
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> Tuple:
        return (self.checker.name, tuple(self.argv), self.cwd, tuple(sorted(self.env.items())))


@dataclass
class Parsed:
    data: Optional[Dict[KeyPath, Any]]
    unknown: List[str] = field(default_factory=list)
    error: Optional[str] = None
    loaded: Optional[str] = None  # config file the tool says it loaded (repo-relative)
    raw: str = ""


def _found(patterns: Sequence["re.Pattern[str]"], text: str) -> List[str]:
    names: List[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            name = next((g for g in match.groups() if g), None)
            if name and name not in names:
                names.append(name)
    return names


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            raise
        return json.loads(text[start:])


def _mentioned(ctx: Context, rel: str) -> bool:
    return bool(gitio.grep_fixed(ctx.root, os.path.basename(rel), exclude=[rel]))


class Resolver:
    name = "resolver"
    tool = "the tool"
    unknown_patterns: Sequence["re.Pattern[str]"] = ()

    def claims(self, rel: str) -> bool:
        raise NotImplementedError

    def owns(self, rel: str, path: KeyPath) -> bool:
        """Which keys of a shared file (package.json, pyproject.toml) this checker judges."""
        return True

    def whole_file_ok(self, rel: str) -> bool:
        """May the file be judged as a whole when it cannot be parsed (e.g. a JS config)?"""
        return True

    def jobs(self, ctx: Context, rel: str, changed: Sequence[str]) -> Tuple[List[Job], Optional[str]]:
        raise NotImplementedError

    def load(self, out: str) -> Any:
        return _json(out)

    def parse(self, ctx: Context, job: Job, res: RunResult) -> Parsed:
        unknown = _found(self.unknown_patterns, res.text)
        raw = res.text[-4000:]
        if not res.ok:
            return Parsed(None, unknown, error_line(res.text) or f"exit code {res.code}", raw=raw)
        try:
            return Parsed(flatten(self.load(res.out)), unknown, raw=raw)
        except ValueError:
            return Parsed(None, unknown, f"could not parse the output of `{job.label}`", raw=raw)

    def orphan(self, ctx: Context, rel: str) -> Optional[str]:
        """A reason string when nothing would ever load this file."""
        return None

    def explain(self, ctx: Context, rel: str,
                pairs: Sequence[Tuple[Parsed, Parsed]]) -> Optional[Tuple[Verdict, str]]:
        """Tool-specific verdict that overrides the generic comparison."""
        return None


class Tsconfig(Resolver):
    name, tool = "tsconfig", "tsc"
    NAME = re.compile(r"^[tj]sconfig(\.[\w.-]+)?\.json$")
    unknown_patterns = (re.compile(r"Unknown (?:compiler|watch|build|type acquisition) option '([^']+)'"),)

    def claims(self, rel):
        return bool(self.NAME.match(os.path.basename(rel)))

    def jobs(self, ctx, rel, changed):
        tsc = ctx.which("tsc", near=os.path.dirname(rel))
        if not tsc:
            return [], "tsc not found (install typescript in the project, or pass --tool tsc=PATH)"
        return [Job(self, [tsc, "--showConfig", "-p", rel], ctx.root, f"tsc --showConfig -p {rel}")], None

    def orphan(self, ctx, rel):
        base = os.path.basename(rel)
        if base in ("tsconfig.json", "jsconfig.json") or _mentioned(ctx, rel):
            return None
        return (f"nothing in the repo mentions {base}: no tsconfig extends or references it and no "
                "script passes it with -p, so tsc never loads it")


class Eslint(Resolver):
    name, tool = "eslint", "eslint"
    FLAT = re.compile(r"^eslint\.config\.(js|mjs|cjs|ts|mts|cts)$")
    LEGACY = re.compile(r"^\.eslintrc(\.(js|cjs|json|yaml|yml))?$")
    unknown_patterns = (
        re.compile(r'Unexpected key "([^"]+)" found'),
        re.compile(r'Unexpected top-level property "([^"]+)"'),
        re.compile(r"Definition for rule '([^']+)' was not found"),
        re.compile(r'Could not find "([^"]+)" in plugin'),
    )

    def claims(self, rel):
        base = os.path.basename(rel)
        return bool(self.FLAT.match(base) or self.LEGACY.match(base)) or base == "package.json"

    def owns(self, rel, path):
        return os.path.basename(rel) != "package.json" or (bool(path) and path[0] == "eslintConfig")

    def whole_file_ok(self, rel):
        return os.path.basename(rel) != "package.json"

    def jobs(self, ctx, rel, changed):
        eslint = ctx.which("eslint", near=os.path.dirname(rel))
        if not eslint:
            return [], "eslint not found (install it in the project, or pass --tool eslint=PATH)"
        samples = ctx.pick_samples(JS_EXTS, os.path.dirname(rel), changed)
        if not samples:
            return [], "no JavaScript or TypeScript file to resolve the config for (pass --sample FILE)"
        return [Job(self, [eslint, "--print-config", s], ctx.root, f"eslint --print-config {s}")
                for s in samples], None

    def explain(self, ctx, rel, pairs):
        base = os.path.basename(rel)
        if not self.LEGACY.match(base):
            return None
        if any("eslint.config" in a.raw for _, a in pairs if a.error):
            return Verdict.NO_OP, (f"ESLint runs in flat-config mode and found no eslint.config.* file; "
                                   f"it ignores {base}")
        flat = sorted(f for f in os.listdir(ctx.root) if self.FLAT.match(f))
        same = all(b.data == a.data for b, a in pairs)
        if flat and same and not any(a.unknown for _, a in pairs):
            return Verdict.NO_OP, f"ESLint loads {flat[0]} (flat config) and ignores {base}"
        return None


class Jest(Resolver):
    name, tool = "jest", "jest"
    NAME = re.compile(r"^jest\.config(\.[\w-]+)?\.(js|cjs|mjs|ts|cts|mts|json)$")
    DEFAULT = re.compile(r"^jest\.config\.(js|cjs|mjs|ts|cts|mts|json)$")
    unknown_patterns = (re.compile(r'Unknown option "([^"]+)" with value'),)

    def claims(self, rel):
        base = os.path.basename(rel)
        return bool(self.NAME.match(base)) or base == "package.json"

    def owns(self, rel, path):
        return os.path.basename(rel) != "package.json" or (bool(path) and path[0] == "jest")

    def whole_file_ok(self, rel):
        return os.path.basename(rel) != "package.json"

    def jobs(self, ctx, rel, changed):
        jest = ctx.which("jest", near=os.path.dirname(rel))
        if not jest:
            return [], "jest not found (install it in the project, or pass --tool jest=PATH)"
        base = os.path.basename(rel)
        natural = base == "package.json" or bool(self.DEFAULT.match(base))
        argv = [jest, "--showConfig"] + ([] if natural else ["--config", base])
        label = "jest --showConfig" + ("" if natural else f" --config {rel}")
        return [Job(self, argv, os.path.join(ctx.root, os.path.dirname(rel)), label)], None

    def orphan(self, ctx, rel):
        base = os.path.basename(rel)
        if base == "package.json" or self.DEFAULT.match(base) or _mentioned(ctx, rel):
            return None
        return f"nothing in the repo mentions {base}: jest only loads it when it is passed with --config"


class Prettier(Resolver):
    name, tool = "prettier", "prettier"
    # Dedicated config files, plus package.json ("prettier" key) and the shared ignore/editor files.
    NAME = re.compile(r"^(\.prettierrc"
                      r"(\.(json|json5|jsonc|yaml|yml|toml|js|cjs|mjs|ts|cts|mts))?"
                      r"|prettier\.config\.(js|cjs|mjs|ts|cts|mts))$")
    # Keys a Prettier config may hold that are not formatting options and must not be flagged.
    STRUCTURAL = {"overrides", "$schema"}

    def claims(self, rel):
        base = os.path.basename(rel)
        return bool(self.NAME.match(base)) or base == "package.json"

    def owns(self, rel, path):
        return os.path.basename(rel) != "package.json" or (bool(path) and path[0] == "prettier")

    def whole_file_ok(self, rel):
        return os.path.basename(rel) != "package.json"

    def _node_and_pkg(self, ctx, rel):
        node = ctx.which("node", near=os.path.dirname(rel))
        prettier = ctx.which("prettier", near=os.path.dirname(rel))
        if not node or not prettier:
            return None, None
        # The prettier bin is <pkg>/bin/prettier.cjs; its package dir holds the importable entry.
        pkg = os.path.dirname(os.path.dirname(os.path.realpath(prettier)))
        return node, pkg

    def jobs(self, ctx, rel, changed):
        node, pkg = self._node_and_pkg(ctx, rel)
        if not node or not pkg:
            return [], "prettier not found (install it in the project, or pass --tool prettier=PATH)"
        sample = self._sample(ctx, rel, changed)
        if not sample:
            return [], "no source file Prettier formats to resolve the config for (pass --sample FILE)"
        return [Job(self, [node, "--eval", PRETTIER_PROGRAM, pkg, sample], ctx.root,
                    f"prettier resolveConfig {sample}")], None

    def _is_config(self, rel):
        """A file Prettier treats as config, never as a formatting subject to resolve options for."""
        base = os.path.basename(rel)
        return (base == "package.json" or base.startswith(".prettierrc")
                or base.startswith("prettier.config.") or base in (".editorconfig", ".prettierignore"))

    def _sample(self, ctx, rel, changed):
        """A real source file to resolve per-file options for, never a config file itself.

        An explicit --sample wins. Otherwise prefer a changed source file (what the PR touches),
        then any tracked one, ranking JS/TS first so `overrides` targeting them are exercised.
        """
        js = set(JS_EXTS)

        def usable(path):
            return (path.endswith(PRETTIER_EXTS) and not self._is_config(path)
                    and os.path.isfile(ctx.abspath(path)))

        if ctx.samples:
            chosen = [s for s in ctx.samples if usable(s)] or list(ctx.samples)
            return chosen[0] if chosen else None

        def rank(path):
            return (0 if path.endswith(tuple(js)) else 1, path.count("/"), path)

        pool = [p for p in changed if usable(p)] or [p for p in ctx.files() if usable(p)]
        return sorted(pool, key=rank)[0] if pool else None

    def parse(self, ctx, job, res):
        raw = res.text[-4000:]
        if not res.ok:
            return Parsed(None, [], error_line(res.text) or f"exit code {res.code}", raw=raw)
        try:
            payload = _json(res.out)
        except ValueError:
            return Parsed(None, [], f"could not parse the output of `{job.label}`", raw=raw)
        config = payload.get("config") or {}
        known = set(payload.get("known") or [])
        # resolveConfig flattens `overrides` into the per-file options, so every remaining key that
        # Prettier does not list as an option (and is not structural) is one it will silently ignore.
        unknown = [k for k in config
                   if k not in known and k not in self.STRUCTURAL] if known else []
        config_file = payload.get("configFile")
        loaded = os.path.relpath(config_file, ctx.root) if config_file else None
        return Parsed(flatten(config), unknown, None, loaded, raw=raw)

    def explain(self, ctx, rel, pairs):
        # When a dedicated Prettier config exists, package.json's "prettier" key is ignored.
        if os.path.basename(rel) != "package.json":
            return None
        loaded = next((a.loaded for _, a in pairs if a.loaded), None)
        if loaded and os.path.basename(loaded) != "package.json":
            same = all(b.data == a.data for b, a in pairs)
            if same and not any(a.unknown for _, a in pairs):
                return Verdict.NO_OP, f"Prettier loads {loaded} and ignores the \"prettier\" key in {rel}"
        return None


def _depth(text: str) -> int:
    return sum(text.count(c) for c in "[{(") - sum(text.count(c) for c in "]})")


def parse_settings_dump(text: str) -> Dict[str, Any]:
    """Turn `ruff check --show-settings` output (`a.b.c = value` lines) into a nested dict."""
    flat: Dict[str, str] = {}
    key: Optional[str] = None
    buf: List[str] = []
    depth = 0
    for line in text.splitlines():
        if key is not None:
            buf.append(line.strip())
            depth += _depth(line)
            if depth <= 0:
                flat[key] = " ".join(buf)
                key, buf = None, []
            continue
        match = re.match(r"^([A-Za-z_][\w.\-]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        if _depth(value) > 0:
            key, buf, depth = name, [value], _depth(value)
        else:
            flat[name] = value
    if key is not None:
        flat[key] = " ".join(buf)
    nested: Dict[str, Any] = {}
    for name, value in flat.items():
        node = nested
        parts = name.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {"": child} if child is not None else {}
                node[part] = child
            node = child
        if isinstance(node.get(parts[-1]), dict):
            node[parts[-1]][""] = value
        else:
            node[parts[-1]] = value
    return nested


class Ruff(Resolver):
    name, tool = "ruff", "ruff"
    unknown_patterns = (re.compile(r"unknown field `([^`]+)`"),)

    def claims(self, rel):
        return os.path.basename(rel) in ("ruff.toml", ".ruff.toml", "pyproject.toml")

    def owns(self, rel, path):
        return os.path.basename(rel) != "pyproject.toml" or tuple(path[:2]) == ("tool", "ruff")

    def whole_file_ok(self, rel):
        return os.path.basename(rel) != "pyproject.toml"

    def jobs(self, ctx, rel, changed):
        ruff = ctx.which("ruff", near=os.path.dirname(rel))
        if not ruff:
            return [], "ruff not found (install it in the project, or pass --tool ruff=PATH)"
        samples = ctx.pick_samples(PY_EXTS, os.path.dirname(rel), changed)
        if not samples:
            return [], "no Python file to resolve the settings for (pass --sample FILE)"
        return [Job(self, [ruff, "check", "--show-settings", s], ctx.root, f"ruff check --show-settings {s}")
                for s in samples], None

    def load(self, out):
        return parse_settings_dump(out)


class Pytest(Resolver):
    name, tool = "pytest", "pytest"
    FILES = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")

    def claims(self, rel):
        return os.path.basename(rel) in self.FILES

    def owns(self, rel, path):
        base = os.path.basename(rel)
        if base == "pyproject.toml":
            return tuple(path[:2]) == ("tool", "pytest")
        if base == "setup.cfg":
            return bool(path) and path[0] == "tool:pytest"
        return bool(path) and path[0] == "pytest"

    def whole_file_ok(self, rel):
        return False

    def jobs(self, ctx, rel, changed):
        fd, dump = tempfile.mkstemp(prefix="poe-pytest-", suffix=".json")
        os.close(fd)
        os.remove(dump)
        pythonpath = os.pathsep.join(p for p in (PLUGIN_DIR, os.environ.get("PYTHONPATH", "")) if p)
        env = {"PYTHONPATH": pythonpath, "POE_PYTEST_DUMP": dump}
        # --co is a safety net: even if the plugin failed to stop pytest, no test would run.
        argv = [ctx.python(), "-m", "pytest", "-p", "poe_pytest_dump", "-p", "no:cacheprovider", "--co", "-q"]
        where = os.path.dirname(rel) or "."
        return [Job(self, argv, os.path.join(ctx.root, os.path.dirname(rel)),
                    f"pytest config dump in {where}", env)], None

    def parse(self, ctx, job, res):
        dump = job.env["POE_PYTEST_DUMP"]
        if not os.path.exists(dump):
            if "No module named pytest" in res.text:
                return Parsed(None, [], f"pytest is not installed for {job.argv[0]}", raw=res.text[-4000:])
            return Parsed(None, [], error_line(res.text) or f"exit code {res.code}", raw=res.text[-4000:])
        with open(dump, encoding="utf-8") as fh:
            data = json.load(fh)
        os.remove(dump)
        loaded = os.path.relpath(data["inifile"], ctx.root) if data.get("inifile") else None
        return Parsed(flatten({"ini": data.get("ini", {})}), list(data.get("unknown", [])), None, loaded)


class Compose(Resolver):
    name, tool = "compose", "docker compose"
    NAME = re.compile(r"^(docker-)?compose(\.[\w.-]+)?\.ya?ml$")
    DEFAULT = {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
    unknown_patterns = (re.compile(r"[Aa]dditional propert(?:y|ies) '?([\w.\-]+)'? (?:is|are) not allowed"),)

    def claims(self, rel):
        return bool(self.NAME.match(os.path.basename(rel)))

    def jobs(self, ctx, rel, changed):
        docker = ctx.which("docker")
        if not docker:
            return [], "docker not found (Docker Compose v2 is needed to resolve compose files)"
        base = os.path.basename(rel)
        natural = base in self.DEFAULT or ".override." in base
        argv = [docker, "compose"] + ([] if natural else ["-f", base]) + ["config", "--format", "json"]
        label = "docker compose config" + ("" if natural else f" -f {rel}")
        return [Job(self, argv, os.path.join(ctx.root, os.path.dirname(rel)), label)], None


RESOLVERS: List[Resolver] = [Tsconfig(), Eslint(), Jest(), Prettier(), Ruff(), Pytest(), Compose()]
