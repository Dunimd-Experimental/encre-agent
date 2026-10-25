# Initialization Rules

workspace-init.py 的文件状态判定规则。

## 扫描范围

- 根目录：`~/.{runtime}/workspace/` (runtime 自动检测,默认 arkclaw)
- 递归扫描所有文件
- **排除**：`.git/`、`node_modules/`、`__pycache__/`、`evolution-data/`、`.{*claw*}/` (任何 claw runtime Ŀ¼)

## 状态判定流程

```
对每个文件 F:

1. F 路径匹配 skills/byted-ark-evolve/* ?
   → status = "skill-owned"

2. F 路径匹配 .{*claw*}/* 或 {*claw*}.json ?
   → status = "runtime" (跳过，不纳入 registry)

3. F 在已知文件映射中（file-semantic-map.md）?
   → 检查是否为默认模板（Step 4）

4. 默认模板检测：
   a. 包含占位符关键词？ → evolvable
   b. 行数 < 基准行数 × 1.2？ → evolvable
   c. 都不满足 → user-owned

5. F 不在已知文件映射中？
   → status = "needs_review"
```

## 占位符关键词列表

以下关键词出现在文件中表示该文件仍为默认模板：

```
_(待定)_
_(optional)_
_(未设置)_
Fill this in
Add whatever helps
_(What do they care about?
Make it yours
This is a starting point
```

## 已知文件基准行数

用于辅助判断文件是否被深度定制。

| File | Baseline Lines | 说明 |
|------|---------------|------|
| SOUL.md | 37 | OpenClaw 默认 SOUL 模板 |
| IDENTITY.md | 24 | 默认身份模板 |
| USER.md | 20 | 默认用户模板 |
| MEMORY.md | 10 | 默认空记忆 |
| AGENTS.md | 210 | 默认规则（较长） |
| TOOLS.md | 41 | 默认工具模板 |
| HEARTBEAT.md | 10 | 默认心跳模板 |
| BOOTSTRAP.md | 30 | 默认引导模板 |

## Write Policy 定义

| Status | Policy | 含义 |
|--------|--------|------|
| evolvable | section-edit | 可以修改指定 section |
| user-owned | append-only | 只能在文件末尾追加新 section |
| skill-owned | skill-managed | 由 skill 自身升级流程管理 |
| needs_review | blocked-until-classified | 进化分析时 Agent 读取后补分类 |

## 版本变化检测

```python
# 判断是否需要重新初始化
current_version = get_openclaw_version()  # 从 openclaw --version 获取
registry_version = registry.get("agent_version")

if current_version != registry_version:
    # 重新扫描，保留 user-owned 状态不降级
    re_init(preserve_user_owned=True)
```
