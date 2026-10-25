# 进化系统 — 新手引导 (v0.3.1)

首次安装进化 Skill 后，Agent 向用户展示此引导。

---

## 什么是进化系统？

进化系统让 Agent 能从你的反馈中学习并改进自己。

简单来说：你用 Agent 的过程中说的"以后别这样"、"这个方法不错"、"能不能改成 XX"，都会被记录下来。攒够一定数量后，系统会分析这些反馈，提出具体的改进方案——但**只有你确认后才会生效**。

## 你需要做什么？

**几乎不需要额外操作。** 日常使用中：

- **正常交流即可** — 你的纠正、表扬、建议会被自动识别和记录
- **确认进化方案** — 当系统提出改进建议时，你需要审核并决定接受或拒绝
- **（可选）主动触发** — 说 `/evolve` 可以随时启动一次进化分析

## 你不需要担心什么？

- **不会偷偷改你的文件** — 所有变更必须经你确认后才执行
- **不会丢失数据** — 被拒绝的方案只是标记为 rejected，不会删除记录
- **不会越改越差** — 进化方案遵循帕累托原则，避免已有能力退化
- **随时可回退** — 每次变更自动创建 git commit，`git revert` 即可撤销

## 交互方式

### 命令

| 命令 | 说明 |
|------|------|
| `/evolve` | 主动触发一次完整进化分析，查看改进建议 |
| `/evolve init` | 首次安装时执行：初始化扫描 + 生成文件注册表 |
| `/evolve scan` | 扫描历史对话，提取已有的反馈信号 |
| `/evolve dashboard` | 生成并打开进化 Dashboard（`evolution-data/dashboard.html`，浏览器查看） |
| `/evolve help` | 展示本引导内容 |

### 自然语言

| 你说 | Agent 做 |
|------|---------|
| "别这样做" / "不要 XX" | 记录 correction 信号 |
| "这个好" / "就是这样" | 记录 positive 信号 |
| "记住这个模式" | 记录 positive 信号，作为后续进化分析的正向参考 |
| "以后别这样做" | 记录 correction 信号，标记需要改进的行为 |
| "我更喜欢简洁的" / "用 X 格式" | 记录 preference 信号（v0.3.1 新增） |
| "我说的 X 指的是 Y" / "你理解错了" | 记录 clarification 信号（v0.3.1 新增） |
| "进化状态" | 查看当前反馈积累量、上次进化时间、验证状态 |
| "进化 Dashboard" | 生成 HTML Dashboard 页面（`evolution-data/dashboard.html`，浏览器打开） |
| "gene 列表" / "gene 概况" | 查看基因模板库和命中情况 |
| "进化报告" | 生成 HTML 格式的进化报告 |

## 常见问题

**Q: 它会改哪些文件？**
A: 主要修改 workspace 内标记为"可进化"（evolvable）的文件。用户自己管理的文件（user-owned）原则上只追加内容。初始化时会扫描并分类所有文件，分类结果保存在 `evolution-data/file-registry.json`。

**Q: 进化多久发生一次？**
A: 有两种方式：

**自动检查**：当累积 ≥5 条 correction/negative/clarification 信号，或 ≥3 条 high severity 信号时，系统会在新对话开始时自动触发分析。

**手动触发**：说 `/evolve` 可以随时启动一次完整分析。

**Q: 我可以回退吗？**
A: 可以。每次进化应用变更时会自动在 workspace 中创建 git commit（commit message 格式：`evolution: apply proposal <group_id>`）。回退方式：
- `git revert <commit>` — 撤销指定的进化变更
- `git log --oneline` — 查看所有进化历史
- 每次进化还会生成 HTML 报告，记录每条变更前后的对比，方便查看改了什么

**Q: 如果我不想用了？**
A: 卸载 evolution skill 即可。已收集的数据保留在 `evolution-data/` 目录（信号、轨迹、报告、Dashboard），不影响其他功能。
