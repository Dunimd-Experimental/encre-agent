---
name: tool-workflow
description: "Orchestrate a multi-step goal as a DAG of sub-agents with dependency-aware parallelism, retries, and optional role-specialised team mode."
hidden: true
context: inline
---

# tool-workflow

`workflow` decomposes one goal into a dependency graph of sub-tasks and executes it with sub-agents: `light` mode runs each node as a plain capable sub-agent with upstream outputs injected; `team` mode assigns roles (architect, coder, reviewer, tester, debugger), gates coder output through a reviewer, shares a blackboard, and runs a consensus step when several agents produce results. The goal must be written in English.

## When to Use

- Build a whole feature or app where design, implementation and verification are separate steps.
- Research a question from several angles with synthesis at the end.
- Refactor a codebase across files with review and testing phases.
- Any goal whose sub-tasks have real dependencies and benefit from parallelism.
- Work needing an explicit review gate between implementation and acceptance (`team`).

## When NOT to Use

- A single focused task — use `agent`.
- Independent parallel tasks with no dependencies or review gate — use `agent` with a `tasks` array.
- Two-step work you can do yourself.
- Inside a sub-agent: workflows cannot be spawned from within one (single delegation level).
- Trivial or purely conversational requests.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `goal` | string | yes | Complete goal **in English**; state end result, scope and acceptance criteria. |
| `mode` | string | no | `light` (default, plain pipeline) or `team` (roles, reviewer gate, consensus). |
| `timeout_seconds` | number | no | Overall wall-clock budget; defaults 1800 (`light`) / 3600 (`team`). |

### Parameter Notes

- The goal **must** be English: planning, state matching and output parsing all run in English.
- `timeout_seconds` is for the whole run, not per node; on expiry partial per-node results are returned.
- `team` is materially heavier — reviewers and consensus add rounds.
- There is no per-node prompt control: decomposition is the planner's job.

## Example

```json
{
  "goal": "Add a CSV export endpoint to the reporting service: implement the handler, cover it with tests, and verify the suite passes offline. Acceptance: new tests pass and existing tests remain green.",
  "mode": "team",
  "timeout_seconds": 2700
}
```

## Best Practices

- Write acceptance criteria into the goal; vague goals decompose into vague tasks.
- Size `timeout_seconds` to the goal: 300–900 for small pipelines, 1800–3600 for multi-step builds.
- Choose `team` only when specialisation and cross-checking genuinely matter.
- Include constraints (offline, no network, files to touch) so every node inherits them.
- Review the returned per-node results rather than only the final summary.

## Common Pitfalls

- Non-English goals degrade planning quality and output parsing.
- Too-short timeouts truncate the run mid-DAG and return partial results.
- Sub-agents do not share the parent conversation — anything they need must be in the goal or the node context.
- `team` mode on a two-step goal burns time for no quality gain.
- Nested workflows are not supported; a node cannot start another workflow.

## Anti-patterns

- Using a workflow for a single file edit.
- Delegating independent, dependency-free tasks instead of using `agent` with `tasks`.
- Writing a goal that describes steps instead of the desired end state.
- Retrying a timed-out workflow at the same budget.

## Pairing with Other Tools

- `agent` — single focused delegation or a flat parallel batch.
- `todo` — user-visible progress while the workflow runs.
- `test_run` / `lint_format` — verification the workflow's nodes may invoke.
- `task_create` — run the workflow in the background and poll it.
