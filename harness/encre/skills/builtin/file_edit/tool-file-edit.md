---
name: tool-file-edit
description: Precise file editing skill. old_str/new_str replacement, replace_all batch, edits multi-spot, when to use vs apply_patch
hidden: true
context: inline
---

## When to Use
- Modify a specific fragment of an existing file (most common)
- Replace a block of code/config/text
- Multiple independent edits via `edits` in one call

## When NOT to Use
- **Large-scale rewrite of the whole file** -> `file_write` (full replacement is better)
- **Multi-file structured patch/diff** -> `apply_patch` (standard diff, cross-file)
- **Create a new file** -> `file_write`
- **Read a file** -> `file_read`

## Key Parameters
- `file_path` (required): target file, absolute or workspace-relative path
- `old_str` (required): text to replace. **Must be unique** and **exact character-for-character** (indentation/blank lines included). Non-unique fails; widen context to make it unique
- `new_str`: replacement content. Empty deletes code
- `replace_all`: true to replace every match (only when you really want all)
- `edits`: `[{old_str, new_str}, ...]` list for several independent spots in one call
- `dry_run`: preview without writing; confirm before the real edit

## Best Practices
- `file_read` first to get the exact text and indentation; copy old_str verbatim
- Give old_str enough context (1-2 surrounding lines) to be unique, but not too much
- Use `edits` for multiple spots in one call to save turns
- When unsure, `dry_run: true` to preview first

## Common Pitfalls / Anti-patterns
- **old_str not unique**: multiple matches fail. Widen context or use `replace_all` (if all should change)
- **Indentation/whitespace mismatch**: tabs vs spaces, trailing spaces cause match failure; copy verbatim
- **old_str too short**: e.g. just a function name may match many places; include the signature line
- **Editing without verifying**: after edit, `file_read` to confirm or run `test_run`/`lint_format`
- **Using file_edit to rewrite a large file**: large changes belong to `file_write` or `apply_patch`

## Pairing with Other Tools
- `file_read`: read before editing, verify after
- `apply_patch`: cross-file / large patches
- `file_write`: whole-file replacement
- `lint_format`/`test_run`: verify after edit