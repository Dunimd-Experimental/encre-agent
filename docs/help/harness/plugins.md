---
sidebar:
  - text: Encre Agent Harness
    items:
      - text: 概览
        link: /harness
      - text: 插件系统
        link: /harness/plugins
---

# 插件系统

插件（Plugin）是运行在 Harness 执行内核里的扩展模块。插件可以拦截执行循环的各个阶段、注册工具、监听事件，从而在不修改内核代码的情况下增强 Agent 的能力。

## 插件放在哪里

Harness 启动时会扫描以下位置发现插件：

| 来源 | 位置 | 说明 |
| --- | --- | --- |
| 项目级 | 工作区下 `.encre/plugins/` | 放置单文件 `.py` 或包含插件的目录，最常用的方式 |
| 内置 | 随应用分发 | `mandatory` / `system-default` 层级，随产品发货 |
| pip 安装 | Python 包入口点 | 通过 entry points 注册的第三方包 |

## 最小插件

在项目根目录创建 `.encre/plugins/hello.py`：

```python
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


class HelloPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="hello",
        version="0.1.0",
        description="Log a line on every turn start",
        icon="megaphone",
        author="Dunimd Team",
        license="Apache-2.0",
        tags=["demo"],
        source=PluginSource.PROJECT,
        tier="user",
        dependencies=[],
        min_ea_version="0.5.0",
        engines={"encre": ">=0.5"},
        activation="deferred",
        activation_events=[],
        permissions=["loop:intercept"],
    )

    async def on_activate(self, context):
        async def on_turn_start(**payload):
            print("[hello] turn started")
            return payload

        context.loop.intercept("on_turn_start", on_turn_start, priority=100)
```

保存后重启会话，插件即被发现并激活。

## Manifest 字段

`PluginManifest` 常用字段（**粗体**为必填——缺任意一个，JSON 声明会被直接拒绝，Python 构造会抛 `TypeError`）：

| 字段 | 说明 |
| --- | --- |
| **`name`** | 插件唯一名（必填） |
| **`version`** | 版本号（必填） |
| **`description`** | 一句话描述（必填） |
| **`icon`** | 图标：lucide 图标名或单个 emoji（必填） |
| **`author`** | 作者（必填） |
| **`license`** | 许可证（必填） |
| **`tags`** | 标签列表（必填，可为空列表但必须显式写出） |
| **`source`** | 来源枚举 `bundled` / `mandatory` / `installed` / `project` / `user`（必填；JSON 声明固定为 `project`） |
| **`tier`** | `user`（第三方）/ `system-default` / `mandatory`（必填） |
| **`dependencies`** | 依赖的其他插件名（必填，可为空列表） |
| **`min_ea_version`** | 兼容版本下限（必填；`engines` 未设时的引擎门回退值） |
| **`engines`** | 引擎约束，如 `{"encre": ">=0.5"}`（必填） |
| **`activation`** | `deferred`（按需加载）/ `core`（工具每轮注入提示词）/ `manual`（等待显式启用）（必填） |
| **`activation_events`** | 懒激活事件列表（必填，可为空列表） |
| **`permissions`** | 声明要使用的权限面（见下文），未声明的能力会被拒绝（必填，可为空列表） |
| `provides_*` | 各能力提供清单（`tools`/`skills`/`hooks`/`backends`/`mcp`/`agent`/…，可选） |
| `transport` | `local`（进程内）/ `process`（子进程沙箱）/ `remote`（HTTP） |
| `integrity` | SHA-256 钉定哈希（可选；声明后加载时精确比对防篡改） |
| `danger_levels` / `resources` | 工具危险级与资源授权（fail-closed，可选） |
| `contributes` | 声明式贡献总表（`hooks`/`backends`/`skills`/`commands`/…，可选） |

## 权限面（fail-closed）

插件只能使用 manifest 中**显式声明**的能力。未声明时，对应注册调用会被拒绝并记录警告——不会静默成功。`mandatory` 与 `system-default` 层级随内核发布，豁免检查。

| 权限 token | 授权的能力 |
| --- | --- |
| `loop:intercept` | 拦截执行循环阶段 |
| `tools:register` | 注册工具 |
| `backends:register` | 注册模型后端 |
| `commands:register` | 注册斜杠命令 |
| `sub_agents:register` | 注册子智能体 |
| `templates:register` | 注册提示词模板 |
| `permissions:register` | 注册权限策略 |
| `context:register` | 提供上下文注入 |
| `ui:register` | 提供界面扩展 |
| `gateways:register` | 注册网关适配器 |
| `memory:register` | 注册记忆存储 |
| `ws:register` / `http:register` / `lifecycle:register` | 服务端集成面 |
| `*` | 通配，授权全部能力（慎用） |

