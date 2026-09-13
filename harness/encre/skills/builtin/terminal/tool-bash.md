---
name: tool-bash
description: Shell command execution skill. When to use bash (build/dev/install/custom scripts), terminal/timeout/cwd/background params, safety rules
hidden: true
context: inline
---

## When to Use
- Build/run a project: `npm run build`, `cargo build`, `python -m pytest`, `make`
- Start a dev server / long process: `npm run dev`
- Install dependencies: `bash npm install`, `bash pip install`
- Truly custom scripts with no dedicated tool

## When NOT to Use (a dedicated tool exists -> use it; bash is the last resort)
- Read a file -> `file_read` (no `cat`/`head`/`tail`)
- Edit a file -> `file_edit` (no `sed`/`echo >>`)
- Write/replace a file -> `file_write` (no `>`/`>>`/`tee`)
- Search contents -> `grep` (no `grep`/`rg` command)
- Find files -> `glob` (no `ls`/`find`/`dir`)
- Fetch a URL -> `web_fetch` (no `rest_client`/`wget`)
- Git ops -> `git` (no `git` command in bash)
- Run tests -> `test_run` (no `pytest`/`npm test`)
- Lint/format -> `lint_format`

## Key Parameters
- `command` (required): shell command. Windows runs `cmd /C`; mind cross-shell differences in scripts
- `terminal`: target a persistent terminal session to keep cwd/env across multi-step commands
- `timeout`: seconds; kills the process on expiry. Long tasks (dev server) set large or use `run_in_background`
- `cwd`: working directory, defaults to workspace root
- `run_in_background`: true to run in background; retrieve output via `bash_output`
- `dangerous`: confirms destructive commands (rm/drop etc.); use with care
- `max_output_chars`: output truncation threshold

## Best Practices
- When unsure if commands can parallelize, run sequentially; independent commands may be split across calls
- Probe a long output on a small range first, then go full once confirmed
- If dependency install fails, read the error then adjust; do not blindly retry

## Common Pitfalls / Anti-patterns
- **Using bash for what a dedicated tool does**: violates "tools first, bash last"; brittle and error-prone
- **Dev server in foreground blocking**: forgot `run_in_background: true`, hangs the turn
- **Assuming cross-platform commands**: Linux commands may not exist on Windows; prefer portable forms
- **No timeout on long commands**: a long command without timeout hangs forever

## Pairing with Other Tools
- `bash_output`/`bash_list`/`bash_kill`: manage background shells
- `test_run`/`lint_format`: verify rather than running bare commands