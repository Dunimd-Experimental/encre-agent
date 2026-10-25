# ea-connector-mattermost

System-default Mattermost connector plugin for Encre Agent.

Bridges a self-hosted (or cloud) Mattermost instance into the Encre Agent
gateway as a first-class platform adapter. Connects via the Mattermost REST
API (v4) and WebSocket for real-time events — no external Mattermost library
required. Ships as a plugin so the connector lifecycle (registration,
start/stop, teardown) follows the plugin system instead of being compiled
into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `MATTERMOST_TOKEN` — bot token or personal-access token
- `MATTERMOST_URL` — server URL (e.g. `https://mm.example.com`)

Optional extras: `MATTERMOST_ALLOWED_USERS`, `MATTERMOST_ALLOWED_CHANNELS`,
`MATTERMOST_FREE_RESPONSE_CHANNELS`, `MATTERMOST_REQUIRE_MENTION`,
`MATTERMOST_REPLY_MODE`, `MATTERMOST_HOME_CHANNEL`.

Reference: https://developers.mattermost.com/
