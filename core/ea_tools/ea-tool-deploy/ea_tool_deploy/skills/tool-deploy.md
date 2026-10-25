---
name: tool-deploy
description: "Build, deploy, roll back, or check status via docker/kubectl/gcloud/vercel/netlify CLIs for a named target."
hidden: true
context: inline
---

# tool-deploy

Thin, opinionated wrapper around a platform's native CLI. It selects the driver from `target`, maps `action` onto that driver's lifecycle verbs, and runs the command with the manifest you point at. It is not a container manager and it is not an IaC engine.

## When to Use

- Repeatable deploy-style operations: `docker build`/`push`, `kubectl apply`, `gcloud run deploy`, `vercel deploy`, `netlify deploy`.
- Rolling a service back to its previous revision.
- Checking whether a deployment is live before running smoke tests.
- Driving a deploy from a manifest already in the repo (`Dockerfile`, k8s YAML, `vercel.json`, `netlify.toml`).

## When NOT to Use

- Ad-hoc container work (start/stop/logs/exec) — use `docker`.
- Terraform, Helm, Pulumi, or any plan/apply pipeline with its own state — run it through `terminal`.
- Local builds that never leave the machine; use `terminal` with the build command.
- Multi-stage CI orchestration with parallelism and artifacts — that belongs in the CI system.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `target` | string enum | yes | `docker`, `kubernetes`, `cloud_run`, `vercel`, `netlify` — selects the CLI driver. |
| `action` | string enum | yes | `build`, `deploy`, `rollback`, `status`. |
| `config_file` | string | no | Manifest path: Dockerfile, k8s YAML, `vercel.json`, `netlify.toml`. |
| `project_name` | string | no | Application/service/deployment name to scope the action; required for some target/action pairs. |

### Parameter Notes

- `target` and `action` are not freely combinable: `rollback` is meaningful for kubernetes/cloud_run/vercel/netlify, not for a local `docker build`.
- `project_name` is effectively required for cloud_run, vercel, and netlify deploys — without it the CLI has no service to target.
- `config_file` is resolved relative to the current working directory; pass an absolute path if the caller's cwd is uncertain.
- Each invocation blocks up to 600 seconds. A large image build or a cold Cloud Run deploy can exceed that.
- The CLI must already be installed and authenticated on the machine. The tool does not log in for you.

## Example

```json
{
  "target": "docker",
  "action": "build",
  "config_file": "D:\\encre\\desktop\\Dockerfile",
  "project_name": "encre-desktop"
}
```

```json
{
  "target": "cloud_run",
  "action": "deploy",
  "project_name": "encre-gateway",
  "config_file": "deploy/cloudrun.yaml"
}
```

```json
{
  "target": "kubernetes",
  "action": "status",
  "project_name": "encre-harness"
}
```

## Best Practices

- Verify credentials first (a cheap `status` or `terminal` check) so you do not burn ten minutes on a build that fails at push time.
- Pin the manifest explicitly with `config_file`; relying on a default `Dockerfile` in cwd is fragile.
- Build and deploy as separate calls so a build failure does not leave a half-rolled deployment.
- Capture the returned revision/tag and report it; "deployed" without a version is unverifiable.
- Run a smoke check after deploy rather than assuming success from exit code 0.
- Keep a known-good revision name ready before calling `rollback`.

## Common Pitfalls

- Timeouts on large builds (600s ceiling) — pre-build layers or use CI for heavy images.
- Mismatched `project_name` between build and deploy, producing a deploy of a stale image.
- Missing CLI or expired auth: the error surfaces as a command failure, not a friendly message.
- Assuming `rollback` reverts data migrations; it only moves the workload.
- Deploying from a dirty working tree and losing track of what is actually live.
- Expecting the tool to create namespaces, secrets, or service accounts.

## Anti-patterns

- Using `deploy` for `docker run`/`logs`/`exec` work.
- Scripting a Terraform apply through this tool.
- Deploying straight to production "to check it works".
- Chaining build → deploy → rollback in one turn without inspecting intermediate output.

## Pairing with Other Tools

- `docker` — container lifecycle and logs around a deploy.
- `git` — confirm the commit being shipped and that the tree is clean.
- `terminal` — for CLIs and pipelines this wrapper does not cover.
- `test_run` — verify the build before promoting it.
- `notify` — tell the user which revision went live.
