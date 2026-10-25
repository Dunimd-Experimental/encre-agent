---
name: tool-memory-profile
description: "Read or update the user profile -- structured observations about the user (expertise, communication style, preferences, OS, editor, etc.) stored as _profile.md inside the unified memory system.\n\nUse this to tailor responses to the user (query) or to record a new observation (update).\nDo NOT use this for free-form memories (use memory_create/update) or for one-off facts that do not belong in the profile.\nTips: query with no args to dump the whole profile; pass field+value to record an observation, optionally with a confidence score.\nPitfalls: profile fields are a fixed enumeration -- see the `field` enum description for the supported set."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# memory_profile

Read or update the user profile -- structured observations about the user (expertise, communication style, preferences, OS, editor, etc.) stored as _profile.md inside the unified memory system.

Use this to tailor responses to the user (query) or to record a new observation (update).
Do NOT use this for free-form memories (use memory_create/update) or for one-off facts that do not belong in the profile.
Tips: query with no args to dump the whole profile; pass field+value to record an observation, optionally with a confidence score.
Pitfalls: profile fields are a fixed enumeration -- see the `field` enum description for the supported set.

## Result Semantics

- Update: `Profile field '<field>' updated to '<value>' (confidence: <c>).`
- Single-field query: `<field>: <value> (confidence: <c>)`; unset fields return `Profile field '<field>' is not set.`
- Bare query: a sectioned dump of the whole profile; empty profile reports that it builds over time.
- `Unknown field '<field>'. Valid fields: ...` -- the field name is outside the fixed enumeration; retry with a listed field.
