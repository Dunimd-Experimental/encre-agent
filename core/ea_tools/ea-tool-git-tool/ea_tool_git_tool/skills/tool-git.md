---
name: tool-git
description: "Run local git subcommands (status, diff, log, branch, add, commit, push, pull, stash, checkout, clone) against a repository from any working directory."
hidden: true
context: inline
---

# tool-git

`git` shells out to the real Git CLI in a target repository, so output, exit codes and flag semantics match what a developer would see in a terminal. Use it for every local version-control operation: inspecting working-tree state and history, staging and committing, branching and checking out, stashing, syncing with a remote, or cloning a repository. Provider-side work (issues, PRs, releases, CI runs) belongs to `github`, not here.

## When to Use

- Check what changed before or after an edit: `command="status"`, `command="diff"`, `command="log"`.
- Record work: `add` the intended paths, then `commit` with a conventional `-m` message.
- Inspect or manipulate branches: list with `branch`, switch with `checkout`.
- Fetch/publish commits: `pull` before starting, `push` after committing.
- Set work aside mid-task: `stash`, then continue with a clean tree.
- Bring an external repository into the workspace with `clone`.

## When NOT to Use

- Issues, pull requests, reviews, releases, gists, Actions — use `github` (works for GitHub **and** GitLab).
- Reading or editing files — use `file_read` / `file_write` / `apply_patch`; `git show` through this tool is clumsy for content.
- Searching code across history or the tree — use `grep` for the working tree; use `git` + `log` only when you specifically need commit history.
- Large binary assets or submodule-heavy repos: clones and fetches can exceed the 120s timeout.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `command` | string | yes | The git subcommand. Enum: `status`, `diff`, `log`, `branch`, `commit`, `add`, `push`, `pull`, `stash`, `checkout`, `clone`. |
| `repo_path` | string | no | Path to the target repository. Defaults to the current working directory — always pass it when the repo is elsewhere. |
| `args` | string | no | Extra flags appended to the subcommand, shell-split with `shlex` (e.g. `"--oneline -n 20"`, `'-m "fix: typo"'`). |

### Parameter Notes

- `args` is **shlex-split**, not passed to a shell: quoting works for spaces (`-m "fix: typo"`), but pipes, redirections, `&&` and `$VAR` do **not** — those need a real shell via `terminal`.
- Every call gets a 120s timeout; slow clones, big fetches and pushes can be killed.
- `commit` without `-m` may open an editor and hang until the timeout — always pass a message.

## Example

```json
{
  "command": "log",
  "repo_path": "D:/encre",
  "args": "--oneline -n 10 --author=team"
}
```

## Best Practices

- Sequence state-changing steps explicitly: `status` → `add <paths>` → `commit -m "..."` → `push`. Do not assume staging.
- Stage specific paths rather than everything; a bare `add` in `args` (e.g. `-A`) can sweep in unrelated user work.
- Use `--oneline -n N` for log reads; full logs flood context.
- Pass `repo_path` as an absolute path so results are reproducible regardless of the session cwd.
- Prefer `git` over `terminal` for git: this tool returns structured output and keeps quoting predictable.

## Common Pitfalls

- Destructive flags are **not** guarded: `reset --hard`, `push --force`, `clean -fd`, `checkout -- .` will silently discard work. Confirm intent before using them.
- `pull` with local changes either merges or fails depending on config — check `status` first, or `stash` beforehand.
- `checkout` doubles as branch creation only with `-b` in `args`; without it, an unknown branch fails.
- Authentication for `push`/`pull` comes from the environment (credential helper, SSH agent); a failure here is usually auth, not the tool.
- Output can be long on `diff` of large trees; narrow with pathspecs in `args`.

## Anti-patterns

- Using `terminal` with `git ...` strings when this tool exists.
- Force-pushing to a shared branch to "fix" history.
- Committing generated artifacts or secrets because the whole tree was staged.
- Using `git` to read issue/PR metadata — that is `github`.

## Pairing with Other Tools

- `grep` / `glob` — locate the files first, then `git diff` those paths.
- `apply_patch` / `file_edit` — make changes, then `git diff` to review and `commit`.
- `github` — after `push`, open the PR or comment on the issue.
- `terminal` — for anything needing a shell pipeline this tool cannot run.
