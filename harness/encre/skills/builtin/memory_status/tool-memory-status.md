---
name: tool-memory-status
description: "Introspect the memory graph: node counts by type, buffered candidates awaiting promotion, archived nodes, tombstoned supersessions, edge totals, and mean node confidence. Use this to check memory health before/after large memory work, to find out whether the buffer is hoarding candidates that should be linked or consolidated, or to explain why a memory is not being recalled (it may be archived or superseded). Do NOT use it to list individual memories (use memory_search) or to read one memory's content (use memory_read). Tips: run it once before bulk memory_link sessions to see what exists; buffered candidates can be pulled into the graph by referencing them with memory_link. Pitfalls: counts reflect the session-bound graph store; externally deleted .md files disappear only after the next ingestion pass."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# memory_status

Introspect the memory graph: node counts by type, buffered candidates awaiting promotion, archived nodes, tombstoned supersessions, edge totals, and mean node confidence. Use this to check memory health before/after large memory work, to find out whether the buffer is hoarding candidates that should be linked or consolidated, or to explain why a memory is not being recalled (it may be archived or superseded). Do NOT use it to list individual memories (use memory_search) or to read one memory's content (use memory_read). Tips: run it once before bulk memory_link sessions to see what exists; buffered candidates can be pulled into the graph by referencing them with memory_link. Pitfalls: counts reflect the session-bound graph store; externally deleted .md files disappear only after the next ingestion pass.

## Result Semantics

- Returns a sectioned report: `Memory graph status:` with long-term nodes (per-type breakdown), buffered candidates, archived, superseded, typed edges + mean confidence.
- `Error reading memory status: ...` -- the graph store could not be read; check memory_status again after a successful memory operation.
