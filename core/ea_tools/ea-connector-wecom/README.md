# ea-connector-wecom

System-default WeCom (Enterprise WeChat) connector plugin for Encre Agent.

Bridges WeCom into the Encre Agent gateway as a first-class platform
adapter through two integration modes:

- **AI Bot (WebSocket)** — `WeComAdapter` speaks WeCom's `aibot_*` JSON
  frames over a WebSocket control connection, with media up/downloads via
  HTTP and QR-code bind provisioning (`qr_scan_for_bot_info`).
- **Callback (HTTP)** — `WecomCallbackAdapter` serves WeCom's encrypted
  XML callbacks on a local aiohttp endpoint (AES-256-CBC envelope codec
  with SHA-1 signature verification in `wecom_crypto`) and delivers agent
  replies through the proactive `message/send` API.

Ships as a plugin so the connector lifecycle (registration, start/stop,
teardown) follows the plugin system instead of being compiled into the
core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `WECOM_BOT_ID` — WeCom bot id
- `WECOM_SECRET` — WeCom bot secret

Reference: https://developer.work.weixin.qq.com/document/path/91770
