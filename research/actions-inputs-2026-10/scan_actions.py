#!/usr/bin/env python3
"""Read-only scanner: how often do popular public GitHub repos pass `with:` inputs
to GitHub Actions that the action does not declare?

Reuses the proof-of-effect parsing code (static_checks / structdiff). Stdlib + PyYAML only.
Resumable via an on-disk cache in <out>/cache.

Usage:
    python3 scan_actions.py --top 1000 --out /abs/out/dir
    python3 scan_actions.py --top 1000 --out DIR --page-start 1 --page-end 2   # one chunk
    python3 scan_actions.py --out DIR --aggregate                              # just rebuild reports
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

POE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "skills", "proof-of-effect", "scripts")
sys.path.insert(0, POE)
from poe import static_checks as sc  # noqa: E402
from poe import structdiff  # noqa: E402

GH = os.environ.get("GH_BIN") or __import__("shutil").which("gh") or "gh"
RAW = "https://raw.githubusercontent.com"
UA = {"User-Agent": "proof-of-effect-scan"}
MAX_FETCH_WORKERS = 8


# --------------------------------------------------------------------------- gh / http

def gh_api(path: str, retries: int = 3) -> Optional[object]:
    """Read-only `gh api <path>` -> parsed JSON, or None on hard failure."""
    for attempt in range(retries):
        proc = subprocess.run([GH, "api", path], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout)
            except ValueError:
                return None
        err = proc.stderr.lower()
        if "404" in err or "not found" in err:
            return None
        if "rate limit" in err or "secondary rate" in err or "abuse" in err:
            time.sleep(20 * (attempt + 1))
            continue
        time.sleep(2 * (attempt + 1))
    return None


def wait_for_core(threshold: int = 200) -> None:
    data = gh_api("rate_limit")
    try:
        core = data["resources"]["core"]  # type: ignore[index]
    except (TypeError, KeyError):
        return
    if core["remaining"] < threshold:
        reset = core.get("reset", time.time() + 60)
        nap = max(5, min(90, reset - time.time() + 3))
        time.sleep(nap)


def wait_for_search() -> None:
    data = gh_api("rate_limit")
    try:
        s = data["resources"]["search"]  # type: ignore[index]
    except (TypeError, KeyError):
        return
    if s["remaining"] < 2:
        reset = s.get("reset", time.time() + 60)
        time.sleep(max(5, min(90, reset - time.time() + 3)))


def fetch_text(url: str, timeout: float = 15) -> Tuple[Optional[str], Optional[str]]:
    """(text, error). text None + error None => 404 (clean miss). error set => real failure."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 fixed https host
            return resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, None
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, UnicodeError) as exc:
        return None, str(exc)


# --------------------------------------------------------------------------- cache

class Cache:
    """Keyed JSON blobs on disk; one file per key hash. Namespaces: repos, wf, meta."""

    def __init__(self, root: str):
        self.dir = os.path.join(root, "cache")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, ns: str, key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:180]
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.dir, f"{ns}__{safe}__{h}.json")

    def get(self, ns: str, key: str):
        p = self._path(ns, key)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    return json.load(fh)
            except ValueError:
                return None
        return None

    def put(self, ns: str, key: str, value) -> None:
        p = self._path(ns, key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh)
        os.replace(tmp, p)


# --------------------------------------------------------------------------- metadata resolution

def parse_meta_from_text(text: str, source: str, reusable: bool) -> Dict:
    meta = sc._parse_meta(text, source, reusable)
    return {"source": meta.source, "inputs": list(meta.inputs.keys()), "docker": meta.docker}


def _candidate_urls(owner: str, repo: str, sub: str, ref: str, reusable: bool) -> List[str]:
    if reusable:
        return [f"{RAW}/{owner}/{repo}/{ref}/{sub}"]
    base = f"{sub}/" if sub else ""
    return [f"{RAW}/{owner}/{repo}/{ref}/{base}action.yml",
            f"{RAW}/{owner}/{repo}/{ref}/{base}action.yaml"]


