# ea-connector-homeassistant

System-default Home Assistant connector plugin for Encre Agent.

Treats Home Assistant as a bidirectional local-device channel: inbound
`state_changed` events are watched over the WebSocket API and filtered by
domain/entity allow-lists, a deny-list, and a per-entity cooldown before
reaching the agent; outbound replies are posted as persistent
notifications on a separate REST session so a write never contends with
the socket being read. Ships as a plugin so the connector lifecycle
(registration, start/stop, teardown) follows the plugin system instead of
being compiled into the core gateway.

## Configuration

Required:

- `HASS_TOKEN` — long-lived access token

Optional:

- `HASS_URL` — instance address (default `http://homeassistant.local:8123`)
- Per-platform extras under `platforms.homeassistant` in `config.yaml`:
  `url`, `watch_domains`, `watch_entities`, `ignore_entities`,
  `watch_all`, `cooldown_seconds`.

Note: with no filters configured the adapter forwards nothing (closed by
default), so a noisy install cannot flood the agent.

## Optional dependencies

- `aiohttp` (install with the `all` extra)

Reference: https://www.home-assistant.io/docs/authentication/
