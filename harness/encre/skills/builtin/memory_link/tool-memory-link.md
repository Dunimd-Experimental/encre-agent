---
name: tool-memory-link
description: "Wire a typed edge between two memory-graph nodes, by node id (or ingested-note filename). This shapes the associative recall surface: links are the paths spreading activation travels, so a linked cluster is recalled together. `supersedes` is special -- it arbitrates contradictions: the target is tombstoned (kept for audit, hidden from recall) and the source becomes the live replacement. Use this to record causal chains (causes), supporting evidence (supports), refinements (refines), corrections (supersedes), or plain associations (mentions) -- and to promote buffered nodes by referencing them. Do NOT use it to duplicate structural links the system already maintains (co_occurs wiring) or to link memories that do not exist yet -- create them first. Tips: node ids are echoed by every memory tool; `weight` (0.0-2.0) scales edge strength for recall spreading; linking a buffered node promotes it to long-term. Pitfalls: supersedes permanently hides the loser from normal recall; edges are not deduplicated across relations -- linking the same pair twice with different relations creates two edges."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# memory_link

Wire a typed edge between two memory-graph nodes, by node id (or ingested-note filename). This shapes the associative recall surface: links are the paths spreading activation travels, so a linked cluster is recalled together. `supersedes` is special -- it arbitrates contradictions: the target is tombstoned (kept for audit, hidden from recall) and the source becomes the live replacement. Use this to record causal chains (causes), supporting evidence (supports), refinements (refines), corrections (supersedes), or plain associations (mentions) -- and to promote buffered nodes by referencing them. Do NOT use it to duplicate structural links the system already maintains (co_occurs wiring) or to link memories that do not exist yet -- create them first. Tips: node ids are echoed by every memory tool; `weight` (0.0-2.0) scales edge strength for recall spreading; linking a buffered node promotes it to long-term. Pitfalls: supersedes permanently hides the loser from normal recall; edges are not deduplicated across relations -- linking the same pair twice with different relations creates two edges.

## Result Semantics

- Plain edge: `Linked '<source>' --[<relation>]--> '<target>' (weight: <w>).`
- Arbitration: `'<old>' was superseded by '<new>' (tombstoned, hidden from recall, kept for audit).`
- `Unknown relation '<x>'. Valid relations: causes, contradicts, refines, supersedes, supports, mentions, co_occurs.` -- the relation enum is strict.
- `Source/Target memory '<ref>' not found in the graph. Use memory_search to locate it.` -- the reference matched no node; use node ids echoed by other memory tools.
- `Error linking memories: ...` -- store-level failure; retry after checking memory_status.
