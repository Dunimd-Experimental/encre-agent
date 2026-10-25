# byted-ark-evolve

Agent self-evolution skill for OpenClaw / agentskills.io. Collects feedback signals from user interactions, accumulates execution trajectories (golden + correction pairs), and proposes mutations to your workspace files (SOUL.md / AGENTS.md / TOOLS.md / USER.md / MEMORY.md) through a Pareto-checked, user-approved evolution pipeline.

## 核心概念

- **Signal** — 从用户反馈中提取的反馈信号（correction / negative / positive / suggestion / preference / clarification）
- **Trajectory** — Golden（成功执行）/ Correction（错误→修正）对，作为后续进化的训练样本
- **Mutation** — 对 workspace 文件的建议变更，**所有变更必须用户确认才会执行**
- **Pareto 约束** — 任何 mutation 如果导致 Quality / Reliability / Efficiency / Cost 维度退化，BLOCK 并请用户决策
- **Gene** — Mutation 标准化模板库（v0.3.1 起本地静态打包，无云端依赖）

## 快速开始

### 1. 首次安装初始化

```
/evolve init
```

扫描 workspace 文件、生成 file-registry、初始化 SQLite DB（`~/.{runtime}/workspace/evolution-data/evolution.db` (runtime: arkclaw / openclaw / 自动检测)）。

### 2. 日常使用

正常工作即可，agent 会自动从你的反馈中提取信号。也可以手动：

| 你说 | 效果 |
|------|------|
| "记住这个模式" | 存为 golden trajectory |
| "以后别这样做" | 存为 correction signal |
| `/evolve` | 触发完整进化分析 + 报告 |
| `/evolve dashboard` | 生成 HTML Dashboard |
| `/evolve scan` | 扫描历史对话提取已有反馈 |

### 3. 查询状态

```shell
python skills/byted-ark-evolve/scripts/query.py dashboard
python skills/byted-ark-evolve/scripts/query.py signals --unprocessed
python skills/byted-ark-evolve/scripts/query.py mutations --status pending
python skills/byted-ark-evolve/scripts/gene.py --summary
```

## 文件结构

```
byted-ark-evolve/
├── README.md                 # 本文件
├── SKILL.md                  # 主入口与触发规则（agent 加载点）
├── scripts/                  # 19 个 Python 脚本（纯 stdlib，零外部依赖）
│   ├── orchestrator.py       # /evolve 主流程（timer-review / session-start / apply-proposal）
│   ├── workspace-init.py     # 初始化扫描 + file-registry 生成
│   ├── apply-proposal.py     # 应用通过审核的 mutation（含 git commit）
│   ├── dashboard-render.py   # 生成 dashboard.html
│   ├── gene.py               # 静态 gene 库读取（list / show / summary）
│   ├── query.py              # 查询 signals / mutations / trajectories
│   ├── signal-record.py      # 记录信号到 SQLite
│   ├── scan-history.py       # 扫描历史对话提取信号
│   └── ...
├── references/               # 详细规则文档（按需读取，不随 session 加载）
│   ├── layer-model.md        # 5 层归因模型（Identity / Context / Protocol / Capability / Runtime）
│   ├── pareto-rules.md       # 帕累托约束 + 验证标准
│   ├── signal-types.md       # 信号识别规则
│   ├── evolution-steps.md    # 进化分析详细流程（Step 0-7）
│   ├── gene-contract.md      # Gene 库格式与使用约定
│   ├── gene-library.json     # 静态 gene 模板库（v0.3.1 起本地打包）
│   └── ...
└── test-conversations/       # 学习样本对话
```

## 数据存储

所有运行时数据写入 `~/.{runtime}/workspace/evolution-data/`：

```
evolution-data/
├── evolution.db          # SQLite（signals / mutations / reviews / trajectories / gene_matches）
├── file-registry.json    # workspace 文件分类注册表
├── snapshot.json         # 文件 hash 快照（兜底变更检测）
├── dashboard.html        # 自动生成的 Dashboard
├── pending-evolution.json
├── daily-digest.json
├── trajectories/{golden,corrections}/
├── reports/              # 进化报告（JSON + HTML）
└── tmp/                  # 临时 workset / candidates
```

## 系统要求

- Python 3.9+（脚本使用 dataclass / type hints / pathlib）
- SQLite 3（Python stdlib `sqlite3`）
- 操作系统：Linux / macOS / Windows（脚本含 Windows UTF-8 修复）
- **零外部 Python 依赖**

## 重要原则

1. **Quality > Reliability > Efficiency > Cost** — 不允许牺牲质量换效率
2. **写入即生效** — 修改 workspace 文件后，下次 session 立刻生效
3. **先对齐再执行** — 进化方案必须先呈现给用户确认
4. **所有进化变更必须通过 orchestrator pipeline 执行** — 禁止 agent 自行修改 workspace 文件
5. **所有 mutation 须经用户确认** — 即使通过帕累托检查也不自动 apply

详细原则与规则请阅读 `SKILL.md` 与 `references/` 目录。

## v0.3.1 主要改动

- Gene 库本地化：删除云端 `--fetch` / API key / endpoint 配置，改为打包静态 `references/gene-library.json`（64 gene）
- 路径统一：所有 inline 命令使用 `skills/byted-ark-evolve/`
- 文件行尾统一为 LF（修复 Linux shebang 兼容性）

## 版本

v0.3.1 — 2026-04