也支持面级通配，如 `tools:*`。

## 拦截执行循环

通过 `context.loop.intercept(phase, handler, priority=..., timeout=...)` 把处理函数挂到执行循环的命名阶段上。常用阶段：

| 阶段 | 时机 |
| --- | --- |
| `turn_start` / `turn_end` | 一轮开始 / 结束 |
| `pre_model_request` | 模型请求发出前，可修改 `messages` / `tools`，或短路请求 |
| `post_model_response` | 模型响应返回后 |
| `pre_tool_exec` / `post_tool_exec` | 工具执行前 / 后 |
| `on_tool_progress` | 工具执行进度 |
| `pre_bash` | bash 命令执行前 |

> 以上为常用阶段清单，实际以当前内核版本暴露的阶段为准。

### 仲裁语义

同一阶段上的所有处理函数由事件骨架统一仲裁，规则如下：

- **执行顺序**：`priority` 数值越小越先执行（默认 `100`）；相同时按注册顺序。
- **超时**：每个处理函数有独立的墙钟预算（默认 5 秒，最小 0.1 秒），超时按失败处理，不会拖死循环。
- **异常隔离**：处理函数抛出异常不会影响内核与其他插件，异常被记录。
- **熔断**：插件处理函数**连续失败 3 次**后被禁用（内置处理函数只记录日志、永不禁用），禁用时内核会发布 `handler_disabled` 事件并记录原因。
- **短路（block）**：处理函数返回 `{"block": True}` 会立即短路该阶段，其返回值成为本阶段的最终结果。例如在 `pre_model_request` 中 block 可以直接拦截一次模型请求。
- **结果合并**：多个处理函数返回 dict 时按执行顺序合并，后执行的键覆盖先执行的；冲突值会被记录并发布。

### 声明式 hook（免代码注册）

除在 `on_activate` 中调用 `intercept` 外，还可以直接在 `encre-plugin.json` 的 `contributes` 里声明 hook，激活时由内核自动导入并挂到仲裁链（需要 `loop:intercept` 权限）：

```json
{
  "contributes": {
    "hooks": [
      { "event": "pre_tool_exec", "handler": "my_plugin.hooks:guard", "priority": 10, "timeout": 2 }
    ]
  }
}
```

`handler` 支持 `模块:函数` 与 `模块:类.方法` 两种引用形式；`priority` / `timeout` 语义与代码注册完全一致。

## 声明式后端

在 `contributes.backends` 里以 `{名称: "模块:类"}` 声明自定义后端类，激活时自动导入并注册到后端工厂（需要 `backends:register` 权限）：

```json
{
  "contributes": {
    "backends": { "my-backend": "my_plugin.backend:MyBackend" }
  }
}
```

## 注册工具

声明 `tools:register` 权限后，可在 `on_activate` 中注册工具：

```python
async def on_activate(self, context):
    def run(query: str) -> str:
        """Run a demo lookup. (English docstring only.)"""
        return f"result for {query}"

    undo = context.tools.register(
        {
            "name": "demo_lookup",
            "description": "Look up demo data",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "execute": run,
        }
    )
```

工具默认进入按需加载区（`activation="deferred"`），不占用每轮提示词；`activation="core"` 的工具每轮直接注入。

## 生命周期

| 状态 | 含义 |
| --- | --- |
| `DISCOVERED` | 已发现，未激活 |
| `PENDING` | 等待用户批准（批准后才导入代码） |
| `ACTIVATED` | 已激活，`on_activate` 已执行 |
| `FAILED` | 激活失败，记录错误 |
| `LAZY` | 注册了激活事件，事件触发后才真正激活 |

停用（deactivate）或卸载插件时，`on_activate` 期间产生的注册会按**后进先出**的顺序精确回滚，不会留下残留副作用。失败的插件不会自动回到激活态，必须重新注册成功。

## 故障排查

- 插件没有被加载：确认文件位于 `.encre/plugins/` 下，且定义了 `EncrePlugin` 的子类。
- 注册被拒绝：检查 manifest 的 `permissions` 是否声明了对应能力。
- 处理函数"消失"：可能因连续失败被熔断禁用，查看内核日志中的 `handler_disabled` 记录。
- 顺序不符合预期：检查 `priority`，数值越小越先执行。
