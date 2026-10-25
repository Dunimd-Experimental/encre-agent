# ADR-0002: Branch switching does not restore file versions

- Status: **Accepted** (documents existing behaviour; no code change)
- Date: 2026-10-25
- Scope: `harness/encre/` session model, `core/encre/` rollback handlers.
- Supersedes the implicit assumption that "branches work like Git branches".

## Context

While fixing the rollback-edit bugs (`docs/audit/harness-baseline.md`,
`docs/adr/0001-kernel-ports.md`), the user reported that the branch system
"sometimes shows errors" and wanted the semantics aligned with Git. The
frontend/backend contract gaps that caused the *errors* were fixed in the
preceding batch (`session_id` now sent with every `rollback`; resync on
`no_session`/`busy`/`not_found`/`invalid_request`/`delete_error`; marker
lifecycle; renderer-id matching in `removeBranchMessages`).

One design question remained once the errors were gone: **does switching
branches restore file versions, the way `git checkout <branch>` restores the
working tree?** The current implementation does not — `switch_branch` rebinds
the active message lineage and runtime caches but leaves the workspace files
untouched.

## Decision

**Branch switching is a conversation-view operation, not a filesystem
operation.** Only `rollback_to` (which is destructive — it removes messages
and descendant branches) restores file versions. The two operations have
different contracts:

| Operation | Touches messages? | Restores files? | Why |
| --- | --- | --- | --- |
| `rollback_to` | yes (removes `seq > target` + descendant branches) | **yes** | the removed turns' effects must be undone |
| `switch_branch` | yes (rebinds active lineage) | **no** | non-destructive view change; the other branch's files may still be wanted |

This is a deliberate divergence from Git. Encre branches are conversation
forks, not filesystem snapshots: two branches share the same working directory,
and a user editing a file in branch A does not expect switching to branch B to
silently revert their edit. Restoring files on every switch would also be
expensive (a full workspace snapshot per branch) and surprising.

## Consequences

- Switching branches never silently changes the workspace. Users who want
  file restoration must use rollback (or a future explicit
  `checkout_files_at_branch` command, not yet built).
- The branch switcher UI is under no obligation to show file diffs; it shows
  message-lineage diffs only.
- `rollback_to` remains the single authority for file-version restoration.
  The commit chain (`EncreRollbackGit.file_states_asof_turn`) is consulted
  only on the rollback path.

## Alternatives considered

- **Restore files on branch switch (Git-like).** Rejected: branches are
  conversation forks sharing one working directory; restoring files on switch
  would revert in-flight edits the user made in the other branch. A workspace
  snapshot per branch is also expensive and the harness has no such store.
- **Snapshot the workspace per branch.** Rejected: the rollback commit chain
  (`EncreRollbackGit`) already snapshots files *per turn*, which is finer than
  per branch and is the right granularity for restoration. Adding a
  per-branch snapshot would duplicate that state and double the storage cost.

## Enforcement

- `harness/tests/test_rollback_files.py` pins the file-restore contract on
  `rollback_to` (shared-file restoration to commit state, only-in-removed-turn
  deletion, kept-turn invariance, descendant-branch wipe).
- There is no test asserting that `switch_branch` restores files, because it
  must not. If a future change adds file restoration to `switch_branch`, this
  ADR must be superseded.
