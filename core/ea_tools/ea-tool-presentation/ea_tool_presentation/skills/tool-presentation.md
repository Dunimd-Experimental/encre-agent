---
name: tool-presentation
description: "Read, create or extend PowerPoint decks (slides, titles, bullet bodies, tables) with python-pptx."
hidden: true
context: inline
---

# tool-presentation

`presentation` edits real `.pptx` files through python-pptx. It can dump a deck's content, extract plain text, report metadata, create a new deck from a list of slides, append one slide with a chosen layout, or list slide titles. Everything it writes is a genuine PowerPoint file the user can open.

## When to Use

- Summarize or quote an existing deck: `action="read"` or `"extract_text"`.
- Produce a shareable slide deck from your findings: `action="create"` with `slides`.
- Append one more slide to an existing deck: `action="add_slide"` with `content`.
- Get an outline of a long deck: `action="list_slides"`.
- Check author/company/slide count before shipping: `action="info"`.

## When NOT to Use

- Word documents — use `document`; spreadsheets — use `spreadsheet`; PDFs — use `pdf`.
- `.odp`, Keynote or Google Slides exports — unsupported formats.
- Decks needing custom templates, animations, charts or embedded video — python-pptx support is limited.
- Inline one-off visuals in chat — use `info` for a card.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `read`, `extract_text`, `info`, `create`, `add_slide`, `list_slides`. |
| `file_path` | string | no | Path to the `.pptx`; required for every action except `create`. |
| `title` | string | no | Title for the title slide when `create`. |
| `slides` | array | no | Initial slides for `create`: strings (titles) or `{title, content}` objects. |
| `slide_type` | string | no | Layout for `add_slide`: `blank`, `title`, `content`, `two_content`, `section_header`. |
| `content` | string | no | Body for `add_slide`: first line becomes the title, following lines become bullets. |

### Parameter Notes

- For `create`, `slides` items are either a plain title string or a dict with `title` and `content`.
- For `add_slide`, the title is **inside** `content` as its first line — there is no separate title argument.
- `slide_type` selects a built-in layout, which may not match a custom template.
- `read` returns structured slide content; `extract_text` returns flat text.

## Example

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

## Best Practices

- Keep 4–6 bullets per slide; decks with long bodies are unreadable.
- Write titles as claims ("Churn fell 2pp") rather than labels ("Churn").
- Use `list_slides` before editing an unfamiliar deck to understand its structure.
- Create a fresh file for generated decks rather than appending to a template you cannot inspect.
- Store the output in a path the user will look for, and tell them where it is.

## Common Pitfalls

- python-pptx must be installed; without it every action fails.
- `add_slide` uses built-in layouts, so decks built from a branded template lose their styling.
- Tables and images in an existing deck are not returned by `read`/`extract_text`.
- Overwriting an existing `file_path` with `create` replaces it without warning.
- Newlines inside `content` drive bullet structure; a single long line becomes one giant bullet.

## Anti-patterns

- Generating a 30-slide deck when a 6-slide summary would communicate better.
- Using this to read text out of a PDF or Word file.
- Rebuilding a whole deck just to add one bullet — use `add_slide`.
- Promising custom animations or transitions.

## Pairing with Other Tools

- `document` — long-form companion document.
- `chart` / `diagram` — generate visuals to reference or embed manually.
- `pdf` — read source material before summarizing into slides.
- `info` — preview the deck outline as a card.
