---
name: tool-memory-update
description: "Replace an existing memory node with corrected content via supersede arbitration: a fresh node is intaken and the old one is tombstoned -- kept for audit, hidden from recall. The graph never leaves two opposing beliefs alive. Use this to correct, refresh, or contradict an existing memory. Do NOT use this to add a new memory (use memory_create) or to record a relation between two existing nodes (use memory_link). Tips: omit `type`/`significance` to inherit them from the old node; `reason` documents why the correction was needed. Pitfalls: supersession is permanent -- the loser never returns to normal recall; if the new content matches an existing node it merges there instead of superseding."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# memory_update

Replace an existing memory node with corrected content via supersede arbitration: a fresh node is intaken and the old one is tombstoned -- kept for audit, hidden from recall. The graph never leaves two opposing beliefs alive. Use this to correct, refresh, or contradict an existing memory. Do NOT use this to add a new memory (use memory_create) or to record a relation between two existing nodes (use memory_link). Tips: omit `type`/`significance` to inherit them from the old node; `reason` documents why the correction was needed. Pitfalls: supersession is permanent -- the loser never returns to normal recall; if the new content matches an existing node it merges there instead of superseding.

## Result Semantics

- `Node '<old-id>' was superseded by '<new-id>' (type: <type>, importance: <n>). The old node is tombstoned: kept for audit, hidden from recall.` -- the correction is live; use `<new-id>` from now on.
- `Content merged into the existing node '<id>' (<placement>); nothing to supersede.` -- duplicate detection matched the new content with a resident node; no arbitration happened.
- `Memory node '<ref>' not found in the graph. Use memory_search to locate it.` -- nothing to replace.
- `Unknown type '<x>'. Valid types: fact, preference, decision, lesson, entity, episode.` -- the type enum is strict.
