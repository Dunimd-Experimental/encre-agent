# ea-connector-slack

System-default Slack connector plugin for Encre Agent.

Bridges Slack (Socket Mode via slack-bolt) and the Slack Web API into the
Encre Agent gateway as a first-class platform adapter. Ships as a plugin so
the connector lifecycle (registration, start/stop, teardown) follows the
plugin system instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `SLACK_BOT_TOKEN` — Slack bot token (`xoxb-...`)
- `SLACK_APP_TOKEN` — Slack app-level token for Socket Mode (`xapp-...`)

Optional extras (via `SLACK_*` environment variables or the `slack:` section
of `config.yaml`): `require_mention`, `strict_mention`, `allow_bots`,
`free_response_channels`, `reactions`, `allowed_channels`, `allowed_users`,
`home_channel`, and rich Block Kit rendering (`slack.extra.rich_blocks`).

Reference: https://docs.slack.dev/
