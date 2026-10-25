---
name: tool-todo
description: "Maintain the session's structured task list: submit the full list each time, with one item in progress, and get stable IDs back."
hidden: true
context: inline
---

# tool-todo

`todo` owns the user-visible plan card. Every call **replaces** the whole list with what you submit, assigns stable IDs to items, and returns the list in full so you never have to remember it. Status changes are the point: mark an item `in_progress` when you start it and `completed` the moment it is done.

## When to Use

- Complex work with three or more distinct steps.
- The user asks you to track tasks or show a plan.
- Several sub-goals where progress should be visible while you work.
- Work spanning multiple tool calls where forgetting a step is likely.
- Handing off a checklist the user can follow.

## When NOT to Use

- A single trivial action or one-shot tool call.
- Pure question-and-answer turns.
- Background execution tracking — that is `task_create` / `task_list`.
- Recurring schedules — `cron_create`.
- A place to store results or notes.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `todos` | array | yes | The complete list; replacement semantics — items you omit are deleted. |
| `summary` | string | no | One-line description of what changed and why, shown in the card header. |
| `reset` | boolean | no | Clear the previous list first (default false); with `todos` it still replaces everything. |

### Parameter Notes

- Replacement semantics: always resend unchanged items verbatim, or they disappear.
- Each item carries a status (`pending`, `in_progress`, `completed`); keep exactly one `in_progress` — a few if truly parallel.
- Duplicate content collapses to a single ID.
- The list is capped at 50 items.

## Example

```json
{
  "summary": "Rewriting tool skill docs; 25 of 65 done",
  "todos": [
    { "content": "Rewrite docs for tools 1-25", "status": "completed" },
    { "content": "Rewrite docs for tools 26-50", "status": "in_progress" },
    { "content": "Rewrite docs for tools 51-65", "status": "pending" },
    { "content": "Validate every doc against its input_schema", "status": "pending" }
  ]
}
```

## Best Practices

- Update immediately when a step starts or finishes — never batch completions at the end.
- Keep items outcome-shaped ("validate docs against schema") rather than activity-shaped ("work on docs").
- Resend the entire list every time; do not send deltas.
- Use `summary` to explain the current focus in one line.
- Start with `reset=true` for a genuinely new objective.

## Common Pitfalls

- Omitting items deletes them — the most common way to lose a plan.
- Two or more `in_progress` items make the card misleading.
- Marking items complete before the work is verified.
- Storing details inside item text instead of keeping items short.
- Using `todo` as a log of finished steps forever, growing past the 50 cap.

## Anti-patterns

- Creating a todo list for a two-step task.
- Marking everything completed in one final call.
- Using it to track background tasks (use `task_list`).
- Updating the list without changing any status.

## Pairing with Other Tools

- `task_create` / `task_list` — actual background execution behind the plan.
- `question` — when a plan step needs a user decision.
- `notify` — announce completion out of band.
- `test_run` / `lint_format` — verification steps that legitimately complete items.
