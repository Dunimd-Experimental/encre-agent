# ea-connector-photon

System-default Photon (iMessage) connector plugin for Encre Agent.

Bridges the Photon Spectrum (iMessage) platform into the Encre Agent gateway
as a first-class platform adapter. Both directions of traffic flow through a
small supervised Node sidecar that runs the `spectrum-ts` SDK; inbound
messages stream over a loopback NDJSON endpoint, outbound sends and media
attachments go through the sidecar's control endpoints. Ships as a plugin so
the connector lifecycle (registration, start/stop, teardown) follows the
plugin system instead of being compiled into the core gateway.

## Configuration

Credentials are resolved from `config.yaml` `platforms.photon.extra`, then
the environment, then the on-disk credential store written by
`encre photon setup`:

- `PHOTON_PROJECT_ID` — Photon project id
- `PHOTON_PROJECT_SECRET` — Photon project secret

Optional extras: `PHOTON_SIDECAR_PORT`, `PHOTON_SIDECAR_TOKEN`,
`PHOTON_SIDECAR_AUTOSTART`, `PHOTON_NODE_BIN`, `PHOTON_HOME_CHANNEL`,
`PHOTON_MARKDOWN`.

Runtime requirements: a `node` executable on `PATH` and the sidecar's
`node_modules` installed (provisioned by `encre photon setup`).
