# ea-connector-webhook

System-default generic Webhook connector plugin for Encre Agent.

Stands up an aiohttp HTTP listener that accepts POSTs from any external
system that can speak HTTP (GitHub, GitLab, JIRA, Stripe, Supabase,
monitoring stacks, other Encre instances) and turns each accepted event
into one agent run. Every route is protected by HMAC signature validation,
per-route rate limiting, body-size gates, and declarative filters with
optional transform scripts; answers can be delivered cross-platform or
straight through without an agent turn (`deliver_only`). Ships as a plugin
so the connector lifecycle (registration, start/stop, teardown) follows the
plugin system instead of being compiled into the core gateway.

## Configuration

Configuration lives in `config.yaml` under `platforms.webhook.extra`.
Adapter-wide keys: `secret`, `port`, `host`, `rate_limit`,
`max_body_bytes`, `script_timeout_seconds`. The `routes` map defines one
entry per route: `events`, `secret` (required), `prompt`, `skills`,
`filters`, `script`, `deliver`, `deliver_extra`, `deliver_only`.

Dynamic subscriptions are hot-reloaded from
`webhook_subscriptions.json` under the data directory.

Security notes: per-route HMAC secrets are validated at startup; the
legacy body-only V1 signature is deprecated; setting a secret to
`INSECURE_NO_AUTH` skips validation (testing only) and is refused on
non-loopback binds.
