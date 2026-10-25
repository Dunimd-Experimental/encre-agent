# ea-connector-matrix

System-default Matrix connector plugin for Encre Agent.

Bridges any Matrix homeserver (self-hosted or matrix.org) into the Encre Agent
gateway as a first-class platform adapter via the mautrix Python SDK, with
optional end-to-end encryption (E2EE). Ships as a plugin so the connector
lifecycle (registration, start/stop, teardown) follows the plugin system
instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `MATRIX_HOMESERVER` — Homeserver URL (e.g. `https://matrix.example.org`)
- `MATRIX_ACCESS_TOKEN` — Access token (preferred auth method)

Alternative auth (instead of an access token): `MATRIX_USER_ID` plus
`MATRIX_PASSWORD`.

Optional extras: `MATRIX_E2EE_MODE` (off | optional | required),
`MATRIX_DEVICE_ID`, `MATRIX_PROXY`, `MATRIX_ALLOWED_USERS`,
`MATRIX_ALLOWED_ROOMS`, `MATRIX_HOME_ROOM`, `MATRIX_REQUIRE_MENTION`,
`MATRIX_AUTO_THREAD`, `MATRIX_MAX_MESSAGE_LENGTH`, and more — see the
adapter module docstring for the full list.

Dependencies (E2EE needs the extras):
`pip install "mautrix[encryption]" asyncpg aiosqlite Markdown aiohttp-socks`

Reference: https://matrix.org/
