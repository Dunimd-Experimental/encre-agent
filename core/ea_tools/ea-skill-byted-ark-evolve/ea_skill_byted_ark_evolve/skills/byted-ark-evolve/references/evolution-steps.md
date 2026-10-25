## 自动编排入口（v0.3.1）

### timer-review

```shell
python skills/byted-ark-evolve/scripts/orchestrator.py timer-review --worker-mode agent
```

夜间运行时完成：
1. 加载 `references/gene-library.json`（静态库）
2. 选择未处理 signal
3. 创建 review 记录（status=`running`）
4. 生成 workset
5. 调用本地 review worker（agent 模式）或 mock worker（测试模式）
6. 落库 `gene_matches` / `mutations`（`proposal_status=pending`）
7. 导出 `pending-evolution.json` / `daily-digest.json`
8. review 置为 `completed` / `failed`

### session-start

```shell
python skills/byted-ark-evolve/scripts/orchestrator.py session-start
```

用户进入 session 时完成：
1. 导出最新 pending/digest
2. 查询 `proposal_status in ('pending','presented')`
3. 将首次可见的 proposal 标记为 `presented`
4. 输出简短摘要，供 session_start 展示给用户

# Evolution Analysis — 详细 7 步流程

`/evolve` 触发时执行。

## Step 0: Snapshot Diff（兜底变更检测）

对比上次快照，捕获 Hook 漏掉的 workspace 变更（手动编辑、Bash 写入等）。

```shell
python skills/byted-ark-evolve/scripts/snapshot.py diff --record
```

流程：
1. 加载 `evolution-data/snapshot.json`（上次进化分析后保存的快照）
2. 扫描当前 workspace 所有文件，计算 SHA256 hash
3. 对比差异：added / modified / removed
4. 排除已有 `user-direct` 记录的文件（Hook 已追踪的）
5. 剩余差异写入 mutations 表（`source='snapshot-diff'`）
6. 这些变更缺失意图信息，进化分析时仅标注"检测到变更"

如果 snapshot.json 不存在（首次运行），跳过此步，直接进 Step 1。

---

## Step 1: 扫描信号

```shell
python skills/byted-ark-evolve/scripts/query.py signals --unprocessed
```

读取未处理的信号，按 layer 分组统计。
输出：每个 layer 的信号数量、severity 分布、高频信号。

## Step 1.5: 轨迹聚类 → Skill 候选检测

检查已有轨迹中是否存在可提取为 skill 的模式。

```shell
python skills/byted-ark-evolve/scripts/trajectory-skill-check.py
```

### Golden 轨迹 → 新 Skill 候选

```
1. 按 task_type 分组查询 golden 轨迹
2. 某个 task_type 下 ≥3 条 → 触发候选
3. 提取共同步骤、工具链、输入输出模式
4. 呈现给用户：
   - "检测到 [task_type] 类任务已有 N 条成功轨迹，建议提取为独立 skill"
   - 列出共同步骤摘要
5. 用户确认 → 调用 skill-creator 生成 SKILL.md
6. 用户未安装 skill-creator → 提示安装
```

### Correction 轨迹 → 防护规则候选

```
1. 按 root_cause 分组查询 correction 轨迹
2. ͬһ root_cause ≥2 条 → 触发候选
3. 检查是否已有相关 skill：
   a. 有 → 建议在该 skill 中加入 "Common Mistakes" section
   b. 无 → 建议新建防护类 skill
4. 呈现给用户确认 → 确认后同样调用 skill-creator
```

如果没有触发任何候选，跳过此步。

---

## Step 2: 归因分析

对每组重复/相关信号，归因到具体层和文件。

### 归因流程

```
1. 读取 file-registry.json 获取当前文件状态
2. 读取 references/layer-model.md 获取归因规则
3. 对每组信号：
   a. 确定根因所在的层（Identity/Context/Protocol/Capability）
   b. 确定受影响的具体文件
   c. 检查 file-registry.json 中该文件的 status 和 write_policy
```

### 归因示例

```
"不要用 curl 发中文" × 4 次
  → 根因：AGENTS.md 缺少编码规则
  → 层归因：Protocol（Layer 3）
  → 文件状态：user-owned → 只能 append
  → 建议：在 AGENTS.md 末尾加入中文编码规范 section
```

