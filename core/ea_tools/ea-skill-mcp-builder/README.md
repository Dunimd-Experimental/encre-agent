# ea-skill-mcp-builder

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `mcp-builder` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-mcp-builder/
  pyproject.toml
  ea_skill_mcp_builder/
    __init__.py
    plugin.py
    skills/
      mcp-builder/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
