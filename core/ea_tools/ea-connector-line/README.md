# ea-connector-line

System-default LINE connector plugin for Encre Agent.

Bridges the LINE Messaging API into the Encre Agent gateway as a
first-class platform adapter. Runs an aiohttp webhook endpoint that verifies
every delivery with `X-Line-Signature` (HMAC-SHA256 keyed by the channel
secret), converts LINE webhook events into shared `MessageEvent` objects,
and ships agent output back over the reply and push endpoints (reply token
preferred, push fallback). Ships as a plugin so the connector lifecycle
(registration, start/stop, teardown) follows the plugin system instead of
being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `LINE_CHANNEL_ACCESS_TOKEN` — channel access token
- `LINE_CHANNEL_SECRET` — channel secret

Optional extras (via `LINE_*` env vars or `platforms.line.extra`):
`LINE_HOST`, `LINE_PORT`, `LINE_PUBLIC_URL`, `LINE_ALLOWED_USERS`,
`LINE_ALLOWED_GROUPS`, `LINE_ALLOWED_ROOMS`, `LINE_SLOW_RESPONSE_THRESHOLD`.

Reference: https://developers.line.biz/en/services/messaging-api/
