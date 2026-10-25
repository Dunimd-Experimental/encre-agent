# ea-connector-dingtalk

System-default DingTalk connector plugin for Encre Agent.

Bridges a DingTalk organization robot (Stream Mode) into the Encre Agent
gateway as a first-class platform adapter. The `dingtalk-stream` SDK owns
the long-lived WebSocket that pushes chatbot callbacks; replies go to the
per-conversation session webhook (markdown), optionally rendered as AI
Cards through the Alibaba Cloud OpenAPI SDK. Ships as a plugin so the
connector lifecycle (registration, start/stop, teardown) follows the
plugin system instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `DINGTALK_CLIENT_ID` — DingTalk application key
- `DINGTALK_CLIENT_SECRET` — DingTalk application secret

Optional extras: `DINGTALK_REQUIRE_MENTION`, `DINGTALK_MENTION_PATTERNS`,
`DINGTALK_FREE_RESPONSE_CHATS`, `DINGTALK_ALLOWED_CHATS`,
`DINGTALK_ALLOWED_USERS` (staff/sender id allowlist, `*` = any).

Reference: https://open.dingtalk.com/document/orgapp/create-and-configure-a-robot
