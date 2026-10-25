# ea-connector-simplex

System-default SimpleX Chat connector plugin for Encre Agent.

Bridges a running `simplex-chat` daemon (WebSocket control interface) into
the Encre Agent gateway as a first-class platform adapter. Inbound messages
arrive over a persistent WebSocket connection; outbound messages use the
same WebSocket with JSON text commands, including native image, document,
and voice-note delivery. Ships as a plugin so the connector lifecycle
(registration, start/stop, teardown) follows the plugin system instead of
being compiled into the core gateway.

## Setup

Start the daemon, e.g.:

    simplex-chat -p 5225
    # or via Docker:
    docker run -p 5225:5225 simplexchat/simplex-chat-cli -p 5225

## Configuration

Required environment variables (or per-adapter settings):

- `SIMPLEX_WS_URL` — WebSocket URL of the daemon (default: `ws://127.0.0.1:5225`)

Optional extras: `SIMPLEX_ALLOWED_USERS`, `SIMPLEX_ALLOW_ALL_USERS`,
`SIMPLEX_AUTO_ACCEPT`, `SIMPLEX_GROUP_ALLOWED`, `SIMPLEX_HOME_CHANNEL`,
`SIMPLEX_HOME_CHANNEL_NAME`, `ENCRE_SIMPLEX_TEXT_BATCH_DELAY`.

Requires the `websockets` Python package (install with the `all` extra).

Reference: https://simplex.chat/
