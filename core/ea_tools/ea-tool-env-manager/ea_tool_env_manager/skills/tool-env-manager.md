---
name: tool-env-manager
description: "Read/write env vars in the process or a .env file, and load/save .env, JSON, or YAML config without hand-editing."
hidden: true
context: inline
---

# tool-env-manager

Manages configuration in two scopes. `process` reads and mutates the running process's environment; `file` edits a `.env` file in place, preserving unrelated lines and comments. It can also load a config file (`.env`, JSON, YAML, TOML) into the process environment and export the current environment back out.

## When to Use

- Set a variable for subsequent tool calls in this session (`scope: "process"`).
- Change one key in a `.env` without rewriting or reordering the rest of it (`scope: "file"`).
- Load a config file into the environment before running a command that expects env vars.
- Snapshot the current environment to a file for debugging a configuration problem.
- Inspect whether a variable is set (and how) before guessing why a tool behaves oddly.

## When NOT to Use

- Storing production secrets — prefer a secrets manager or OS keychain; `.env` files leak into git.
- Editing `pyproject.toml`, `package.json`, or app config that is not env-shaped.
- Persisting values across restarts of a different process; `process` scope dies with this session.
- Changing the environment of an already-running background service.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | `get`, `set`, `delete`, `list`, `load`, `save`. |
| `key` | string | for `set`/`delete` | Variable name. Optional for `get`, which returns everything when omitted. |
| `value` | string | for `set` | Value to assign. |
| `file_path` | string | for `load`/`save`; for `set`/`delete` with `scope: "file"` | Config file path. |
| `data` | object | no | Key/value map for bulk writes. |
| `format` | string enum | no | `env` (default), `json`, `yaml` — used by `save`; `load` infers from the extension. |
| `scope` | string enum | no | `process` (default) or `file` — where `set`/`delete` apply. |

### Parameter Notes

- `scope: "file"` requires `file_path`; the edit is targeted, so comments and ordering elsewhere in the file survive.
- `load` infers the parser from the extension (`.env`, `.json`, `.yaml`/`.yml`, `.toml`); a mismatched extension is parsed with the wrong parser or rejected.
- `save` writes the whole process environment — it is a dump, not a curated config file. Expect noise and secrets.
- `get` with no `key` returns all variables, which can be very large; use a key unless you are debugging broadly.
- `delete` on a missing key is typically a no-op rather than an error.

## Example

```json
{ "action": "set", "key": "ENCRE_LOG_LEVEL", "value": "debug", "scope": "process" }
```

```json
{
  "action": "set",
  "key": "OPENAI_API_KEY",
  "value": "<key>",
  "scope": "file",
  "file_path": "D:\\encre\\.env"
}
```

```json
{ "action": "load", "file_path": "D:\\encre\\config\\dev.yaml" }
```

```json
{ "action": "get", "key": "ENCRE_LOG_LEVEL" }
```

## Best Practices

- Prefer `process` scope for transient, session-only values; use `file` only when the value must survive.
- Check `.gitignore` before writing a `.env` with credentials.
- After `load`, `get` one key to confirm the parser picked up the format you expected.
- Use `data` for a batch of related keys instead of N sequential `set` calls.
- When exporting for diagnostics, filter out anything with `KEY`, `TOKEN`, `SECRET`, or `PASSWORD` in the name before sharing.

## Common Pitfalls

- Assuming `set` with `scope: "process"` affects already-spawned subprocesses.
- Writing secrets to a `.env` that is tracked by git.
- Loading a `.toml` file renamed to `.yaml`; inference fails or yields nonsense.
- Using `save` as a backup of "the config" when it dumps the entire environment.
- Expecting `delete` to remove a key from a file when scope defaulted to `process`.
- Case sensitivity: Windows env vars are case-insensitive, POSIX ones are not.

## Anti-patterns

- Hand-editing `.env` with `file_write` and clobbering comments the user wrote.
- Setting a variable then immediately reading it back in a different process.
- Dumping the full environment into a chat message.
- Storing credentials in `process` scope and assuming they persist to the next session.

## Pairing with Other Tools

- `terminal` — run commands that consume the variables you just set.
- `file_read` / `file_write` — inspect or create config files that are not env-shaped.
- `docker` — pass env vars into containers (`-e`) after resolving them here.
- `ssh` — remote environment inspection.
- `lint_format` — validate config syntax when a load fails.
