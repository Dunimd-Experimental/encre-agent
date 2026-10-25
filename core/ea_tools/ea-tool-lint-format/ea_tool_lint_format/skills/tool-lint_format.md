---
name: tool-lint-format
description: "Run ruff, eslint+prettier or cargo fmt+clippy over a workspace with auto-detection, returning structured per-file/line/column diagnostics."
hidden: true
context: inline
---

# tool-lint-format

`lint_format` wraps the project's real toolchains — `ruff` for Python, `eslint` + `prettier` for JavaScript/TypeScript, `cargo fmt` + `cargo clippy` for Rust — and parses their output into structured diagnostics (file, line, column, code, message). It auto-detects the toolchain from `pyproject.toml`, `package.json` or `Cargo.toml`, so the same call works across the repo's polyglot packages.

## When to Use

- After editing Python/harness code: `mode="check"` on the touched paths.
- Before handing work back: `mode="format"` to normalize style.
- Applying safe linter fixes in bulk: `mode="fix"`.
- Rust verification: clippy + fmt in one call with `linter="cargo"`.
- Diagnosing a specific file without running the whole workspace: pass `paths`.

## When NOT to Use

- Type-checking with `mypy`/`tsc`/`cargo check` — not covered; use `terminal` for those.
- Running tests — use `test_run`.
- Formatter/linter for languages outside ruff/eslint/prettier/cargo (Go, Java, C++) — no support.
- Fixing a diagnostic you have not read; `fix` can rewrite semantics-adjacent code.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `workspace` | string | yes | Absolute path to the project root; must exist. |
| `linter` | string | no | `ruff`, `eslint`, `cargo`; auto-detected when omitted. |
| `mode` | string | no | `check` (default, no writes), `fix` (auto-fixers), `format` (formatter). |
| `paths` | array | no | Files/directories relative to `workspace`; whole workspace when omitted. |
| `max_duration` | number | no | Wall-clock cap in seconds, default 120; the subprocess is killed after it. |

### Parameter Notes

- `workspace` must be absolute — a relative path is rejected rather than resolved against the session cwd.
- `paths` are **relative to** `workspace`, not absolute.
- `mode` changes only what runs: `check` never writes, `fix` runs `ruff check --fix` / `eslint --fix` / `clippy --fix` / `cargo fmt`, `format` runs `ruff format` / `prettier --write` / `cargo fmt`.
- `linter` picks the family; `mode` picks the operation within it.

## Example

```json
{
  "workspace": "D:/encre/harness",
  "mode": "check",
  "paths": ["encre/loop", "encre/loop_skills.py"],
  "max_duration": 90
}
```

## Best Practices

- Always run `check` first, read the diagnostics, then decide between hand-fixes and `fix`.
- Scope with `paths` while iterating; a whole-workspace clippy run is slow.
- Raise `max_duration` for Rust workspaces before the first compile.
- Re-run `check` after `fix`/`format` to confirm the result is clean.
- Match `workspace` to the package that owns the config file, not the repository root.

## Common Pitfalls

- Auto-detection fails on monorepos with several configs at the root — set `linter` explicitly and point `workspace` at the package.
- `fix` and `format` write to disk; in a dirty worktree this mixes with user changes.
- Timeouts kill the subprocess and return partial output; check the report for a truncation note.
- Diagnostics reference generated or vendored files you may not want to touch.
- `cargo clippy --fix` can require a prior successful build and may fail in a clean checkout.

## Anti-patterns

- Running `mode="format"` across the entire workspace as a first step.
- Piping ruff/eslint/cargo through `terminal`.
- Ignoring `check` output and applying `fix`, then assuming the code is correct.
- Using this as a test runner.

## Pairing with Other Tools

- `apply_patch` / `file_edit` — address diagnostics by hand when `fix` is unsafe.
- `test_run` — verify behavior after fixes.
- `git` — review the reformat diff before committing.
- `terminal` — type-checkers and build commands outside this tool's scope.