def resolve_remote_meta(uses: str, ref_override: Optional[str] = None) -> Dict:
    """Resolve an owner/repo[/sub]@ref action's metadata.
    Returns {ok, inputs, docker, source, reusable, error}."""
    m = sc.ACTION_REF.match(uses)
    if not m:
        return {"ok": False, "error": "unparseable uses", "unresolved": False}
    owner, repo, sub, ref = m.group(1), m.group(2), m.group(3).strip("/"), m.group(4)
    if ref_override:
        ref = ref_override
    reusable = bool(sc.YAML_FILE.search(sub))
    errors: List[str] = []
    for url in _candidate_urls(owner, repo, sub, ref, reusable):
        text, err = fetch_text(url)
        if text is not None:
            try:
                meta = parse_meta_from_text(text, f"{owner}/{repo}/{sub}@{ref}" if sub else f"{owner}/{repo}@{ref}",
                                            reusable)
            except Exception as exc:  # noqa: BLE001 - YAML parse blowups count as unresolved
                return {"ok": False, "error": f"parse: {exc}", "unresolved": True}
            meta.update({"ok": True, "reusable": reusable, "error": None, "unresolved": False})
            return meta
        if err:
            errors.append(err)
    if errors:  # real fetch error, not a clean 404
        return {"ok": False, "error": errors[0], "unresolved": True}
    return {"ok": False, "error": "no action.yml at ref (404)", "unresolved": False}


def resolve_local_meta(owner: str, repo: str, branch: str, uses: str) -> Dict:
    """Local `./path` action/reusable-workflow inside the scanned repo."""
    rel = uses[2:] if uses.startswith("./") else uses
    reusable = bool(sc.YAML_FILE.search(rel))
    if reusable:
        candidates = [rel]
    else:
        candidates = [f"{rel.rstrip('/')}/action.yml", f"{rel.rstrip('/')}/action.yaml"]
    errors: List[str] = []
    for path in candidates:
        url = f"{RAW}/{owner}/{repo}/{branch}/{path}"
        text, err = fetch_text(url)
        if text is not None:
            try:
                meta = parse_meta_from_text(text, f"{owner}/{repo}//{path}@{branch}", reusable)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"parse: {exc}", "unresolved": True}
            meta.update({"ok": True, "reusable": reusable, "error": None, "unresolved": False})
            return meta
        if err:
            errors.append(err)
    if errors:
        return {"ok": False, "error": errors[0], "unresolved": True}
    return {"ok": False, "error": "local action not found (404)", "unresolved": False}


# --------------------------------------------------------------------------- repo listing

def list_repos(top: int, cache: Cache) -> List[Dict]:
    repos: List[Dict] = []
    pages = (top + 99) // 100
    for page in range(1, pages + 1):
        key = f"page{page}"
        cached = cache.get("repos", key)
        if cached is None:
            wait_for_search()
            data = gh_api(
                f"search/repositories?q=stars:%3E1000&sort=stars&order=desc&per_page=100&page={page}")
            items = (data or {}).get("items", []) if isinstance(data, dict) else []
            cached = [{"full_name": it["full_name"], "stars": it["stargazers_count"],
                       "default_branch": it.get("default_branch", "HEAD")} for it in items]
            cache.put("repos", key, cached)
            time.sleep(2.2)  # search API: 30 req/min
        repos.extend(cached)
    return repos[:top]


def list_workflows(owner: str, repo: str, cache: Cache) -> Optional[List[Dict]]:
    """[{name, download_url}] or None when the repo has no .github/workflows (404)."""
    key = f"{owner}/{repo}"
    cached = cache.get("wf", key)
    if cached is not None:
        return cached if cached != "__none__" else None
    wait_for_core()
    data = gh_api(f"repos/{owner}/{repo}/contents/.github/workflows")
    if not isinstance(data, list):
        cache.put("wf", key, "__none__")
        return None
    out = [{"name": e["name"], "download_url": e.get("download_url")}
           for e in data if isinstance(e, dict) and re.search(r"\.ya?ml$", e.get("name", ""))
           and e.get("download_url")]
    cache.put("wf", key, out)
    return out


# --------------------------------------------------------------------------- per-repo scan

def _is_local(uses: str) -> bool:
    return uses.startswith(("./", "../"))


