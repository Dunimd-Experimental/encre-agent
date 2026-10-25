---
name: write-docs
description: Write technical documentation — API reference, README, ADR, changelog, tutorial, and runbook — from real source code, with verified examples and a consistent structure.
aliases:
  - document
  - docs
  - doc-writer
argument_hint: "[code, API, or project to document]"
user_invocable: true
hidden: true
context: inline
---

## Technical Documentation Writing

You are writing documentation for: **{{args}}**

If no target was provided above, assume the specified code. Read the actual source before writing — never document from assumptions or memory of an API.

## When to Use

- Write an API reference, README, ADR, changelog, tutorial, or runbook for code.
- Document an existing but undocumented module.
- Bring stale documentation back in sync with the code.

## When NOT to Use

- **Commenting inline code** — `file_edit` the comment where the code lives; docs are separate files.
- **Explaining a design decision in conversation** — just answer; `write-docs` is for durable artifacts.
- **Generating tests** — use the `gen-test` skill.
- **Refactoring first** — refactor, then document the stable result. Documenting churn is wasted work.
- **Reviewing prose style of an existing doc** — use the `writing-guidelines` skill.

## Tools You Use

| Task | Tool | Key params |
|------|------|------------|
| Read the source to document | `file_read` | `path`, `offset`, `limit` |
| Find every public entry point | `grep` | `pattern`, `path` |
| List the module surface | `glob` | `pattern` |
| Write the doc | `file_write` | `path`, `content` |
| Lint or format the markdown | `lint_format` | `file_path` or `file_paths` |
| Export a finished report | `document` | `action: create`, `file_path`, `content` |

## Documentation Types

| Type | When | Structure |
|------|------|-----------|
| API reference | Public functions, classes, endpoints | Signature, parameters, return, exceptions, examples |
| README | A project or package | What, why, quick start, configuration, contributing |
| ADR | A design decision | Context, decision, consequences, alternatives considered |
| Changelog | A release | Added, changed, deprecated, removed, fixed, security |
| Tutorial | Learning a task | Step by step with working examples and expected output |
| Runbook | Operating something | Symptoms, checks, actions, escalation, rollback |

## Quality Standards

- **Clear** — one sentence, one idea; avoid jargon unless it earns its place.
- **Complete** — cover every public interface, edge case, and error state.
- **Correct** — every code example must run; an example that does not compile is worse than none.
- **Structured** — headings, lists, and tables in the right roles.
- **Scannable** — the key information is findable at a glance.
- **Motivated** — explain why the design exists and when to use it, not only what it does.

## Workflow

### 1. Read the source

Read the actual files. Confirm signatures, defaults, parameter names, and error behavior against the code — never against memory.

### 2. Map the public surface

Use `grep` and `glob` to enumerate every public entry point so the docs cover the whole surface, not just what you remember.

### 3. Identify the audience

A new contributor, an integrating service owner, and an on-call engineer need different docs. State the audience at the top and write for it.

### 4. Pick the structure

Choose the type above and follow its structure. Do not invent a new layout mid-document.

### 5. Write with real examples

Lift examples from working code. Every snippet must be runnable, and every parameter name and default must match the source exactly.

### 6. Review for accuracy and completeness

Re-check every signature against the file, verify examples, and confirm no public interface is missing.

### 7. Lint if the project has rules

If the repo lints markdown (markdownlint, vale), run `lint_format` on the new files.

## Examples

### API reference entry

```
### `create_task(name, priority=0, timeout=30)`

Queue a unit of work and return its id.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | str | required | Human-readable task name |
| `priority` | int | `0` | Higher runs first |
| `timeout` | float | `30` | Seconds before the task is cancelled |

Raises `ValueError` if `name` is empty; `TimeoutError` on expiry.

```python
task = create_task("resize", priority=5)
```
```

### ADR skeleton

```
# ADR-014: Store session state as JSON files

## Context
Sessions must survive restarts without introducing a database dependency.

## Decision
Write each session to `data/sessions/<uuid>/meta.json`.

## Consequences
Simple and debuggable, but unbounded growth; no concurrent-write safety.

## Alternatives
SQLite (dependency and migration burden), Redis (external service).
```

### Changelog section

```
## Changed
- `create_task` now accepts `timeout` in seconds (was milliseconds).

## Fixed
- Retry no longer duplicates the task when the first attempt was queued but not started.

## Security
- Session paths are now sanitized before being written to disk.
```

## Common Pitfalls

- **Documenting from memory** — signatures drift, parameters get renamed, defaults change. Always `file_read` the current code.
- **Unverified examples** — an example that does not run misleads; run it or trace it mentally, and prefer snippets lifted from working code.
- **Over-documenting internals** — private functions couple the docs to the implementation and go stale fast. Document the public contract; note internals only where a hidden invariant matters.
- **Stale docs after a code change** — docs that contradict the code erode trust in all docs. Flag in the changelog whenever a documented surface changes.
- **Hand-typed signatures** — copying a signature into prose and getting a name or default wrong. Pull the real signature from the file.
- **Writing for yourself** — the author knows the code, the reader does not. Define terms on first use.
- **No why, only what** — a doc that only says what a function does is a comment. The valuable doc explains why, when to use it, and the tradeoffs.

## Best Practices

- One document per audience and purpose; do not merge a tutorial into an API reference.
- Put the quick start first — the reader who only skims should leave knowing how to use it.
- Prefer a code fence over prose for signatures, config, and CLI usage.
- Keep tables for enumerable facts (parameters, flags, status codes), not for narrative.
- Update the changelog in the same change that alters a documented surface.

## Anti-patterns

- A 400-line README that buries the quick start.
- Screenshots of terminals instead of copyable commands.
- "See the source code for details" in place of an actual description.
- Version-history dumps older than two releases in the README.

## Pairing with Other Tools

- `file_read` — read the real source, never document from memory.
- `grep` / `glob` — enumerate the full public surface.
- `code-review` — review undocumented code first; documenting buggy code bakes the bugs into the contract.
- `gen-test` — make doc examples double as runnable tests.
- `lint_format` — apply the project's markdown rules.
- `document` / `pdf` — produce a distributable copy of the finished doc.
