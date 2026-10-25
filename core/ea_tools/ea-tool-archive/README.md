# ea-tool-archive

EA (Encre Agent) system-default archive tool.

| Field | Value |
|-------|-------|
| Tool | `archive` |
| Tier | `system-default` (uninstallable) |
| Package | `ea-tool-archive` |
| Module | `ea_tool_archive` |
| Author | Dunimd Team <dunimd@outlook.com> |
| Version | 0.5.0 |

Creates, extracts, lists, and inspects zip/tar/gz/bz2/xz archives behind a
single tool interface with structured JSON output.

## Layout

```
ea-tool-archive/
  pyproject.toml
  README.md
  ea_tool_archive/
    __init__.py
    tool.py              # archive_tool (build_tool)
    plugin.py            # EncrePlugin manifest + create_plugin factory
    skills/
      tool-archive.md    # Skill instructions
```
