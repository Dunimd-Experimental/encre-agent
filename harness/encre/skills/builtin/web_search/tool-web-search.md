---
name: tool-web-search
description: Web search skill. Query construction strategy, num/language/categories filters, when to search vs web_fetch
hidden: true
context: inline
---

## When to Use
- Need current/latest information (events, versions, prices, news after the knowledge cutoff)
- Uncertain factual queries
- Find an entry point to official docs/usage/examples

## When NOT to Use
- **Known specific URL** -> `web_fetch` to fetch it directly
- **Local in-codebase search** -> `grep`/`glob` (not web)
- **Call an API** -> `rest_client`
- **Static knowledge / general concepts**: the model already knows; no need to search

## Key Parameters
- `query` (required): search terms. Construction is the key to results (see below)
- `num`: number of results; default is enough, raise for deeper digging
- `language`: result language preference
- `categories`: restrict result category (e.g. general/code/news)

## Best Practices (query construction)
- **Specific beats broad**: "python asyncio gather exception handling" beats "python asyncio"
- **Add time words**: for time-sensitive info add "2026" or "latest"
- **Add authority domains**: `site:docs.python.org`, `site:stackoverflow.com`
- **Rotate synonyms**: if nothing returns, swap keywords; don't conclude "info doesn't exist"
- **Broad then narrow**: broad query to find direction, narrow query to dig

## Common Pitfalls / Anti-patterns
- **Query too broad**: noisy results; add specific tech words/version/scenario
- **Concluding from one search**: cross-validate with at least 2-3 queries
- **Searching but not reading**: `web_search` gives snippets; to read deeply use `web_fetch` for full text
- **Trusting non-authoritative sources**: prices/news/regulations need authoritative sources; don't trust aggregators/cached pages
- **Ignoring recency**: add time words for "latest"; general knowledge needs none

## Pairing with Other Tools
- `web_fetch`: after search finds a URL, fetch to read full text
- `rest_client`: call a specific API (not search)
- `file_write`: persist research results to a file