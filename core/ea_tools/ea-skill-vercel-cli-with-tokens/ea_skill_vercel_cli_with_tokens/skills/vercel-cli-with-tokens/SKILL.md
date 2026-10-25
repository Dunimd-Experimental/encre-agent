---
name: vercel-cli-with-tokens
description: "Deploy and manage Vercel projects with token authentication instead of interactive login. Use for token-based deploys, environment variables, deployment inspection, and domain management."
aliases:
  - "vercel token"
  - "ci deploy"
argument_hint: Project to deploy or manage
user_invocable: true
hidden: true
---

# Vercel CLI with Tokens

Deploy and manage projects on Vercel using token authentication, without relying on `vercel login`. Use this skill when the user has access tokens or is deploying from a non-interactive environment.

## When to Use

- Deploying with a token rather than interactive login.
- Setting, listing, or pulling Vercel environment variables.
- Inspecting deployments, build logs, and runtime logs.
- Adding or listing domains on a project.

## When NOT to Use

- First-time setup where the user is present for `vercel login` — use `deploy-to-vercel`.
- Cost and performance optimization on a deployed project — use `vercel-optimize`.
- Framework or React performance questions — use `react-best-practices`.

## Step 1: Locate the Token

Work through these in order before asking the user for anything.

**A) Already in the environment.**

```shell
printenv VERCEL_TOKEN
```

If this returns a value, go to Step 2.

**B) In a `.env` file as `VERCEL_TOKEN`.**

```shell
grep '^VERCEL_TOKEN=' .env 2>/dev/null
export VERCEL_TOKEN=$(grep '^VERCEL_TOKEN=' .env | cut -d= -f2-)
```

**C) In a `.env` file under another name.** Vercel tokens typically start with `vca_`.

```shell
grep -i 'vercel' .env 2>/dev/null
export VERCEL_TOKEN=$(grep '^<VARIABLE_NAME>=' .env | cut -d= -f2-)
```

**D) Nothing found.** Ask the user for a token; they can create one at vercel.com/account/tokens.

**Never pass the token as a `--token` flag.** Once it is exported, the CLI reads it natively. Arguments end up in shell history and process listings.

```shell
# Bad: token visible in shell history and process listings
vercel deploy --token "vca_abc123"

# Good: the CLI reads VERCEL_TOKEN from the environment
export VERCEL_TOKEN="vca_abc123"
vercel deploy
```

## Step 2: Locate the Project and Team

```shell
printenv VERCEL_PROJECT_ID
printenv VERCEL_ORG_ID
grep -i 'vercel' .env 2>/dev/null
```

From a project URL like `https://vercel.com/my-team/my-project`, the first path segment is the team slug:

```shell
echo "$PROJECT_URL" | sed 's|https://vercel.com/||' | cut -d/ -f1
```

If you have both ids, export them and the CLI targets the right project without any `.vercel/` directory. They must be set together; setting only one is an error.

```shell
export VERCEL_ORG_ID="<org-id>"
export VERCEL_PROJECT_ID="<project-id>"
```

## CLI Setup

```shell
npm install -g vercel
vercel --version
```

## Deploying a Project

Always deploy as a preview unless the user explicitly requests production.

### Quick deploy: you have the project id

No linking needed when `VERCEL_TOKEN` and `VERCEL_PROJECT_ID` are exported.

```shell
vercel deploy -y --no-wait
vercel deploy --scope <team-slug> -y --no-wait
vercel deploy --prod --scope <team-slug> -y --no-wait   # production only when asked
vercel inspect <deployment-url>
```

### Full flow: no project id, you must link

Check state first:

```shell
git remote get-url origin 2>/dev/null
cat .vercel/project.json 2>/dev/null || cat .vercel/repo.json 2>/dev/null
```

Link:

```shell
vercel link --repo --scope <team-slug> -y          # git remote: preferred
vercel link --scope <team-slug> -y                  # no git remote
vercel link --project <project-name> --scope <team-slug> -y   # by name
```

`--repo` reads the git remote and connects to the matching Vercel project, creating `.vercel/repo.json`. Plain `vercel link` matches by directory name and creates `.vercel/project.json`. If the project is already linked, verify the `orgId` in its `.vercel/` file matches the intended team.

Deploy:

- **Git push (preferred when a remote exists).** Ask the user before pushing, then:

