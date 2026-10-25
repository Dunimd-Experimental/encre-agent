---
name: tool-question
description: "Ask the user for clarification, a decision or missing input; the conversation pauses until every question is answered."
hidden: true
context: inline
---

# tool-question

`question` is the only correct way to block on the user. It surfaces one or several questions in the interface — with optional clickable options and an explanation of why you are asking — and the turn does not continue until the answers arrive. Use it when proceeding would mean guessing at something that materially changes the outcome.

## When to Use

- The request has two or more plausible interpretations with different results.
- A destructive or irreversible action needs explicit confirmation.
- Required input is missing: a path that does not exist, a credential, a target environment.
- A choice set is small and known — present it as `options` so the user clicks instead of typing.
- Several independent clarifications are needed at once — send them in `questions`.

## When NOT to Use

- You can infer the answer from context or sensible defaults.
- Open-ended exploration where trying something and iterating is cheaper than a round-trip.
- The user is mid-flow and a question would interrupt without adding value.
- You need to notify rather than ask — use `notify`.
- The "question" is rhetorical or a status update.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `question` | string | no | A single question. Use `questions` for several at once. |
| `details` | string | no | Why you are asking; shown with the question to help the user decide. |
| `options` | array | no | Predefined choices rendered as clickable buttons. |
| `questions` | array | no | Multiple questions answered together before the model continues. |

### Parameter Notes

- Use either the single-question form (`question` + `details` + `options`) **or** the batch form (`questions`), not both.
- `options` should be mutually exclusive and cover the realistic space; the UI also allows free-form input.
- In `questions`, each entry carries its own text/options; all must be answered before the turn resumes.
- There is no timeout and no default — an unanswered question halts progress indefinitely.

## Example

```json
{
  "question": "Which deployment target should I build?",
  "details": "The artifact differs per platform and rebuilding takes several minutes.",
  "options": ["Windows x64", "macOS arm64", "Linux x64", "All three"]
}
```

## Best Practices

- Ask at most once per decision point, and batch related questions into a single call.
- State the consequence in `details` — "this will delete the session data" beats "are you sure?".
- Offer options whenever the answer space is small; free text invites ambiguity.
- Make questions answerable without re-reading the whole conversation.
- Once answered, act on the answer; do not re-ask the same thing later.

## Common Pitfalls

- Over-asking: each low-value question costs a full user round-trip and erodes trust.
- Vague questions ("what do you want?") force the user to do the thinking for you.
- Asking something already stated earlier in the conversation.
- Options that overlap or omit the obvious default.
- Blocking on a question when a safe reversible default existed.

## Anti-patterns

- Using `question` to confirm every small step.
- Asking a question whose answer you can verify with a tool (`glob`, `file_read`).
- Sending five separate single-question calls instead of one batch.
- Using this to deliver a notification.

## Pairing with Other Tools

- `glob` / `file_read` — resolve factual uncertainty before asking.
- `notify` — inform without blocking.
- `todo` / `task_create` — track the work implied by the answer.
- `agent` — delegate a self-contained subtask instead of asking the user.
