---
name: tool-memory-read
description: "Read one memory-graph node in full: metadata (type, importance, confidence, status, access count, links) plus its verbatim detail layer. Use this when you already have a node id (echoed by every memory tool) and want the complete node. Do NOT use this to discover memories by meaning (use memory_search) or to inspect the user profile (use memory_profile). Tips: notes the user edited by hand can be referenced as filenames too -- they enter the graph through ingestion. Pitfalls: a node whose status says superseded is kept only for audit; follow the status pointer to the live replacement."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# memory_read

Read one memory-graph node in full: metadata (type, importance, confidence, status, access count, links) plus its verbatim detail layer. Use this when you already have a node id (echoed by every memory tool) and want the complete node. Do NOT use this to discover memories by meaning (use memory_search) or to inspect the user profile (use memory_profile). Tips: notes the user edited by hand can be referenced as filenames too -- they enter the graph through ingestion. Pitfalls: a node whose status says superseded is kept only for audit; follow the status pointer to the live replacement.

## Result Semantics

- Returns a header block (`Memory node <label> (<id>)`) with type/importance/confidence/status, access count, the link list (`relation -> target` per edge), then the verbatim detail body; empty nodes report `(empty node)`.
- `status: superseded by <ref>` -- the node lost arbitration and exists for audit only; follow the pointer for the live content.
- `Memory node '<ref>' not found in the graph. Use memory_search to locate it.` -- no node matched the reference.
