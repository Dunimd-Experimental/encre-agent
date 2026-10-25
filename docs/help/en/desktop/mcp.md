MCP (Model Context Protocol) is a standard protocol for connecting the agent to third-party tools/data sources. Add servers under **Settings → MCP Server** and the agent can call those external capabilities.

## Supported transports

| Transport | Description | Example |
| --- | --- | --- |
| stdio | Launch a local command-line process | `npx -y @modelcontextprotocol/server-filesystem` |
| HTTP / SSE | Connect to a remote endpoint, with timeout | `https://example.com/mcp` |

## Adding an MCP server

1. Open **Settings → MCP Server** and click **New**
2. Choose the transport and fill in:
   - Command / Url
   - Arguments (for stdio, e.g. `-y`, package name)
   - Environment variables (extra config for the server)
   - Working directory cwd (optional)
3. Save; the connection status appears.

### Import JSON config

If you already have an MCP config file, paste standard MCP JSON to import quickly:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
      "env": {}
    }
  }
}
```

![MCP server management](/screenshots/mcp-list.png)

## Using MCP tools

- Once connected, the server's exposed tools appear in the agent's available tools
- When a tool call comes from MCP in chat, the card is labeled with the MCP source
- References also show MCP entries where relevant

## FAQ

**Q: MCP server fails to connect?**
A: For stdio, first verify the command runs manually and the path is correct; for HTTP/SSE, check address reachability, timeout settings, and complete env vars.

**Q: Are MCP tools safe?**
A: MCP tool execution still obeys [Permissions](/en/desktop/permissions); dangerous operations ask for approval first.