```shell
git add .
git commit -m "deploy: <description of changes>"
git push
sleep 5
vercel ls --format json --scope <team-slug>
```

Find the latest entry in the `deployments` array.

- **CLI deploy when there is no remote.**

```shell
vercel deploy --scope <team-slug> -y --no-wait
vercel inspect <deployment-url>
```

### Deploying from a remote repository you have not cloned

```shell
git clone <repo-url>
cd <repo-name>
vercel link --repo --scope <team-slug> -y
```

Then deploy via git push if you have push access, otherwise via the CLI.

### About `.vercel/`

- `.vercel/project.json` — from `vercel link`; holds `projectId` and `orgId`.
- `.vercel/repo.json` — from `vercel link --repo`; holds `orgId`, `remoteName`, and a `projects` map.

Neither is needed when `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` are both exported. Never run `vercel project inspect` or `vercel link` in an unlinked directory to detect state — they prompt or silently link. `vercel ls` and `vercel whoami` are safe anywhere.

## Managing Environment Variables

```shell
echo "value" | vercel env add VAR_NAME --scope <team-slug>
echo "value" | vercel env add VAR_NAME production --scope <team-slug>
vercel env ls --scope <team-slug>
vercel env pull --scope <team-slug>          # writes .env.local
vercel env rm VAR_NAME --scope <team-slug> -y
```

## Inspecting Deployments

```shell
vercel ls --format json --scope <team-slug>
vercel inspect <deployment-url>
vercel inspect <deployment-url> --logs        # build logs, requires CLI v35+
vercel logs <deployment-url>                  # runtime logs, follows by default
vercel logs <deployment-url> --no-follow      # one-shot snapshot
```

## Managing Domains

```shell
vercel domains ls --scope <team-slug>
vercel domains add <domain> --scope <team-slug>              # linked directory, one argument
vercel domains add <domain> <project> --scope <team-slug>    # unlinked directory, needs <project>
```

## Stripe Projects Plan Changes

If the project is managed by Stripe Projects, ask the user before any paid or destructive plan change: upgrades bill a real card, downgrades remove seats. First confirm the local resource name with `stripe projects status --json`; the examples below assume the default `vercel-plan`.

- Upgrade: `stripe projects add vercel/pro` or `stripe projects upgrade vercel-plan pro`
- Downgrade: `stripe projects downgrade vercel-plan hobby`

Pro adds a $20/month platform fee with $20/month of usage credit, Turbo build machines (30 vCPUs, 60 GB) for new projects, one deploying seat plus unlimited Viewer seats, and larger allocations (1 TB Fast Data Transfer, 10M Edge Requests per month). Details: https://vercel.com/docs/plans/pro-plan

## Working Agreement

- Never pass `VERCEL_TOKEN` as a `--token` flag. Export it and let the CLI read it.
- Check the environment and `.env` files before asking the user for credentials.
- Default to preview deployments; production only when explicitly asked.
- Ask before pushing to git.
- Do not edit `.vercel/` files. Reading them to verify `orgId` is fine.
- Do not fetch deployed URLs to verify. Return the link.
- Use `--format json` when you need to parse output, and `-y` to avoid interactive prompts.

## Troubleshooting

**Token not found.**

```shell
printenv | grep -i vercel
grep -i vercel .env 2>/dev/null
```

**Authentication error.** The token may be expired or invalid. Verify with `vercel whoami` and ask for a fresh one.

**Wrong team.** `vercel whoami --scope <team-slug>` shows whether the scope is right.

**Build failure.** Read `vercel inspect <deployment-url> --logs`. Common causes: incomplete `package.json`, missing environment variables, or wrong framework detection — Vercel auto-detects Next.js, Vite, and others from `package.json`; override with `vercel.json`.

**CLI missing.** `npm install -g vercel`.

## Tools You Use

| Task                      | Tool       | Note                                           |
| ------------------------- | ---------- | ---------------------------------------------- |
| Run every CLI command     | `terminal` | Export tokens as env vars, not arguments       |
| Find config in the repo   | `grep`, `glob` | Search for `vercel` in `.env` and config   |
| Read `.vercel/` metadata  | `file_read` | Read only                                    |
| Verify the auth state     | `terminal` | `vercel whoami`                                |

## Sources

- Upstream: Vercel, listed at skills.sh as `vercel-labs/agent-skills/vercel-cli-with-tokens`.
