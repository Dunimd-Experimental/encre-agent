# ea-connector-qqbot

System-default QQ Bot connector plugin for Encre Agent.

Bridges the QQ Bot (official API v2) WebSocket Gateway and REST API into the
Encre Agent gateway as a first-class platform adapter. Ships as a plugin so
the connector lifecycle (registration, start/stop, teardown) follows the
plugin system instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `QQ_APP_ID` — QQ Bot application ID
- `QQ_CLIENT_SECRET` — QQ Bot client secret

Optional extras (via `adapter_qqbot_*` settings keys): `dm_policy`,
`allow_from`, `group_policy`, `group_allow_from`, `stt` (voice transcription).

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
