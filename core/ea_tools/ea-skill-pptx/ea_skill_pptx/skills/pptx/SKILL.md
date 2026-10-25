---
name: pptx
description: Read, create and edit PowerPoint decks with python-pptx — slides, titles, bullets, tables, metadata, plus pptx-to-markdown conversion.
aliases:
  - powerpoint
  - slides
  - ppt
when_to_use: ".pptx .ppt"
argument_hint: "a .pptx path or 'create a deck from these notes'"
user_invocable: true
hidden: true
context: inline
---

# PowerPoint (.pptx)

`presentation` drives python-pptx against a real `.pptx` file: read slide structure, dump plain text, report metadata, create a new deck, append one slide, or list slide titles. Everything it writes opens in PowerPoint.

## When to Use

- "Summarize this deck", "what does slide 3 say", "extract the text".
- "Make me a deck from these notes" — a new `.pptx` with a title slide and content slides.
- "Add one more slide to the deck".
- "Convert this deck to Markdown" (so it can be read, diffed, or edited as text).
- "How many slides / who authored it?" — metadata checks before shipping.

## When NOT to Use

- Word documents → `docx`; spreadsheets → `xlsx`; PDFs → `pdf`.
- `.odp`, Keynote, or Google Slides files — unsupported formats.
- Custom templates, animations, transitions, embedded video, or on-slide charts — python-pptx support is limited; generate charts separately with `chart` and place them manually.
- Pixel-perfect redesigns of a branded deck. For a full deck rebuild, generate a fresh file with `create` instead of mutating the template.

## Tools You Use

| Task | Tool | Action / key params |
|---|---|---|
| Structured slide dump | `presentation` | `action=read`, `file_path` |
| Plain text only | `presentation` | `action=extract_text`, `file_path` |
| Author / title / slide count | `presentation` | `action=info`, `file_path` |
| New deck | `presentation` | `action=create`, `file_path`, `title`, `slides` |
| Append one slide | `presentation` | `action=add_slide`, `file_path`, `content`, `slide_type` |
| Outline of a long deck | `presentation` | `action=list_slides`, `file_path` |
| Visuals referenced by the deck | `chart`, `diagram` | save with `output_path` |
| Read a source PDF first | `pdf` | `action=read`, `pages` |

`create` accepts `slides` as a mix of plain title strings and `{title, content}` objects. For `add_slide`, the title is the **first line inside `content`** — there is no separate title parameter. `slide_type` is one of `blank`, `title`, `content`, `two_content`, `section_header` (built-in layouts only).

## Workflows

### Read an existing deck

1. `presentation` → `action=list_slides` for the outline.
2. `presentation` → `action=read` for structured content, or `action=extract_text` for plain text.
3. If tables or images matter, note they are **not** returned by `read`/`extract_text` — use `action=info` for counts and report that limitation.

### Convert pptx to Markdown

1. `presentation` → `action=extract_text`.
2. Rewrite each slide as a heading plus bullet list.
3. `file_write` the `.md` next to the deck.

### Create a deck from notes

1. Outline it first: one claim per slide, 4–6 bullets max.
2. `presentation` → `action=create` with `title` and `slides`.
3. Verify with `action=list_slides`, then `action=read` on slide 1.
4. Tell the user the exact output path.

### Append a slide

`presentation` → `action=add_slide` with `content` where the first line is the title and each following line becomes a bullet.

## Examples

```json
{
  "action": "create",
  "file_path": "D:/out/q3-review.pptx",
  "title": "Q3 Review",
  "slides": [
    { "title": "Highlights", "content": "Revenue +18%\nChurn -2pp\nTwo launches shipped" },
    "Next steps"
  ]
}
```

```json
{
  "action": "add_slide",
  "file_path": "D:/out/q3-review.pptx",
  "slide_type": "content",
  "content": "Next steps\nLock scope for the skill index\nShip the plugin registry audit"
}
```

## Best Practices

- Titles are claims, not labels: "Churn fell 2pp" beats "Churn".
- 4–6 bullets per slide; long bodies become unreadable at display size.
- Always `list_slides` before editing a deck you have not seen.
- Prefer `create` for generated decks and `add_slide` for single additions.
- Keep `dpi`/image sizes modest when embedding charts; a 300 dpi image bloats the file.
- Verify after writing — `create` overwrites an existing path without warning.

## Common Pitfalls

- python-pptx is not installed, so every action fails.
- `add_slide` uses built-in layouts, so a branded template loses its styling.
- Tables and images in an existing deck are invisible to `read`/`extract_text`.
- One long line inside `content` becomes a single giant bullet — use `\n` between bullets.
- Newlines in `content` drive bullet structure, so a stray blank line creates an empty bullet.
- Rebuilding a whole deck just to add one bullet.

## Anti-patterns

- Generating 30 slides when a 6-slide summary communicates better.
- Promising custom animations, transitions, or a custom template.
- Using `presentation` to read text out of a PDF or Word file.
- Appending the same section twice because the append was never verified.

## Pairing with Other Tools

- `document` — a long-form companion report for the same content.
- `spreadsheet` — the numbers behind the slides as a real table.
- `chart` / `diagram` — visuals saved to disk, then referenced or embedded by hand.
- `pdf` — read source material before summarizing into slides.
- `info` — preview the deck outline as a card.
