---
name: tool-memory-search
description: "Search the persistent memory graph and return the most relevant nodes for the query. Recall is associative: direct matches seed the search, then activation spreads along the graph's typed links (causes/contradicts/refines/supersedes/supports/mentions/co_occurs), so recalling one memory pulls in the cluster it belongs to. Use this when you need to recall a memory by meaning rather than by filename, e.g. 'what does the user prefer for testing?' -- and always before creating a new memory to avoid duplicates. Do NOT use this when you already know the filename (use memory_read) or for structured user profile fields (use memory_profile). Tips: write the query as a natural-language question for best recall; raise `top_k` to broaden the result set; narrow with `type` (fact/preference/decision/lesson/entity/episode) or pass `include_archived` to reach decayed nodes; hits are annotated with freshness, node confidence, and the link path when they were reached by spreading rather than a direct match. Pitfalls: every search records access for the hits, strengthening their retention against decay -- do not spam queries; very generic queries can surface many similar memories, narrow the wording or reduce `top_k` to focus results."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# memory_search

Search the persistent memory graph and return the most relevant nodes for the query. Recall is associative: direct matches seed the search, then activation spreads along the graph's typed links (causes/contradicts/refines/supersedes/supports/mentions/co_occurs), so recalling one memory pulls in the cluster it belongs to. Use this when you need to recall a memory by meaning rather than by filename, e.g. 'what does the user prefer for testing?' -- and always before creating a new memory to avoid duplicates. Do NOT use this when you already know the filename (use memory_read) or for structured user profile fields (use memory_profile). Tips: write the query as a natural-language question for best recall; raise `top_k` to broaden the result set; narrow with `type` (fact/preference/decision/lesson/entity/episode) or pass `include_archived` to reach decayed nodes; hits are annotated with freshness, node confidence, and the link path when they were reached by spreading rather than a direct match. Pitfalls: every search records access for the hits, strengthening their retention against decay -- do not spam queries; very generic queries can surface many similar memories, narrow the wording or reduce `top_k` to focus results.

## Result Semantics

- Returns a numbered list of hits: label with node id, node type, score, confidence, freshness, and -- when a hit was reached through the graph rather than a direct match -- `spread via N link(s) from a direct match`, plus a 200-char content preview.
- `No memories found matching '<query>'.` -- empty result (an over-narrow `type` filter can also cause this); reword the query or drop the filter before concluding the memory does not exist.
- `Unknown type '<x>'. Valid types: fact, preference, decision, lesson, entity, episode.` -- the type enum is strict.
- Each hit records an access that strengthens the node against decay: search deliberately, not in a loop.
