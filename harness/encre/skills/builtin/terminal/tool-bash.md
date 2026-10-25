---
name: tool-bash
description: Shell command execution skill. When to use bash (build/dev/install/custom scripts), terminal/timeout/cwd/background params, safety rules
hidden: true
context: inline
---

## When to Use
- Build/run a project: `npm run build`, `cargo build`, `python -m pytest`, `make`
- Start a dev server / long process: `npm run dev`
- Install dependencies: `npm install`, `pip install`, `cargo add`
- Truly custom scripts with no dedicated tool

## When NOT to Use (a dedicated tool exists -> use it; bash is the last resort)
- Read a file -> `file_read` (no `cat`/`head`/`tail`)
- Edit a file -> `file_edit` / `apply_patch` (no `sed`/`echo >>`)
- Write/replace a file -> `file_write` (no `>`/`>>`/`tee`)
- Search contents -> `grep` tool (no shell `grep`/`rg`)
- Find files -> `glob` tool (no `ls`/`find`/`dir`)
- Fetch a URL -> `web_fetch` (no `curl`/`wget`)
- Git ops -> `git` tool (no bare `git` command in bash)

## Key Parameters
- `command` (required): the shell command to run.
- `terminal` (optional, default `"auto"`): the platform's recommended shell is
  picked automatically -- PowerShell on Windows, zsh on macOS, bash on Linux --
  with automatic fallback if that binary is missing.  Pass an explicit value
  (`powershell`, `pwsh`, `cmd`, `bash`, `zsh`, `sh`, `python`, `node`) only when
  the task needs a specific shell.  Each explicit (terminal, cwd) pair gets a
  persistent session: cwd and environment survive across calls with the same
  pair; different pairs run as independent sessions and never inherit each
  other's state.
- `timeout` (optional, default 120, max 600): seconds.  On expiry the shell is
  terminated and the result is flagged `timed_out: true` -- size it to the
  task (installs need more; probes need less).  For dev servers use
  `run_in_background` instead of a huge timeout.
- `cwd` (optional): working directory for this call, defaults to the workspace.
- `run_in_background` (optional, default false): start the command in the
  background and return a shell id; poll with `bash_output`, stop with
  `bash_kill`.
- `max_output_chars` (optional, default 30000): truncation threshold for
  stdout/stderr.

## Result Fields You Must Check
- `exit_code`: the real exit status of the command.  Decide success from this,
  never assume 0.
- `interrupted: true`: the user stopped the command; `stdout` holds whatever
  streamed before the stop.  Do not retry immediately -- ask or adapt.
- `timed_out: true`: the command outlived `timeout` and its shell was killed.
  Raise `timeout`, split the work, or switch to `run_in_background`.
- `stdout_truncated` / `stderr_truncated`: output was clipped; narrow the
  command (filters, head/tail) instead of raising `max_output_chars` blindly.

## Permission Model
Every bash command passes the permission gate BEFORE execution:
- Dangerous commands (file deletion, disk ops, network shutdown patterns,
  package uninstalls...) trigger an ask: the frontend shows a confirmation
  popup and the user decides allow / deny / ask.
- A deny blocks the command entirely -- do not rephrase the same command to
  sneak it through; adjust the approach or explain to the user.
- Keep destructive commands explicit and narrow (quote paths, avoid wildcards
  on `rm`) so the user can judge them quickly.

## Best Practices
- Match the shell dialect to the platform prompt you were given (PowerShell on
  Windows, zsh/bash on Unix); write portable forms when unsure.
- Probe first: run the cheap, narrowed version of a command before the full
  one (e.g. `git status` before a scripted `git` chain).
- Independent commands may go in separate calls (parallel sessions exist per
  (terminal, cwd) pair); dependent commands must stay sequential.
- Prefer `run_in_background` for anything that keeps running (servers,
  watchers) and manage it via `bash_output` / `bash_kill`.

## Common Pitfalls / Anti-patterns
- **Using bash for what a dedicated tool does**: violates "tools first, bash
  last"; brittle and error-prone.
- **Dev server in foreground blocking the turn**: forgot
  `run_in_background: true`.
- **Assuming one shell everywhere**: `&&`, `$?`, `export` semantics differ
  between cmd / PowerShell 5.1 / PowerShell 7 / POSIX shells.
- **Retrying after deny or repeated timeout**: read the error, change the
  approach.
- **Trusting `success` without reading `exit_code`**: non-zero exits are real
  failures even when some output streamed.

## Pairing with Other Tools
- `bash_output` / `bash_list` / `bash_kill`: manage background shells.
- `test_run` / `lint_format`: verify rather than running bare commands.
