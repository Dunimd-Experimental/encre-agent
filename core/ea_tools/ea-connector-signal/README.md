# ea-connector-signal

System-default Signal connector plugin for Encre Agent.

Bridges Signal into the Encre Agent gateway through a `signal-cli` HTTP
daemon: inbound envelopes arrive on an SSE event stream, outbound messages
go over the REST API, and per-message rate limits plus a send scheduler
keep the daemon happy. Ships as a plugin so the connector lifecycle
(registration, start/stop, teardown) follows the plugin system instead of
being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `SIGNAL_HTTP_URL` — signal-cli HTTP daemon base URL
- `SIGNAL_ACCOUNT` — Signal account identifier (phone number or UUID)

Reference: https://github.com/AsamK/signal-cli
