# ea-connector-teams

System-default Microsoft Teams connector plugin for Encre Agent.

Bridges Microsoft Teams into the Encre Agent gateway through the Microsoft
Bot Framework connection handler webhook: Teams pushes conversation activity
events, which the adapter normalises into gateway message events, and agent
replies are posted back as activity messages and adaptive cards. Ships as a
plugin so the connector lifecycle (registration, start/stop, teardown)
follows the plugin system instead of being compiled into the core gateway.

## Configuration

Required environment variables (or per-adapter settings):

- `TEAMS_CLIENT_ID` — Microsoft Entra (AAD) app client ID
- `TEAMS_CLIENT_SECRET` — Microsoft Entra (AAD) app client secret
- `TEAMS_TENANT_ID` — Microsoft Entra (AAD) tenant ID

Optional extras: `TEAMS_PORT`, `TEAMS_SERVICE_URL`, `TEAMS_HOME_CHANNEL`,
`TEAMS_ALLOWED_USERS`, `TEAMS_ALLOW_ALL_USERS`, `TEAMS_DELIVERY_MODE`,
`TEAMS_INCOMING_WEBHOOK_URL`, `TEAMS_GRAPH_ACCESS_TOKEN`, `TEAMS_TEAM_ID`,
`TEAMS_CHANNEL_ID`, `TEAMS_CHAT_ID`.

Requires the `microsoft-teams-apps` and `aiohttp` Python packages
(install with the `all` extra).

Reference: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/what-are-bots
