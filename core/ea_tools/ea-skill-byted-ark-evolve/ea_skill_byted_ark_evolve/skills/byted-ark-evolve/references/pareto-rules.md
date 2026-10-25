# Pareto Constraint Rules

## Core Principle

**Quality > Reliability > Efficiency > Cost**

不允许牺牲质量换效率。

## Pareto Check for Mutations

```
对于任何进化变异 M:

IF M.quality_after < M.quality_before:
    BLOCK — 不允许自动执行
    → 生成退化报告，交给用户决策

IF M.quality_after >= M.quality_before AND any_dimension_improves:
    ACCEPT — 帕累托改进

IF M.quality_after >= M.quality_before AND no_dimension_improves:
    SKIP — 无意义的变异
```

## Evaluation Dimensions

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Quality | 0.40 | 输出的准确性、完整性、适用性 |
| Reliability | 0.25 | 一致性、可重复性、无 regression |
| Efficiency | 0.15 | 完成速度、token 消耗 |
| Cost | 0.10 | 直接的 API/计算成本 |
| Reusability | 0.10 | 跨任务/跨 session 的复用价值 |

## Verification Standards

### Three-Level Verification

| ״̬ | 条件 | 含义 |
|------|------|------|
| 待验证 | 刚提出 | 等待在真实场景中验证 |
| 已观察 | 1 credible credit | 初步验证，但不可信赖 |
| 已验证 | ≥3 credible credits, 跨 session | 可信的行为改变 |
| 已复发 | 已验证后再次违反 | 需要加固 |
| 部分生效 | 部分场景有效 | 部分未覆盖 |

### Observation Source Weights

| Source | Weight | Notes |
|--------|--------|-------|
| user_confirmed | 1 credit | 用户明确确认行为正确 |
| no_negative | 0.2 credit | 相关场景完成，用户未纠正 |
| self_reported | 0.2 credit | Agent 自己声称遵守（最弱） |

「已验证」需要 3 credible credits（非 3 次原始计数）。

## Saturation Detection

```
IF 最近 3 次进化的 mutation_count <= 1:
    → 降低进化频率
    → 通知用户："进化趋于饱和，建议关注新方向"
```

## Cost Tracking

每次进化分析必须记录成本：
- `evolution_cost`: 本次分析消耗
- `analyzed_sessions_cost`: 被分析 session 的总消耗
- `roi_ratio`: evolution_cost / analyzed_sessions_cost

目标：ROI ratio < 0.25（进化成本不超过被分析内容成本的 25%）
