# Gene Contract (v0.3.1, static-library)

## 定位

Gene 是 mutation 模板库。它不是分析器，不负责根因归因；也不是执行器，不直接修改文件。

## Source of Truth

- **主事实源**：本 skill 包内的静态库 `references/gene-library.json`
- 库随 skill 版本一起发布（更新需升级 skill 版本）
- Agent 不应做联网拉取，gene.py 不再有云端调用路径

## 数据格式

`references/gene-library.json` 是一个 JSON 数组，每条记录至少包含：

- `id` 或 `gene_id` — 唯一标识
- `summary` 或 `name` — 简短描述
- `pattern_key` — 失败模式分类（用于 summary 聚合）
- `rule_text` — 规则文本（前 200 字符在 `--list` 中展示）
- `created_at` — 创建时间（可选）

## CLI 命令

```shell
python skills/byted-ark-evolve/scripts/gene.py --list      # 列出全部 gene ժҪ
python skills/byted-ark-evolve/scripts/gene.py --show <id> # 展开单条 gene 全部字段
python skills/byted-ark-evolve/scripts/gene.py --summary   # 库统计 + 高频 pattern_key
```

不再有 `--fetch / --base-url / --api-key / --allow-cache-fallback` 选项。

## Agent Judgment Contract

Step 2.5 中 Agent 阅读：

- 未处理 signal
- attribution 结果
- gene 库中的 `id / summary / pattern_key / rule_text`

Agent 必须回答：

1. 这个 gene 针对什么失败模式？
2. 如果提前应用它的方案，能否避免当前 signal 对应的问题复发？
3. 它是在治根因，还是只缓解表象？

## Candidate Output

推荐写入：`evolution-data/tmp/gene-candidates.json`

字段建议：

- `signal_id`
- `gene_id`
- `reason`
- `confidence`（可选）
- `rejected_genes`（可选）

## 版本更新策略

Gene 库内容随 skill 版本发布。如需新 gene，发布新版 skill（v0.3.2 / v0.5.0 等）覆盖 `references/gene-library.json` 即可。
