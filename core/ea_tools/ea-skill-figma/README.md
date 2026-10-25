# ea-skill-figma

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `figma` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-figma/
  pyproject.toml
  ea_skill_figma/
    __init__.py
    plugin.py
    skills/
      figma/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
