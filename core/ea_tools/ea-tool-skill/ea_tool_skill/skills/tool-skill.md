---
name: tool-skill
description: "Activate a skill by name — a domain skill or a `tool-<tool>` usage guide — so its detailed guidance is injected into the next step."
hidden: true
context: inline
---

# tool-skill

`skill` is the activation mechanism for procedural knowledge that is too long to keep in the prompt. Two kinds exist: **domain skills** (`travel-flights`, `pdf`, `data-viz`, ...) that add expertise for a class of request, and **tool usage guides** (`tool-glob`, `tool-browser`, `tool-database`, ...) that document one tool's exact parameters, best practices and pitfalls. Activating one injects its body into the next step's system prompt.

## When to Use

- A tool's arguments are not obvious from its schema — load `tool-<tool>` **before** the first call.
- The user's request matches a domain skill's purpose (flight search, PDF processing, charting).
- You need the exact semantics of an uncommon parameter (`computer`, `browser`, `database`).
- You are about to use a tool whose pitfalls could waste several calls.
- You want to see what is available: `name="list"`.

## When NOT to Use

- The tool schema already answers the question (simple tools like `todo`, `task_get`).
- The guidance is already in the prompt: guides auto-load on first use, and re-activating is a no-op.
- You need current facts rather than procedure — use `web_search` / `web_fetch`.
- You are looking for a code snippet in the repo — use `grep`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | yes | Skill name — a domain skill (`travel-flights`, `pdf`, `data-viz`) or a tool guide (`tool-glob`, `tool-browser`). `list` enumerates everything. |
| `args` | string | no | Optional context string forwarded to the skill (request details or parameters). |

### Parameter Notes

- Names are exact and hyphenated: `tool-apply-patch`, not `tool_apply_patch`; `tool-hash-crypto`, not `tool_hash_crypto`.
- `name="list"` returns the catalogue including tool guides; it activates nothing.
- Activation is per session and idempotent — a second load of the same name does nothing.
- `args` is advisory context for the skill, not a substitute for tool arguments.

## Example

```json
{
  "name": "tool-browser",
  "args": "need to fill a login form and capture a screenshot"
}
```

## Best Practices

- Load a tool guide *before* the first call, not after a failure.
- Load at most the guides you will actually use this turn; each adds tokens.
- Prefer `list` over guessing names when the catalogue is unfamiliar.
- Pair a domain skill with the specific tool guide it references.
- Re-read the injected guidance in the next step before composing arguments.

## Common Pitfalls

- Unknown or misspelled names return an error rather than a suggestion.
- Using underscores instead of hyphens fails the lookup.
- Loading a guide does not execute the tool; you still have to call it.
- Guidance is injected for the *next* step — the current turn's already-built prompt does not change.
- Domain skills and tool guides share one namespace, so names must be unambiguous.

## Anti-patterns

- Activating every guide "just in case".
- Using `skill` to fetch facts or search the web.
- Calling the tool first and loading the guide only after an error.
- Assuming activation changed the current step's prompt.

## Pairing with Other Tools

- Any tool — load its `tool-<name>` guide first.
- `web_search` / `web_fetch` — for facts rather than procedure.
- `todo` — track the multi-step work a domain skill describes.
- `agent` — delegate when the skill's workflow is long and self-contained.
