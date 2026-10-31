# Roadmap

Work happens top to bottom. Bugs reported in issues and breaking changes in supported tools jump the queue.

| # | Item | Why | Status |
|---|---|---|---|
| 1 | GitHub Action: `uses: awesome-proof-of-effect/proof-of-effect@v1`, which checks a PR's config changes against its base and writes the table to the job summary | Lets teams run it on every PR without an agent | done ([action.yml](action.yml)) |
| 2 | Findings report: Action inputs that popular repositories pass but the action never declares | Shows how common silent no-ops are, with real data | done ([report](research/actions-inputs-2026-10/)) |
| 3 | Terminal demo (GIF) in the README | Shows the value in five seconds | done ([demo.svg](research/demo/demo.svg), [generator](skills/proof-of-effect/scripts/make_demo.py)) |
| 4 | One-command install for Claude Code, Codex, Kiro and other `SKILL.md` agents | Removes setup friction | done ([install.py](skills/proof-of-effect/scripts/install.py)) |
| 5 | Docker Compose: integration test in CI (runners have Docker) | The compose checker is still marked experimental | todo |
| 6 | Prettier checker (resolved options per file) | Very common config, often edited by agents | done ([resolvers.py](skills/proof-of-effect/scripts/poe/resolvers.py), [tests](tests/test_real_tools.py)) |
| 7 | Next.js and Vite config checkers | The most-edited frontend configs | todo |
| 8 | Shared base configs: also judge every config that extends a changed base | `tsconfig.base.json` edits in monorepos | todo |
| 9 | Babel (`BABEL_SHOW_CONFIG_FOR`) and Cargo (unused manifest keys) checkers | More ecosystems | todo |
| 10 | mypy checker | Python type-checking config | todo |
| 11 | pre-commit hook | Catch no-ops before they reach a PR | todo |
| 12 | Windows support (paths, tool lookup) | Wider reach | todo |
