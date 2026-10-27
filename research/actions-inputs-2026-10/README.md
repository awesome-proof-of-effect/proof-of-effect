# Undeclared `with:` inputs in popular GitHub Actions workflows

When a workflow passes a `with:` input that the called action does not declare,
GitHub emits only a non-blocking **"Unexpected input"** warning in the run log and
the action silently ignores the value (unless the action's own code reaches for
undeclared inputs, which is rare). The step still succeeds, so the mistake is easy
to miss. This report measures how often that happens across the most-starred public
repositories.

## Method

- **Population.** The most-starred public repositories (`stars:>1000`, descending),
  as returned by the GitHub search API. After the index's own de-duplication this
  resolved to **925** distinct repositories.
- For each repository we listed `.github/workflows/` and downloaded every `.yml`/`.yaml`
  file over `raw.githubusercontent.com` (no token).
- In each workflow we found every `uses:`+`with:` site (both steps and
  reusable-workflow jobs) and resolved the called action's declared inputs:
  - `owner/repo[/sub]@ref` → `action.yml`/`action.yaml` at that **exact ref**
    (a `.yml`/`.yaml` ref is a reusable workflow, read from `on.workflow_call.inputs`);
  - local `./path` → resolved **inside the scanning repo at its default branch**
    (kept strictly per-repo — a local action at a shared relative path is never
    shared between repositories);
  - `docker://` images declare no inputs and are skipped.
  - Metadata is cached per exact action reference; inputs compare case-insensitively;
    Docker container actions additionally accept `args`/`entrypoint`.
- **Classification.** For every undeclared input we also fetched the action's
  metadata at its default branch (`HEAD`):
  - **newer-version-only** — declared at HEAD but not at the pinned ref (the input
    exists now; the workflow pins an older version that predates it);
  - **typo** — a near-match to a declared input (e.g. `fetch_depth` → `fetch-depth`);
  - **removed** — present in neither the ref nor HEAD and with no near match.
- Parsing reuses the open-source proof-of-effect checker
  (`static_checks._parse_meta`, `_uses_sites`, `closest_input`, `ACTION_REF`;
  `structdiff.load_yaml_text`). The scanner is stdlib + PyYAML only and resumes
  from an on-disk cache. **Read-only throughout** — no issues, PRs, comments, stars
  or forks were created.

## Aggregate results

| Metric | Value |
|---|---|
| Repositories scanned | 925 |
| Repositories with workflows | 751 (81.19%) |
| Workflow files parsed | 8,793 |
| `uses:`+`with:` sites | 43,549 |
| Individual inputs checked | 102,293 |
| **Undeclared inputs found** | **113** |
| Repositories with ≥1 undeclared input | 46 (4.97%) |
| Unresolved (fetch/parse failures, **not** counted as unknown) | 3 |

Undeclared inputs are rare in absolute terms — about **113 in
102,293** inputs (0.11%)
— but they are spread across **46** well-known projects,
so roughly **1 in 20** of these repositories ships at least one workflow input the
action does not declare. Leaving out `octokit/request-action`, which forwards undeclared
inputs on purpose, it is **95 inputs in 44 repositories**.

Reproduce with `python3 research/actions-inputs-2026-10/scan_actions.py --help` (needs the
GitHub CLI signed in, read-only). Scanned on 2026-10-27.

### Classification

| Class | Count | Share |
|---|---|---|
| removed (gone from ref and HEAD, no near match) | 70 | 61.95% |
| newer-version-only (exists at HEAD, not at the pinned ref) | 23 | 20.35% |
| typo (near-match to a declared input) | 20 | 17.70% |

Two patterns dominate. **removed** is the largest: inputs that an action once accepted
(or, for forwarding actions like `octokit/request-action`, never declared because they
are passed straight through to an API) and that the pinned version does not declare.
**newer-version-only** is the clearest actionable signal — the workflow pins a version
older than the one that introduced the input, so the value is ignored until the action
is bumped. **typo** inputs are silent misconfigurations (e.g. `cached` for `cache`).

## Actions receiving the most undeclared inputs

