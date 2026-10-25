# ea-skill-byted-acep-api

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `byted-acep-api` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-byted-acep-api/
  pyproject.toml
  ea_skill_byted_acep_api/
    __init__.py
    plugin.py
    skills/
      byted-acep-api/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
