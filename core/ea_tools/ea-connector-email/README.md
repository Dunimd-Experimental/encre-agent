# ea-connector-email

System-default Email connector plugin for Encre Agent.

Bridges a conventional mail account into the Encre Agent gateway as a
first-class platform adapter: inbound traffic is discovered by polling an
IMAP inbox for unseen messages, and outbound traffic is delivered over a
TLS SMTP connection with MIME attachments flattened to cached local files.
Ships as a plugin so the connector lifecycle (registration, start/stop,
teardown) follows the plugin system instead of being compiled into the
core gateway.

The adapter is standard-library only (`imaplib` / `smtplib`) — no optional
dependency extras are required.

## Configuration

Required environment variables (or per-adapter settings):

- `EMAIL_ADDRESS` — Email address for the agent
- `EMAIL_PASSWORD` — Email password or app-specific password
- `EMAIL_IMAP_HOST` — IMAP server host (e.g. `imap.gmail.com`)
- `EMAIL_SMTP_HOST` — SMTP server host (e.g. `smtp.gmail.com`)

Optional extras: `EMAIL_IMAP_PORT` (default 993), `EMAIL_SMTP_PORT`
(default 587), `EMAIL_POLL_INTERVAL` (default 15s), `EMAIL_ALLOWED_USERS`
(comma-separated allowlist), `EMAIL_ALLOW_ALL_USERS`, `EMAIL_AUTHSERV_ID`,
`EMAIL_TRUST_FROM_HEADER`, and the `skip_attachments` /
`require_authenticated_sender` keys under `platforms.email` in
`config.yaml`.

Sender authentication (SPF/DKIM/DMARC via the `Authentication-Results`
header) is on by default and fail-closed when an allowlist is in effect
(see GHSA-rxqh-5572-8m77).
