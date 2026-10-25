技能（Skills）是可随时启用的能力包，命令（Commands）是你自定义的快捷指令。两者都在 **设置 → 技能与命令** 中管理。

## 技能

技能以 Markdown（`SKILL.md`）或 zip 包形式安装，是给 Agent 的即插即用知识/操作说明。例如 Encre Agent 内置多套技能：debug、loop、batch、verify、stuck-recovery、code-review、refactor、gen-test、web-research、data-viz、write-docs 等，覆盖调试、批量处理、代码审查、重构、测试生成、网页研究、数据可视化、文档撰写等场景。

### 启用 / 停用

在技能列表里打开开关即可启用。已启用的技能 Agent 会在合适时机调用。

> 技能还可通过在线技能市场安装更多（延伸能力见关于页的社区入口）。

## 自定义命令（Slash Commands）

命令让你把常用提示词做成快捷指令。在输入框输入 `/` 即可出现命令菜单。

### 创建命令

在「技能与命令」中点击「新建命令」：

| 字段 | 说明 |
| --- | --- |
| 名称 | 斜杠后的名字，例如 `review` → 输入 `/review` |
| 标题 | 命令菜单里显示的名字 |
| 描述 | 说明这个命令做什么 |
| 图标 | 用 lucide 图标库的名称，如 `file-code` |
| 提示词 | 实际发送给 Agent 的内容（可包含占位说明） |

> [!NOTE]
> 命令名称仅允许**字母、数字和连字符**。

自定义命令可以内置常用的 Agent 指令，例如每周周报模板、代码审查口令等，让你和团队用同一套标准提问。

![技能与命令管理](/screenshots/skills-list.png)

## 常用内置命令回顾

输入 `/` 可唤起：`/plan`、`/spec`、`/new-session`、`/clear-session`、`/init` 等（详见 [对话与会话](/desktop/chat.md)）。

## FAQ

**问：技能和命令有什么区别？**
答：技能是给 Agent 的能力/知识扩展（自动或按需调用）；命令是给你用的输入快捷方式，帮你快速发起固定格式的请求。

**问：技能会影响安全吗？**
答：技能只是提示词能力包，实际执行仍受 [权限与安全](/desktop/permissions.md) 管控。