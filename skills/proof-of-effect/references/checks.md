# What each checker runs

`check` runs the resolver checkers (before/after tool runs, one changed file at a time) and then the static checkers (no file is touched).

## Resolver checkers

| Checker | Files | Runs | Catches |
|---|---|---|---|
| tsconfig | `tsconfig*.json`, `jsconfig*.json` | `tsc --showConfig -p FILE` | options outside `compilerOptions` (tsc ignores unknown top-level keys), unknown compiler options, a new tsconfig that nothing extends, references or passes with `-p` |
| eslint | `eslint.config.*`, `.eslintrc*`, `package.json#eslintConfig` | `eslint --print-config SAMPLE` | edits to `.eslintrc*` while ESLint runs in flat-config mode, unknown keys and rules |
| jest | `jest.config.*`, `package.json#jest` | `jest --showConfig` | unknown options (jest only warns), non-default config files nothing passes with `--config` |
| prettier | `.prettierrc*`, `prettier.config.*`, `package.json#prettier` | `prettier.resolveConfig(SAMPLE)` via the project's own Prettier | options Prettier does not recognise (it silently ignores them), a `package.json#prettier` key or config file that a closer config overrides for the sample file |
| ruff | `ruff.toml`, `.ruff.toml`, `pyproject.toml#tool.ruff` | `ruff check --show-settings SAMPLE` | unknown fields, settings that resolve to the same value |
| pytest | `pytest.ini`, `pyproject.toml#tool.pytest`, `tox.ini#pytest`, `setup.cfg#tool:pytest` | pytest with a plugin that dumps the resolved ini options and stops before collection | unknown ini keys, edits to a file pytest does not load (for example `pyproject.toml` when `pytest.ini` exists) |
| compose | `compose*.y(a)ml`, `docker-compose*.y(a)ml` | `docker compose config --format json` | properties Compose rejects or ignores. **Experimental: not yet tested against a real Docker install** |
| pyproject | other `[tool.*]` tables | — | a table name one typo away from a known tool (`[tool.ruf]`) is NOT-READ. Other tool tables are UNVERIFIABLE: probe them |

SAMPLE is a source file the per-file config applies to: a changed file if there is one, otherwise a tracked file under `src/`, `lib/` or `app/`. Override it with `--sample FILE`.

Tools are looked up in `node_modules/.bin` (from the config's folder up to the repo root), then `.venv/bin` and `venv/bin`, then `PATH`. `--tool NAME=PATH` overrides the lookup.

How a key gets its verdict: the before and after resolved configs are diffed, and each changed key in your file is matched to the resolved changes by name (`line-length` matches `linter.line_length`). A key with no match is NO-OP when the file's other edits explain every resolved change. It is UNCLEAR when some resolved change is left unexplained.

## Static checkers

| Checker | Looks at | Verdicts |
|---|---|---|
| gha | `with:` keys you changed in `.github/workflows/*.yml`, checked against the action's `action.yml` (local, public via raw.githubusercontent.com, private via `gh api`) or a reusable workflow's `workflow_call.inputs` | UNKNOWN-KEY when the input does not exist (GitHub only warns "Unexpected input"); EXISTS otherwise, with the deprecation message if there is one |
| env | variables added or changed in `.env*` files (and every variable in `--env-file` files) | NOT-READ when nothing in the repo mentions the name, or when it is read only in browser code without the prefix Vite (`VITE_` or a custom `envPrefix`), Next.js client components (`NEXT_PUBLIC_`) or Create React App (`REACT_APP_`) require. EXISTS when code reads it or a known runtime does (`NODE_OPTIONS`, `AWS_*`, ...) |
| cli | long flags added to `package.json` scripts, workflow `run:` lines, Makefile recipes and `*.sh` files | PHANTOM-FLAG when the installed tool's `--help` does not list the flag. It accepts `--no-x` as the negation of `--x`, and camelCase or kebab-case spellings |

The CLI checker only reads `--help` from a fixed list of tools whose help is safe to run and complete (tsc, eslint, prettier, jest, vitest, vite, next, ruff, pytest, mypy, cargo, docker, kubectl, helm, gh, ...). It skips package managers, because `yarn build --help` would run a script. It also skips commands with global flags before the subcommand, because they are too ambiguous to judge. `--allow-cli NAME` adds a tool.

## Known gaps

- EFFECTIVE means the resolved config changed. It does not prove the behaviour you wanted. Explicitly setting an option to its default can show as EFFECTIVE in tools that echo explicit values.
- Edits to a shared base config (for example `tsconfig.base.json`) are judged by resolving that file itself, not every config that extends it.
- Deleted config files are not judged.
- Not covered yet: Next.js/Vite/webpack config files, Babel, Prettier, mypy, Cargo, Maven, Terraform, Kubernetes manifests. Use `probe` for these.
