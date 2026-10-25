---
name: tool-pdf
description: "Extract per-page text or document metadata from text-based PDFs with pdfplumber, using PyPDF2 as a fallback."
hidden: true
context: inline
---

# tool-pdf

`pdf` reads PDF files: page text (`read` / `extract_text`) or document metadata plus page count (`metadata`). It uses pdfplumber first and falls back to PyPDF2, so it handles most born-digital PDFs but not scans. Always scope pages on large documents.

## When to Use

- Read a report, paper, contract or spec stored as PDF.
- Pull a specific section: `pages="3-7"` rather than the whole file.
- Find a phrase across pages before quoting it.
- Check page count and metadata (title, author, producer, creation date).
- Convert a PDF's text into a document or summary downstream.

## When NOT to Use

- Scanned image-only PDFs — no text layer; OCR the rendered pages with `image`.
- Editing, merging, splitting or annotating PDFs — read-only tool.
- Office documents (`.docx`, `.xlsx`, `.pptx`) — use `document`, `spreadsheet`, `presentation`.
- Rendering pages as images — not supported here.
- Audio/video subtitles — use `media`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `read` or `extract_text` (per-page text), `metadata` (info + page count). |
| `file_path` | string | yes | Path to the PDF (absolute or relative). |
| `pages` | string | no | 1-indexed selector: `1-5` for a range, `1,3,5` for a list. Defaults to the first 50 pages. |

### Parameter Notes

- `read` and `extract_text` are the same operation under two names.
- `pages` is 1-indexed and accepts ranges, comma lists, or both (`1-3,7`).
- Without `pages`, only the first 50 pages are read — later pages need an explicit selector.
- `metadata` ignores `pages`.

## Example

```json
{
  "action": "read",
  "file_path": "D:/docs/architecture.pdf",
  "pages": "2-6"
}
```

## Best Practices

- Call `metadata` first to learn the page count, then request the pages you need.
- Read in chunks (5–15 pages) instead of dumping the whole document into context.
- If the extracted text looks empty or garbled, the PDF is probably a scan — stop and OCR instead.
- Use page selectors when quoting, so citations carry a page number.
- Normalize whitespace when comparing extracted text to a known string; layout inserts line breaks.

## Common Pitfalls

- Scanned or photographed pages yield little or no text.
- Multi-column layouts interleave text across columns in reading order.
- Tables come out as loose cell fragments, not rows — use `spreadsheet` on the source if available.
- Encrypted/password-protected PDFs fail to open; there is no password parameter.
- Very large PDFs without `pages` produce enormous output and may be truncated.

## Anti-patterns

- Extracting a 300-page manual in one call.
- Assuming empty text means an empty document.
- Using `pdf` on a `.docx` renamed to `.pdf`.
- Re-reading the same pages instead of keeping the extracted text.

## Pairing with Other Tools

- `image` — OCR rendered pages of scanned PDFs.
- `document` — turn extracted text into a shareable doc.
- `spreadsheet` — when the source data exists as a real table.
- `file_write` — persist a cleaned transcript of the pages.
