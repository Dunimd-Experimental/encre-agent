---
name: docx
description: Read, create and edit Word documents with python-docx — headings, bullets, tables, metadata, plus conversion to .txt or .md.
aliases:
  - word
  - word-doc
when_to_use: ".docx .doc"
argument_hint: "[path to .docx file or task description]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

# Word (.docx)

`document` manipulates Word files through python-docx: read in formatted or plain form, report metadata, create a new file, append text and tables, preview tables, and convert to `.txt` or `.md`. It works at paragraph and table level — it is not a layout engine.

## When to Use

- Extract the text of a Word document the user attached or pointed at.
- Produce a `.docx` deliverable from structured content (headings, paragraphs, tables).
- Append a section or a results table to an existing report.
- Convert a `.docx` to Markdown so it can be read, diffed or committed.
- Inspect table structure before extracting values.

## When NOT to Use

- Plain text, Markdown or code files → `file_read` / `file_edit`.
- PDFs → `pdf`; spreadsheets → `spreadsheet`; slide decks → `presentation`.
- `.odt`, `.rtf`, or legacy binary `.doc` — unsupported; convert them externally first.
- Pixel-precise layout, character-level styles, headers/footers, or tracked changes.
- A one-line fix in a plain-text file.

## Tools You Use

| Task | Tool | Action / key params |
|---|---|---|
| Structured read | `document` | `action=read`, `file_path` |
| Plain text | `document` | `action=extract_text`, `file_path` |
| Author / counts | `document` | `action=info`, `file_path` |
| New document | `document` | `action=create`, `file_path`, `content` |
| Append text | `document` | `action=add_text`, `file_path`, `text` |
| Append table | `document` | `action=add_table`, `file_path`, `data` |
| Table preview | `document` | `action=list_tables`, `file_path` |
| Convert to text/md | `document` | `action=convert`, `source`, `target` |
| Search saved output | `grep` | over the converted file |

### Parameter Notes

- `create` writes a **new** file — it does not merge into an existing one. Use `add_text` / `add_table` to append.
- Markdown-style prefixes in `content` / `text` become real paragraphs: `#`, `##`, `###` are headings, `-` / `*` are bullets. This is textual, not literal text.
- `add_table` expects uniform row widths; ragged rows produce a malformed table.
- `convert` supports only `.txt` and `.md` targets. Never point `target` at `.docx` or `.pdf`.
- `info` reports metadata (author, title, timestamps, counts), not content.

## Workflows

### Read a document

1. `document` → `action=info` for scale and authorship.
2. `action=read` when headings and tables matter, `action=extract_text` when feeding content downstream.
3. Large document: read section by section and summarize as you go.

### Create a report

1. Outline it first: title, sections, where each table goes.
2. `action=create` with a `#`/`##` skeleton and bullets.
3. Append results with `action=add_table` using uniform rows.
4. Verify with `action=read` or `action=list_tables`, then give the user the path.

### Append to an existing report

`action=add_text` for prose, `action=add_table` for data. Never use `create` on an existing path.

### Convert to Markdown

1. `action=convert` with `source` and a `.md` `target`.
2. `file_read` the result and check tables survived.
3. Fix anything that did not, with `file_edit`.

## Examples

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

- Build documents top-down: `create` the skeleton, then `add_text` sections, then `add_table` results.
- Use real headings rather than bold text, so the outline is navigable.
- Keep tables narrow; wide Word tables become unreadable when converted to Markdown.
- Check `info` before touching a file that may carry authorship metadata the user cares about.
- Always verify after writing — `create` overwrites without warning, and appends can land in the wrong section.
- Convert to `.md` anything that will be reviewed or version-controlled.

## Common Pitfalls

- Using `file_read` on a `.docx` and concluding it is unreadable — it is a zip archive.
- Passing a `.doc` or `.odt` file; python-docx only understands `.docx`.
- Pointing `convert` at a `.pdf` or `.docx` target and expecting a rendered document.
- Feeding ragged `data` rows and getting a table with missing cells.
- Losing formatting: `content` / `text` only encode headings and bullets, not bold runs, colors or alignment.
- Reading a very large document with `read` when only one summary paragraph was needed.

## Anti-patterns

- Round-tripping a `.docx` through `convert` → edit Markdown → re-`create` when the original formatting matters.
- Generating a Word file when the deliverable should be Markdown or PDF.
- Appending the same section twice because the first append was never verified.
- Using `document` on a spreadsheet renamed to `.docx`.

## Pairing with Other Tools

- `pdf` — read or produce the PDF version of the content.
- `spreadsheet` — move a document's tables into a real `.xlsx` when the numbers are the focus.
- `presentation` — slide decks.
- `file_read` — inspect the converted `.md` / `.txt` output.
- `grep` — search across extracted text for a term.
- `diagram` / `chart` — produce images to place in the report by hand.
