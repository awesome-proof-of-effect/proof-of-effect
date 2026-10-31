---
name: proof-of-effect
description: Prove that a config, CLI-flag or env-var change actually changes what the tool does before calling it a fix. Catches silent no-ops such as keys in the wrong place or misspelled, options the installed version does not have, config files nothing loads, env vars nothing reads (or missing a VITE_/NEXT_PUBLIC_ prefix), GitHub Action inputs that do not exist, and CLI flags the installed tool does not list. Use after editing tsconfig, ESLint, Jest, Prettier, Ruff, pytest or Docker Compose config, CI workflows, .env files, package.json scripts, Makefiles or shell scripts, and whenever the fix you are about to report is a config change.
---

# Proof of effect

A config "fix" can be valid JSON or YAML, pass CI and look right in review, and still change nothing, because the tool silently ignores the key. This skill asks the tool itself: it resolves the config with the base version of the file and with your version, and compares.

## When to run it

- You changed a config file, a CLI flag in a script or workflow, or an env var, and you are about to say it fixes something.
- Someone says a setting "doesn't seem to work".
- Before opening a PR that touches build, lint, test, CI or deploy configuration.

## Run it

`SKILL_DIR` is the folder that contains this file. Run from the repository root:

```bash
python3 SKILL_DIR/scripts/poe.py check                     # working tree vs HEAD
python3 SKILL_DIR/scripts/poe.py check --base origin/main  # everything on this branch
python3 SKILL_DIR/scripts/poe.py check --all               # audit what is already there (Action inputs, env vars, script flags)
```

It needs Python 3.9+ and git. PyYAML enables the workflow checks. Exit code 1 means at least one change does not take effect. Add `--json` for machine-readable output.

## What each verdict means

| Verdict | Meaning | What you do |
|---|---|---|
| EFFECTIVE | The tool's resolved config changed in that setting | Fine. Quote the evidence in your report |
| NO-OP | The tool resolves the same config before and after | It is not a fix. The evidence often names the file the tool really loads; move the setting there, or revert |
| UNKNOWN-KEY | The tool, or the action's metadata, does not know the name | Fix the spelling, nesting or version |
| NOT-READ | Nothing loads the file, nothing reads the variable, or a browser prefix is missing | Wire it up (`extends`, `--config`, the prefix) or revert |
| PHANTOM-FLAG | The installed CLI's `--help` does not list the flag | Use a flag that exists in this version |
| BREAKS | The tool worked before the change and fails after it | Fix the error shown |
| UNCLEAR, UNVERIFIABLE | It could not be judged here | Run a probe (below) |
| EXISTS | A static check passed (the input, flag or variable exists); the effect was not measured | Fine for inputs and flags; probe if the effect matters |

Never report a change as a fix unless it is EFFECTIVE or a probe shows the behaviour changed. After fixing a NO-OP, UNKNOWN-KEY, NOT-READ or PHANTOM-FLAG, run `check` again and show the new verdict.

## Probe anything else

Pick a command whose output should change because of your edit. The probe runs it once with the base version of the files and once with yours, then compares output and exit code:

```bash
python3 SKILL_DIR/scripts/poe.py probe --files vite.config.ts -- npx vite build --mode staging
python3 SKILL_DIR/scripts/poe.py probe --files config/app.yaml --shell 'python -c "import app; print(app.settings.timeout)"'
```

Identical output means NO-OP. Timings and timestamps are masked; add `--ignore REGEX` for other noise.

## Report it

Paste the Markdown table into the PR description or your final message.

## Safety

- `check` and `probe` briefly write the base version of one changed file at a time, run the tools, then restore the file byte for byte (mode and mtime too). Backups and a recovery manifest live in `.git/poe/`. If a run is killed, `python3 SKILL_DIR/scripts/poe.py restore` puts everything back, and the next run refuses to start until you do. Stop file watchers and dev servers first, because they may react to the swap. `--no-swap` runs only the static checks.
- It runs the project's own tools (tsc, eslint, jest, prettier, ruff, pytest, docker compose) and reads `--help` only from a fixed list of CLIs. JavaScript configs execute exactly as they do when you lint or test. It never runs package scripts.
- Network use is limited to fetching `action.yml` for GitHub Actions. `--offline` turns it off.

## Limits

- EFFECTIVE means the tool's resolved config changed. Explicitly setting an option to its default can still show as EFFECTIVE in tools that echo explicit values. Probe it if that matters.
- Git-ignored files such as `.env` are invisible to the diff. Pass `--env-file .env`.
- [references/checks.md](references/checks.md) lists what each checker runs and what it can miss.
