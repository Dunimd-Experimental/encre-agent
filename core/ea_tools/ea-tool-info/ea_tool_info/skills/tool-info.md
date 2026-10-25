---
name: tool-info
description: "Render a self-contained HTML/CSS/JS card, dashboard or chart inline, or show images/videos in a native media gallery."
hidden: true
context: inline
---

# tool-info

`info` is the display layer for anything richer than markdown: dashboards, KPI cards, comparison tables, custom charts, formatted reports. You hand it a complete HTML document and it renders inside a sandboxed iframe in the chat timeline. Passing `media` instead (or as well) produces a native gallery for images and videos. The alternative `display="code"` shows the source rather than rendering it.

## When to Use

- The user asks for a card, dashboard, visualization or "nice layout" that markdown cannot express.
- Several metrics or sections need visual hierarchy (grid, colors, typography).
- A chart is better drawn with inline SVG/CSS than through a charting backend.
- Images or videos should appear as a gallery rather than as links.
- You need to show the HTML source itself for review — `display="code"`.

## When NOT to Use

- Plain prose, lists, tables or code — normal markdown is cheaper and renders natively.
- Data charts that must be editable or exported as a file — use `chart`.
- Generating slide decks or documents — use `presentation`, `document`, `spreadsheet`.
- Embedding remote content that needs network access; the iframe sandbox blocks it.
- Long-form reports that the user will copy into another system.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `display` | string | yes | `base` renders the card, `code` shows raw source, `split` is reserved. |
| `content` | string | yes | Self-contained HTML/CSS/JS payload; a full `<html><head><body>` document is strongly preferred. |
| `title` | string | no | Card title shown above the rendered content. |
| `media` | array | no | List of `{type, src}` media items; when present, renders a native gallery. |

### Parameter Notes

- `display` is mandatory — never omit it. Use `base` unless you deliberately want to expose the source.
- `content` is required by the schema even when you mostly intend to show `media`; pass a minimal wrapper in that case.
- `media` items are `{type, src}` where `type` distinguishes image/video and `src` is a path or URL the client can resolve.
- Everything in the card must be inline: no external `<script src>`, no CDN stylesheets, no `fetch`.

## Example

```json
{
  "display": "base",
  "title": "Build Health",
  "content": "<html><head><style>body{font-family:system-ui;padding:16px;background:#0f1115;color:#e6e6e6}.kpi{display:flex;gap:16px}.card{flex:1;background:#1a1d24;border-radius:12px;padding:16px}.v{font-size:32px;font-weight:700;color:#4ade80}</style></head><body><h2>Build Health</h2><div class=\"kpi\"><div class=\"card\"><div>Tests</div><div class=\"v\">482</div></div><div class=\"card\"><div>Failures</div><div class=\"v\">0</div></div></div></body></html>"
}
```

## Best Practices

- Emit a complete document with `<html>`, `<head>`, an inline `<style>` and `<body>`; fragments render but look broken.
- Use system font stacks and relative units so the card survives narrow panes.
- Inline all assets: CSS in `<style>`, scripts in `<script>`, images as `data:` URIs or via `media`.
- Give the card a `title` so the timeline entry is identifiable.
- Keep one card per idea; several small cards read better than one dense page.

## Common Pitfalls

- External resources (CDN fonts, remote scripts, `fetch` calls) are blocked by the sandbox and silently fail.
- Unescaped quotes in JSON break `content`; escape `"` inside HTML attributes.
- Very tall documents scroll inside the iframe and hide content — keep cards compact.
- `display="split"` is reserved and does not behave as split view.
- Passing `media` with a path the client cannot resolve shows an empty gallery slot.

## Anti-patterns

- Wrapping ordinary markdown in HTML just to "look nicer".
- Embedding a whole framework (React/Vue from CDN) inside a card.
- Using `info` for charts that the user needs as a file — that is `chart`.
- Rendering secrets or tokens in a card, since output is visible to everyone in the conversation.

## Pairing with Other Tools

- `chart` — generate a chart file when the artifact must persist.
- `generate_image` — produce assets referenced through `media`.
- `presentation` / `document` — deliverable documents rather than inline cards.
- `web_fetch` — gather data first, then render it as a card.
