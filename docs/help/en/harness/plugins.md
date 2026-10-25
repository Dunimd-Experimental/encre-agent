---
sidebar:
  - text: Encre Agent Harness
    items:
      - text: Overview
        link: /en/harness
      - text: Plugin System
        link: /en/harness/plugins
---

# Plugin System

A plugin is an extension module that runs inside the Harness execution kernel. Plugins can intercept phases of the execution loop, register tools and observe events — extending the agent without touching kernel code.

## Where plugins live

The Harness discovers plugins from the following locations:

| Source | Location | Notes |
| --- | --- | --- |
| Project | `.encre/plugins/` under the workspace | A single `.py` file or a directory; the most common option |
| Bundled | Shipped with the app | `mandatory` / `system-default` tiers |
| pip-installed | Python package entry points | Third-party packages registered via entry points |

## Minimal plugin

Create `.encre/plugins/hello.py` in the project root:

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

Restart the session and the plugin is discovered and activated.

## Manifest fields

Common `PluginManifest` fields (**bold** = required — a JSON declaration missing any of them is rejected outright, and a Python build raises `TypeError`):

| Field | Description |
| --- | --- |
| **`name`** | Unique plugin name (required) |
| **`version`** | Version string (required) |
| **`description`** | One-line summary (required) |
| **`icon`** | Glyph: lucide icon name or a single emoji (required) |
| **`author`** | Author (required) |
| **`license`** | License (required) |
| **`tags`** | Tag list (required; an empty list is fine but must be written out) |
| **`source`** | Origin enum `bundled` / `mandatory` / `installed` / `project` / `user` (required; fixed to `project` for JSON declarations) |
| **`tier`** | `user` (third-party) / `system-default` / `mandatory` (required) |
| **`dependencies`** | Names of other plugins this one depends on (required, may be empty) |
| **`min_ea_version`** | Compatibility floor (required; engine-gate fallback when `engines` is unset) |
| **`engines`** | Engine constraints, e.g. `{"encre": ">=0.5"}` (required) |
| **`activation`** | `deferred` (on demand) / `core` (tools injected every turn) / `manual` (explicit enable) (required) |
| **`activation_events`** | Lazy-activation event list (required, may be empty) |
| **`permissions`** | Surfaces the plugin intends to use (see below); undeclared capabilities are denied (required, may be empty) |
| `provides_*` | Capability listings (`tools`/`skills`/`hooks`/`backends`/`mcp`/`agent`/…, optional) |
| `transport` | `local` (in-process) / `process` (subprocess sandbox) / `remote` (HTTP) |
| `integrity` | Pinned SHA-256 hash (optional; exact-compare at load for tamper evidence) |
| `danger_levels` / `resources` | Tool danger levels and resource grants (fail-closed, optional) |
| `contributes` | Declarative contributions table (`hooks`/`backends`/`skills`/`commands`/…, optional) |

## Permission surfaces (fail-closed)

A plugin may only use the capabilities **explicitly declared** in its manifest. Undeclared registration attempts are rejected with a logged warning — they never silently succeed. The `mandatory` and `system-default` tiers ship with the kernel and are exempt from the check.

| Permission token | Grants |
| --- | --- |
| `loop:intercept` | Intercept execution-loop phases |
| `tools:register` | Register tools |
| `backends:register` | Register model backends |
| `commands:register` | Register slash commands |
| `sub_agents:register` | Register sub-agents |
| `templates:register` | Register prompt templates |
| `permissions:register` | Register permission policies |
| `context:register` | Provide context injection |
| `ui:register` | Provide UI extensions |
| `gateways:register` | Register gateway adapters |
| `memory:register` | Register memory stores |
| `ws:register` / `http:register` / `lifecycle:register` | Server integration surfaces |
| `*` | Wildcard — grants everything (use sparingly) |

Surface-level wildcards such as `tools:*` are also accepted.

## Intercepting the loop

Attach handlers to named phases of the execution loop with `context.loop.intercept(phase, handler, priority=..., timeout=...)`. Common phases:

| Phase | Fires |
| --- | --- |
| `turn_start` / `turn_end` | Start / end of a turn |
| `pre_model_request` | Before a model request; may modify `messages` / `tools` or short-circuit |
| `post_model_response` | After the model responds |
| `pre_tool_exec` / `post_tool_exec` | Before / after a tool runs |
| `on_tool_progress` | Tool progress updates |
| `pre_bash` | Before a bash command runs |

> This is the common-phase list; the authoritative set depends on the current kernel version.

### Arbitration semantics

All handlers on the same phase are arbitrated by the event backbone:

- **Order**: lower `priority` runs first (default `100`); ties break by registration order.
- **Timeout**: each handler has its own wall-clock budget (default 5s, minimum 0.1s); a timeout counts as a failure and never stalls the loop.
- **Error isolation**: a raising handler never affects the kernel or other plugins; the error is logged.
- **Circuit breaker**: a plugin handler disabled after **3 consecutive failures** (builtin handlers are only logged, never disabled); the kernel publishes a `handler_disabled` event with the reason.
- **Short-circuit**: a handler returning `{"block": True}` immediately short-circuits the phase and its dict becomes the phase result — e.g. blocking inside `pre_model_request` cancels the model request outright.
- **Merging**: dict results are merged in run order (later keys win); conflicting values are logged and published.

### Declarative hooks (code-free registration)

Instead of calling `intercept` inside `on_activate`, declare hooks directly in the `contributes` section of `encre-plugin.json`; the kernel imports and attaches them to the arbitration chain at activation (requires the `loop:intercept` permission):

```json
{
  "contributes": {
    "hooks": [
      { "event": "pre_tool_exec", "handler": "my_plugin.hooks:guard", "priority": 10, "timeout": 2 }
    ]
  }
}
```

`handler` accepts both `module:function` and `module:Class.method` references; `priority` / `timeout` share the exact semantics of code-based registration.

## Declarative backends

Declare custom backend classes in `contributes.backends` as a `{name: "module:Class"}` mapping; they are imported and registered into the backend factory at activation (requires the `backends:register` permission):

```json
{
  "contributes": {
    "backends": { "my-backend": "my_plugin.backend:MyBackend" }
  }
}
```

## Registering tools

With the `tools:register` permission, register tools inside `on_activate`:

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

Tools land in the deferred zone by default (`activation="deferred"`) and cost no prompt tokens until used; `activation="core"` tools are injected into every turn.

## Lifecycle

| State | Meaning |
| --- | --- |
| `DISCOVERED` | Found, not yet activated |
| `PENDING` | Awaiting user approval (code is only imported after approval) |
| `ACTIVATED` | Active; `on_activate` has run |
| `FAILED` | Activation failed; error recorded |
| `LAZY` | Declares activation events; activates when one fires |

Deactivating or uninstalling rolls back every registration made in `on_activate` in **LIFO order**, leaving no residual side effects. A failed plugin never silently re-enters the active state — it must re-register successfully.

## Troubleshooting

- Plugin not loaded: make sure the file sits under `.encre/plugins/` and defines an `EncrePlugin` subclass.
- Registration denied: check that the manifest `permissions` declares the surface.
- Handler "disappeared": it was likely disabled by the circuit breaker — look for `handler_disabled` entries in the kernel log.
- Unexpected ordering: check `priority`; lower values run first.
