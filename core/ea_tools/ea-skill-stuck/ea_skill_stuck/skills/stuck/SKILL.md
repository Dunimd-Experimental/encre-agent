---
name: stuck
description: Self-diagnosis for stuck or looping agents - detect patterns, identify root cause, and apply recovery strategies
aliases: [unstuck, recover, diagnose-loop, self-fix]
when_to_use: ""
argument_hint: "[description of what the agent is stuck on]"
user_invocable: true
hidden: true
context: inline
---

You are analyzing a stuck or looping situation in: {{args}}

If no context was provided above, assume the current conversation.

The agent appears to be stuck in an unproductive cycle. Perform a thorough self-diagnosis and recommend concrete escape strategies.

## Step 1: Pattern Detection

Analyze the recent message history (last 10-20 turns) for these stuck patterns:

### A. Repetitive Action Loop
- Are the same tool calls being made repeatedly with the same or trivially different arguments?
- Is the agent reading the same files, searching the same patterns, running the same commands?
- Count: how many times has each unique action been performed?

### B. Fix-Revert Cycle
- Is the agent making a change, then reverting it, then remaking it?
- Are edits oscillating between two or more approaches?
- Check: do consecutive SearchReplace operations undo each other?

### C. Escalating Scope
- Is the task scope expanding with each iteration instead of converging?
- Is the agent discovering "one more thing to fix" indefinitely?
- Compare: initial task description vs. current work scope

### D. Error-Silence-Error
- Is the agent encountering an error, going silent, then hitting the same error?
- Are error messages being ignored or not properly analyzed?
- Check: are tool results with errors being read and acted upon?

### E. Analysis Paralysis
- Is the agent spending excessive turns thinking/planning without taking action?
- Is the agent asking for clarification repeatedly without making progress?
- Ratio: thinking/planning messages vs. action/completion messages

### F. Dependency Deadlock
- Are two or more tasks blocked waiting for each other?
- Is there a circular dependency preventing any task from completing?

## Step 2: Root Cause Diagnosis

For each detected pattern, identify the root cause:

1. **Missing Information**: The agent lacks critical context about the codebase, conventions, or requirements
2. **Incorrect Mental Model**: The agent has an incorrect understanding of how the system works
3. **Tool Limitations**: Available tools cannot express the needed operation
4. **Ambiguous Instructions**: The original task is underspecified or contradictory
5. **Environmental Issue**: The runtime environment has a misconfiguration (wrong Python version, missing dependency, permission issue)
6. **Algorithmic Stuck**: The agent is using an approach that cannot converge on this problem type
7. **Attention Drift**: The agent has lost track of the original goal and is pursuing tangents

## Step 3: Recovery Strategy

Based on the diagnosis, recommend one of these recovery strategies:

### Strategy A: Pivot Approach
- Abandon the current approach entirely
- Propose 2-3 fundamentally different solution strategies
- Explain why the current approach cannot succeed
- Select and commit to the most promising alternative

### Strategy B: Narrow Scope
- Reduce the task to the smallest possible unit of progress
- Explicitly list what is OUT of scope for now
- Complete one micro-task successfully before expanding
- Set a hard limit of 3 turns for the first micro-task

### Strategy C: Seek Clarification
- Identify the exact piece of missing information
- Formulate a specific, answerable question for the user
- Pause all autonomous work until the answer is received
- Do NOT proceed with assumptions

### Strategy D: Brute Force Simplification
- Revert all changes back to a known-good state
- Make the absolute minimum change to advance the goal
- Verify that single change works before making any further changes
- Commit to incrementalism: one change, verify, repeat

### Strategy E: External Reference
- Search for documentation, examples, or reference implementations
- Look at how similar problems are solved in the same codebase
- Check if there's a library or built-in function that already does what's needed
- Prefer existing patterns over novel solutions

## Step 4: Self-Correction Commitment
After applying the recovery strategy:
- Set explicit success criteria: "We are unstuck when X happens"
- Track iterations since recovery was applied
- If still stuck after 5 more iterations, escalate to a different recovery strategy
- NEVER silently repeat a failed action - if it failed once, it will fail again without a change

## Output Format
```markdown
## Stuck Analysis Report

### Detected Patterns
- [Pattern A/B/C/D/E] detected: [evidence]

### Root Cause
- Primary: [cause]
- Contributing: [cause(s)]

### Recommended Strategy
- Strategy: [A/B/C/D/E]
- Rationale: [why this strategy is appropriate]
- Action Plan: [concrete next steps]

### Success Criteria
- [Measurable condition that indicates progress]
```

## Common Pitfalls
- **Diagnosing without reading recent history** - the stuck pattern is in the last 10-20 turns; guessing the pattern from memory misses it. Actually scan the recent tool calls and their arguments.
- **Recommending "try harder"** - "retry with more detail" is not a recovery strategy. If an action failed, identify *why* it failed; the fix is a different action, not a louder version of the same one.
- **Picking a strategy that doesn't fit the pattern** - e.g. recommending decomposition (B) for a tool-protocol error (needs A: context reduction). Match the strategy to the *detected* pattern, not a default.
- **No success criteria** - "try again and see" has no stopping condition. State a measurable condition (specific output, specific tool succeeding) that proves the recovery worked.
- **Silently re-applying a failed action** - the single biggest stuck amplifier. If it failed once, it fails again unchanged. Always change something (query, approach, scope) before retrying.

## Pairing with Other Tools
- `memory_search` - recall whether this stuck pattern happened before and what resolved it
- `grep` / `file_read` - if stuck on a code question, re-read the actual source rather than re-guessing
- `terminal` - reproduce the failing step in isolation to isolate the cause
- `agent` - delegate a fresh investigation if the main context is too polluted to see the pattern
