# ea-skill-theme-factory

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `theme-factory` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-theme-factory/
  pyproject.toml
  ea_skill_theme_factory/
    __init__.py
    plugin.py
    skills/
      theme-factory/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
