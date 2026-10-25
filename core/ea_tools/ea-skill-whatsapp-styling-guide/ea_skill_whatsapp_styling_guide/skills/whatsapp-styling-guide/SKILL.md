---
name: whatsapp-styling-guide
description: "Format text for WhatsApp: *bold*, _italic_, ~strike~, monospace, bullets, quotes — and no markdown headings or tables. Use before sending any styled message to WhatsApp."
---

# WhatsApp Styling Guide

WhatsApp renders a small, strict subset of formatting. Markdown that looks
fine elsewhere (`**bold**`, `# heading`, tables) arrives as literal
asterisks and hashes. This skill keeps outgoing WhatsApp text readable.

## When to Use

- A reply will be delivered through WhatsApp (or the user asks to format for it).
- You are drafting a notification, summary or instructions that a person will read on a phone.
- Text already written in markdown needs to be converted before sending.
- A message mixes code/IDs and prose and must not turn into symbol soup.

## When NOT to Use

- The channel is Slack, Discord, Telegram, Feishu or email — those accept markdown (see `slack-integration` for Slack's variant).
- The content is a document, not a chat message (`write-docs`, `doc-coauthoring`).
- A table is genuinely required — WhatsApp cannot render one; send a file or a code block instead.

## The Syntax

| Intent | WhatsApp syntax | Example |
|---|---|---|
| Bold | single asterisks | `*Total: 42*` |
| Italic | single underscores | `_pending review_` |
| Strikethrough | tildes | `~cancelled~` |
| Monospace | triple backticks | ``` `order-8812` ``` |
| Bullet list | `* ` at line start | `* Item one` |
| Numbered list | `1. ` | `1. First step` |
| Quote | `> ` | `> quoted text` |

## Prohibited Patterns

- No headings (`#`, `##`, `###`) — use `*BOLD CAPS*` on its own line.
- No markdown tables — convert to bullet lines (`* Mon: 09:00–17:00`).
- No horizontal rules (`---`) — a line of underscores `__________` if a separator is truly needed.
- No nested emphasis (`*_text_*`) — it renders unpredictably on some clients.
- No double asterisks for bold — they show up literally.

## Conversion Workflow

1. Draft the message normally.
2. Convert headings to bold caps lines; drop the `#`.
3. Convert `**text**` → `*text*`.
4. Flatten tables into `* Label: value` lines.
5. Wrap identifiers, commands and numbers in triple backticks.
6. Keep lines short (under ~60 characters) — long lines wrap badly on phones.
7. Re-read once, checking that no `#`, `**`, `|---|` or `---` survived.

## Example

Before:

```markdown
## Order status
**Order:** #8812
| Item | Qty |
|---|---|
| Cable | 2 |
```

After:

```text
*ORDER STATUS*

*Order:* `#8812`

*Items*
* Cable — qty 2
```

## Best Practices

- Prefer short paragraphs and bullets over dense blocks; people read this on a phone.
- Use bold for the one thing that matters, not for every line.
- Keep emoji sparse and meaningful — they are not decoration for structure.
- Preserve numbers, IDs and URLs exactly; wrap them in monospace so they are easy to copy.

## Common Pitfalls

- Mixing markdown and WhatsApp syntax in the same message.
- Using `*` for bullets and bold on the same line (`* *text*`), which renders ambiguously.
- Long unbroken URLs — WhatsApp may not linkify them cleanly.
- Assuming the renderer strips unsupported syntax; it usually shows it raw.

## Anti-patterns

- Sending a markdown table and asking the reader to "imagine the columns".
- Decorative separators made of `===` or `---`.
- Restyling plain prose that has no formatting at all.
