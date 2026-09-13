---
name: tool-file-write
description: File write/create skill. file_path/content full replacement or new file, when to use file_write vs file_edit vs apply_patch
hidden: true
context: inline
---

## When to Use
- Create a new file
- Fully rewrite an existing file (large-scale change)
- Write generated content (script output, rendered templates)

## When NOT to Use
- **Change a small fragment of an existing file** -> `file_edit` (precise replacement, not full rewrite)
- **Cross-file structured patch** -> `apply_patch`
- **Append content** -> `file_edit` replacing a fragment that includes the append point (no append tool)

## Key Parameters
- `file_path` (required): target path. Parent directories are created if missing
- `content` (required): full file content. **Full replacement** (not append); overwrites existing content

## Best Practices
- Don't use it for small changes; use `file_edit` (full rewrite risks losing content, costs tokens)
- Confirm `content` is complete before writing (code especially: syntax, indentation, encoding)
- For new files, confirm the path and naming follow project conventions first
- Verify after writing: `file_read` or `lint_format`

## Common Pitfalls / Anti-patterns
- **Using file_write for a small change**: full rewrite is wasteful and error-prone; small edits belong to file_edit
- **Overwriting content you didn't inspect**: write replaces fully; read existing content first if unsure
- **Incomplete content**: missing lines / wrong indentation; always verify after write
- **Blindly overwriting an existing file**: may lose original content; confirm a full replace is intended

## Pairing with Other Tools
- `file_edit`: small changes
- `apply_patch`: multi-file patches
- `file_read`: verify after write
- `lint_format`: check syntax/format after write