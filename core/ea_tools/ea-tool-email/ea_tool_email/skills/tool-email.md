---
name: tool-email
description: "Send mail over SMTP and read/search folders over IMAP: compose, fetch recent messages, or run IMAP criteria."
hidden: true
context: inline
---

# tool-email

A compact SMTP/IMAP client. `send` composes and delivers a plain-text message; `read`, `search`, and `list_folders` work against an IMAP mailbox. Everything is text — there is no rich HTML body and no attachment upload path.

## When to Use

- Delivering a report, alert, or summary to an address outside the chat platform.
- Reading recent messages from a mailbox the user pointed you at.
- Searching a mailbox by sender, subject, or date to find one specific message.
- Discovering folder names before reading a non-INBOX folder.

## When NOT to Use

- Chat/IM platforms (DingTalk, Telegram, Slack) — use the platform gateway or `notify`.
- Real-time coordination; email is asynchronous and delivery is not guaranteed.
- Sending attachments; not supported by the parameter surface.
- Bulk marketing mail or anything that needs HTML templates, tracking, or bounce handling.
- Reading a shared mailbox that requires OAuth flows beyond basic auth.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | `send`, `read`, `search`, `list_folders`. |
| `host` | string | usually | SMTP or IMAP hostname (e.g. `smtp.gmail.com`, `imap.gmail.com`). Required unless configured globally. |
| `port` | integer | no | Port; defaults are inferred per action (587 SMTP STARTTLS, 465 SMTP SSL, 993 IMAP SSL, 143 IMAP plain). |
| `username` | string | yes (auth) | Account username, usually the full address. |
| `password` | string | yes (auth) | Password or app-specific password. Use an app password when 2FA is on. |
| `use_tls` | boolean | no | STARTTLS for SMTP (default `true`); disable only for plaintext servers. |
| `to` | string | for `send` | Recipient(s), comma-separated. |
| `cc` | string | no | CC recipients, comma-separated. |
| `bcc` | string | no | BCC recipients, comma-separated. |
| `subject` | string | for `send` | Subject line. |
| `body` | string | for `send` | Plain-text body. |
| `folder` | string | no | IMAP folder for `read`/`search`; default `INBOX`. |
| `max_emails` | integer | no | Max messages for `read` (default 10). |
| `mark_seen` | boolean | no | Mark fetched messages as read (default `false`). |
| `criteria` | string | for `search` | IMAP search criteria, e.g. `FROM user@example.com`, `SUBJECT "report"`, `SINCE 01-Jan-2026`. |
| `max_results` | integer | no | Max search hits (default 10). |

### Parameter Notes

- Port/encryption pairs must agree: 587 + `use_tls: true`, 465 + SSL, 993 + IMAP SSL. Mixing them yields hangs or handshake errors.
- `criteria` is raw IMAP syntax passed to the server; quoting and date formats are server-specific (RFC 3501 is the reference).
- `mark_seen: false` is the safe default — it lets you read without changing the user's unread state.
- `max_emails` / `max_results` are the main defence against enormous responses; keep them at 10–20.
- Credentials are per-call unless configured globally; do not hard-code them into prompts that get logged.

## Example

```json
{
  "action": "send",
  "host": "smtp.example.com",
  "port": 587,
  "use_tls": true,
  "username": "agent@example.com",
  "password": "<app-password>",
  "to": "team@example.com",
  "cc": "lead@example.com",
  "subject": "Nightly build failed",
  "body": "The 02:00 build failed at the desktop packaging stage.\n\nSee data/jobs.json for the job id and error."
}
```

```json
{
  "action": "search",
  "host": "imap.example.com",
  "port": 993,
  "username": "agent@example.com",
  "password": "<app-password>",
  "folder": "INBOX",
  "criteria": "SUBJECT \"invoice\" SINCE 01-Sep-2026",
  "max_results": 5
}
```

## Best Practices

- Use an app-specific password whenever the provider offers one; account passwords often fail under 2FA or get blocked.
- Keep the body plain and scannable: a one-line summary, then details, then where to look next.
- Search narrowly (sender + date window) instead of fetching the inbox and filtering locally.
- Leave `mark_seen` false unless the user asked for messages to be marked read.
- Verify a send by checking the returned status, not by assuming SMTP success means delivery.
- For recurring reports, combine with `cron_create` so the message content is generated fresh each run.

## Common Pitfalls

- Provider blocking SMTP from an unfamiliar IP or region; the error looks like an auth failure.
- Wrong port/encryption pairing causing a silent timeout.
- IMAP folder names that differ from the display name (`[Gmail]/All Mail`); list folders first.
- Date formats in `criteria` that the server rejects (use `01-Jan-2026`, not `2026-01-01`).
- Fetching 200 messages and blowing the context budget.
- Assuming `send` supports attachments or HTML.

## Anti-patterns

- Using email for real-time back-and-forth.
- Sending to a large recipient list without the user's explicit request.
- Putting credentials in plain text in a shared transcript when env/global config exists.
- Polling the inbox on a loop instead of using a scheduled job.

## Pairing with Other Tools

- `cron_create` — schedule periodic report emails.
- `notify` — chat-side notification when email is the wrong channel.
- `document` / `spreadsheet` — generate the report content before sending a summary.
- `question` — confirm recipients before an irreversible send.
- `web_fetch` — pull the data a digest email reports on.
