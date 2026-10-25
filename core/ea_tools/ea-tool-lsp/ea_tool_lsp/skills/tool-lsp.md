---
name: tool-lsp
description: "Symbol-accurate code intelligence via a language server: definition, references, hover, diagnostics and document symbols at a cursor position."
hidden: true
context: inline
---

# tool-lsp

`lsp` asks the language server for the workspace what an IDE would know: where a symbol is defined, every reference to it, its type/signature, the file's diagnostics, and the document outline. Unlike `grep`, results are resolved by the language, so you get real definitions and complete reference sets instead of text matches. A server must be running for the file's language.

## When to Use

- Jump to the true definition of a symbol (`go_to_definition`) instead of guessing among grep hits.
- Enumerate all references before renaming or changing a signature (`find_references`).
- Read inferred type/signature/docs at a position (`hover`).
- Get compiler-grade errors and warnings for a file (`diagnostics`).
- Map a file's structure — classes, functions, fields — without reading all of it (`document_symbols`).
- Start/stop the server for a workspace (`initialize`, `shutdown`).

## When NOT to Use

- Plain text or regex search — `grep` is faster and needs no server.
- Languages without an initialized server; the call fails or returns nothing.
- Whole-repo architecture questions — combine `document_symbols` with `grep`, or read code.
- Editing code — this tool is read-only.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `operation` | string | yes | `go_to_definition`, `find_references`, `hover`, `diagnostics`, `document_symbols`, `initialize`, `shutdown`. |
| `file_path` | string | no | Absolute path to the target file; required for every operation except `initialize`/`shutdown`. |
| `line` | integer | no | **0-based** line of the cursor; used by definition/references/hover. |
| `character` | integer | no | **0-based** column of the cursor; used by definition/references/hover. |
| `workspace` | string | no | Absolute workspace root; required for `initialize` only. |

### Parameter Notes

- Coordinates are 0-based: a symbol on editor line 42, column 8 is `line=41, character=7`. Off-by-one lands on the wrong token and yields empty results.
- The cursor must sit **on** the identifier (usually its first character), not merely on the line.
- `initialize` takes only `workspace`; every query operation takes `file_path` plus cursor coordinates.
- `diagnostics` and `document_symbols` ignore `line`/`character`.

## Example

```json
{
  "operation": "find_references",
  "file_path": "D:/encre/harness/encre/loop_skills.py",
  "line": 153,
  "character": 4
}
```

## Best Practices

- Call `initialize` once per workspace before the first query, then reuse the server.
- Use `hover` first to confirm you are on the intended symbol before trusting references.
- Prefer `find_references` over `grep` for renames: it excludes comments, strings and unrelated same-name symbols.
- Use `document_symbols` to plan edits in a large file without reading it end to end.
- Shut down servers you started in throwaway workspaces to free resources.

## Common Pitfalls

- A server that is not installed or not configured for the language returns an error, not a partial result.
- Coordinates are 0-based while most editors display 1-based — convert deliberately.
- `find_references` depends on the project being loadable (dependencies installed); unresolved imports collapse the result set.
- Results can lag behind unsaved edits made outside the editor buffer.
- Diagnostics reflect only the queried file, not the whole project.

## Anti-patterns

- Using `lsp` for textual search or config-value lookup.
- Querying before `initialize`, then concluding the language is unsupported.
- Treating an empty reference list as proof a symbol is unused (check the server is healthy).
- Requesting diagnostics as a substitute for running the test suite.

## Pairing with Other Tools

- `grep` — fall back to text search when no server exists, and to cross-check results.
- `file_read` — read the definition location returned.
- `apply_patch` / `file_edit` — apply a rename across the reference set.
- `lint_format` — style and lint, which LSP diagnostics may not cover.
