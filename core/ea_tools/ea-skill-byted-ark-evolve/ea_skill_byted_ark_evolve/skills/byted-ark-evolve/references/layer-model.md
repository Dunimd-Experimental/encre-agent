# OpenClaw 5-Layer Evolution Model

## Layer Mapping

| Layer | 层级 | OpenClaw 文件 | 进化内容 | 写入权限 |
|-------|------|--------------|---------|---------|
| **L1 Identity** | 身份 | SOUL.md, IDENTITY.md | 价值观、原则、角色定位 | 必须用户确认 |
| **L2 Context** | 上下文 | USER.md, MEMORY.md, memory/*.md | 用户偏好、环境知识、历史记忆 | USER 重大变更需确认，memory 自动 |
| **L3 Protocol** | 协议 | AGENTS.md, TOOLS.md | 行为规则、工具使用规范、决策模式 | 必须用户确认 |
| **L4 Capability** | 能力 | skills/, plugins/ | 技能定义、方法论 | 重大变更需确认 |
| **L5 Runtime** | 运行时 | openclaw.json | 模型选择、channel 配置 | 通常不修改 |

## Attribution Rules

```
问自己：这个问题的根因在哪一层？

- 价值观/原则性问题 → L1 Identity
  例："该不该先对齐再执行"

- 用户偏好/个人知识 → L2 Context
  例："用户喜欢简洁回复"

- 跨任务决策模式/行为规则 → L3 Protocol
  例："收到模糊指令如何处理"、"编码规范"

- 特定任务方法论 → L4 Capability
  例："怎么系统性 debug"、"搜索策略"

- 模型/配置问题 → L5 Runtime
  例："这个任务需要更强的模型"

错误归因 = 治标不治本。
Protocol 层的问题包装成 Capability 不会真正解决。
```

## Context Injection Order

OpenClaw 每次 session 的 system prompt 注入顺序：

```
1. SOUL.md + IDENTITY.md    → Agent 的身份和价值观
2. USER.md                  → 用户模型
3. MEMORY.md + memory/*.md  → 持久化记忆
4. AGENTS.md                → 行为规则
5. TOOLS.md                 → 工具使用指南
6. Skills list (names only) → 可用技能列表
7. Channel context          → 当前通道信息
```

进化写入任何文件后，下次 session 立刻生效（写入即生效）。

## Evolution Priority

按层影响范围排序（从大到小）：

1. **L1 Identity** — 影响所有行为，慎重修改
2. **L3 Protocol** — 影响决策模式，次优先
3. **L2 Context** — 影响知识储备，可频繁更新
4. **L4 Capability** — 影响特定任务，按需修改
5. **L5 Runtime** — 影响运行配置，极少修改