### needs_review 文件处理

如果归因指向一个 needs_review 文件：
1. 读取该文件内容
2. 推断其语义（layer、governs、write_tier）
3. 更新 file-registry.json 中的分类
4. 然后继续正常归因

## Step 2.5: Gene 候选筛选

在完成归因后，加载本 skill 包内的静态 gene 库，由 Agent 基于 signal + attribution + gene 内容做候选判断。

```shell
python skills/byted-ark-evolve/scripts/gene.py --list
```

### 规则

1. **Gene 静态库（`references/gene-library.json`）是主事实源**，随 skill 版本发布
2. `gene.py` **不做本地匹配逻辑**；它只负责加载和展示 gene
3. Agent 阅读：
   - 当前未处理 signal（尤其是 correction / negative / error）
   - Step 2 的 attribution 结果
   - gene 列表中每个 gene 的 `id / summary / pattern_key / rule_text`
4. Agent 对每个相关 signal 判断：
   - 哪些 gene 的解决方案如果提前应用，能避免该负向反馈或 error 再次发生
   - 哪些 gene 只能缓解表象，不能解决根因
   - 哪些 gene 与当前问题无关
5. Agent 输出 `evolution-data/tmp/gene-candidates.json`，至少包含：
   - `signal_id`
   - `gene_id`
   - `reason`
   - 可选 `confidence`

### 输出示例

```json
{
  "selected_genes": [
    {
      "signal_id": 12,
      "gene_id": "gene_abc123",
      "reason": "该 gene 强调工具文档优先与环境预检；若提前应用，可避免在未确认环境状态下重复重试命令",
      "confidence": 0.82
    }
  ]
}
```

## Step 3: 设计 Mutation

Agent 基于 attribution + `gene-candidates.json` + 当前 workspace 可修改点生成 mutation。

每个 mutation 包含：

```json
{
  "target_file": "AGENTS.md",
  "mutation_type": "add",           // add / modify / remove
  "layer": "protocol",
  "description": "加入中文编码规范",
  "before_text": null,              // modify/remove 时填写
  "after_text": "## 中文编码规范\n...",
  "signal_ids": [1, 2, 5, 8],
  "write_policy": "append-only",    // 从 file-registry 获取
  "pareto_check": "ACCEPT",
  "verification_criteria": "下次涉及中文 API 调用时，自动使用 Python 而非 curl",
  "gene_id": "gene_abc123",
  "gene_reason": "该 gene 的方案可预防当前错误再次发生"
}
```

### Write Policy 约束

| File Status | 允许的 mutation_type |
|------------|-------------------|
| evolvable | add, modify, remove |
| user-owned | add only（append section） |
| skill-owned | 不允许（由 skill 升级流程管理） |
| needs_review | 先分类，再决定 |

## Step 4: 用户确认

**必须在对话中逐条展示所有 mutation，用户明确回复前不执行任何写入。**

### 展示格式

```
### 进化方案（共 N 条变更）

1. 📝 [目标文件] — 变更描述
   - 层归因：<layer>
   - 变更类型：<add/modify/remove>（write_policy 说明）
   - 帕累托检查：✅/❌
   - Before/After 摘要（modify/remove 时展示）

...

以上 N 条变更是否执行？你可以：
- 全部接受
- 全部拒绝
- 逐条选择（如"接受 1，拒绝 2"）
```

### 展示规则

- 每条 mutation 列出：目标文件、变更类型、层归因、帕累托结果
- user-owned 文件标注"仅允许追加"
- modify/remove 类变更展示 Before → After ժҪ
- 如果变更较长，展示关键行 + 省略号，完整内容在报告中查看

### 用户回复处理

- 全部接受 → 执行所有写入
- 全部拒绝 → 所有 mutation 标记 `rejected`
- 逐条选择 → 按用户指定执行/拒绝
- 用户未回复 → 等待，不主动执行

## Step 5: 生成报告 + Dashboard

### 5a. 组装报告数据（JSON）

基于前面步骤的分析结果，组装一个 JSON 文件。JSON 只包含纯数据，不包含任何 HTML 或渲染逻辑。

