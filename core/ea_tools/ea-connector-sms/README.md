# ea-connector-sms

System-default SMS (Twilio) connector plugin for Encre Agent.

Bridges the cellular SMS network into the Encre Agent gateway through a
Twilio account: the adapter opens a local HTTP webhook for inbound
deliveries (HMAC-verified) and calls the Twilio REST API for outbound
sends, chunking replies to the SMS segment budget. Ships as a plugin so the
connector lifecycle (registration, start/stop, teardown) follows the plugin
system instead of being compiled into the core gateway.

## Configuration

Required environment variables:

- `TWILIO_ACCOUNT_SID` — Twilio account SID
- `TWILIO_AUTH_TOKEN` — Twilio auth token
- `TWILIO_PHONE_NUMBER` — E.164 from-number (e.g. `+15551234567`)

Gateway-side extras: `SMS_WEBHOOK_PORT` (default 8080), `SMS_WEBHOOK_HOST`
(default 127.0.0.1), `SMS_WEBHOOK_URL` (the public URL Twilio posts to,
required for signature verification), `SMS_INSECURE_NO_SIGNATURE`,
`SMS_ALLOWED_USERS`, `SMS_ALLOW_ALL_USERS`, `SMS_HOME_CHANNEL`.

Requires the `aiohttp` Python package (install with the `all` extra).
