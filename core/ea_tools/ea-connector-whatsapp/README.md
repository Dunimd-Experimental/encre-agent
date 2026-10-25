# ea-connector-whatsapp

System-default WhatsApp connector plugin for Encre Agent.

Bridges WhatsApp into the Encre Agent gateway through a Node.js bridge
subprocess (whatsapp-web.js / Baileys): the bridge runs the WhatsApp Web
client and forwards messages over HTTP/IPC to the adapter, and outbound
agent replies travel back through the bridge with WhatsApp-flavored
markdown formatting. Ships as a plugin so the connector lifecycle
(registration, start/stop, teardown) follows the plugin system instead of
being compiled into the core gateway.

## Configuration

Required environment variables:

- `WHATSAPP_ENABLED` — set to `true` to opt in (the bridge handles auth)

Optional extras: `WHATSAPP_MODE`, `WHATSAPP_ALLOWED_USERS`,
`WHATSAPP_ALLOW_ALL_USERS`, `WHATSAPP_GROUP_POLICY`,
`WHATSAPP_GROUP_ALLOWED_USERS`, `WHATSAPP_REQUIRE_MENTION`,
`WHATSAPP_MENTION_PATTERNS`, `WHATSAPP_FREE_RESPONSE_CHATS`,
`WHATSAPP_REPLY_PREFIX`.

Requires a Node.js runtime for the bridge subprocess (optional `psutil`
Python package for robust bridge-process cleanup).

Reference: https://developers.facebook.com/docs/whatsapp/