参考 schema：`references/report-schema-example.json`

必填字段：
- `date`, `summary`, `stats`（signals / changes / rejected）
- `changes`（每条含 file / description / reason / status，可选 before/after）
- `signals_by_layer`, `next_steps`

可选字段：
- `high_severity_signals`, `all_signals`, `trajectory`, `saturation`, `cost`, `user_direct_changes`

写入路径：`evolution-data/reports/evolution-<date>.json`

### 5b. 渲染报告 HTML

```shell
python skills/byted-ark-evolve/scripts/report-render.py \
  evolution-data/reports/evolution-<date>.json \
  --output evolution-data/reports/evolution-<date>.html
```

Python 脚本处理所有条件渲染（有/无拒绝、高严重度信号、饱和状态等），Agent 不需要操心 HTML 结构。

### 5c. 更新 Dashboard（可选，有 dashboard 数据时执行）

组装 dashboard JSON，参考 schema：`references/dashboard-schema-example.json`

```shell
python skills/byted-ark-evolve/scripts/dashboard-render.py \
  evolution-data/dashboard-data.json \
  --output evolution-data/dashboard.html
```

Dashboard 包含：3 卡片统计、Gate 状态、时间线、验证状态（按优先级分组）、信号趋势图、能力概览、报告归档。

## Step 5.4: 导出 pending proposal / digest

在夜间 review 或离线分析结束后，先将待处理提案导出为文件，供后续 session_start 展示。

```shell
python skills/byted-ark-evolve/scripts/pending-evolution.py export
```

导出结果：
- `evolution-data/pending-evolution.json`：给用户看的未处理提案队列
- `evolution-data/daily-digest.json`：夜间总结摘要

规则：
- 这两个文件只是导出层，**DB 才是主状态源**
- proposal 不是“次日晨报一次性提醒”，而是未处理队列；若第二天下午或第三天才打开 session，只要 proposal 仍然有效，就仍可展示
- 过期/被更新覆盖的 proposal 应标记为 `stale` / `superseded`，避免反复提醒旧结论

## Step 5.5: 进化总结（对话输出）

报告生成后，在对话中直接输出一段自然语言总结。这不是写入文件，而是直接告知用户。

### 输出模板

```
本次进化处理了 X 条反馈，产生 Y 条变更：
- [文件A] 新增了「XXX」— 因为你 N 次提到 YYY
- [文件B] 修改了 ZZZ — 增加了某某逻辑

被拒绝：W 条
完整报告：evolution-data/reports/evolution-<date>.html

下一步：当你再次遇到相关场景时，我会观察这些改进是否生效。
```

### 规则

- 用自然语言，不用术语（不说 mutation / signal / layer / severity）
- 每条变更说"改了什么" + "为什么改"（关联到用户原始反馈）
- 告知 HTML 报告路径，但不以路径为主要输出
- 提及后续验证计划（让用户知道改进会被跟踪）
- 如果有被拒绝的变更，简要说明

---

## Step 5.6: session_start չʾ pending 提案

每次 session_start 时检查 DB / `pending-evolution.json` 是否存在未处理提案：

- 首次展示：完整摘要
- 再次展示：简短提醒 + 可展开详情
- 若用户接受：将 proposal / mutation 标记为 `accepted`，再进入 apply
- 若用户拒绝：标记 `rejected`
- 若超过阈值或被新提案覆盖：标记 `stale` / `superseded`

## Step 6: 标记信号已处理

所有与本次进化相关的信号标记 `processed = 1`。
记录 evolution_run 到 DB。

## Step 7: 更新快照

```shell
python skills/byted-ark-evolve/scripts/snapshot.py save
```

保存当前 workspace 文件的 SHA256 快照到 `evolution-data/snapshot.json`。
下次进化分析的 Step 0 会对比此快照检测遗漏变更。


### apply-proposal（v0.2.4）

```shell
python skills/byted-ark-evolve/scripts/orchestrator.py apply-proposal --group <proposal_group_id>
```

流程：
1. 校验该 proposal_group 的状态
2. 逐条应用 mutation
3. 每个 proposal_group 执行一次 git commit
4. 保存 snapshot
5. 写回 review/result（commit hash / applied ids / failed ids）
