---
name: tool-file-read
description: File reading skill. file_path/offset/limit pagination, as_image for images, max_pages for PDF, when to read vs grep
hidden: true
context: inline
---

## When to Use
- Read content of a file at a known path
- View code, config, docs, logs
- Read images (as_image), PDFs (max_pages pagination)

## When NOT to Use
- **Search code by content** -> `grep` (grep searches, read reads a known file)
- **Find files by name** -> `glob`
- **Jump to definition / references** -> `lsp`
- **Re-reading an already-read file**: content is in the conversation; re-reading wastes a turn

## Key Parameters
- `file_path` (required): absolute or workspace-relative path
- `offset`: starting line (0-based), read from the middle of a file
- `limit`: number of lines. For large files, read a small range first to see structure, then read targeted sections
- `as_image`: true to read images/PDF pages as images (multimodal understanding of charts/screenshots)
- `max_pages`: PDF page cap (large PDFs must set this or the call fails)

## Best Practices
- For large files, read the head first without offset/limit to see structure, then offset into specific sections
- PDFs must set `max_pages` (e.g. `max_pages: 5`); huge PDFs without a page cap fail or time out
- For charts/screenshots/scans use `as_image: true`
- Reuse already-read content; do not re-read

## Common Pitfalls / Anti-patterns
- **Reading a huge file fully**: burns tokens. Probe with limit first, then targeted reads
- **PDF without max_pages**: large PDFs fail or time out
- **Using file_read to search content**: searching is grep's job; read is for known files
- **Re-reading the same file**: content is in context; repeated reads waste turns
- **Reading binary as text**: garbles; use as_image

## Pairing with Other Tools
- `grep`: grep to locate a line number, then file_read with offset to read precisely
- `glob`: glob to find the path, then read
- `lsp`: definition/reference lookup beats file_read rummaging
- `file_edit`: read then edit