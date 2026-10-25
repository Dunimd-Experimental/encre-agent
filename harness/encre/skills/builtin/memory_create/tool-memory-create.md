---
name: tool-memory-create
description: "Intake a new memory node into the persistent memory graph. The node lands in the short-term buffer and is promoted to long-term by repetition or when referenced by memory_link; duplicates merge into the existing node instead of duplicating. Use this to record durable facts, user preferences, decisions, lessons learned, or episodes that future sessions should recall. Do NOT use this for ephemeral scratch data (use todo/task tools) or to correct an existing memory (use memory_update -- the graph is append-only, corrections supersede). Tips: search first (memory_search) to avoid duplicates; `type` classifies the node for richer recall (fact/preference/decision/lesson/entity/episode); `significance` (0.0-1.0) drives promotion and decay resistance; `reason` records why this is worth remembering. Pitfalls: buffered nodes are recallable but weaker -- link or repeat them to make them durable; node ids are the primary handle -- use the id echoed in the result for memory_read/memory_link."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# memory_create

Intake a new memory node into the persistent memory graph. The node lands in the short-term buffer and is promoted to long-term by repetition or when referenced by memory_link; duplicates merge into the existing node instead of duplicating. Use this to record durable facts, user preferences, decisions, lessons learned, or episodes that future sessions should recall. Do NOT use this for ephemeral scratch data (use todo/task tools) or to correct an existing memory (use memory_update -- the graph is append-only, corrections supersede). Tips: search first (memory_search) to avoid duplicates; `type` classifies the node for richer recall (fact/preference/decision/lesson/entity/episode); `significance` (0.0-1.0) drives promotion and decay resistance; `reason` records why this is worth remembering. Pitfalls: buffered nodes are recallable but weaker -- link or repeat them to make them durable; node ids are the primary handle -- use the id echoed in the result for memory_read/memory_link.

## Result Semantics

- `Memory node '<id>' (type: <type>, importance: <n>) <placement>.` where `<placement>` is one of: `merged into the existing long-term node`, `promoted to long-term (repeated observation)`, `promoted to long-term`, or `held in the short-term buffer (promotes on repetition or when linked)`.
- `Unknown type '<x>'. Valid types: fact, preference, decision, lesson, entity, episode.` -- the type enum is strict.
- `Error: content is required.` -- empty statement rejected.
- The echoed node id is the handle for memory_read/memory_update/memory_link; store it in the conversation, do not guess it later.
