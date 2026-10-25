---
name: pdf
description: Extract per-page text and metadata from born-digital PDFs with pdfplumber/PyPDF2, with page ranges, chunked reading, and a scan detection fallback.
aliases:
  - pdf-doc
  - pdf-reader
when_to_use: ".pdf"
argument_hint: "[path to PDF file or task description]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

# PDF

`pdf` reads text-based PDFs: `read` / `extract_text` for per-page text (pdfplumber first, PyPDF2 as fallback) and `metadata` for page count plus title, author, producer and creation date. It is read-only and does not render pages.

## When to Use

- Read a report, paper, contract, spec or manual stored as PDF.
- Pull one section instead of the whole file: `pages="3-7"`.
- Locate a phrase across pages before quoting it.
- Check page count and metadata before deciding how to process the file.
- Turn extracted text into a summary, a `document`, or a table in `spreadsheet`.

## When NOT to Use

- Scanned or photographed pages — no text layer; `pdf` returns empty or garbled text.
- Editing, merging, splitting, annotating or form-filling — this tool is read-only.
- Rendering pages to images — not supported here.
- Office documents (`.docx`, `.xlsx`, `.pptx`) — use `document`, `spreadsheet`, `presentation`.
- Raw `.doc`, `.odt`, `.rtf`, `.rtf` or a PDF renamed from another format.

## Tools You Use

| Task | Tool | Action / key params |
|---|---|---|
| Page text | `pdf` | `action=read` (or `extract_text`, same thing), `file_path`, `pages` |
| Page count + metadata | `pdf` | `action=metadata`, `file_path` |
| Search extracted text | `grep` | over the file saved by `file_write` |
| Keep a clean transcript | `file_write` | write extracted pages to `.txt`/`.md` |
| Read a source PDF into a doc | `document` | `action=create`, `content` |
| Restructure tabular text | `spreadsheet` | `action=write`, `data` |

### Parameter Notes

- `pages` is **1-indexed** and accepts ranges, comma lists, or both: `1-5`, `1,3,5`, `1-3,7`.
- Without `pages`, only the first 50 pages are read — later pages need an explicit selector.
- `metadata` ignores `pages`.
- There is no password parameter; encrypted PDFs fail to open.

## Workflows

### Read a PDF

1. `pdf` → `action=metadata` to learn the page count first.
2. `pdf` → `action=read` with `pages` scoped to what you need, in chunks of 5–15 pages.
3. If the text comes back empty or garbled, the file is a scan — stop and say so.
4. Quote page numbers back to the user (`p. 12`) so citations are checkable.

### Large document

Never extract wholesale. Size it with `metadata`, then read only the ranges that matter. Keep what you have already read; do not re-read the same pages.

### Scanned PDF

`pdf` cannot render or OCR. If the user needs a scan's content, render pages to images with `terminal` (for example `pdftoppm`), then OCR each image with `image` — and report that this requires the external renderer.

### PDF to text file

1. Read the pages in chunks.
2. Normalize whitespace; layout inserts line breaks that will confuse comparisons.
3. `file_write` the transcript, then `grep` it for verification.

## Examples

```json
{ "action": "read", "file_path": "D:/docs/architecture.pdf", "pages": "2-6" }
```

```json
{ "action": "metadata", "file_path": "D:/docs/architecture.pdf" }
```

```json
{ "action": "extract_text", "file_path": "D:/docs/contract.pdf", "pages": "1-3,7" }
```

## Best Practices

- `metadata` before any extraction — page count changes your whole strategy.
- Read in chunks of 5–15 pages, never the whole document.
- Use page selectors when quoting, so every citation carries a page number.
- Prefer `spreadsheet` over extracting tables from PDF text when the real table exists elsewhere.
- Verify by re-reading a small range rather than trusting the first pass.

## Common Pitfalls

- Extracting a 300-page manual in one call and flooding context.
- Assuming empty text means an empty document — it usually means a scan.
- Multi-column layouts interleaving text in the wrong reading order.
- Tables coming out as loose cell fragments instead of rows.
- Re-reading pages you already read.
- Encrypted or password-protected PDFs failing with no useful hint.
- Comparing extracted text to a known string without normalizing whitespace.

## Anti-patterns

- Dumping a whole document into context because one page was missing.
- Using `pdf` on a `.docx` renamed to `.pdf`.
- Promising to render, annotate or edit a PDF.
- Silently falling back to guessing content that the extraction cannot produce.

## Pairing with Other Tools

- `image` — OCR rendered pages of scanned PDFs (after `terminal` renders them).
- `document` — turn extracted text into a shareable Word file.
- `spreadsheet` — when the source data exists as a real table.
- `grep` — search across a saved transcript for a term.
- `file_write` — persist a cleaned transcript of the pages.
