---
name: tool-test-run
description: "Run the project's tests with auto-detected pytest, vitest, jest or cargo and return structured per-test pass/fail/skip results."
hidden: true
context: inline
---

# tool-test_run

`test_run` executes the repository's test suite and parses the runner's output into structured results: per-test status, duration, file location and a truncated failure message. It auto-detects the framework from `pyproject.toml`, `package.json` or `Cargo.toml`, so one tool covers Python, JS/TS and Rust. Use `filter` to stay fast while iterating.

## When to Use

- Verify a code change did not break the suite.
- Reproduce a failure reported by the user.
- Check one area quickly with `filter` before running everything.
- Confirm a fix: run the specific failing test, then the full suite.
- Establish a baseline before refactoring.

## When NOT to Use

- Linting or formatting — use `lint_format`.
- Type-checking — not covered; use `terminal`.
- Manual exploratory checks — that is `notebook` or `terminal`.
- Benchmarks or load tests — different harness.
- Suites that need services, credentials or network access you lack; the report will show connection failures, not logic failures.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `workspace` | string | no (recommended) | Absolute path to the project root; must exist. Auto-detect needs it. |
| `framework` | string | no | `pytest`, `vitest`, `jest`, `cargo`; auto-detected when omitted. |
| `filter` | string | no | pytest `-k` expression, jest/vitest `-t` substring, or a cargo test name. |
| `max_duration` | number | no | Wall-clock cap in seconds, default 120; the subprocess is killed after it. |

### Parameter Notes

- `workspace` should be absolute and point at the package owning the test config, not necessarily the repo root.
- `filter` semantics differ per framework: pytest takes an expression, jest/vitest a substring, cargo a name prefix.
- `max_duration` kills the run mid-suite; results up to that point may still be reported.
- There is no coverage, parallelism or environment-variable parameter.

## Example

```json
{
  "workspace": "D:/encre/harness",
  "framework": "pytest",
  "filter": "loop_skills",
  "max_duration": 180
}
```

## Best Practices

- Iterate with a narrow `filter`, then run the full suite once before declaring done.
- Raise `max_duration` for cargo (first compile is slow) and for large pytest suites.
- Read the failure message and file location before re-running; blind retries waste time.
- Keep the workspace pointed at the sub-package that owns the tests.
- Treat skips as signal: a suite that skips everything is not a pass.

## Common Pitfalls

- Auto-detection picks the wrong framework in polyglot monorepos — set `framework` explicitly.
- Timeouts return partial output that can look like a clean run if you do not check the report.
- Network-marked tests fail offline; exclude them rather than chasing spurious errors.
- Environment differences (missing deps, wrong Python) surface as collection errors, not test failures.
- Failure messages are truncated; open the file for the full traceback.

## Anti-patterns

- Running the full suite on every small edit.
- Piping the runner through `terminal` and parsing text yourself.
- Ignoring failures that are "probably unrelated".
- Marking work complete without a passing run.

## Pairing with Other Tools

- `lint_format` — style and lint checks before or after tests.
- `terminal` — type-checkers and builds outside this tool.
- `apply_patch` / `file_edit` — fix what the report points at.
- `git` — confirm only intended files changed when a test breaks.
