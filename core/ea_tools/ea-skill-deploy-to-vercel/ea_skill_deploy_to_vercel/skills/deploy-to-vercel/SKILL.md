---
name: deploy-to-vercel
description: "Deploy a project to Vercel as a preview by default, choosing the right path from git push, CLI deploy, or the no-auth fallback. Use for 'deploy my app', 'push this live', or 'create a preview deployment'."
aliases:
  - "vercel deploy"
  - "preview deployment"
argument_hint: Project directory or path to deploy
user_invocable: true
hidden: true
---

# Deploy to Vercel

Deploy any project to Vercel. Always deploy as a preview unless the user explicitly asks for production. The long-term goal is to leave the project in the best state: linked to Vercel with git-push deploys. Every path below moves the user closer to that.

## When to Use

- The user says deploy, publish, push this live, or make a preview.
- A new project needs a hosted preview URL.
- Deciding between git-push deploys and one-off CLI deploys.

## When NOT to Use

- Token-based or CI deployment with no interactive login available — use `vercel-cli-with-tokens`.
- Cost and performance work on an already-deployed project — use `vercel-optimize`.
- Deploying to Volcengine IGA Pages — use `iga-pages`.

## Step 1: Gather Project State

Run all four checks before choosing a method. Only `vercel whoami` is safe to run anywhere.

```shell
git remote get-url origin 2>/dev/null
cat .vercel/project.json 2>/dev/null || cat .vercel/repo.json 2>/dev/null
vercel whoami 2>/dev/null
vercel teams list --format json 2>/dev/null
```

**Team selection.** If the user belongs to multiple teams, present the team slugs as a bulleted list and ask which one to deploy to. Once the user picks, proceed without further confirmation, and pass the slug via `--scope` on every later CLI command. If the project is already linked, the `orgId` in its `.vercel/` file decides the team. If there is one team or a personal account, skip the prompt.

**What `.vercel/` means.** A linked project has one of:

- `.vercel/project.json` — from `vercel link`; contains `projectId` and `orgId`.
- `.vercel/repo.json` — from `vercel link --repo`; contains `orgId`, `remoteName`, and a `projects` map of directories to project ids.

Either file means linked; check for both. Never run `vercel project inspect`, `vercel ls`, or `vercel link` to *detect* state in an unlinked directory — they prompt interactively, or with `-y` silently link as a side effect.

## Step 2: Choose a Method

| Linked | Git remote | Method |
| ------ | ---------- | ------ |
| Yes    | Yes        | Git push (ideal) |
| Yes    | No         | `vercel deploy` |
| No     | Authenticated | Link, then deploy |
| No     | Not authenticated | Install, auth, link, deploy |
| Any    | No CLI access | No-auth fallback |

### Linked + git remote: git push

Ask before pushing — never push without explicit approval:

```
This project is connected to Vercel via git. I can commit and push to trigger a
deployment. Want me to proceed?
```

Then:

```shell
git add .
git commit -m "deploy: <description of changes>"
git push
```

Vercel builds from the push. Non-production branches get preview deployments; the production branch usually gets a production deployment. Retrieve the preview URL:

```shell
sleep 5
vercel ls --format json
```

The JSON has a `deployments` array; the latest entry's `url` is the preview URL. If the CLI is not authenticated, tell the user to check the dashboard or the commit status checks on their git provider.

### Linked + no git remote: CLI deploy

```shell
vercel deploy [path] -y --no-wait
vercel inspect <deployment-url>
```

Use `--no-wait` so the CLI returns the URL immediately instead of blocking on the build. Production only when explicitly asked:

```shell
vercel deploy [path] --prod -y --no-wait
```

### Not linked + authenticated: link, then deploy

1. Ask which team to deploy to (skip if only one).
2. Announce what happens and proceed: "Linking this project to `<team>` on Vercel. This creates a Vercel project to deploy to and enables automatic deployments on future git pushes."
3. Link with the selected scope. Prefer repo-based linking when a git remote exists — it reads the remote URL and matches existing Vercel projects, and creates `.vercel/repo.json`. Plain `vercel link` matches by directory name and often fails when the folder and the project differ in name.