| Action (`uses`) | Undeclared inputs | Most common bad input names |
|---|---|---|
| `octokit/request-action@b91aabaa861c777dcdb14e2387e30eddf04619ae` | 12 | `repo`×4, `number`×2, `issue_number`×1 |
| `dtolnay/rust-toolchain@e081816240890017053eacbb1bdf337761dc5582` | 10 | `toolchain`×10 |
| `trunk-io/analytics-uploader@14c0f18990953eecd01c9c5afe3cc63a73b6c371` | 8 | `tags`×8 |
| `dephraiim/translate-readme@main` | 7 | `COMMIT`×7 |
| `octokit/request-action@v2.x` | 6 | `owner`×1, `repo`×1, `title`×1 |
| `taiki-e/install-action@44c6d64aa62cd779e873306675c7a58e86d6d532` | 5 | `version`×5 |
| `grafana/shared-workflows/actions/get-vault-secrets@2f1ddaf3ce1aa677356243d0dd584be33cfa35e3` | 4 | `export_env`×4 |
| `google-github-actions/run-gemini-cli@a3bf79042542528e91937b3a3a6fbc4967ee3c31` | 3 | `upload_artifacts`×3 |
| `peter-evans/create-pull-request@v7` | 3 | `git-token`×3 |
| `jurplel/install-qt-action@v4.1.1` | 3 | `cached`×3 |
| `lukka/run-vcpkg@v11` | 3 | `vcpkgArguments`×1, `appendedCacheKey`×1, `vcpkgTriplet`×1 |
| `astral-sh/setup-uv@v5` | 2 | `cache-python`×2 |
| `actions/upload-artifact@65462800fd760344b1a7b4382951275a0abb4808` | 2 | `include-hidden-files`×2 |
| `dessant/lock-threads@89ae32b08ed1a541efecbab17912962a5e38981c` | 2 | `issue-lock-inactive-days`×1, `pr-lock-inactive-days`×1 |
| `SethCohen/github-releases-to-discord@24d166886aee4646d448c8a389ff9e1ebcab3682` | 2 | `embed_color`×1, `include_assets`×1 |

## Verification / spot-check

We re-read the **exact `action.yml` at the exact pinned ref** for 20 reported
undeclared inputs spanning all three classes and the highest-frequency actions.
**All 20 were confirmed genuinely undeclared; 0 false positives.**

| Action | Input | Class | Re-read verdict |
|---|---|---|---|
| `taiki-e/install-action@44c6d6…` | `version` | removed | ✅ undeclared |
| `dtolnay/rust-toolchain@e08181…` | `toolchain` | newer-version-only | ✅ undeclared |
| `astral-sh/setup-uv@v5` | `cache-python` | newer-version-only | ✅ undeclared |
| `actions/upload-artifact@654628…` | `include-hidden-files` | newer-version-only | ✅ undeclared |
| `trunk-io/analytics-uploader@14c0f1…` | `tags` | removed | ✅ undeclared |
| `dessant/lock-threads@89ae32…` | `issue-lock-inactive-days` | typo | ✅ undeclared |
| `dessant/lock-threads@89ae32…` | `pr-lock-inactive-days` | typo | ✅ undeclared |
| `octokit/request-action@b91aab…` | `repo` | removed | ✅ undeclared |
| `octokit/request-action@b91aab…` | `number` | removed | ✅ undeclared |
| `octokit/request-action@b91aab…` | `issue_number` | removed | ✅ undeclared |
| `octokit/request-action@b91aab…` | `name` | removed | ✅ undeclared |
| `octokit/request-action@b91aab…` | `owner` | removed | ✅ undeclared |
| `octokit/request-action@b91aab…` | `title` | removed | ✅ undeclared |
| `octokit/request-action@b91aab…` | `body` | removed | ✅ undeclared |
| `octokit/request-action@b91aab…` | `labels` | removed | ✅ undeclared |
| `peter-evans/create-pull-request@v7` | `git-token` | removed | ✅ undeclared |
| `jurplel/install-qt-action@v4.1.1` | `cached` | typo | ✅ undeclared |
| `lukka/run-vcpkg@v11` | `vcpkgArguments` | removed | ✅ undeclared |
| `lukka/run-vcpkg@v11` | `appendedCacheKey` | removed | ✅ undeclared |
| `lukka/run-vcpkg@v11` | `vcpkgTriplet` | removed | ✅ undeclared |

A false-positive source found and fixed during verification: local `./path` actions
were initially cached by their bare relative path, which is not unique across
repositories, so one repository's local action could leak its declared inputs to
another's. After keying local metadata by `repo@branch::path`, the undeclared-input
count fell from an inflated 622 to the verified **113**, with
remote actions (globally unique references) dominating the corrected results — as
expected.

## Limits

- **Snapshot in time.** HEAD metadata is read live, so `newer-version-only`
  reflects the action's current default branch at scan time.
- **Forwarding actions.** A few actions (notably `octokit/request-action`) intentionally
  accept arbitrary inputs and forward them to an API; their `action.yml` declares none of
  them, so they are correctly counted as undeclared even though the author's intent is valid.
  This is the same "Unexpected input" warning a user sees, so it is in scope.
- **Reusable workflows** are read from `on.workflow_call.inputs`; a few repos use
  expression-built matrices our static read cannot resolve.
- **Unresolved** fetch or YAML-parse failures (3) are reported
  separately and never counted as undeclared inputs.
- Repository names are intentionally kept out of this prose; per-input detail lives in
  [`findings.json`](findings.json).
