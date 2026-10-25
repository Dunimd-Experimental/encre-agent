# File Semantic Map

已知 OpenClaw workspace 文件的语义映射。初始化和进化归因时引用。

## Workspace 根目录文件

| File | Layer | Governs | Write Risk | Notes |
|------|-------|---------|------------|-------|
| SOUL.md | identity | tone, values, style, boundaries | high | Agent 的"灵魂"，改动影响全局风格 |
| IDENTITY.md | identity | name, persona, appearance | high | Agent 自我认知，改动影响自称方式 |
| USER.md | context | user-prefs, timezone, conventions | medium | 用户模型，重大变更需确认 |
| MEMORY.md | context | long-term-memory | medium | 长期记忆，主会话才载入 |
| AGENTS.md | protocol | rules, permissions, workflows, red-lines | high | 行为准则，改动影响决策模式 |
| TOOLS.md | protocol | device-config, search-prefs, local-env | low | 环境配置，改动风险低 |
| HEARTBEAT.md | protocol | periodic-checks, proactive-tasks | low | 周期任务清单 |
| BOOTSTRAP.md | protocol | first-run-setup | low | 首次启动流程，通常一次性 |

## 子目录

| Path Pattern | Layer | Governs | Write Risk | Notes |
|-------------|-------|---------|------------|-------|
| memory/*.md | context | daily-logs, short-term-memory | low | 每日记忆，可自动写入 |
| skills/*/SKILL.md | capability | skill-definition, methods | medium | 技能定义，重大变更需确认 |
| skills/*/scripts/* | capability | skill-logic, automation | medium | 技能实现代码 |
| skills/*/references/* | capability | skill-knowledge, rules | low | 技能参考资料 |
| .{arkclaw,openclaw}/* | runtime | internal-state | high | Claw runtime 内部状态，不修改 |

## 配置文件（workspace 外）

| File | Layer | Governs | Write Risk | Notes |
|------|-------|---------|------------|-------|
| ~/.{arkclaw,openclaw}/{arkclaw,openclaw}.json | runtime | model, channels, gateway, plugins | high | 通常不通过进化修改 |

## 进化归因速查

```
用户说"语气不对" → SOUL.md (identity/tone)
用户说"你不了解我" → USER.md (context/user-prefs)
用户说"你不该这么做" → AGENTS.md (protocol/rules)
用户说"搜索方式不好" → TOOLS.md (protocol/search-prefs) 或 skills/ (capability)
用户说"这个skill有bug" → skills/*/scripts/* (capability/skill-logic)
```
