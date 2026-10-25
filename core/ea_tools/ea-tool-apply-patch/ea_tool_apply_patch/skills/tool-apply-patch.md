---
name: tool-apply-patch
description: "Apply a unified git-style diff across multiple files atomically; all hunks validate before anything is written."
hidden: true
context: inline
---

# tool-apply-patch

Applies a real unified diff (the `git diff` format) to the working tree. Every file is recomputed in memory first and nothing is written unless all hunks in all files validate, so a single bad hunk leaves the tree untouched. This is the right tool when you already hold a patch, not when you are improvising an edit.

## When to Use

- You have a prepared multi-file patch (from a review comment, a PR, a generated diff, or your own earlier reasoning recorded in the conversation).
- A known-good edit sequence must be replayed across many files or across several environments.
- You want an all-or-nothing change: either every file lands or none does.
- The edit is textually delicate (renames, deletions, no-newline-at-EOF markers) and line-oriented edits would lose fidelity.

## When NOT to Use

- One or two small edits in one file: `file_edit` is simpler and gives better error messages.
- You do not have the current file content. Never generate a patch from memory — read the file first (`file_read`), then patch.
- The target is a binary file; binary diffs are rejected.
- You need to create a file with new content from scratch: `file_write` is the direct path.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `patch` | string | yes | Unified diff text. Must include `---` / `+++` markers and `@@` hunk headers. Paths are relative to `root`. |
| `root` | string | no | Directory the patch paths resolve against. Defaults to the current working directory. Paths escaping this root are rejected. |
| `dry_run` | boolean | no | Validate and report per-file results without touching disk. Default `false`. |

### Parameter Notes

- Atomicity is real: validation happens for all files before the first write, so a failure in file 7 does not leave files 1–6 modified.
- Fuzzy placement tolerates roughly 200 lines of line-number drift and whitespace/indentation differences, using hunk context as anchors.
- Line endings from the original file (CRLF / LF / CR) are preserved; the patch does not rewrite them.
- Existing destination files are never silently overwritten — the patch must describe the content it expects.
- Path traversal is refused: `../` outside `root` fails the whole patch.

## Example

```json
{
  "root": "D:\\encre",
  "dry_run": true,
  "patch": "--- a/harness/encre/loop_skills.py\n+++ b/harness/encre/loop_skills.py\n@@ -150,6 +150,12 @@ class SkillManager:\n     def render_skill_catalogue(self) -> str:\n-        skills = [s for s in self.registry.list_all() if s.user_invocable and not s.name.startswith(\"tool-\")]\n+        skills = [\n+            s\n+            for s in self.registry.list_all()\n+            if s.user_invocable and not s.name.startswith(\"tool-\")\n+        ]\n         return self._format(skills)\n"
}
```

## Best Practices

- Run with `dry_run: true` first on anything larger than a couple of files; the per-file report tells you exactly which hunk failed.
- Keep hunks small and context-rich (3 lines of leading/trailing context is the sweet spot for fuzzy matching).
- Read the file immediately before composing the patch. A patch built from a stale read fails by design.
- Split unrelated changes into separate patches so a failure in one concern does not block the other.
- Verify after a real apply: re-read the changed region or run the project's formatter/linter.

## Common Pitfalls

- Hand-writing `@@` line numbers that do not match reality; the fuzzy matcher helps but cannot rescue wrong content.
- Omitting the trailing-newline marker (`\ No newline at end of file`) when the original lacked one — the file may gain a newline you did not intend.
- Assuming a failed apply partially succeeded and "fixing" files that were never touched. Check the result report first.
- Using Windows backslash paths inside the patch body; hunk paths use forward slashes, only `root` is a native path.
- Patching a file that another process (or a running app) has open and is rewriting.
- Building one giant patch for ten unrelated features; when it fails you learn almost nothing.

## Anti-patterns

- "I'll just write the patch from the description in the issue" — without reading the file this is a guess, and the tool exists precisely to refuse guesses.
- Applying a patch with `root` set to the repo top when every path in the patch is already repo-relative *and* prefixed — you double the prefix and every hunk misses.
- Retrying the identical patch after a failure. The content drifted; re-read and regenerate.
- Using `apply_patch` as a bulk find-and-replace engine for a token that appears everywhere.

## Pairing with Other Tools

- `file_read` — mandatory before authoring the patch; also the fastest post-apply verification.
- `file_edit` — for single-file, few-line changes.
- `file_write` — for new files or full rewrites, which do not need diff semantics.
- `diff` — to review what a patch actually changed before committing it.
- `git` — to stage, inspect, or revert the result.
- `lint_format` / `test_run` — to confirm the applied patch still builds and passes.
