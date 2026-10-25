---
name: tool-agent
description: "Spawn parallel sub-agents for independent sub-tasks; each runs as a full autonomous session and returns one aggregated result."
hidden: true
context: inline
---

# tool-agent

Delegates work to one or more sub-agents. A sub-agent is not a tool call with extra steps: it is a complete agent session with its own context window, its own tool budget, and its own reasoning loop. The parent process receives only the sub-agent's final report, so everything the sub-agent needs must be inside the prompt you hand it.

## When to Use

- A goal splits into genuinely independent workstreams ("audit the auth module" while "draft the migration script") and doing them serially would waste wall-clock time.
- You want breadth: fan out the same question across several angles, files, or candidate approaches and compare the answers.
- A sub-task would flood your own context (reading 40 files, scanning a large log) and you only need the conclusion.
- You need an isolated experiment that must not touch the parent working tree — use `mode: "isolated"` or `isolated: true`.

## When NOT to Use

- A single linear task with no parallelism: just do it inline, delegation only adds latency and loses detail.
- A sequenced pipeline whose stages depend on each other's output: use the `workflow` tool, which models stages and dependencies explicitly.
- Role-specialised multi-agent review (author / reviewer / judge): use `workflow` in team mode.
- When you already know the exact one-line answer — spawning an agent to run one command is waste.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | yes (single mode) | Complete instruction for one sub-agent. Must be written in English. Include all context, inputs, file paths, and the exact expected output format. Ignored when `tasks` is given. |
| `agent_name` | string | no | Built-in sub-agent profile that scopes capabilities: `Explore` (read-only), `Plan` (no writes), `general-purpose` (full access), `coder`, `researcher`, `critic`, `architect`, `planner`. Omit for an unrestricted agent. |
| `mode` | string enum | no | Lifecycle: `sync` (default, wait inline), `async` (fire-and-forget, returns a session id), `background` (auto-switch to async for long tasks), `isolated` (temp git worktree). |
| `isolated` | boolean | no | Run in an isolated temporary git worktree; changed files are synced back on completion. Default `false`. |
| `timeout_seconds` | number | no | Wall-clock budget. Decide it from task complexity: 60–300 for lookups, 600–1800 for coding or research. `0`/omitted = no limit. On timeout the agent stops and partial output is returned. |
| `tasks` | array | no | Concurrent batch (max 4 in flight, extras queue). Each entry has `prompt` (required, English) plus optional `agent_name`. Results come back in submission order. |

### Parameter Notes

- `prompt` and `tasks` are alternatives. Supplying `tasks` makes the top-level `prompt` irrelevant.
- `mode: "async"` returns immediately with a session id; you must collect the result later with `task_get` / `task_output`, not by re-calling `agent`.
- `isolated: true` is redundant with `mode: "isolated"` — use one or the other, not both.
- Sub-agents cannot spawn further sub-agents. Delegation is exactly one level deep.
- The 4-in-flight cap is per call, not global; the queue drains as slots free up.

## Example

```json
{
  "tasks": [
    {
      "agent_name": "Explore",
      "prompt": "Find every call site of `render_skill_catalogue` in D:\\encre\\harness. Return a markdown table with file path, line number, and the one-line purpose of each call. Do not modify files.",
      "timeout_seconds": 300
    },
    {
      "agent_name": "coder",
      "prompt": "In D:\\encre, add a `--check` flag to scripts/validate_docs.py that exits 1 when any doc is missing a `## Key Parameters` section. Match the existing argparse style in that file. Return the diff and the command you ran to verify it.",
      "timeout_seconds": 900
    }
  ]
}
```

Single-agent form:

```json
{
  "agent_name": "Plan",
  "prompt": "Produce a step-by-step implementation plan (no code) for adding retry semantics to the plugin loader. Read harness/encre/plugins/registry.py first. Output: numbered steps, each with the file to touch and the risk involved.",
  "timeout_seconds": 600
}
```

## Best Practices

- Write prompts as if the reader has amnesia: absolute paths, the exact command to run, the output format, and what "done" means.
- Always state the output shape (table, JSON keys, diff only, max length). Aggregated results are otherwise inconsistent across sub-agents.
- Give each sub-agent a narrow, non-overlapping scope. Two agents editing the same file in parallel produce conflicting reports and wasted work.
- Set `timeout_seconds` explicitly. An unbounded agent that goes down a rabbit hole blocks the whole turn.
- Choose the least powerful `agent_name` that can do the job (`Explore`/`Plan` for research) so a read task cannot accidentally write.
- Use `isolated` for speculative refactors or anything touching the parent tree you are not sure you want.

## Common Pitfalls

- Vague prompts ("look into the bug") return vague summaries that you then have to redo yourself.
- Passing conversation history by reference ("as discussed above"). Sub-agents see only the prompt string — there is no shared context.
- Forgetting that all prompts must be English; mixed-language prompts degrade the sub-agent's tool-call parsing.
- Treating `mode: "async"` as if it returns the answer. It returns a handle; the result must be fetched.
- Expecting nested delegation ("the sub-agent will spawn helpers") — it cannot.
- Assuming parallel edits merge. They do not; the isolated worktree syncs back, last writer wins on conflicts.

## Anti-patterns

- Spawning one agent per file in a 40-file directory instead of giving one agent a batch instruction.
- Delegating a task you can finish with one `grep` — the overhead dwarfs the work.
- Using `agent` as a retry mechanism for a failing tool; diagnose the failure instead.
- Fanning out with overlapping scopes and then asking a third agent to "reconcile" the outputs.

## Pairing with Other Tools

- `workflow` — when stages are dependent or you need sequential/team structure rather than pure fan-out.
- `task_get`, `task_output`, `task_list` — to collect results from `mode: "async"` / `background` runs.
- `question` — to confirm scope with the user before spending several sub-agent budgets.
- `todo` — to track the aggregated follow-ups a multi-agent run usually produces.
