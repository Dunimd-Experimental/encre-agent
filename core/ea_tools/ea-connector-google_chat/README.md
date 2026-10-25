# ea-connector-google_chat

System-default Google Chat connector plugin for Encre Agent.

Bridges Google Chat (Chat API v1) into the Encre Agent gateway as a
first-class platform adapter: inbound events arrive via a Cloud Pub/Sub
pull subscription (or an authenticated HTTP callback for no-public-URL
deployments), and outbound replies are Chat REST calls executed through
`asyncio.to_thread`. Ships as a plugin so the connector lifecycle
(registration, start/stop, teardown) follows the plugin system instead of
being compiled into the core gateway.

## Configuration

Service-account (Chat app) credentials via env vars or the
`platforms.google_chat` section of `config.yaml`; Pub/Sub subscription
path in `projects/<project>/subscriptions/<sub>` form. Optional
per-user OAuth (the `oauth` helper module) backs native attachment
delivery, because Google Chat's `media.upload` endpoint rejects
service-account authentication.

## Optional dependencies

Install with the `all` extra:

- `google-api-python-client`
- `google-auth`, `google-auth-httplib2`, `google-auth-oauthlib`
- `google-cloud-pubsub`
- `httplib2`
- `aiohttp`

Reference: https://developers.google.com/chat
