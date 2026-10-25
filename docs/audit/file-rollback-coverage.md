# File-version rollback — coverage audit

- Snapshot: 2026-10-25
- Scope: the file-rollback mechanism in `harness/encre/` (the fix for the
  user's "files don't roll back, unstable" symptom). This document records
  what is captured, what is not, and why each gap is a semantic decision
  rather than a bug.

## What is captured

A file snapshot is taken when **all three** conditions hold:

1. The tool call's name passes `_is_write_tool_name`
   (`harness/encre/utils/loop_helpers.py:381`): it is in
   `_WRITE_TOOL_NAMES = {"file_write", "file_edit", "write_file", "writeFile",
   "apply_patch"}`, **or** its lowercased name contains one of
   `write`/`edit`/`patch`/`delete`/`save`/`append` (the heuristic that lets
   plugin tools participate).
2. The write target path is resolvable. `extract_write_paths` reads:
   - for `apply_patch`: every `files[].{old_path, new_path}` descriptor;
   - for everything else: the first present key in
     `_WRITE_PATH_ARG_KEYS = (file_path, path, file, target_path,
     destination, output_path)`.
3. `path_guard.resolve_write_target` accepts the path for the session's
   sandbox (`harness/encre/session.py:561`).

The snapshot is the **pre-write** content (first 8 MiB, base64). A missing
file is stored as `""` (delete-on-rollback). The post-turn content is
recorded by the rollback commit chain at end-of-turn
(`harness/encre/loop/phases/end_of_turn.py:481` →
`EncreRollbackGit.commit`), keyed by `files_touched_since_commit`.

`rollback_to` (`harness/encre/session.py:1306`) prefers the commit-time state
(`file_states_asof_turn(self.id, self.turn_count)`) and falls back to the
pre-write snapshot for paths the commit chain does not cover.

## Coverage gaps (semantic decisions, not bugs)

### 1. Shell-driven file writes are not captured

`bash`/`bash_output`/`bash_kill`/`bash_list`/`process_list`/`process_kill`
can write files via shell redirection (`> file`, `tee`, `sed -i`, `mv`, etc.),
but their names contain no `write`/`edit`/`patch`/`delete`/`save`/`append`
token, and the harness does not parse shell output for paths. **A file
modified only through a shell tool is not restored on rollback.**

This is the most plausible source of the user's "sometimes unstable"
report for file rollback. The decision is to leave it out of scope: parsing
shell output for path mutations is unreliable and out of the rollback
subsystem's responsibility. A future `bash` integration could declare write
targets explicitly via the tool's input schema (a `writes_paths` field), which
the existing capture path would then honour.

### 2. `manage` has its own revert path

`manage` (installs/uninstalls) records lifecycle ops in an append-only
journal and is reverted by `revert_manage_ops_after_turn` — a separate path
from file snapshots. It works and is intentionally separate (package
state is not file content).

### 3. `memory_*` writes to the memdir store, not the workspace

Memory tool writes live in the memdir store, not the working directory, and
are purged by `purge_memory_after_turn` keyed by `turn`/`created_at`. This is
correct: memory is not part of the workspace and must not be restored as a
file.

### 4. Plugin tools with non-standard arg keys

A plugin tool named `pdf_export` with an arg `output` (not in
`_WRITE_PATH_ARG_KEYS`) would not be captured. Plugin authors who want their
writes to roll back should use one of the six recognised keys, or name the
tool with a `write`/`save`/`export`... token and use `output_path`.

### 5. 8 MiB snapshot cap

`capture_file_snapshot` reads at most 8 MiB. Larger files get a truncated
snapshot; the undo stack marks them `content_available: false`, but
`file_snapshots` still stores the truncated 8 MiB. A rollback would restore
the first 8 MiB and silently drop the rest. This is a real failure mode for
large files and is tracked as a follow-up (not yet fixed).

### 6. No `file_delete` builtin

`file_edit`/`file_write` do not delete files. A plugin `*_delete` tool would
be captured by the heuristic (name contains `delete`), and its pre-write
snapshot would be the file's content — restore-on-rollback would re-create
the file. This is correct behaviour for a delete tool.

## Pinned by regression tests

`harness/tests/test_rollback_files.py` (7 tests):

| Test | Pins |
| --- | --- |
| `test_shared_file_restored_to_rollback_point` | commit-state wins over pre-write snapshot for multi-turn files |
| `test_file_created_only_in_rolled_back_turn_is_deleted` | only-in-removed file is deleted |
| `test_file_touched_only_in_kept_turn_untouched` | kept-turn files are invariant |
| `test_no_commit_chain_falls_back_to_pre_write_snapshot` | no commits → pre-write snapshot (delete for new files) |
| `test_descendant_branch_wiped_on_rollback` | descendant branch + messages purged |
| `test_target_message_is_retained` | `seq > target` removed, target kept |
| `test_extract_file_paths_handles_apply_patch` | apply_patch multi-file old/new paths covered |

The gaps above are **not** pinned by tests, because they are documented
behaviour, not contracts.
