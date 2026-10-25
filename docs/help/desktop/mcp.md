MCP（Model Context Protocol）是让 Agent 接入第三方工具/数据源的标准协议。在 **设置 → MCP Server** 中添加服务器后，Agent 就能调用这些外部能力。

## 支持的接入方式

| 方式 | 说明 | 示例 |
| --- | --- | --- |
| stdio | 本地启动一个命令行进程 | `npx -y @modelcontextprotocol/server-filesystem` |
| HTTP / SSE | 连接远程服务地址，可配置超时 | `https://example.com/mcp` |

## 添加 MCP 服务器

1. 打开 **设置 → MCP Server**，点击「新建」
2. 选择传输方式并填写：
   - 命令 / Url
   - 参数（stdio 时，例如 `-y`、包名）
   - 环境变量（Environment，可给服务器传入配置）
   - 工作目录 cwd（可选）
3. 保存后连接状态会展示

### 导入 JSON 配置

如果已有 MCP 配置文件，可以直接粘贴标准 MCP JSON 快速导入：

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

![MCP Server 管理](/screenshots/mcp-list.png)

## 使用 MCP 工具

- 服务器连接成功后，其暴露的工具会出现在 Agent 可用工具集中
- 对话中出现 MCP 相关工具调用时，卡片会标注 MCP 来源
- 引用参考中也会显示来自 MCP 的条目

## FAQ

**问：MCP 服务器连接失败？**
答：stdio 方式先确认命令能手动执行、路径正确；HTTP/SSE 方式检查地址可访问性与超时设置，以及相关 env 是否完整。

**问：MCP 工具安全吗？**
答：MCP 工具执行仍受 [权限与安全](/desktop/permissions.md) 管控，危险操作同样会先征求同意。