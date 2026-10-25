---
name: debug
description: Systematic debugging workflow - gather logs, analyze root cause, isolate, fix, and verify errors
aliases: [dbg, diag, troubleshoot]
when_to_use: ".log .txt .err .out .traceback"
argument_hint: "[target: file, module, or component to debug]"
user_invocable: true
hidden: true
context: inline
---

You are debugging: {{args}}

If no specific target was provided above, assume the current project.

Follow this systematic debugging workflow:

## Phase 1: Information Gathering
1. Read and examine any log files present in the workspace (check for files like *.log, output.log, error.log, stderr, stdout captures, build logs, test logs, crash logs, dumps)
2. Collect error messages from the most recent run or build
3. Identify the exact error messages, stack traces, or failure points
4. Note the exact line numbers, file paths, and function names mentioned in errors

## Phase 2: Root Cause Analysis
1. Read the relevant source files at the exact line numbers indicated in the error
2. Trace the execution flow backwards from the failure point
3. Check variable states, input validation, and boundary conditions at the failure point
4. Look for common patterns: null/None dereference, index out of bounds, type mismatches, race conditions, resource exhaustion, import errors, configuration issues
5. If the error is in a dependency or library, check the library version and compatibility

## Phase 3: Reproduction and Isolation
1. Identify the minimal reproduction case
2. Determine if the issue is deterministic or intermittent
3. Check if the issue depends on specific: input data, environment variables, OS, Python version, library versions, concurrency, timing
4. Isolate the failing component from the rest of the system if possible

## Phase 4: Fix and Verify
1. Apply the minimal fix that addresses the root cause (not just the symptom)
2. Verify the fix does not introduce new issues
3. If tests exist, ensure they pass after the fix
4. Document the root cause and fix for future reference

## Output Format
Present your findings clearly:
- **Error Summary**: What failed, where, and with what message
- **Root Cause**: The underlying problem identified
- **Fix Applied**: What change was made and why
- **Verification**: Evidence the fix works

If you cannot determine the root cause, explain what additional information you need and what you have ruled out so far.

## Common Pitfalls
- **Fixing the symptom, not the cause** - silencing an error (try/except pass, commenting out the failing line) makes it disappear but the bug remains and resurfaces elsewhere. Always trace to the root cause before editing.
- **Debugging from memory of the code** - the file changed since you last read it. `file_read` the actual current source at the error line before theorizing.
- **Changing multiple things at once** - editing three suspected spots, re-running, seeing green - you don't know which fix worked (or if it's even fixed). Change one thing, verify, repeat.
- **Trusting the first hypothesis** - the obvious cause is often wrong. After forming a hypothesis, try to *disprove* it with a targeted check before committing the fix.
- **Skipping verification** - "I think that fixes it" without re-running. The error log is the proof; re-run and confirm the error is gone.
- **Looping on the same failed fix** - retrying the same edit that didn't work. After 2-3 identical failures, stop and reconsider the root cause; a different approach is needed.

## Pairing with Other Tools
- `grep` / `lsp` - find the error site and all related call sites
- `file_read` - read the actual source at the failure line
- `terminal` - reproduce the error in isolation (minimal repro is the fastest path to cause)
- `test_run` - confirm the fix doesn't regress and (ideally) add a regression test
- `code-review` - review the fix before declaring done
- `verify` - run the full verification pipeline after the fix