def meta_for(uses: str, owner: str, repo: str, branch: str, cache: Cache,
             metacache: Dict[str, Dict]) -> Dict:
    """Pinned-ref metadata, cached per exact key.

    Remote `owner/repo@ref` uses strings are globally unique, so they key on `uses`.
    A local `./path` action is repo-specific, so it MUST be keyed by repo+branch+path;
    keying it on the bare `uses` would leak one repo's local action to every other repo
    that has an action at the same relative path.
    """
    ck = uses if not _is_local(uses) else f"{owner}/{repo}@{branch}::{uses}"
    if ck in metacache:
        return metacache[ck]
    disk = cache.get("meta", ck)
    if disk is not None:
        metacache[ck] = disk
        return disk
    if uses.startswith("docker://"):
        res = {"ok": False, "error": "docker image action", "unresolved": False, "skip": True}
    elif _is_local(uses):
        res = resolve_local_meta(owner, repo, branch, uses)
    else:
        res = resolve_remote_meta(uses)
    cache.put("meta", ck, res)
    metacache[ck] = res
    return res


def head_meta_for(uses: str, cache: Cache, metacache: Dict[str, Dict]) -> Dict:
    """HEAD (default-branch) metadata for a remote action, to classify unknown inputs."""
    m = sc.ACTION_REF.match(uses)
    if not m:
        return {"ok": False}
    ck = f"HEAD::{m.group(1)}/{m.group(2)}{m.group(3)}"
    if ck in metacache:
        return metacache[ck]
    disk = cache.get("meta", ck)
    if disk is not None:
        metacache[ck] = disk
        return disk
    res = resolve_remote_meta(uses, ref_override="HEAD")
    cache.put("meta", ck, res)
    metacache[ck] = res
    return res


def classify(uses: str, key: str, pinned: Dict, cache: Cache, metacache: Dict[str, Dict]) -> str:
    """newer-version-only | typo | removed  for a remote action's unknown input."""
    name = key.lower()
    if _is_local(uses):
        # local action at default branch IS head; a near match => typo else removed
        guess = _closest(pinned.get("inputs", []), key)
        return "typo" if guess else "removed"
    head = head_meta_for(uses, cache, metacache)
    if head.get("ok") and name in [i.lower() for i in head.get("inputs", [])]:
        return "newer-version-only"
    guess = _closest(pinned.get("inputs", []), key)
    if guess:
        return "typo"
    return "removed"


def _closest(inputs: List[str], key: str) -> Optional[str]:
    """closest_input over a plain input-name list (rebuild a tiny ActionMeta)."""
    meta = sc.ActionMeta("x", {i.lower(): None for i in inputs})
    return sc.closest_input(meta, key)


def scan_repo(repo: Dict, cache: Cache, metacache: Dict[str, Dict]) -> Tuple[List[Dict], Dict]:
    """Returns (findings, per-repo stats)."""
    full = repo["full_name"]
    owner, name = full.split("/", 1)
    branch = repo.get("default_branch") or "HEAD"
    stats = {"has_workflows": False, "wf_files": 0, "sites": 0, "inputs": 0,
             "unknown": 0, "unresolved": 0, "repo_has_unknown": False}
    findings: List[Dict] = []

    wfs = list_workflows(owner, name, cache)
    if not wfs:
        return findings, stats
    stats["has_workflows"] = True

    # Parallel raw fetch of workflow file bodies.
    urls = [w["download_url"] for w in wfs if w.get("download_url")]
    bodies: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    with cf.ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as ex:
        for url, res in zip(urls, ex.map(lambda u: fetch_text(u), urls)):
            bodies[url] = res

    for w in wfs:
        url = w.get("download_url")
        if not url:
            continue
        text, err = bodies.get(url, (None, None))
        if text is None:
            if err:
                stats["unresolved"] += 1
            continue
        stats["wf_files"] += 1
        doc = structdiff.load_yaml_text(text)
        if not isinstance(doc, dict):
            stats["unresolved"] += 1
            continue
        wf_path = f".github/workflows/{w['name']}"
        for prefix, uses, with_ in sc._uses_sites(doc):
            if not isinstance(with_, dict) or not with_:
                continue
            if uses.startswith("docker://"):
                continue
            stats["sites"] += 1
            meta = meta_for(uses, owner, name, branch, cache, metacache)
            if not meta.get("ok"):
                if meta.get("unresolved"):
                    stats["unresolved"] += 1
                continue
            declared = {i.lower() for i in meta.get("inputs", [])}
            docker = meta.get("docker", False)
            for key in with_:
                stats["inputs"] += 1
                low = str(key).lower()
                if low in declared or (docker and low in ("args", "entrypoint")):
                    continue
                cls = classify(uses, str(key), meta, cache, metacache)
                guess = _closest(meta.get("inputs", []), str(key))
                stats["unknown"] += 1
                stats["repo_has_unknown"] = True
                findings.append({
                    "repo": full, "stars": repo.get("stars"), "workflow": wf_path,
                    "step": structdiff.fmt_path(prefix), "uses": uses, "input": str(key),
                    "class": cls, "closest": guess,
                })
    return findings, stats


