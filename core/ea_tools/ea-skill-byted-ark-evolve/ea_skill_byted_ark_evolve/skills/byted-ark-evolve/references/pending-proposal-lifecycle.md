# Pending Proposal Lifecycle (v0.2.2)

## 核心语义

pending proposal 不是“第二天早上一次性提示”，而是**未处理提案队列**。

ֻҪ proposal 仍然有效且未被用户确认/拒绝，就应在后续 session_start 中可见。

## ״̬

- `pending`：已生成，尚未展示给用户
- `presented`：已展示，但未决策
- `accepted`：用户接受，允许应用
- `rejected`：用户拒绝
- `stale`：过久未处理或环境已变化
- `superseded`：被更新的 proposal 覆盖

## 建议转移

- review worker 完成 → `pending`
- 首次 session_start չʾ → `presented`
- 用户确认应用 → `accepted`
- 用户拒绝 → `rejected`
- 超过 72 小时或出现更新 proposal → `stale` / `superseded`
