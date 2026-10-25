# ea-connector-bluebubbles

System-default BlueBubbles (iMessage) connector plugin for Encre Agent.

Bridges Apple's iMessage onto the Encre Agent gateway through a locally
hosted BlueBubbles server (the macOS app that exposes iMessage as a REST
API). Outbound messages resolve the target to a chat GUID and post to the
BlueBubbles REST API; inbound events arrive as POSTs on a localhost webhook
listener the adapter registers at connect time. Ships as a plugin so the
connector lifecycle (registration, start/stop, teardown) follows the plugin
system instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `BLUEBUBBLES_URL` — BlueBubbles server URL
- `BLUEBUBBLES_PASSWORD` — BlueBubbles server password (API key)

Optional extras: `BLUEBUBBLES_SERVER_URL`, `BLUEBUBBLES_WEBHOOK_HOST`,
`BLUEBUBBLES_WEBHOOK_PORT`, `BLUEBUBBLES_WEBHOOK_PATH`,
`BLUEBUBBLES_REQUIRE_MENTION`, `BLUEBUBBLES_MENTION_PATTERNS`.

Reference: https://bluebubbles.app/
