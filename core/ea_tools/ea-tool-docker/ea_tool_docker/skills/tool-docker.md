---
name: tool-docker
description: "Docker CLI wrapper for ps/run/stop/logs/build/pull/push/rm/rmi/compose with verbatim flags and a 300s cap."
hidden: true
context: inline
---

# tool-docker

Executes a fixed set of Docker subcommands. Flags pass through verbatim via `options`, the target is named with `image_or_container`, and the call is bounded at 300 seconds. It covers local container and image workflows, including compose.

## When to Use

- Inspecting running containers (`ps`) or reading recent output (`logs`).
- Building an image, pulling/pushing to a registry, or starting a container from an image.
- Tearing down a compose project or a single container/image during cleanup.
- Reproducing a bug in a clean container without touching the host.

## When NOT to Use

- Kubernetes or any cluster orchestration — use `deploy` with `target: "kubernetes"`.
- Long-running service logs you want to follow. `logs` returns a snapshot; streaming belongs in a terminal.
- Heavy builds or pulls that exceed 300 seconds; run them in CI or through `terminal` with no cap.
- Registry browsing, manifest inspection, or vulnerability scanning.
- Running a full deploy pipeline of build → push → rollout; use `deploy`.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `command` | string enum | yes | `ps`, `run`, `stop`, `logs`, `build`, `pull`, `push`, `rm`, `rmi`, `compose`. |
| `image_or_container` | string | no | Image name/id for `run`/`build`/`pull`/`push`/`rmi`, or container name/id for `stop`/`logs`/`rm`. |
| `options` | string | no | Whitespace-separated flags appended verbatim, e.g. `-d --restart unless-stopped -p 8080:80`. |

### Parameter Notes

- `options` is not parsed or validated; whatever you write is appended to the subcommand. Quote values containing spaces yourself.
- `compose` takes the compose subcommand (up/down/ps/...) inside `options`, plus `-f <file>` if the file is not the default.
- `ps` accepts flags such as `-a` and `--format` through `options`.
- `rm`, `rmi`, `stop`, and `compose down` are destructive and flagged as such; they do not prompt for confirmation.
- A 300-second timeout kills the process; partial output may still be returned.

## Example

```json
{ "command": "ps", "options": "-a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'" }
```

```json
{
  "command": "build",
  "image_or_container": "encre-desktop:0.5.0",
  "options": "-t encre-desktop:0.5.0 -f desktop/Dockerfile ."
}
```

```json
{
  "command": "run",
  "image_or_container": "redis:7-alpine",
  "options": "-d --name encre-redis -p 6379:6379"
}
```

```json
{ "command": "logs", "image_or_container": "encre-redis", "options": "--tail 100" }
```

Compose teardown:

```json
{ "command": "compose", "options": "-f deploy/docker-compose.yml down -v" }
```

## Best Practices

- Name containers explicitly (`--name`) so later `logs`/`stop`/`rm` calls are unambiguous.
- Use `-t <repo>:<tag>` for builds; untagged images are nearly impossible to clean up later.
- Prefer `logs --tail N` over unbounded output; the transcript is not a log sink.
- Clean up what you create in the same session: `rm` the container, `rmi` the image, `compose down` the project.
- Check `ps -a` before assuming a name is free.
- Run `compose` with an explicit `-f` path so the wrong file is not picked up from the cwd.

## Common Pitfalls

- Assuming a `run` is detached; without `-d` it may block until timeout.
- Passing `options` with shell quoting that does not survive (nested quotes around `--format` are a common trap).
- Treating a 300s timeout as "the build failed" when it was killed mid-way.
- Using `stop` when the container needs removal (`rm`), leaving stale containers behind.
- `compose down -v` deleting volumes with data you expected to keep.
- Port collisions producing a "bind: address already in use" error that looks like an image problem.

## Anti-patterns

- Building large images through this tool and losing the build to the timeout.
- Streaming logs by polling `logs` in a loop.
- Running `docker` via `terminal` when the structured wrapper suffices, or vice versa for unsupported subcommands.
- Using `rmi -f` to clear disk without checking what depends on the images.

## Pairing with Other Tools

- `deploy` — build/push/rollout across Docker, k8s, Cloud Run, Vercel, Netlify.
- `terminal` — subcommands not in the enum, or uncapped long operations.
- `archive` — package build context before shipping.
- `rest_client` — hit the service once the container is up.
- `notify` — report the container URL/port to the user.
