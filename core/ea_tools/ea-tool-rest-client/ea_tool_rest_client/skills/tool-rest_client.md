---
name: tool-rest-client
description: "Call a REST or GraphQL API endpoint and get back status, headers and a parsed body, with JSON handling and a timeout."
hidden: true
context: inline
---

# tool-rest_client

`rest_client` performs one HTTP request against an API endpoint and returns `{status, headers, body}` with JSON parsed automatically. It is the general-purpose integration tool: authenticated REST calls, GraphQL queries, webhooks, and any JSON resource that is not a human-readable page.

## When to Use

- Integrate with a documented third-party API by URL.
- Send a GraphQL query as a POST with a JSON body.
- Call an internal service endpoint and inspect the structured response.
- Fetch a raw JSON/XML resource when you need headers or the status code.
- Probe an endpoint's auth requirements with a safe GET.

## When NOT to Use

- Reading a normal web page for its content — use `web_fetch` (renders JS, strips boilerplate).
- Anything requiring clicks, logins or form fills — use `browser`.
- GitHub/GitLab object operations — use `github`.
- Posting a chat notification — use `notify`.
- Downloading a large file to disk — use `terminal` with a proper client.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `method` | string | yes | `GET`, `POST`, `PUT`, `DELETE`, `PATCH`. |
| `url` | string | yes | Absolute URL including scheme and query string. |
| `headers` | object | no | Request headers; include `Content-Type` and `Authorization` as needed. |
| `body` | string | no | Raw body string, sent verbatim; JSON APIs need a JSON-serialized string. |
| `timeout` | integer | no | Seconds before abort; default 30. |

### Parameter Notes

- `body` is **verbatim**: no automatic JSON encoding, no form encoding. Serialize it yourself and set `Content-Type` to match.
- `headers` values must be strings; numeric or nested values break the request.
- `timeout` is per request, not per retry — there are no retries.
- Non-GET methods are flagged destructive: they change remote state.

## Example

```json
{
  "method": "POST",
  "url": "https://api.example.com/v1/tickets",
  "headers": {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${API_TOKEN}"
  },
  "body": "{\"title\":\"Checkout fails on retry\",\"priority\":\"high\"}"
}
```

## Best Practices

- Prefer environment/injected credentials over literal tokens in `body` or `headers`.
- Inspect `status` before trusting `body`; error payloads often parse as valid JSON.
- Set a smaller `timeout` for unreachable hosts and a larger one for slow batch endpoints.
- Use `GET` to discover the response shape before writing mutating calls.
- Keep request bodies minimal; add fields only when the API documents them.

## Common Pitfalls

- Bodies over 50K characters are truncated — large responses lose data.
- A JSON string with unescaped quotes becomes an invalid request body.
- Some APIs require `Accept` headers or a versioned path; missing them yields 406/404.
- Redirects may be followed, turning a GET into an unintended target.
- Rate limits return 429 with a `Retry-After` header — respect it instead of retrying immediately.

## Anti-patterns

- Using `rest_client` to scrape HTML pages that `web_fetch` handles better.
- Sending DELETE/PATCH "to see what happens" in production.
- Passing a raw Python dict as `body` instead of a JSON string.
- Hard-coding API keys into arguments.

## Pairing with Other Tools

- `web_fetch` — human-readable pages and JS-rendered content.
- `json_tool` — inspect, query or validate the response body.
- `github` — provider-native issue/PR operations.
- `hash_crypto` — sign payloads or verify webhook signatures.
