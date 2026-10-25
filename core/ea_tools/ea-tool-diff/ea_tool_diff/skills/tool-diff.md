---
name: tool-diff
description: "Structured diff for text, files, or directory trees: unified/context/JSON/HTML, or change statistics."
hidden: true
context: inline
---

# tool-diff

Compares two things and returns a structured result: line diffs for text and files, a tree comparison for directories, SHA-256 comparison for binary content, and a statistics mode when you only want magnitudes. Prefer it over a shell `diff` when you need machine-readable output or directory/binary modes.

## When to Use

- Checking what changed between two versions of a file or two directories (e.g. `dist/` vs `build/`).
- Producing a reviewable unified diff for a patch or PR comment.
- Measuring how different two texts are (similarity ratio) without reading the diff.
- Verifying a rewrite preserved intended content — diff the old and new files.

## When NOT to Use

- Applying a change — `diff` only reads; use `apply_patch` or `file_edit` to modify.
- Comparing version control history; use `git diff` semantics through `git`.
- Comparing huge trees when you care about two files; scope to `file` mode.
- Semantic/AST comparison; this is line-based (with SHA-256 for binaries).

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | `text`, `file`, `directory`, `statistics`. |
| `text1` | string | for `text` | First text content. |
| `text2` | string | for `text` | Second text content. |
| `file1` | string | for `file`/`directory` | First file or directory. Optional for `statistics`. |
| `file2` | string | for `file`/`directory` | Second file or directory. Optional for `statistics`. |
| `context_lines` | integer | no | Context lines around each change (default 3). |
| `ignore_case` | boolean | no | Case-insensitive comparison (default `false`). |
| `ignore_whitespace` | boolean | no | Ignore whitespace differences (default `false`). |
| `output_format` | string enum | no | `unified` (default), `context`, `json`, `html` — for `text`/`file`. |

### Parameter Notes

- `statistics` accepts either text pairs or file paths; if both are supplied the file inputs win.
- Binary files under `file` mode are compared by SHA-256 (equal / differ) rather than line by line.
- `directory` mode classifies entries as only-in-left, only-in-right, differing, and identical — it recurses.
- `output_format` is ignored by `directory` and `statistics`.
- `ignore_whitespace` covers indentation and trailing spaces; it does not normalise line endings unless they produce whitespace-only changes.
- `json` output is the one to use when you intend to post-process counts programmatically.

## Example

```json
{
  "action": "file",
  "file1": "harness/encre/loop_skills.py.bak",
  "file2": "harness/encre/loop_skills.py",
  "context_lines": 5,
  "output_format": "unified"
}
```

```json
{
  "action": "directory",
  "file1": "dist/vendor",
  "file2": "build/vendor"
}
```

```json
{
  "action": "statistics",
  "file1": "docs/old-guide.md",
  "file2": "docs/new-guide.md"
}
```

## Best Practices

- Use `statistics` first on big comparisons; if similarity is ~1.0, skip reading the diff.
- Set `ignore_whitespace: true` when comparing generated vs formatted files to avoid noise from reformatting.
- Keep `context_lines` small (3–5) for review output, larger only when the change is hard to locate.
- For directories, diff scoped subtrees rather than an entire repo root.
- Pair a diff with `file_read` on the changed region when the diff alone is ambiguous.

## Common Pitfalls

- Passing directories to `file` mode (or files to `directory`) — the action decides the semantics, so mismatch errors are confusing.
- Expecting binary content to be shown; only equality is reported.
- Reading a `html` diff in a terminal transcript; use `unified` or `json` for model consumption.
- Forgetting that line endings count as differences unless whitespace is ignored.
- Treating a high similarity ratio as semantic equivalence (reordered code scores low, renamed identifiers score high).

## Anti-patterns

- Diffing two enormous trees "just to see" instead of comparing specific artefacts.
- Using `diff` to detect whether a command changed anything, when a checksum or `git status` is cheaper.
- Piping a diff into an edit operation by hand; use `apply_patch` semantics instead.

## Pairing with Other Tools

- `file_read` — inspect the actual content around a hunk.
- `apply_patch` — turn a reviewed diff into a change.
- `git` — history-aware diffs and working-tree status.
- `hash_crypto` — whole-file integrity checks where a line diff is overkill.
- `lint_format` — to explain diffs caused purely by formatting.
