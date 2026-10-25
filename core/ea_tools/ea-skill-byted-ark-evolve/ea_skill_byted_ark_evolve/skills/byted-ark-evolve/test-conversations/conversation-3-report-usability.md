# 测试对话 3：报告易用性改进

**场景**：用户查看 Agent 生成的进化报告后，连续提出多条改进建议，涉及文件标注、术语解释、自动产出等。

**预期信号**：3 条 suggestion（capability × 2, protocol × 1）

---

## 对话

**User**: 把进化报告给我看看

**Agent**: 报告已生成：`evolution-data/reports/evolution-20260312.html`

（用户打开报告查看）

**User**: 变更前和变更后的那部分，有没有可能把具体变更的文件标明出来

**Agent**: 好建议，我来调整报告模板，在 Before/After diff 头部加上目标文件名。

（Agent 修改了 report-gen.py）

**User**: 还有几个问题。ROI 比率、session、协议、能力这些关键词，是不是要用灰色字做解释？对第一次使用的用户比较友好

**Agent**: 确实，这些术语对新用户不太友好。我加一些灰色提示文字在相关模块下面。

**User**: 对了，进化报告不应该需要我手动触发吧？完成进化后应该自动产出

**Agent**: 你说得对。完成进化分析 + 用户确认后，报告应该自动生成，不需要额外触发。我更新一下流程。

---

## 预期信号提取

```json
[
  {
    "type": "suggestion",
    "layer": "capability",
    "severity": "low",
    "raw_text": "变更前和变更后的那部分，有没有可能把具体变更的文件标明出来",
    "context": "用户查看进化报告，发现 Before/After diff 区域没有标注目标文件名"
  },
  {
    "type": "suggestion",
    "layer": "capability",
    "severity": "medium",
    "raw_text": "ROI比率、session、协议、能力等关键词要用灰色字做解释，对第一次使用的用户比较友好",
    "context": "用户查看进化报告，认为术语对新用户不友好，建议加灰色提示文字"
  },
  {
    "type": "suggestion",
    "layer": "protocol",
    "severity": "low",
    "raw_text": "进化报告不用用户触发，完成进化自动产出",
    "context": "用户认为进化报告应在确认 mutation 后自动生成，不需要手动触发"
  }
]
```

## 预期归因

- Signal 1 → capability 层，report-gen.py 模板缺少文件名标注
- Signal 2 → capability 层，report-gen.py 模板缺少术语解释
- Signal 3 → protocol 层，进化流程定义缺少"自动生成报告"步骤
- 潜在 Mutation：修改 report-gen.py 模板 + 更新 evolution-steps.md 流程
