---
name: verify
description: Code verification pipeline - static analysis, type checking, linting, test execution, build check, smoke test
aliases: [check, validate, test, qa]
when_to_use: ".py .rs .js .ts .go .java .cpp .c .h"
argument_hint: "[files or directories to verify, or 'all' for entire project]"
user_invocable: true
hidden: true
context: inline
---

You are verifying code quality and correctness for: {{args}}

If no scope was provided above, assume all modified files in the current workspace.

Execute the following verification pipeline systematically. Do not skip steps.

## Step 1: Identify Verification Targets
First, determine what needs to be verified:
- If specific files or directories were mentioned, verify those
- If a scope like "all" or "project" is given, identify all modified files via git diff or recent file timestamps
- List the files you will verify before proceeding

## Step 2: Static Analysis

### 2.1 Syntax and Import Check
- Attempt to compile/parse each target file (e.g., `python -m py_compile file.py`)
- Verify all imports resolve correctly (no ImportError)
- Check for circular imports
- Verify Python version compatibility

### 2.2 Type Checking
- Run the project's type checker if configured (e.g., mypy, pyright, pytype)
- Focus on the modified files and their direct dependencies
- Report any type mismatches, missing type annotations, or unsafe operations

### 2.3 Lint Check
- Run the project's linter (e.g., ruff, flake8, pylint)
- Focus on errors and warnings (not style nits unless project convention requires them)
- Common issues to flag: unused imports, undefined variables, bare except, mutable defaults, dangerous default args

## Step 3: Unit and Integration Tests

### 3.1 Test Discovery
- Locate the test runner configuration (pytest, unittest, nose)
- Identify relevant test files matching the modified source files
- If no tests exist for a modified file, note it as a coverage gap

### 3.2 Test Execution
- Run all relevant tests using the project's test command
- If the full suite takes too long (>2 min), run only tests related to modified files
- Capture the full test output including failures, errors, and skipped tests

### 3.3 Test Analysis
- For each failure: identify the failing assertion, trace it to the source change, and explain the root cause
- Distinguish between pre-existing failures and newly introduced failures
- If a test needs to be updated (not a bug fix), explain why

## Step 4: Build Verification
- If the project has a build step, run it (e.g., `python setup.py build`, `cargo build`, `npm run build`)
- Verify the build completes with 0 errors
- Check for any warnings and categorize their severity

## Step 5: Runtime Smoke Test
- If applicable, run a quick smoke test of the application
- Verify the application starts without errors
- Test a minimal happy-path workflow

## Step 6: Report Generation
Produce a structured verification report:

```markdown
## Verification Report

### Scope
[List of files/directories verified]

### Static Analysis
- Syntax/Parsing: [PASS/FAIL] - N files checked, N errors
- Type Checking: [PASS/FAIL] - N errors, N warnings
- Linting: [PASS/FAIL] - N errors, N warnings

### Tests
- Test Runner: [pytest/unittest/other]
- Tests Run: N
- Passed: N
- Failed: N (N pre-existing, N new)
- Skipped: N
- Coverage: XX%

### Build
- Status: [PASS/FAIL]
- Warnings: N

### Smoke Test
- Status: [PASS/FAIL/SKIPPED]
- Details: ...

### Summary
[OVERALL: PASS / FAIL WITH ISSUES / BLOCKED]

### Action Items
- [ ] Critical: ...
- [ ] Warning: ...
- [ ] Suggestion: ...
```

If verification fails at any step, provide clear, actionable guidance on how to fix each issue before proceeding.

## Common Pitfalls
- **Skipping steps selectively** - "I'll just run the tests" and calling it verified, skipping static analysis / lint / build. A step skipped is a class of bug not checked. Run every step; mark SKIPPED with a reason rather than silently omitting.
- **Reporting PASS without evidence** - "tests pass" without the actual output. Attach the command output (or the relevant excerpt); a claim of pass is not verification.
- **Verifying the wrong scope** - "all modified files" but you only checked the one you edited, missing a file the change transitively affects. Use `git diff` to enumerate the real scope first.
- **Treating green tests as proof of correctness** - tests pass when they don't exercise the changed path or assertions are weak. Verify the changed lines are actually covered; pair with `gen-test` if coverage is thin.
- **Stopping at the first failure** - one failing test and you stop reporting. Run the full pipeline so the user sees the complete picture (which steps pass, which fail), not just the first hit.
- **Marking SKIPPED to avoid a red** - skipping build/lint because "it's probably fine" hides real issues. Only skip when the step genuinely doesn't apply (e.g. no tests for a docs-only change).

## Pairing with Other Tools
- `terminal` - run build / compile checks (`npm run build`, `python -m py_compile`)
- `test_run` - execute the test suite
- `lint_format` - lint and format checks
- `grep` - confirm no leftover debug code, TODOs, or hardcoded values
- `git` - `git diff` to scope verification to actual changes
- `code-review` - qualitative review complements the mechanical pipeline above
