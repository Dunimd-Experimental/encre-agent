---
name: tool-notify
description: "Send an out-of-band notification through a generic webhook, Slack, Discord, or a native desktop toast."
hidden: true
context: inline
---

# tool-notify

`notify` pushes a message out of the conversation: an arbitrary HTTP webhook (`webhook`), a Slack incoming webhook (`slack`), a Discord webhook (`discord`), or a native OS toast (`computer`). It is for alerting a person or channel that something happened — a build finished, a long job failed, a watcher fired — not for chatting.

## When to Use

- Announce completion or failure of a long-running job to a channel.
- Ping a monitoring endpoint with a structured JSON body.
- Show a desktop toast when a local task needs attention.
- Wire automation output into Slack or Discord without a full bot.
- Deliver a short status line to a channel the user is watching.

## When NOT to Use

- Asking the user something that needs an answer — use `question`.
- Ordinary conversation or status updates — just reply in chat.
- Sending rich, multi-block messages with attachments — use a platform-specific channel tool.
- Email — use `email`.
- Recurring schedules — pair with `cron_create` rather than looping here.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `webhook`, `slack`, `discord`, `computer`. |
| `message` | string | yes | Notification text; required for every action. Keep it short. |
| `url` | string | no | Target endpoint for `webhook`. |
| `method` | string | no | HTTP method for `webhook`; default `POST`. |
| `headers` | object | no | Custom headers for `webhook`; Content-Type is added if missing. |
| `body` | string | no | Raw request body for `webhook`; JSON endpoints need a JSON-serialized string. |
| `content_type` | string | no | Content-Type for `webhook`; default `application/json`. |
| `webhook_url` | string | no | Incoming webhook URL for `slack` / `discord`. |
| `channel` | string | no | Slack channel override (`#alerts` or a user ID). |
| `username` | string | no | Bot display name for `slack` / `discord`; default `Encre Bot`. |
| `icon_emoji` | string | no | Slack avatar emoji, e.g. `:robot_face:`. |
| `title` | string | no | Title for the `computer` toast; default `Encre Notification`. |

### Parameter Notes

- `action` decides which addressing field matters: `url` for `webhook`, `webhook_url` for `slack`/`discord`, neither for `computer`.
- For `webhook`, `message` is required but only `body` is actually posted; build `body` explicitly if the endpoint expects a schema.
- `content_type` and `headers["Content-Type"]` are redundant — set one.
- `channel` only affects Slack; Discord webhooks post to the channel they were created for.

## Example

```json
{
  "action": "slack",
  "webhook_url": "https://hooks.slack.com/services/T000/B000/XXXX",
  "message": "Nightly build failed on Windows (job #4821)",
  "channel": "#alerts"
}
```

## Best Practices

- Keep `message` to one or two lines; chat clients truncate long text silently.
- For arbitrary endpoints, set `body` to a JSON string and `content_type` to `application/json`.
- Include the failing identifier (job, file, commit) in the message so it is actionable.
- Prefer `slack`/`discord` actions over raw `webhook` when posting to those services; the payload shape is handled for you.
- Use `computer` for interactive sessions where the user is at the machine.

## Common Pitfalls

- Missing or rotated webhook URLs return 404/403; a silent "sent" is not confirmation.
- Slack incoming webhooks do not accept `Authorization` headers — the URL is the secret.
- `computer` needs OS support: Linux requires `libnotify-bin`, and headless sessions show nothing.
- Long or multiline `message` bodies are the most common cause of malformed payloads.
- Webhook secrets in arguments end up in transcripts; rotate anything exposed.

## Anti-patterns

- Using `notify` to ask a blocking question.
- Sending one notification per item in a loop instead of batching into one summary.
- Posting secrets or full stack traces to a shared channel.
- Building a cron job that calls `notify` every minute with no dedupe.

## Pairing with Other Tools

- `question` — when the user must respond.
- `cron_create` — schedule recurring notifications.
- `email` — formal or long-form delivery.
- `test_run` / `deploy` — notify on their outcome.
