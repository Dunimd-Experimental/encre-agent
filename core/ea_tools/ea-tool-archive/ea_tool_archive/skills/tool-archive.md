---
name: tool-archive
description: "Create, extract, list, or inspect zip/tar/gz/bz2/xz archives with one structured interface instead of shelling out."
hidden: true
context: inline
---

# tool-archive

One interface over `zip`, `tar`, `gz`, `bz2`, and `xz`. It creates archives from files or whole directories (recursive), extracts them, lists entries, and reports archive metadata as structured JSON rather than raw CLI text.

## When to Use

- You need to package a directory or a set of files into a single artifact for delivery, upload, or backup.
- You need to inspect an archive before trusting it: `action: "list"` gives you entry names and sizes, `action: "info"` gives format, size, and entry count.
- You must unpack an archive whose format you only know from the extension.
- You want consistent behavior across formats without remembering tar's flag alphabet or zip's quirks.

## When NOT to Use

- Compressing a single file for transport where no container is needed — a plain `terminal` call to `gzip` may be all you want, though `archive` with `format: "gz"` also works.
- Reading the contents of a file *inside* an archive repeatedly; extract once, then use `file_read` on the extracted path.
- Backing up a live directory that other processes are writing to — take a filesystem-level snapshot instead.
- Compressing huge trees where you only need a checksum; use `hash_crypto` instead.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | `create`, `extract`, `list`, or `info`. |
| `archive_path` | string | yes | Path to the archive file. For `create`, parent directories are created automatically. |
| `source_paths` | array | yes for `create` | Files or directories to archive. Directories are added recursively. |
| `dest_dir` | string | no | Extraction target. Defaults to the archive's own parent directory. |
| `format` | string enum | no | `zip` (default), `tar`, `gz`, `bz2`, `xz` — used when creating. |
| `compression` | string enum | no | For tar archives: `""`, `gz`, `bz2`, `xz`. Overrides the compression implied by `format`. |

### Parameter Notes

- `format` and `compression` overlap for tarballs. `format: "tar"` + `compression: "xz"` produces `.tar.xz`; `format: "xz"` is the shorthand for the same thing.
- Set `format` explicitly on `create`. Inferring the format from the filename is ambiguous (`foo.gz` could be a gzipped single file or a gzipped tar).
- `dest_dir` is created if missing; extraction never escapes it with `../` entry names.
- Relative paths in the archive are stored as given, so an absolute `source_paths` entry produces absolute-looking member names — prefer running from the parent directory and passing relative paths.

## Example

```json
{
  "action": "create",
  "archive_path": "D:\\encre\\dist\\harness-docs.tar.xz",
  "source_paths": ["harness/encre/skills/builtin", "harness/encre/loop_skills.py"],
  "format": "tar",
  "compression": "xz"
}
```

Inspect before extracting:

```json
{
  "action": "list",
  "archive_path": "D:\\encre\\dist\\harness-docs.tar.xz"
}
```

## Best Practices

- Always `list` an archive that came from outside the workspace before `extract`; you learn the layout and spot absolute or traversal paths early.
- Extract into a dedicated empty directory (`dest_dir`) instead of the repo root so you can delete or diff the result as a unit.
- Choose `zip` when the artifact must open on Windows without extra tooling, `tar.xz` for the smallest size, `tar.gz` for the best compatibility/speed trade-off.
- Keep `source_paths` relative and run from a stable directory so archives are reproducible.
- After extraction, verify the expected top-level entry exists — a wrong `dest_dir` silently produces an empty directory.

## Common Pitfalls

- Forgetting `source_paths` on `create`; the action needs an explicit input list, it does not default to the current directory.
- Assuming `extract` overwrites safely. Existing files may be replaced — check `list` output for names that collide with real project files.
- Passing a directory as `archive_path` (e.g. a path ending in `\`); the tool writes a file, not a tree.
- Expecting `info` to report per-entry compression ratios; it reports archive-level metadata only.
- Using `zip` for very large files (>4 GB) without verifying the zip64 reader on the target machine.
- Extracting a tarball whose entries were created with absolute paths — members land outside `dest_dir` conceptually even though the extractor sanitizes them.

## Anti-patterns

- `terminal` + `tar czf` when `archive` is available: you lose structured errors and cross-platform flag differences for no gain.
- Extracting an untrusted archive straight into `C:\` or the repo root to "see what's inside".
- Re-creating the same archive on every step because you forgot you already built it.
- Nesting archives (a zip containing tars) to work around a missing format — pick the right `format`/`compression` instead.

## Pairing with Other Tools

- `terminal` — for `gzip`-level operations on single streams, or when you need a format the tool does not cover (7z, rar).
- `file_read` / `file_write` — after extraction, to work with the unpacked content.
- `cloud_storage` — to upload the produced archive or download one to extract.
- `hash_crypto` — to checksum the archive for integrity verification.
- `diff` — to compare two extracted trees.
