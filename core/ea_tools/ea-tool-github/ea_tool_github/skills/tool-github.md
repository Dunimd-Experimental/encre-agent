---
name: tool-github
description: "GitHub and GitLab REST operations: issues, pull/merge requests, reviews, repos, releases, gists, and Actions workflow runs."
hidden: true
context: inline
---

# tool-github

`github` talks to the provider REST API (GitHub by default, GitLab via `platform="gitlab"`). One call performs exactly one operation, selected by `action`. Use it whenever the task lives *on the platform* rather than in the local clone: reading or triaging issues, opening and merging pull requests, posting reviews, browsing repositories, publishing releases, creating gists, or dispatching CI workflows.

## When to Use

- Triage: `issues_list` with `state`/`labels`, `issues_get`, or `issues_search` with a `query`.
- File or update work items: `issues_create`, `issues_update` (title, body, labels, state).
- Pull requests: `prs_list`, `prs_get`, `prs_files`, `prs_commits`, `prs_review`, `prs_merge`.
- Repository metadata: `repo_get`, `repo_list`, `repo_search`, `repo_create`.
- CI: `actions_list_workflows`, `actions_list_runs`, `actions_dispatch`.
- Quick sharing: `gists_create`; quota checks: `rate_limit`; identity: `user_info`.

## When NOT to Use

- Local commits, branches, pushes, diffs of the working tree — use `git`.
- Reading a file that already exists locally — use `file_read` or `grep`.
- Deep code review of a large diff: fetch with `prs_files` and read selectively; do not dump every file.
- GitLab-specific endpoints not in the action list are unsupported; fall back to `rest_client` only if truly required.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | Operation: `issues_list`, `issues_create`, `issues_update`, `issues_get`, `issues_search`, `prs_list`, `prs_create`, `prs_get`, `prs_merge`, `prs_review`, `prs_files`, `prs_commits`, `repo_search`, `repo_get`, `repo_create`, `repo_list`, `releases_list`, `gists_list`, `gists_create`, `actions_list_workflows`, `actions_dispatch`, `actions_list_runs`, `user_info`, `rate_limit`. |
| `platform` | string | no | `github` (default) or `gitlab`; selects API base URL and token env var. |
| `repo` | string | no | `owner/repo`. Required for nearly every repo-scoped action. |
| `token` | string | no | PAT for `Authorization`; falls back to `GITHUB_TOKEN`/`GH_TOKEN` or `GITLAB_TOKEN`/`GITLAB_PRIVATE_TOKEN`. |
| `issue_number` | integer | no | Required by `issues_get` / `issues_update`. |
| `pr_number` | integer | no | Required by `prs_get`, `prs_merge`, `prs_review`, `prs_files`, `prs_commits`. |
| `title` | string | no | Title for a new issue/PR. |
| `body` | string | no | Markdown body for issues, PRs, reviews, gists. |
| `head` | string | no | Source branch when creating a PR. |
| `base` | string | no | Target branch; defaults to `main`. |
| `state` | string | no | `open` / `closed` / `all` filter for listings. |
| `labels` | array | no | Label names to attach on create/update. |
| `query` | string | no | Search string for `issues_search` / `repo_search` (GitHub search syntax). |
| `name` | string | no | New repo name (no owner) for `repo_create`. |
| `description` | string | no | Description for a new repo or gist. |
| `private` | boolean | no | Create the repository private. |
| `files` | object | no | `{filename: content}` map for `gists_create`; at least one entry. |
| `public` | boolean | no | Make the gist publicly discoverable. |
| `workflow_id` | string | no | Workflow ID or filename (e.g. `deploy.yml`) for `actions_dispatch`. |
| `ref` | string | no | Branch/tag to run the workflow on; defaults to `main`. |
| `inputs` | object | no | Workflow inputs; must match the workflow's declared inputs. |
| `merge_method` | string | no | `merge` / `squash` / `rebase`. |
| `event` | string | no | Review verdict: `APPROVE` / `REQUEST_CHANGES` / `COMMENT` (GitHub only). |
| `per_page` | integer | no | Page size for list endpoints; clamped to 100. |

### Parameter Notes

- `action` is a closed set — there is no arbitrary-path passthrough. Pick the matching action instead of inventing one.
- `platform` changes both the URL base and the token source; `event` is GitHub-only, while GitLab maps `prs_*` onto merge requests.
- `per_page` is a cap, not paging: there is no cursor parameter, so narrow with `state`, `labels` or `query` instead of paging forever.

## Example

```json
{
  "action": "prs_create",
  "repo": "octocat/Hello-World",
  "title": "fix: handle empty config",
  "head": "fix/empty-config",
  "base": "main",
  "body": "Closes #42.\n\nGuards against a missing config section."
}
```

## Best Practices

- Prefer `GITHUB_TOKEN` in the environment over passing `token` in arguments (tokens in transcripts leak).
- Keep `per_page` small (10–30) for exploratory listings; raise it only when you truly need volume.
- Pass `repo` every time — omitting it is the single most common failure.
- When creating a PR, push the head branch first with `git`, then verify with `prs_create`'s returned URL.
- Read `prs_files` before `prs_review` so the review body cites real paths.

## Common Pitfalls

- Rate limiting: unauthenticated calls hit 60/hour. Check `rate_limit` when calls start returning 403.
- `base` defaults to `main`; repositories on `master` need it stated explicitly.
- `prs_merge` fails silently-ish on conflicts — inspect the response body for `mergeable: false` and report it instead of retrying.
- `actions_dispatch` returns no run ID; follow with `actions_list_runs` (filter by branch) to find it.
- Request timeout is 60s; large `prs_files` responses on huge PRs need a narrower read.

## Anti-patterns

- Using this tool to commit files (that is `git`).
- Opening a PR before the branch is pushed.
- Dumping a full issue thread and a full diff into context at once.
- Hard-coding a token into files or commands.

## Pairing with Other Tools

- `git` — commit and push the branch the PR is built from.
- `grep` / `glob` — locate code referenced in an issue before replying.
- `apply_patch` — fix the issue locally, then push and link the PR.
- `web_fetch` — read public documentation or raw files not covered by the API actions.