# --------------------------------------------------------------------------- aggregate

def aggregate(out_dir: str) -> Dict:
    findings = _load_json(os.path.join(out_dir, "results", "findings.json"), [])
    repo_stats = _load_json(os.path.join(out_dir, "cache", "_repo_stats.json"), {})

    by_class = Counter(f["class"] for f in findings)
    action_unknowns = Counter(f["uses"] for f in findings)
    action_badnames: Dict[str, Counter] = defaultdict(Counter)
    for f in findings:
        action_badnames[f["uses"]][f["input"]] += 1

    repos_scanned = len(repo_stats)
    repos_with_wf = sum(1 for s in repo_stats.values() if s.get("has_workflows"))
    wf_files = sum(s.get("wf_files", 0) for s in repo_stats.values())
    sites = sum(s.get("sites", 0) for s in repo_stats.values())
    inputs_checked = sum(s.get("inputs", 0) for s in repo_stats.values())
    unresolved = sum(s.get("unresolved", 0) for s in repo_stats.values())
    repos_unknown = sum(1 for s in repo_stats.values() if s.get("repo_has_unknown"))

    top_actions = []
    for uses, cnt in action_unknowns.most_common(15):
        top_actions.append({
            "uses": uses, "unknown_inputs": cnt,
            "top_bad_inputs": action_badnames[uses].most_common(5),
        })

    summary = {
        "repos_scanned": repos_scanned,
        "repos_with_workflows": repos_with_wf,
        "workflow_files": wf_files,
        "uses_with_inputs_sites": sites,
        "inputs_checked": inputs_checked,
        "unknown_inputs_total": len(findings),
        "unknown_inputs_by_class": dict(by_class),
        "repos_with_unknown_input": repos_unknown,
        "repos_with_unknown_input_pct": round(100 * repos_unknown / repos_scanned, 2) if repos_scanned else 0,
        "unresolved_count": unresolved,
        "top_actions": top_actions,
    }
    _dump_json(os.path.join(out_dir, "results", "summary.json"), summary)
    return summary


def _load_json(path: str, default):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _dump_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2)


# --------------------------------------------------------------------------- driver

def run_scan(top: int, out_dir: str, page_start: int, page_end: int) -> None:
    cache = Cache(out_dir)
    os.makedirs(os.path.join(out_dir, "results"), exist_ok=True)
    all_repos = list_repos(top, cache)

    # chunk by search page (100 repos each)
    lo = (page_start - 1) * 100
    hi = min(page_end * 100, len(all_repos))
    chunk = all_repos[lo:hi]

    findings_path = os.path.join(out_dir, "results", "findings.json")
    stats_path = os.path.join(out_dir, "cache", "_repo_stats.json")
    findings = _load_json(findings_path, [])
    repo_stats = _load_json(stats_path, {})
    # drop any findings from repos we are about to rescan (idempotent resume)
    rescanning = {r["full_name"] for r in chunk}
    findings = [f for f in findings if f["repo"] not in rescanning]

    metacache: Dict[str, Dict] = {}
    done = 0
    for repo in chunk:
        full = repo["full_name"]
        fnd, st = scan_repo(repo, cache, metacache)
        findings.extend(fnd)
        repo_stats[full] = st
        done += 1
        if done % 20 == 0:
            _dump_json(findings_path, findings)
            _dump_json(stats_path, repo_stats)
            wait_for_core()
    _dump_json(findings_path, findings)
    _dump_json(stats_path, repo_stats)
    aggregate(out_dir)
    print(f"chunk pages {page_start}-{page_end}: scanned {done} repos, "
          f"findings now {len(findings)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=1000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--page-start", type=int, default=1)
    ap.add_argument("--page-end", type=int, default=10)
    ap.add_argument("--aggregate", action="store_true", help="just rebuild summary.json")
    args = ap.parse_args()
    out_dir = os.path.abspath(args.out)
    if args.aggregate:
        s = aggregate(out_dir)
        print(json.dumps(s, indent=2))
        return 0
    run_scan(args.top, out_dir, args.page_start, args.page_end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
