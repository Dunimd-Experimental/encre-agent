# ea-skill-xlsx

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `xlsx` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-xlsx/
  pyproject.toml
  ea_skill_xlsx/
    __init__.py
    plugin.py
    skills/
      xlsx/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
