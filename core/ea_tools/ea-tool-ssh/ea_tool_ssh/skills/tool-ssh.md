---
name: tool-ssh
description: "Run commands or multi-line scripts, upload and download files, and test connectivity on remote hosts over SSH/SCP."
hidden: true
context: inline
---

# tool-ssh

`ssh` wraps the system `ssh` and `scp` clients for remote server work: execute a single command (`exec`), push or pull files (`upload`, `download`), verify reachability (`ping`), or upload-and-run a terminal script (`script`). Each call opens its own connection — this is not an interactive session.

## When to Use

- Inspect a remote host: uptime, disk, logs, service status — `action="exec"`.
- Deploy a built artifact to a server — `action="upload"`.
- Retrieve logs or data files — `action="download"`.
- Test credentials and network reachability before a longer operation — `action="ping"`.
- Run a multi-step remote procedure — `action="script"` with a terminal body.
- Restart a service with a one-liner.

## When NOT to Use

- Working in Docker on the same host — `docker` is simpler.
- Local shell commands — use `terminal`.
- Interactive shells, port forwarding, tunnels or long-running daemons — not supported.
- Copying between two remote hosts (no `-3` support) — stage locally.
- Bulk file sync of a directory tree — use `terminal` with `rsync`/`scp -r`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `exec`, `upload`, `download`, `ping`, `script`. |
| `host` | string | yes | Hostname, FQDN or IP. |
| `port` | integer | no | SSH port; default 22. |
| `user` | string | no | Login user; defaults to `root`. |
| `key_file` | string | no | Private key path (`ssh -i`); preferred over password. |
| `password` | string | no | Password auth; requires `sshpass` on the local system. |
| `command` | string | no | Remote command for `exec`. |
| `local_path` | string | no | Local file path for `upload` / `download`. |
| `remote_path` | string | no | Remote destination (upload) or source (download). |
| `content` | string | no | Bash script body for `script`. |
| `timeout` | integer | no | Kill after N seconds; default 60 (`exec`) or 120 (`script`). |

### Parameter Notes

- `exec` needs `command`; `script` needs `content`; transfers need both `local_path` and `remote_path`.
- `user` defaults to `root` — always set it explicitly on hosts where root login is disabled.
- `password` only works when `sshpass` is installed locally; otherwise the call hangs or fails.
- Each action opens a fresh connection: no state, no `cd` persistence between calls.

## Example

```json
{
  "action": "exec",
  "host": "10.0.0.14",
  "user": "deploy",
  "key_file": "C:/Users/me/.ssh/id_ed25519",
  "command": "systemctl is-active encre && tail -n 40 /var/log/encre/app.log",
  "timeout": 45
}
```

## Best Practices

- Prefer `key_file` over `password`; it is more reliable and avoids secrets in arguments.
- Keep `exec` commands idempotent and pipe-free where possible; quote arguments for the remote shell.
- Raise `timeout` deliberately for installs or large transfers instead of assuming success.
- Use `ping` as a cheap connectivity/auth check before a multi-step procedure.
- Set `user` explicitly, and use absolute remote paths.

## Common Pitfalls

- `StrictHostKeyChecking` is disabled and `ConnectTimeout` is 15s — a `ping` success does not validate host identity.
- SCP transfers of large files hit the 120s limit; there is no resume.
- Long-running processes started via `exec` may be killed when the session ends — use `nohup`/`systemd-run` inside `command`.
- Password auth without `sshpass` fails obscurely.
- A missing `remote_path` directory causes `scp` to fail rather than create parents.

## Anti-patterns

- Running interactive programs (`vim`, `top`, prompts) over `exec`.
- Passing root passwords in plaintext when keys exist.
- Assuming shell state persists across calls.
- Deploying by `scp`-ing individual files when `deploy` or `docker` covers the flow.

## Pairing with Other Tools

- `terminal` — local commands before/after remote work.
- `docker` — container operations on the local host.
- `deploy` — higher-level artifact deployment.
- `file_read` / `file_write` — prepare scripts locally before `script` uploads them.
