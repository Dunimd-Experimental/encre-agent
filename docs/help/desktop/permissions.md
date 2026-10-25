权限与安全决定 Agent 能"动"到什么程度。默认设置已足够安全，建议只在你熟悉时才放开。在 **设置 → 权限** 中配置。

## 权限模式

每个工具可设置四种策略：

| 策略 | 行为 |
| --- | --- |
| 默认 | 遵循全局默认策略（推荐）：危险操作每一步先询问 |
| 允许 | 允许自动执行，不再询问 |
| 询问 | 每次执行前征求你同意 |
| 禁止 | 完全禁止，Agent 无法调用 |

> [!NOTE]
> 默认策略下，Bash 等危险工具会先征求确认；「询问」只针对单次执行，不会记住放行。

## 可管理的工具

权限面板列出了几乎所有工具，包括：

- bash（终端命令）、ssh
- file_write / file_edit / apply_patch / archive
- docker、deploy、database
- browser、desktop、computer_use
- email、notify、env_manager、cloud_storage
- git、github、manage、swarm、workflow
- lint_format、rest_client、file_api、batch_api、fine_tuning_api
- agent、generate_image、edit_image、transcribe_audio、translate_audio
- create_embeddings、create_moderation
- task_create / task_stop、cron_create / cron_delete
- memory_create / update / delete 等

![权限策略](/screenshots/permissions-list.png)

## 内置权限模式详解

应用预置了多种黄金权限模式，可以在会话中切换：

| 模式 | 说明 |
| --- | --- |
| **默认**（推荐） | 每次操作前询问，最安全 |
| 完全放行（bypass） | 全程无询问，风险最高 |
| 自动允许（dont_ask） | 记录并自动允许 |
| 仅允许编辑（accept_edits） | 自动允许文件编辑类操作 |
| 计划（plan） | 先制定计划，确认后执行 |
| 自动（auto） | 由 AI 判断是否询问 |

> [!WARNING]
> 「完全放行」会让 Agent 直接执行任何操作。仅在你完全信任当前任务与模型时使用。

## 附加防护

Encre Agent 在权限系统外还内置多层防护：

- **SSRF 防护**：DNS + CIDR 黑名单拦截恶意请求
- **Docker 沙箱**、Linux **Landlock** 限制
- **AI 风险分类器**：对操作风险自动分级
- **频率限制**：防止滥用

## FAQ

**问：改乱了设置怎么恢复？**
答：权限面板提供「重置默认值」，一键回到出厂权限。

**问：某工具一直询问很烦？**
答：把该工具策略改为「允许」即可（注意仅在可信任场景这样做）。

**问：为什么我设置了允许，有时还是会问？**
答：某些高危动作受内置防护兜底，即使工具策略为「允许」，风险分级仍可能触发确认。