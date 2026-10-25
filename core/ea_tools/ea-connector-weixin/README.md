# ea-connector-weixin

System-default WeChat connector plugin for Encre Agent.

Bridges a personal WeChat account, authenticated through Tencent's iLink
Bot API, into the Encre Agent gateway as a first-class platform adapter.
Inbound traffic arrives via long polling on ``ilink/bot/getupdates``;
replies go out through ``ilink/bot/sendmessage`` with per-message chunking,
and media travels through ``ilink/bot/getuploadurl`` plus an AES-128-ECB
encrypted POST to the WeChat CDN. Ships as a plugin so the connector
lifecycle (registration, start/stop, teardown) follows the plugin system
instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `WEIXIN_BOT_TOKEN` — iLink Bot token

Accounts bind by scanning a QR code; credentials and per-account
`context_token` values persist (encrypted) under the Encre data directory,
so a restart keeps conversational continuity.

Reference: https://www.wechatbot.dev/zh/protocol
