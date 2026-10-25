# ea-tool-apply-patch

EA (Encre Agent) system-default apply-patch tool.

| Field | Value |
|-------|-------|
| Tool | `apply_patch` |
| Tier | `system-default` (uninstallable) |
| Package | `ea-tool-apply-patch` |
| Module | `ea_tool_apply_patch` |
| Author | Dunimd Team <dunimd@outlook.com> |
| Version | 0.5.0 |

Applies a unified git-style diff across one or more files atomically, with
dry-run preview and fuzzy hunk placement.

## Layout

```
ea-tool-apply-patch/
  pyproject.toml
  README.md
  ea_tool_apply_patch/
    __init__.py
    tool.py                # apply_patch_tool (build_tool)
    plugin.py              # EncrePlugin manifest + create_plugin factory
    skills/
      tool-apply-patch.md  # Skill instructions
```
