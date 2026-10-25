# ea-skill-notion-integration

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `notion-integration` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-notion-integration/
  pyproject.toml
  ea_skill_notion_integration/
    __init__.py
    plugin.py
    skills/
      notion-integration/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
