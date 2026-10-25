# ea-connector-telegram

System-default Telegram connector plugin for Encre Agent.

Bridges the Telegram Bot API (long-polling or webhook) into the Encre Agent
gateway as a first-class platform adapter. Ships as a plugin so the connector
lifecycle (registration, start/stop, teardown) follows the plugin system
instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `TELEGRAM_BOT_TOKEN` — Telegram Bot API token

Optional extras (via `adapter_telegram_*` settings keys): `dm_policy`,
`allow_from`, `group_policy`, `require_mention`, `stt` (voice transcription),
`disable_link_previews`, webhook settings (`TELEGRAM_WEBHOOK_URL`,
`TELEGRAM_WEBHOOK_SECRET`), and restricted-network transport options
(`TELEGRAM_PROXY`, DNS-over-HTTPS fallback IP discovery).

Reference: https://core.telegram.org/bots/api
