# ea-connector-raft

System-default Raft connector plugin for Encre Agent.

Bridges the Raft chat protocol into the Encre Agent gateway as a first-class
platform adapter. Unlike REST-style adapters it runs a *local server*: the
adapter binds an aiohttp endpoint, spawns `raft agent bridge` as a child
process, and the bridge wakes the agent via content-free wake hints while
Encre talks back to Raft through the `raft` CLI. A bounded activity queue
lets the bridge report agent activity back without polling. Ships as a
plugin so the connector lifecycle (registration, start/stop, teardown)
follows the plugin system instead of being compiled into the core gateway.

## Configuration

Configuration lives in `config.yaml` under `platforms.raft.extra`:
`host`, `port`, `path`, `bridge_token`, `runtime_session`,
`max_body_bytes`. Setting `RAFT_PROFILE` auto-enables the platform.

Runtime requirements: `aiohttp` (already an Encre dependency) and a `raft`
executable on `PATH` (needed to spawn the bridge and for CLI delivery).
