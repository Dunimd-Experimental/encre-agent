---
name: tool-grep
description: "Regex content search with ripgrep semantics (context lines, type/glob filters, literal and whole-word modes, paging, three output modes)."
hidden: true
context: inline
---

# tool-grep

`grep` searches file contents for a regular expression, using ripgrep when available and a Rust-native fallback with the same flag set. Use it instead of shelling out to `grep`/`rg`: results come back structured, flags are ripgrep-compatible, and paging keeps large searches out of the context window.

## When to Use

- Locate a symbol, string or error message: `pattern="def build_tool"`, `pattern="TODO"`.
- Find all call sites before a refactor: `pattern="render_skill_catalogue\("`, `output_mode="content"`.
- Just need to know *where* matches live: `output_mode="files_with_matches"`, then read specific files.
- Measure spread: `output_mode="count"` shows per-file match counts.
- Reach into multi-line constructs: `multiline=true` with `-C` context.
- Filter by language or path shape with `type` and `glob`.

## When NOT to Use

- Listing files by name only — use `glob`.
- Semantic/approximate code understanding — read the file with `file_read`.
- Searching agent memory — use `memory_search`.
- Searching the public web — use `web_search`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `pattern` | string | yes | Regular expression to search for. |
| `path` | string | no | File or directory to search; defaults to the session cwd. |
| `glob` | string | no | File filter, e.g. `*.py` or `**/*.ts`. |
| `type` | string | no | Type alias: `py`, `python`, `js`, `ts`, `json`, `md`, `css`, `html`, `yaml`, `rust`, `go`, `java`, `c`, `cpp`, `sql`. |
| `-i` | boolean | no | Case-insensitive. |
| `-n` | boolean | no | Include line numbers (default true in content mode). |
| `-F` | boolean | no | Treat `pattern` as a literal string. |
| `-w` | boolean | no | Whole-word matches only. |
| `-A` | integer | no | Lines of context after each match. |
| `-B` | integer | no | Lines of context before each match. |
| `-C` | integer | no | Context before and after; overrides `-A`/`-B`. |
| `multiline` | boolean | no | `.` matches newlines and patterns may span lines. |
| `head_limit` | integer | no | Cap output lines / files / counts. |
| `offset` | integer | no | Skip the first N output entries (paging). |
| `output_mode` | string | no | `content` (default), `files_with_matches`, `count`. |

### Parameter Notes

- Names are the flags themselves: pass `"-i": true`, not `"ignore_case": true`. Keys with a leading dash are valid JSON string keys.
- `-C` wins over `-A`/`-B`; setting all three is redundant.
- `offset` + `head_limit` is the paging pair: page with `offset=100, head_limit=100`, not by rewriting the pattern.
- `type` and `glob` compose (AND); `path` still limits the root.
- In `files_with_matches` and `count`, `-A`/`-B`/`-C` have no effect.

## Example

```json
{
  "pattern": "async def (_collect_tool_skill|_render_tool_skill_index)",
  "path": "D:/encre/harness/encre",
  "type": "py",
  "-n": true,
  "-C": 2,
  "head_limit": 60
}
```

## Best Practices

- Start broad in `files_with_matches` mode, then switch to `content` on the narrowed file set.
- Always cap with `head_limit` when the pattern could match hundreds of places.
- Add `-F` for literal strings containing regex metacharacters (dots in filenames, `[]`, `(`, `\`).
- Scope with `path` + `type` before adding context lines; context multiplies output size.
- Escape backslashes for JSON: a regex `\bfoo\b` is `"\\bfoo\\b"`.

## Common Pitfalls

- Regex dialect differences: the native fallback supports the same flags but not every ripgrep-only feature (e.g. PCRE lookarounds).
- Binary files are skipped; a "missing" match may be inside a binary or an ignored directory.
- `multiline=true` without `head_limit` can return enormous blocks.
- Ignored paths (`.gitignore`, hidden dirs) are not searched by default — point `path` at them explicitly if needed.
- A pattern with unbalanced brackets fails as an invalid regex; validate with `-F` first to confirm the text exists at all.

## Anti-patterns

- Shelling out to `grep`/`rg`/`findstr`.
- Searching the whole workspace with a single-letter pattern.
- Using grep to list files (that is `glob`).
- Paging by rerunning the same query and manually ignoring earlier hits instead of using `offset`.

## Pairing with Other Tools

- `glob` — enumerate candidates, then grep within them.
- `file_read` — open the file around a reported line number.
- `apply_patch` / `file_edit` — change every site found.
- `memory_search` — when the target is remembered conversation state rather than files.
