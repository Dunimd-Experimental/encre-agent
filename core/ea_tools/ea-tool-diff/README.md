# ea-tool-diff

EA (Encre Agent) system-default diff tool.

| Field | Value |
|-------|-------|
| Tool | `diff` |
| Tier | `system-default` (uninstallable) |
| Package | `ea-tool-diff` |
| Module | `ea_tool_diff` |
| Author | Dunimd Team <dunimd@outlook.com> |
| Version | 0.5.0 |

Compares text, files, or directories and returns a structured diff with
unified/context/JSON/HTML output, binary hash mode, and directory tree mode.

## Layout

```
ea-tool-diff/
  pyproject.toml
  README.md
  ea_tool_diff/
    __init__.py
    tool.py           # diff_tool (build_tool)
    plugin.py         # EncrePlugin manifest + create_plugin factory
    skills/
      tool-diff.md    # Skill instructions
```