```shell
vercel link --repo --scope <team-slug>   # git remote exists
vercel link --scope <team-slug>          # no git remote
```

4. Deploy by the best available method: git push if a remote exists, otherwise `vercel deploy [path] -y --no-wait --scope <team-slug>` followed by `vercel inspect <url>`.

### Not linked + not authenticated: install, auth, link, deploy

```shell
npm install -g vercel
vercel login
vercel teams list --format json
vercel link --repo --scope <team-slug>   # or without --repo
vercel deploy -y --no-wait --scope <team-slug>
```

`vercel login` completes authentication in the user's browser. If the environment cannot do interactive login, go to the no-auth fallback.

### No-auth fallback

Last resort when the CLI cannot be installed or authenticated. Needs no authentication and returns a **Preview URL** plus a **Claim URL**.

```shell
sh resources/deploy.sh [path]
```

Arguments: `path` is the directory to deploy or a `.tgz` file; it defaults to the current directory. The script auto-detects the framework from `package.json`, packages the project excluding `node_modules`, `.git`, and `.env`, uploads it, and waits for the build to finish. Tell the user: "Your deployment is ready at `<previewUrl>`. Claim it at `<claimUrl>` to manage your deployment."

Both `resources/deploy.sh` and `resources/deploy-codex.sh` are shell scripts at the skill root; invoke them with the POSIX shell your environment provides.

## Output

Always show the deployment URL.

- Git push: use `vercel ls --format json`; if not authenticated, point the user at the dashboard or commit status checks.
- CLI deploy: show the URL returned by `vercel deploy --no-wait`, then report `vercel inspect <url>` build status.
- No-auth fallback: show both URLs.

```
Deployment successful!

Preview URL: https://my-app-abc123.vercel.app
Claim URL:   https://vercel.com/claim-deployment?code=...

View your site at the Preview URL.
To transfer this deployment to your Vercel account, visit the Claim URL.
```

Do not curl or fetch the deployed URL to verify it. Just return the link.

## Troubleshooting

**Network egress blocked.** Tell the user: deployment failed due to network restrictions; add `*.vercel.com` to the allowed domains in their environment settings and try again.

**Authentication failure.** If `vercel login` or `vercel deploy` fails on credentials, check `vercel whoami`, ask for a fresh token, or fall back to the no-auth script. Do not escalate permissions for a plain availability check; only the deploy command itself needs elevated network access when your environment sandboxes it.

**Wrong team.** The linked `orgId` wins. Verify with `vercel whoami --scope <team-slug>` and relink with the right `--scope` if needed.

**Build failure.** Read `vercel inspect <deployment-url> --logs`. Common causes: incomplete `package.json`, missing environment variables (add with `vercel env add`), or wrong framework detection (override in `vercel.json`; Vercel auto-detects Next.js, Vite, and others from `package.json`).

## Tools You Use

| Task                        | Tool       | Note                                            |
| --------------------------- | ---------- | ----------------------------------------------- |
| Run checks, link, deploy    | `terminal` | Every command above runs through it             |
| Commit and push             | `git`      | Or raw git through `terminal`                   |
| Read `.vercel/` files       | `file_read`| Read only; never edit these files               |
| Run the fallback script     | `terminal` | `resources/deploy.sh [path]`                    |

## Best Practices

- Default to preview. Production is the user's explicit choice.
- Ask before pushing to git. Every other step can proceed on your own.
- Prefer `vercel link --repo`; plain `vercel link` matches by directory name.
- Use `-y` on commands that prompt, and `--format json` when you need to parse output.
- Leave `.vercel/` alone; the CLI owns that directory.

## Anti-patterns

- Detecting link state with `vercel project inspect` in an unlinked directory, which links it as a side effect.
- Deploying to production because the user did not specify.
- Pushing commits to trigger a deploy without asking.
- Blocking on `vercel deploy` without `--no-wait` and then having to guess the URL.
- Fetching the deployed URL to confirm it works.

## Sources

- Fallback scripts: `resources/deploy.sh`, `resources/deploy-codex.sh` at the skill root.
- Upstream: Vercel, listed at skills.sh as `vercel-labs/agent-skills/deploy-to-vercel`.
