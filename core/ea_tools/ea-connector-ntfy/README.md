# ea-connector-ntfy

System-default ntfy connector plugin for Encre Agent.

Bridges an ntfy topic into the Encre Agent gateway as a first-class platform
adapter, using no vendor SDK — only `httpx`. Inbound traffic arrives over a
long-lived `GET {server}/{topic}/json?poll=false` streaming connection;
outbound traffic is `POST {server}/{publish_topic}`. Ships as a plugin so
the connector lifecycle (registration, start/stop, teardown) follows the
plugin system instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `NTFY_TOPIC` — subscribe topic (required)

Optional extras: `NTFY_SERVER_URL` (default `https://ntfy.sh`),
`NTFY_PUBLISH_TOPIC`, `NTFY_TOKEN`, `NTFY_MARKDOWN`,
`NTFY_ALLOWED_USERS`, `NTFY_ALLOW_ALL_USERS`, `NTFY_HOME_CHANNEL`.

Reference: https://ntfy.sh/docs/
