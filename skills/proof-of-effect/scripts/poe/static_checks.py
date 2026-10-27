"""Checks that need no before/after run: GitHub Actions inputs, env vars and CLI flags."""
from __future__ import annotations

import base64
import difflib
import json
import os
import re
import shlex
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from . import gitio
from . import structdiff as sd
from .gitio import ChangedFile
from .model import Finding, Verdict
from .runner import Context, first_line, run

# --------------------------------------------------------------------------- GitHub Actions

WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
ACTION_REF = re.compile(r"^([\w.-]+)/([\w.-]+)((?:/[^@\s]+)?)@(\S+)$")
YAML_FILE = re.compile(r"\.ya?ml$")


@dataclass
class ActionMeta:
    source: str
    inputs: Dict[str, Optional[str]]  # lower-cased input name -> deprecation message
    docker: bool = False


def fetch_text(url: str, timeout: float = 15) -> Optional[str]:
    """GET a public URL; None on 404."""
    request = urllib.request.Request(url, headers={"User-Agent": "proof-of-effect"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _gh_contents(owner: str, repo: str, path: str, ref: str) -> Optional[str]:
    """Private repositories: ask the GitHub CLI, if it is installed and signed in."""
    gh = shutil.which("gh")
    if not gh:
        return None
    res = run([gh, "api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}", "--jq", ".content"],
              cwd=os.getcwd(), timeout=20)
    if not res.ok or not res.out.strip():
        return None
    try:
        return base64.b64decode(res.out).decode("utf-8", "replace")
    except ValueError:
        return None


def _parse_meta(text: str, source: str, reusable: bool) -> ActionMeta:
    doc = sd.load_yaml_text(text)
    doc = doc if isinstance(doc, dict) else {}
    if reusable:
        on = doc.get("on", doc.get(True))  # YAML 1.1 reads the key `on` as True
        call = on.get("workflow_call") if isinstance(on, dict) else None
        inputs = call.get("inputs") if isinstance(call, dict) else None
        return ActionMeta(source, {str(k).lower(): None for k in (inputs or {})})
    inputs = doc.get("inputs") if isinstance(doc.get("inputs"), dict) else {}
    runs = doc.get("runs") if isinstance(doc.get("runs"), dict) else {}
    meta = {str(k).lower(): (v.get("deprecationMessage") if isinstance(v, dict) else None)
            for k, v in inputs.items()}
    return ActionMeta(source, meta, docker=str(runs.get("using", "")).lower() == "docker")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_action_meta(ctx: Context, uses: str) -> Tuple[Optional[ActionMeta], str]:
    if uses.startswith("docker://"):
        return None, "Docker image actions declare no inputs"
    if uses.startswith(("./", "../")):
        target = os.path.normpath(os.path.join(ctx.root, uses))
        if YAML_FILE.search(uses):
            if os.path.isfile(target):
                return _parse_meta(_read(target), os.path.relpath(target, ctx.root), True), ""
            return None, f"{uses} does not exist"
        for name in ("action.yml", "action.yaml"):
            path = os.path.join(target, name)
            if os.path.isfile(path):
                return _parse_meta(_read(path), os.path.relpath(path, ctx.root), False), ""
        return None, f"there is no action.yml in {uses}"
    match = ACTION_REF.match(uses)
    if not match:
        return None, "cannot parse this `uses:` value"
    if ctx.offline:
        return None, "skipped (--offline)"
    owner, repo, sub, ref = match.group(1), match.group(2), match.group(3).strip("/"), match.group(4)
    reusable = bool(YAML_FILE.search(sub))
    candidates = [sub] if reusable else [f"{sub}/{n}" if sub else n for n in ("action.yml", "action.yaml")]
    errors: List[str] = []
    for path in candidates:
        try:
            text = fetch_text(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}")
        except (urllib.error.URLError, OSError) as exc:
            text = None
            errors.append(str(exc))
        if text is None:
            text = _gh_contents(owner, repo, path, ref)
        if text is not None:
            return _parse_meta(text, f"{owner}/{repo}/{path}@{ref}", reusable), ""
    return None, "could not fetch its metadata" + (f" ({errors[0]})" if errors else "")


def closest_input(meta: ActionMeta, key: str) -> Optional[str]:
    """The declared input a misspelled key most likely meant (fetch_depth -> fetch-depth)."""
    wanted = sd.norm(key)
    for name in meta.inputs:
        if sd.norm(name) == wanted:
            return name
    close = difflib.get_close_matches(key.lower(), list(meta.inputs), n=1, cutoff=0.75)
    return close[0] if close else None


def unknown_input_evidence(meta: ActionMeta, key: str) -> str:
    guess = closest_input(meta, key)
    hint = f" (did you mean `{guess}`?)" if guess else ""
    known = ", ".join(sorted(meta.inputs)[:8]) or "none"
    return (f"{meta.source} declares no input `{key}`{hint}. GitHub only warns \"Unexpected input\", and the "
            f"action ignores the value unless its code reads undeclared inputs. Declared: {known}")


def _uses_sites(doc: dict) -> Iterator[Tuple[Tuple, str, dict]]:
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if isinstance(job.get("uses"), str) and isinstance(job.get("with"), dict):
            yield ("jobs", job_id), job["uses"].strip(), job["with"]
        for idx, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and isinstance(step.get("uses"), str) and isinstance(step.get("with"), dict):
                yield ("jobs", job_id, "steps", idx), step["uses"].strip(), step["with"]


def check_workflows(ctx: Context, changed: Sequence[ChangedFile]) -> List[Finding]:
    findings: List[Finding] = []
    cache: Dict[str, Tuple[Optional[ActionMeta], str]] = {}
    for cf in changed:
        if cf.status == "D" or not WORKFLOW.match(cf.path):
            continue
        if not sd.yaml_available():
            findings.append(Finding(Verdict.UNVERIFIABLE, "gha", cf.path, "(whole file)",
                                    "PyYAML is not installed, so workflows cannot be read", change="file"))
            continue
        ok_a, head = sd.load_structured(cf.path, ctx.head_bytes(cf.path))
        ok_b, base = sd.load_structured(cf.path, ctx.base_bytes(cf.path))
        if not ok_a or not isinstance(head, dict):
            continue
        base = base if ok_b else sd.MISSING
        base_flat = sd.flatten(base)
        touched = {c.path for c in sd.diff(base, head)}
        for prefix, uses, with_ in _uses_sites(head):
            keys = [k for k in with_ if any(p[:len(prefix) + 2] == prefix + ("with", k) for p in touched)]
            if not keys:
                continue
            if uses not in cache:
                cache[uses] = load_action_meta(ctx, uses)
            meta, why = cache[uses]
            for key in keys:
                path = prefix + ("with", key)
                kind = "changed" if any(p[:len(path)] == path for p in base_flat) else "added"
                subject = sd.fmt_path(path)
                name = str(key).lower()
                if meta is None:
                    findings.append(Finding(Verdict.UNVERIFIABLE, "gha", cf.path, subject, f"{uses}: {why}",
                                            change=kind))
                elif name in meta.inputs or (meta.docker and name in ("args", "entrypoint")):
                    note = meta.inputs.get(name)
                    findings.append(Finding(Verdict.EXISTS, "gha", cf.path, subject,
                                            f"declared input of {meta.source}"
                                            + (f"; deprecated: {first_line(note)}" if note else ""), change=kind))
                else:
                    findings.append(Finding(Verdict.UNKNOWN_KEY, "gha", cf.path, subject,
                                            unknown_input_evidence(meta, str(key)), change=kind))
    return findings


# --------------------------------------------------------------------------- env vars

ENV_FILE = re.compile(r"(^|/)\.env(\.[^/]+)?$")
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "poetry.lock", "uv.lock",
             "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum"}
KNOWN_VARS = {
    "NODE_ENV": "Node.js tooling", "NODE_OPTIONS": "Node.js", "NODE_PATH": "Node.js",
    "NODE_EXTRA_CA_CERTS": "Node.js", "TZ": "the C runtime", "LANG": "the C runtime",
    "LC_ALL": "the C runtime", "PATH": "the shell", "HOME": "the shell", "CI": "CI-aware tools",
    "DEBUG": "the debug logger", "FORCE_COLOR": "colour-aware CLIs", "NO_COLOR": "colour-aware CLIs",
    "PYTHONPATH": "Python", "PYTHONUNBUFFERED": "Python", "PYTHONDONTWRITEBYTECODE": "Python",
    "PYTHONHASHSEED": "Python", "PYTHONWARNINGS": "Python", "PYTHONUTF8": "Python",
    "VIRTUAL_ENV": "Python tooling", "HTTP_PROXY": "HTTP clients", "HTTPS_PROXY": "HTTP clients",
    "NO_PROXY": "HTTP clients", "http_proxy": "HTTP clients", "https_proxy": "HTTP clients",
    "no_proxy": "HTTP clients", "KUBECONFIG": "kubectl", "SSL_CERT_FILE": "OpenSSL",
    "REQUESTS_CA_BUNDLE": "requests",
}
KNOWN_PREFIXES = (
    ("AWS_", "AWS SDKs and CLI"), ("AZURE_", "Azure SDKs"), ("GOOGLE_", "Google Cloud SDKs"),
    ("CLOUDSDK_", "gcloud"), ("OTEL_", "OpenTelemetry SDKs"), ("NPM_CONFIG_", "npm"), ("npm_config_", "npm"),
    ("YARN_", "Yarn"), ("PNPM_", "pnpm"), ("PIP_", "pip"), ("UV_", "uv"), ("POETRY_", "Poetry"),
    ("PYTEST_", "pytest"), ("CARGO_", "Cargo"), ("RUSTUP_", "rustup"), ("GIT_", "git"), ("DOCKER_", "Docker"),
    ("COMPOSE_", "Docker Compose"), ("TF_", "Terraform"), ("NEXT_TELEMETRY_", "Next.js"), ("TURBO_", "Turborepo"),
    ("NX_", "Nx"), ("PLAYWRIGHT_", "Playwright"), ("PUPPETEER_", "Puppeteer"), ("HF_", "Hugging Face libraries"),
    ("CUDA_", "CUDA"), ("JAVA_", "the JVM"), ("MAVEN_", "Maven"), ("GRADLE_", "Gradle"), ("GH_", "the GitHub CLI"),
    ("GITHUB_", "GitHub Actions"), ("RUNNER_", "GitHub Actions"),
)
FRAMEWORK_VARS = {
    "next": ("Next.js", {"PORT", "HOSTNAME"}),
    "cra": ("Create React App", {"PORT", "HOST", "BROWSER", "PUBLIC_URL", "GENERATE_SOURCEMAP",
                                 "FAST_REFRESH", "WDS_SOCKET_PORT"}),
}
VITE_BUILTINS = {"MODE", "BASE_URL", "PROD", "DEV", "SSR"}


def parse_env(data: Optional[bytes]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if match:
            out[match.group(1)] = match.group(2).strip()
    return out


def _known_reader(name: str) -> Optional[str]:
    if name in KNOWN_VARS:
        return KNOWN_VARS[name]
    for prefix, who in KNOWN_PREFIXES:
        if name.startswith(prefix):
            return who
    return None


def _vite_prefix(ctx: Context, directory: str) -> Optional[str]:
    for name in ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts", "vite.config.cjs"):
        data = ctx.head_bytes(os.path.join(directory, name))
        if data is None:
            continue
        text = data.decode("utf-8", "replace")
        if "envPrefix" not in text:
            return "VITE_"
        match = re.search(r"envPrefix\s*:\s*['\"]([A-Za-z0-9_]+)['\"]", text)
        return match.group(1) if match else None  # an array or computed prefix: skip the rule
    return "VITE_"


def _frameworks(ctx: Context, path: str, cache: Dict[str, dict]) -> dict:
    """Frameworks declared in the package.json nearest to `path`."""
    directory = os.path.dirname(path)
    while True:
        pkg = os.path.join(directory, "package.json") if directory else "package.json"
        if pkg in cache:
            return cache[pkg]
        data = ctx.head_bytes(pkg)
        if data is not None:
            try:
                doc = json.loads(data)
            except ValueError:
                doc = {}
            deps: Dict[str, str] = {}
            for field in ("dependencies", "devDependencies", "peerDependencies"):
                if isinstance(doc.get(field), dict):
                    deps.update(doc[field])
            found: dict = {}
            if "vite" in deps:
                found["vite"] = _vite_prefix(ctx, directory)
            if "next" in deps:
                found["next"] = True
            if "react-scripts" in deps:
                found["cra"] = True
            cache[pkg] = found
            return found
        if not directory:
            return {}
        directory = os.path.dirname(directory)


def _is_client_file(ctx: Context, path: str) -> bool:
    for line in (ctx.head_bytes(path) or b"").decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        return stripped.rstrip(";") in ('"use client"', "'use client'")
    return False


def _prefix_problem(ctx: Context, name: str, hits, cache: Dict[str, dict]) -> Optional[str]:
    client: List[Tuple[str, str]] = []
    other = False
    for path, _, line in hits:
        fw = _frameworks(ctx, path, cache)
        rule = None
        if "vite" in fw and f"import.meta.env.{name}" in line:
            prefix = fw["vite"]
            if prefix and not name.startswith(prefix) and name not in VITE_BUILTINS:
                rule = f"Vite exposes only {prefix}-prefixed variables on import.meta.env"
        elif fw.get("next") and f"process.env.{name}" in line and _is_client_file(ctx, path):
            if not name.startswith("NEXT_PUBLIC_"):
                rule = "Next.js inlines only NEXT_PUBLIC_ variables into client components"
        elif fw.get("cra") and f"process.env.{name}" in line and ("/" + path).find("/src/") >= 0:
            if not name.startswith("REACT_APP_") and name not in ("NODE_ENV", "PUBLIC_URL"):
                rule = "Create React App exposes only REACT_APP_ variables to the browser"
        if rule:
            client.append((path, rule))
        else:
            other = True
    if client and not other:
        files = ", ".join(sorted({p for p, _ in client})[:3])
        return f"only read in browser code ({files}), but {client[0][1]}"
    return None


def check_env(ctx: Context, changed: Sequence[ChangedFile]) -> List[Finding]:
    targets: List[Tuple[str, List[Tuple[str, str]]]] = []
    for cf in changed:
        if cf.status == "D" or not ENV_FILE.search(cf.path):
            continue
        head, base = parse_env(ctx.head_bytes(cf.path)), parse_env(ctx.base_bytes(cf.path))
        names = [(n, "added" if n not in base else "changed") for n in head if base.get(n) != head[n]]
        targets.append((cf.path, names))
    for rel in ctx.env_files:
        if not any(t[0] == rel for t in targets):
            targets.append((rel, [(n, "file") for n in parse_env(ctx.head_bytes(rel))]))
    if not targets:
        return []
    env_paths = {t[0] for t in targets} | {p for p in ctx.files() if ENV_FILE.search(p)}
    cache: Dict[str, dict] = {}
    out: List[Finding] = []
    for rel, names in targets:
        for name, kind in names:
            who = _known_reader(name)
            if who:
                out.append(Finding(Verdict.EXISTS, "env", rel, name, f"read by {who}", change=kind))
                continue
            hits = [h for h in gitio.grep_word(ctx.root, name)
                    if h[0] not in env_paths and os.path.basename(h[0]) not in LOCKFILES]
            if not hits:
                fw = _frameworks(ctx, rel, cache)
                implicit = [label for key, (label, names_) in FRAMEWORK_VARS.items() if fw.get(key) and name in names_]
                if implicit:
                    out.append(Finding(Verdict.EXISTS, "env", rel, name, f"read by {implicit[0]}", change=kind))
                else:
                    out.append(Finding(Verdict.NOT_READ, "env", rel, name,
                                       f"nothing in the repo reads {name}: no code, config or script mentions it",
                                       change=kind))
                continue
            problem = _prefix_problem(ctx, name, hits, cache)
            if problem:
                out.append(Finding(Verdict.NOT_READ, "env", rel, name, problem, change=kind))
                continue
            where = ", ".join(sorted({f"{p}:{n}" for p, n, _ in hits})[:3])
            out.append(Finding(Verdict.EXISTS, "env", rel, name, f"referenced at {where}", change=kind))
    return out


# --------------------------------------------------------------------------- CLI flags

# Tools whose `--help` is safe to run and lists every long flag. Package managers are left out on
# purpose: `yarn build --help` would run a package script.
SAFE_HELP = {
    "tsc", "eslint", "prettier", "jest", "vitest", "vite", "next", "astro", "rollup", "esbuild", "tsup", "tsx",
    "playwright", "mocha", "concurrently", "turbo", "nx", "biome", "oxlint", "stylelint", "tailwindcss",
    "prisma", "drizzle-kit", "pip", "pip3", "uv", "pytest", "ruff", "black", "isort", "mypy", "pyright",
    "flake8", "pylint", "coverage", "tox", "pre-commit", "cargo", "docker", "kubectl", "helm", "gh", "jq", "curl",
}
SUBCOMMANDS = {"docker", "kubectl", "helm", "gh", "cargo", "uv", "pip", "pip3", "nx", "turbo", "prisma",
               "drizzle-kit", "vitest", "playwright", "ruff", "coverage", "pre-commit", "next", "astro", "biome"}
SAFE_SUBS = {"turbo": {"run"}, "nx": {"run", "run-many", "affected"}}
TWO_LEVEL = {("docker", "compose"), ("docker", "buildx"), ("docker", "image"), ("docker", "container"),
             ("kubectl", "rollout"), ("kubectl", "config"), ("helm", "repo"), ("uv", "pip"), ("uv", "tool"),
             ("uv", "python"), ("prisma", "migrate"), ("prisma", "db")}
# After the first positional argument, flags belong to the program being run, not to the tool.
STOP_AT_POSITIONAL = {"tsx", "docker run", "docker exec", "docker compose run", "docker compose exec",
                      "kubectl exec", "kubectl run"}
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FLAG = re.compile(r"^--[A-Za-z0-9][\w.-]*$")


def _added_lines(old: Optional[bytes], new: Optional[bytes]) -> List[str]:
    a = (old or b"").decode("utf-8", "replace").splitlines()
    b = (new or b"").decode("utf-8", "replace").splitlines()
    out: List[str] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.extend(b[j1:j2])
    return out


def _joined(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    buf = ""
    for line in lines:
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _run_sites(doc: dict) -> Iterator[Tuple[str, str]]:
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for idx, step in enumerate(job.get("steps") or []):
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield f"jobs.{job_id}.steps[{idx}].run", step["run"]


def _commands(ctx: Context, cf: ChangedFile) -> Iterator[Tuple[str, str, Optional[str]]]:
    """(where, new command, previous version of the same command or None)."""
    rel, base = cf.path, os.path.basename(cf.path)
    new, old = ctx.head_bytes(rel), ctx.base_bytes(rel)
    if base == "package.json":
        ok_n, doc_n = sd.load_structured(rel, new)
        ok_o, doc_o = sd.load_structured(rel, old)
        scripts = doc_n.get("scripts") if ok_n and isinstance(doc_n, dict) else None
        previous = doc_o.get("scripts") if ok_o and isinstance(doc_o, dict) else None
        previous = previous if isinstance(previous, dict) else {}
        for name, command in (scripts or {}).items() if isinstance(scripts, dict) else []:
            if isinstance(command, str) and previous.get(name) != command:
                prev = previous.get(name)
                yield f"scripts.{name}", command, prev if isinstance(prev, str) else None
    elif WORKFLOW.match(rel):
        ok_n, doc_n = sd.load_structured(rel, new)
        ok_o, doc_o = sd.load_structured(rel, old)
        if not ok_n or not isinstance(doc_n, dict):
            return
        old_lines = set()
        if ok_o and isinstance(doc_o, dict):
            for _, script in _run_sites(doc_o):
                old_lines.update(_joined(script.splitlines()))
        for where, script in _run_sites(doc_n):
            for line in _joined(script.splitlines()):
                if line.strip() and line not in old_lines:
                    yield where, line.strip(), None
    elif base in ("Makefile", "makefile", "GNUmakefile") or base.endswith(".mk"):
        for line in _joined(_added_lines(old, new)):
            if line.startswith("\t"):
                yield "recipe", line.strip().lstrip("@-+").strip(), None
    elif base.endswith((".sh", ".bash")):
        for line in _joined(_added_lines(old, new)):
            if line.strip() and not line.strip().startswith("#"):
                yield "script", line.strip(), None


def split_simple(command: str) -> List[List[str]]:
    """Split a shell line into simple commands (on ; && || | and parentheses)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    commands: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if token and set(token) <= set(";&|()<>"):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def unwrap(argv: List[str]) -> Optional[List[str]]:
    """Strip env assignments and launchers (npx, cross-env, uv run, python -m ...) to reach the tool."""
    i, n = 0, len(argv)
    while i < n:
        token = argv[i]
        name = os.path.basename(token)
        following = argv[i + 1] if i + 1 < n else ""
        if ENV_ASSIGN.match(token):
            i += 1
        elif name in ("env", "cross-env", "time", "exec", "nohup", "command", "npx", "bunx"):
            i += 1
            while i < n and argv[i].startswith("-"):
                i += 1
        elif name == "dotenv":
            rest = argv[i:]
            i = i + rest.index("--") + 1 if "--" in rest else n
        elif name in ("pnpm", "yarn", "npm", "bun") and following in ("exec", "dlx", "x"):
            i += 2
            while i < n and argv[i].startswith("-"):
                i += 1
        elif name in ("uv", "poetry", "pipenv", "pdm", "hatch", "rye") and following == "run":
            i += 2
            while i < n and argv[i].startswith("-"):
                takes_value = argv[i] in ("--with", "--python", "-p", "--env-file", "--extra", "--group", "--package")
                i += 2 if takes_value and "=" not in argv[i] else 1
        elif name in ("python", "python3") and following == "-m" and i + 2 < n:
            return [argv[i + 2]] + argv[i + 3:]
        else:
            return argv[i:]
    return None


def _flags(rest: Sequence[str], stop_at_positional: bool) -> List[str]:
    flags: List[str] = []
    for token in rest:
        if token == "--":
            break
        if not token.startswith("-"):
            if stop_at_positional:
                break
            continue
        name = token.split("=", 1)[0]
        if FLAG.match(name) and name not in flags:
            flags.append(name)
    return flags


def parse_tool_flags(argv: List[str]) -> Optional[Tuple[str, List[str], List[str]]]:
    """(tool, subcommand words, long flags) for one unwrapped command, or None when unreliable."""
    tool = os.path.basename(argv[0])
    rest = argv[1:]
    sub: List[str] = []
    if tool in SUBCOMMANDS and rest:
        if rest[0].startswith("-"):
            return None  # global flags before the subcommand: too ambiguous to judge
        sub, rest = [rest[0]], rest[1:]
        if (tool == "gh" or (tool, sub[0]) in TWO_LEVEL) and rest and not rest[0].startswith("-"):
            sub, rest = sub + [rest[0]], rest[1:]
        if tool in SAFE_SUBS and sub[0] not in SAFE_SUBS[tool]:
            return None
    stop = " ".join([tool] + sub) in STOP_AT_POSITIONAL or tool in STOP_AT_POSITIONAL
    return tool, sub, _flags(rest, stop)


def flag_in_help(flag: str, text: str) -> Optional[str]:
    """None when `text` does not document `flag`; otherwise a (possibly empty) note."""
    def documented(candidate: str) -> bool:
        return re.search(r"(?<![\w-])" + re.escape(candidate) + r"(?![\w-])", text) is not None

    name = flag[2:]
    if documented(flag) or documented("--[no-]" + name):
        return ""
    if name.startswith("no-") and (documented("--" + name[3:]) or documented("--[no-]" + name[3:])):
        return f" as the negation of --{name[3:]}"
    alt = re.sub(r"-(\w)", lambda m: m.group(1).upper(), name) if "-" in name else \
        re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), name)
    if alt != name and documented("--" + alt):
        return f" as --{alt}"
    return None


def _help_argv(exe: str, tool: str, sub: List[str]) -> List[str]:
    if tool == "curl":
        return [exe, "--help", "all"]
    if tool == "tsc":  # `tsc --help` lists only the common options
        return [exe, "--all"]
    return [exe] + sub + ["--help"]


def _read_help(ctx: Context, exe: str, tool: str, sub: List[str]) -> Tuple[Optional[str], str]:
    """(help text or None, how to cite it, e.g. "`tsc --all` (Version 5.6.3)")."""
    argv = _help_argv(exe, tool, sub)
    shown = "`" + " ".join([tool] + argv[1:]) + "`"
    res = run(argv, cwd=ctx.root, timeout=min(ctx.timeout, 30))
    text = res.text
    if res.missing or res.timed_out or not text.strip():
        return None, shown
    if tool == "kubectl":  # global flags live in `kubectl options`
        text += "\n" + run([exe, "options"], cwd=ctx.root, timeout=30).text
    version = run([exe, "--version"], cwd=ctx.root, timeout=10)
    line = first_line(version.text, 40) if version.ok else ""
    return text, shown + (f" ({line})" if line else "")


def check_cli_flags(ctx: Context, changed: Sequence[ChangedFile]) -> List[Finding]:
    out: List[Finding] = []
    helps: Dict[Tuple[str, Tuple[str, ...]], Tuple[Optional[str], str]] = {}
    seen = set()
    for cf in changed:
        if cf.status == "D":
            continue
        for where, command, previous in _commands(ctx, cf):
            old_flags = set()
            for argv in split_simple(previous or ""):
                unwrapped = unwrap(argv)
                parsed = parse_tool_flags(unwrapped) if unwrapped else None
                if parsed:
                    old_flags.update(parsed[2])
            for argv in split_simple(command):
                unwrapped = unwrap(argv)
                parsed = parse_tool_flags(unwrapped) if unwrapped else None
                if not parsed:
                    continue
                tool, sub, flags = parsed
                if tool not in SAFE_HELP and tool not in ctx.allow_cli:
                    continue
                new = [f for f in flags if f not in old_flags]
                if not new:
                    continue
                exe = ctx.which(tool, near=os.path.dirname(cf.path))
                label = " ".join([tool] + sub)
                for flag in new:
                    key = (cf.path, label, flag)
                    if key in seen:
                        continue
                    seen.add(key)
                    subject = f"{label} {flag}"
                    if not exe:
                        out.append(Finding(Verdict.UNVERIFIABLE, "cli", cf.path, subject,
                                           f"{tool} is not installed here, so its --help cannot be read "
                                           f"(in {where})", change="added"))
                        continue
                    hkey = (exe, tuple(sub))
                    if hkey not in helps:
                        helps[hkey] = _read_help(ctx, exe, tool, sub)
                    text, shown = helps[hkey]
                    if text is None:
                        out.append(Finding(Verdict.UNVERIFIABLE, "cli", cf.path, subject,
                                           f"{shown} printed nothing (in {where})", change="added"))
                        continue
                    note = flag_in_help(flag, text)
                    if note is None:
                        out.append(Finding(Verdict.PHANTOM_FLAG, "cli", cf.path, subject,
                                           f"{shown} does not list {flag} (in {where}); check the docs for "
                                           "the installed version", change="added"))
                    else:
                        out.append(Finding(Verdict.EXISTS, "cli", cf.path, subject,
                                           f"listed in {shown}{note} (in {where})", change="added"))
    return out
