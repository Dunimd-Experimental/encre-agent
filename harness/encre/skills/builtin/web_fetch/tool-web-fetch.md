---
name: tool-web-fetch
description: URL fetch skill. Fetch and read full page content, format/timeout params, when to fetch vs search
hidden: true
context: inline
---

## When to Use
- Read the full content of a known URL (docs, articles, raw files)
- Deep-read a page found via `web_search`
- Fetch an API endpoint's raw response as text (for richer API calls use `rest_client`)

## When NOT to Use
- **Don't know the URL** -> `web_search` to find it first
- **Calling a REST/GraphQL API with headers/body** -> `rest_client` (proper method/headers/auth)
- **Local file** -> `file_read`

## Key Parameters
- `url` (required): full URL including scheme
- `format`: output format (e.g. html/markdown/text). Markdown/text strips noise for reading
- `timeout`: seconds before the fetch aborts; raise for slow sites

## Best Practices
- Use `web_search` to find the URL, then `web_fetch` to read it fully (search snippets are not enough)
- Prefer markdown/text format to reduce noise when you only need the content
- Cross-validate claims from a single page against at least one other source

## Common Pitfalls / Anti-patterns
- **Fetching a URL you guessed**: guessing URLs is unreliable; search first
- **Trusting a single source**: one page may be wrong or outdated; cross-check
- **Using fetch instead of rest_client for API calls**: API calls need method/headers/body; use `rest_client`
- **Ignoring timeout on slow sites**: a hung fetch blocks the turn; set a timeout

## Pairing with Other Tools
- `web_search`: search to find URLs, then fetch to read
- `rest_client`: structured API calls (method/headers/body)
- `file_write`: persist fetched content to a file