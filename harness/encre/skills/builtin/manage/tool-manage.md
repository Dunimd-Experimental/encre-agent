---
name: tool-manage
description: "Plugin manager for the runtime: search the tool index (informational -- every registered tool is already active), install new tools / sub-agents / skills / MCP servers, and manage the full plugin lifecycle (list, install, uninstall, enable, disable, reload a single plugin, or restart the whole plugin system)."
hidden: true
context: inline
tier: system-default
author: Dunimd Team
version: 0.5.0
---

# manage

Plugin manager for the runtime: search the tool index for what already exists, install new tools / sub-agents / skills / MCP servers, and manage the full plugin lifecycle. MCP servers, skills, and agents are all defined as plugins, so the install_* actions create plugin-backed capabilities. All changes take effect immediately and are available on the very next turn.

Registration IS activation: every tool of every activated plugin is registered and visible to the model from startup -- there is no deferred zone. Discovery is purely informational: it tells you WHAT exists (name, category, parameter card) so you can invoke the right tool directly.

WHEN to use: before installing anything, run `discover` with a natural-language description of the capability you need -- a matching tool (database, browser, docker, ssh, email, ...) may already exist and is already callable from its card. Use `plugin_list` to inspect installed plugins and PENDING third-party plugins awaiting confirmation; `plugin_install` confirms a pending plugin (this executes third-party code -- review its permissions first). Use the install_* actions only when no existing plugin covers the need.
WHEN NOT to use: if a built-in tool already covers the task, prefer it over installing a new one; do not disable or uninstall plugins without a reason -- there is no protected tier, so any plugin (bundled or third-party) can be disabled or uninstalled and will not come back until re-enabled or reinstalled.
TIPS: describe the capability in natural language (e.g. "run SQL queries"), not a tool name; pass `category` to narrow the discover search; keep new tool code self-contained (the `code` field is exec'd in a fresh namespace and must define an `async def execute(**kwargs)`); give agents a focused system_prompt and a narrow tool_policy to limit blast radius; verify MCP server connectivity with a trivial call right after install_mcp; after plugin_restart check the returned 'failed' map.
PITFALLS: calling `discover` repeatedly for the same capability wastes turns -- search once, then call the matching tool directly; very vague queries may return too many irrelevant matches; install_tool runs arbitrary Python via exec -- only install code you trust; install_mcp with stdio spawns a long-lived child process, so prefer HTTP transport when available; plugin_disable persists across restarts for user-installed plugins (session-only otherwise).

Actions:
  discover         - Search the tool index and report matches (pure information lookup: every match is already active and callable; this only tells you what exists and its parameters). Requires: query. Optional: top_k (default 5, max 15), category (filesystem/search/shell/web/infra/data/code_intel/gui/docs/media/memory/task/delegation).
  install_tool     - Create and register a brand new tool. Requires: name, description, input_schema (JSON Schema), code (Python async function body named `execute`). Optional: category, intents, always_available.
  install_agent    - Register a new named sub-agent. Requires: name, description. Optional: system_prompt, tool_policy (all/readonly/no_writes).
  install_skill    - Install a new skill. Requires: name, description, body (skill prompt text). Optional: aliases.
  install_mcp      - Connect a new MCP server. Requires: name, and either command (stdio) or url (HTTP). Optional: args, env, cwd, timeout.
  plugin_list      - List all plugins with state/enabled/tier, failed reasons, and PENDING items awaiting confirmation.
  plugin_install   - Confirm and activate a PENDING plugin (executes its code). Requires: name.
  plugin_activate  - Activate a registered plugin (logical, non-destructive: merges contributions into the live runtime; its tools join the model payload immediately). Requires: name.
  plugin_uninstall - Logical uninstall (deactivate, unregister, ledger-remove; no files are deleted). Requires: name.
  plugin_enable    - Re-enable a disabled plugin. Requires: name.
  plugin_disable   - Disable a plugin (persists for user-installed plugins). Requires: name.
  plugin_reload    - Hot-reload one plugin (deactivate, re-import, re-activate). Requires: name.
  plugin_restart   - Restart the whole plugin system (deactivate all, rediscover, reactivate). No arguments.

Every install_* result is a real plugin (namespaced `model-tool-*`, `model-agent-*`, `model-skill-*`, `model-mcp-*`): it appears in plugin_list, is recorded in the installed ledger, and can be disabled, uninstalled, or hot-reloaded like any other plugin. Disabling or uninstalling rolls back its runtime contributions (tool registration, sub-agent, skill, MCP connection) automatically.
