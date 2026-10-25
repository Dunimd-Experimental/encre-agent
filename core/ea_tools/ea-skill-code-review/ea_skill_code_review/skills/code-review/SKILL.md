---
name: code-review
description: Expert code audit - correctness, security, performance, maintainability, and codebase fit analysis
aliases: [review, audit, inspect]
when_to_use: ".py .rs .js .ts .go .java .cpp .c .h"
argument_hint: "[files, modules, or pull request to review]"
user_invocable: true
hidden: true
context: inline
---

## Code Review Mode - Expert Auditor

You are performing a thorough code review on: **{{args}}**

If no target was provided above, assume the specified code. Read the code before reviewing - never review code you haven't read. Run `git diff` (or `git diff --staged`) first if reviewing changes.

### When to Use
- Review a pull request, branch, or specific files before merge
- Audit a module for correctness, security, or performance issues
- Second-pass review after implementing a feature

### When NOT to Use
- **Fix a bug you already found** -> just `file_edit` the fix (review is for *finding*, not fixing)
- **Verify tests pass / lint clean** -> `test_run` / `lint_format` (review is qualitative)
- **Generate tests for the code** -> `gen-test`
- **Refactor without changing behavior** -> `refactor`

### Review Dimensions

**1. Correctness & Logic**
- Does the code do what it intends? Check for off-by-one, race conditions, null safety
- Are there any obvious logic errors or edge cases not handled?

**2. Security**
- Check for: injection vulnerabilities (SQL, XSS, command), insecure deserialization, path traversal, hardcoded secrets, unsafe cryptographic practices
- Validate input sanitization and output encoding

**3. Performance**
- Identify: N+1 queries, unnecessary allocations, blocking calls in async context, large object allocations in hot paths
- Check algorithmic complexity - is there a better data structure?

**4. Maintainability**
- Is the code readable? Are names clear? Is there unnecessary complexity?
- Check error handling - are exceptions specific? Are resources cleaned up?
- Are there appropriate tests? Do they test the right things?

**5. Codebase Fit**
- Does the code follow existing project conventions and patterns?
- Does it fit the architecture or introduce unnecessary coupling?

### Review Output Format
- **Critical** issues (must fix): Impact + Location + Suggested Fix
- **Warning** issues (should fix): Impact + Location + Suggested Fix
- **Nitpick** (nice to have): Suggestion only
- **Positive** feedback: What was done well

### Common Pitfalls
- **Reviewing only the diff, missing the surrounding context** - a change looks fine in isolation but breaks a caller or an invariant. Read the function and its callers, not just the changed lines.
- **Trusting "tests pass" as proof of correctness** - tests pass when assertions are weak or the path isn't actually exercised. Check that assertions verify the behavior, not just that no exception was thrown.
- **Flagging style as critical** - naming/formatting is a nitpick, not a blocker. Reserve Critical for correctness/security/data-loss risks.
- **Missing concurrency issues** - async without await, shared mutable state, non-atomic check-then-act. These hide in code that "works locally".
- **Ignoring error paths** - the happy path is reviewed; the `except`/`catch`/fallback branches are not. Bugs live in the error paths.
- **Not running the existing tests yourself** - a review claim "this breaks X" is stronger if verified: run `test_run` on the affected suite.
- **Reviewing code you haven't read this session** - relying on memory of the file leads to reviewing a stale mental model. Re-read if unsure.

### Pairing with Other Tools
- `git` - `git diff` / `git diff --staged` to scope the review to actual changes
- `grep` - find all callers of a changed function before declaring it safe
- `test_run` - verify a suspected bug actually reproduces, or that a fix doesn't regress
- `lint_format` - catch mechanical issues so the review focuses on logic
- `gen-test` - after review, generate tests for the edge cases the review surfaced
- `refactor` - if the review found structural problems, hand off to refactor (separate from fixing bugs)

## Severity Grading (merged from deep-code-reviewer)

Grade every finding P0–P4; the grade decides merge behavior:

- **P0** — Crash, data corruption, or security vulnerability. **Blocks merge.** Must be fixed before the change lands.
- **P1** — Functional error (wrong behavior, broken edge case, regression). Needs a fix; fix in this change or an immediately following one.
- **P2** — Design or performance concern (poor complexity, risky coupling, hot-path IO). Fix recommended, not blocking.
- **P3** — Style / readability (naming, formatting, comments). Optional; nitpick only.
- **P4** — Discussion items / open questions (architecture alternatives, future work). No action required now.

Use grades consistently: a P0 must never be softened to P2 for convenience, and style issues must never be escalated to P0.

## Multi-Language Quality Checklist (merged from multi-lang-code-quality-guard)

Run the same four dimensions for **every** language, then apply the language-specific checkpoints:

- **Correctness**: boundary conditions, null/None/nil handling, concurrency and race conditions.
- **Security**: injection (SQL/command/XSS), privilege/authorization checks, secret leakage (hardcoded keys, tokens in logs).
- **Performance**: algorithmic complexity, IO patterns (N+1 queries, blocking calls), memory (leaks, large allocations).
- **Maintainability**: naming clarity, code duplication, coupling and cohesion.

**Python**: check mutable default arguments and late-binding closures; ensure exceptions are specific (`except ValueError`, not bare `except:`); watch for GIL-bound assumptions and unsafe shared state in async/threaded code.

**TypeScript**: enable strict-mode thinking — verify no non-null assertions (`!`) hiding real nulls; check `any` leaks and unvalidated external input at API boundaries; prefer `Promise.all` error handling review (unhandled rejections) over fire-and-forget calls.

**Rust**: check `unwrap()`/`expect()` in non-test paths (propagate with `?`/`anyhow` instead); verify `unsafe` blocks are justified and minimal; watch for unnecessary `.clone()` in hot paths and `Arc<Mutex<>>` where lock granularity hurts concurrency.

