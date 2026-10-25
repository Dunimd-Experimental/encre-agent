---
name: tool-document
description: "Read, create, and edit .docx files: formatted or plain text, append text/tables, metadata, convert to md/txt."
hidden: true
context: inline
---

# tool-document

Manipulates Microsoft Word `.docx` files through python-docx. It reads documents in formatted or plain form, reports metadata, creates new files, appends text and tables, previews tables, and converts to `.txt` or `.md`. It is not a layout engine and does not touch `.odt` or `.pdf`.

## When to Use

- Extract the text of a Word document the user attached or pointed at.
- Produce a `.docx` deliverable from structured content (headings, paragraphs, tables).
- Append sections or a results table to an existing report.
- Convert a `.docx` to Markdown so it can be read, diffed, or committed.
- Inspect table structure before extracting values.

## When NOT to Use

- PDFs — use `pdf` (read, extract, merge, render).
- Spreadsheets (`.xlsx`) — use `spreadsheet`.
- Slide decks (`.pptx`) — use `presentation`.
- `.odt`, `.rtf`, or legacy `.doc` — unsupported; convert externally first.
- Pixel-precise layout, styles, headers/footers, or tracked changes; the tool works at paragraph/table level.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | `read`, `extract_text`, `info`, `create`, `add_text`, `add_table`, `list_tables`, `convert`. |
| `file_path` | string | yes except `convert` | The `.docx` to read or modify. |
| `content` | string | for `create` | Body text; markdown-style `#`, `##`, `###` become headings, `-`/`*` become bullets. |
| `text` | string | for `add_text` | Text to append; same heading conventions apply. |
| `data` | array | for `add_table` | 2-D rows; the first row is treated as headers, e.g. `[["Name","Age"],["Ada",36]]`. |
| `source` | string | for `convert` | Source `.docx` path. |
| `target` | string | for `convert` | Destination path; extension must be `.txt` or `.md`. |

### Parameter Notes

- `read` returns a formatted dump (headings, paragraphs, tables preserved structurally); `extract_text` returns plain text suitable for downstream parsing.
- `create` writes a new file — it does not merge into an existing document. Use `add_text`/`add_table` to append.
- Markdown prefix handling is textual: a line starting with `## ` becomes a Heading 2 paragraph, not literal text.
- `add_table` expects uniform row widths; ragged rows produce a malformed table.
- `convert` supports only `.txt` and `.md`. Do not point `target` at `.docx` or `.pdf`.
- `info` reports document metadata (author, title, created/modified timestamps, counts) rather than content.

## Example

```json
{ "action": "extract_text", "file_path": "D:\\encre\\docs\\report.docx" }
```

```json
{
  "action": "create",
  "file_path": "reports\\2026-10-review.docx",
  "content": "# Q3 Review\n\n## Summary\n- Shipped the skill index fix\n- Rewrote 65 tool guides\n\n## Risks\nScope creep on the plugin registry."
}
```

```json
{
  "action": "add_table",
  "file_path": "reports\\2026-10-review.docx",
  "data": [
    ["Metric", "Before", "After"],
    ["Tool guides indexed", "0", "65"],
    ["Stub guides", "42", "0"]
  ]
}
```

```json
{
  "action": "convert",
  "source": "docs\\release-notes.docx",
  "target": "docs\\release-notes.md"
}
```

## Best Practices

- Use `extract_text` when feeding content to another tool or a summary; use `read` when structure (headings/tables) matters.
- Check `info` before overwriting a file that might carry authorship metadata the user cares about.
- Build documents top-down: `create` with the skeleton, then `add_text` sections, then `add_table` results.
- Keep tables narrow; Word tables with many columns become unreadable when converted to Markdown.
- Convert to `.md` for anything that will be reviewed or version-controlled.
- Verify after writing: `read` or `list_tables` to confirm the structure landed as intended.

## Common Pitfalls

- Assuming `create` appends when the path already exists — check behaviour and back up first if unsure.
- Passing a `.doc` or `.odt` file; python-docx only understands `.docx`.
- Using `convert` with a `.pdf` target and expecting a rendered document.
- Losing formatting because `text`/`content` only encode headings and bullets; bold runs, colors, and alignment are not expressible.
- Feeding ragged `data` rows and getting a table with missing cells.
- Reading a very large document with `read` when only page 1's summary was needed.

## Anti-patterns

- Round-tripping a `.docx` through `convert` → edit Markdown → re-create when the original formatting matters.
- Generating a Word file when the deliverable should be Markdown or PDF.
- Using `document` to read a spreadsheet renamed to `.docx`.
- Appending the same section twice because the first append was not verified.

## Pairing with Other Tools

- `pdf` — read/extract/merge PDFs.
- `spreadsheet` — tabular data as `.xlsx` rather than a Word table.
- `presentation` — slide decks.
- `file_read` — inspect the converted `.md`/`.txt` output.
- `diagram` / `chart` — produce images to embed manually after generation.
