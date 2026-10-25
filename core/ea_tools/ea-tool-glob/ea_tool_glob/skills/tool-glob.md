---
name: tool-glob
description: "Fast glob-pattern file discovery (e.g. \"**/*.py\") returning alphabetically sorted paths, skipping hidden and tooling directories."
hidden: true
context: inline
---

# tool-glob

`glob` matches file paths against a glob pattern and returns a clean, alphabetically sorted list. It is the fastest way to answer "what files exist here?" and should replace `ls`, `find`, `tree` and directory-listing shell calls. Hidden directories and common tooling folders (`.git`, `node_modules`, build output) are skipped automatically.

## When to Use

- Discover files by extension or name: `**/*.py`, `src/**/*.ts`, `data/*.csv`, `**/pyproject.toml`.
- Check whether a path exists before reading or editing it.
- Enumerate a package or module before planning a refactor.
- List config/docs at the top level: `*.md`, `*.yaml`.
- Scope a later `grep` to a subtree by first confirming the directory's contents.

## When NOT to Use

- Searching file **contents** — use `grep` (glob only matches path names).
- Reading a specific known file — use `file_read`.
- Listing a single small directory where you already know the names.
- Huge monorepos with patterns like `**/*` — thousands of paths waste context.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `pattern` | string | yes | Glob pattern; `**` recurses (`**/*.py`, `src/**/*.ts`). |
| `path` | string | no | Root directory to search; defaults to the current working directory. Absolute paths give reproducible results. |

### Parameter Notes

- `**` must be a full path segment: use `src/**/*.ts`, not `src**/*.ts`.
- `path` **scopes** the walk; it is not part of the pattern. To list a directory's children, pass it as `path` with `pattern="*"`.
- Results are paths only — no sizes, timestamps or contents.

## Example

```json
{
  "pattern": "**/*.md",
  "path": "D:/encre/core/ea_tools"
}
```

## Best Practices

- Narrow `path` to the smallest subtree that can contain the answer.
- Anchor the filename when you know part of it: `**/tool-*.md` beats `**/*.md`.
- Combine with `grep` afterwards on the returned file set instead of re-walking the tree.
- Use one pattern per call; several patterns means several calls, not a brace-everything pattern.

## Common Pitfalls

- `**/*` in a monorepo can return tens of thousands of paths and blow the context budget.
- Files inside hidden directories are never returned, so a `.encre/rules.md` must be requested by its parent path rather than found by a root-level `**/*.md`.
- Windows paths work with either separator, but forward slashes are safer inside patterns.
- Empty results usually mean the pattern's `**` placement is wrong, not that the files are missing.

## Anti-patterns

- Shelling out to `ls`/`dir`/`find` when this tool exists.
- Globbing to find text — that is `grep`'s job.
- Using glob to verify a file's *content* or size.

## Pairing with Other Tools

- `grep` — search inside the files glob just found.
- `file_read` — open a discovered path.
- `file_edit` / `apply_patch` — modify a file located by glob.
- `git` — check status of the discovered paths.
