# Trajectory Templates

轨迹存储格式。Agent 写入轨迹时参考此文件。

## Golden Trajectory（做对了）

存储到 `evolution-data/trajectories/golden/<task-type>-<date>.md`

```markdown
---
type: golden
task_type: <任务类型，如 api-call, search, report>
created: <YYYY-MM-DD>
tags: [tag1, tag2]
---

## 场景
<简述用户需求和执行环境>

## 关键步骤
1. <步骤 1>
2. <步骤 2>
3. <步骤 3>

## 可复用 Pattern
- <提炼出的通用规律>
```

### 示例

```markdown
---
type: golden
task_type: api-call
created: 2026-03-11
tags: [encoding, chinese, api]
---

## 场景
用户要求调用 API 发送中文内容

## 关键步骤
1. 用 Python urllib 而非 curl
2. ensure_ascii=True
3. 返回结果用 UTF-8 解码

## 可复用 Pattern
- Windows 环境下涉及中文的 API 调用，走 Python
```

## Correction Trajectory（做错了→修正）

存储到 `evolution-data/trajectories/corrections/<issue>-<date>.md`

```markdown
---
type: correction
task_type: <任务类型>
created: <YYYY-MM-DD>
signal_ref: "<触发此修正的用户原话>"
tags: [tag1, tag2]
---

## 错误做法
<描述错误行为>

## 正确做法
<描述修正后的行为>

## 根因
<为什么会犯这个错>

## 影响的文件
<哪些 workspace 文件需要更新>
```

### 示例

```markdown
---
type: correction
task_type: api-call
created: 2026-03-11
signal_ref: "不要用 curl 发中文"
tags: [encoding, curl, chinese]
---

## 错误做法
用 terminal curl 发送包含中文的 JSON body → 乱码

## 正确做法
用 Python urllib + ensure_ascii=True → 正常

## 根因
Windows terminal 环境下 curl 不支持 UTF-8 传参

## 影响的文件
AGENTS.md — 加入编码规则
```

## 轨迹检索规则

执行任务前：
1. 根据当前任务类型，检索 `corrections/` 下相关文件
2. 匹配方式：task_type 匹配 + tags 交集
3. 如果找到相关修正，在开始执行前主动复述修正要点
4. 复述格式："上次类似任务中学到：<修正内容>，本次将遵循。"
