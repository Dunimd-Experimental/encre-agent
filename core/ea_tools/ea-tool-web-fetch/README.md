# ea-tool-web-fetch

EA (Encre Agent) system-default plugin providing the `web_fetch` tool.

## Tools

- `web_fetch` — Fetch a single URL and return its content as plain text,
  markdown, or raw HTML. Uses the Exa crawler (JS rendering) as the primary
  path with an SSRF-guarded httpx fallback (manual redirect re-validation,
  5 MiB body cap, 50k-char output cap).

## Tier

`system-default`, deferred activation — the model discovers it via
`manage(discover)` when a task needs to fetch a specific URL.

## Install

```shell
pip install ea-tool-web-fetch
```

Dependencies: `encre-harness`, `httpx`, `markdownify`, `beautifulsoup4`, `lxml`.
