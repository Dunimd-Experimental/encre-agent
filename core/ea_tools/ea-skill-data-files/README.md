# ea-skill-data-files

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `data-files` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-data-files/
  pyproject.toml
  ea_skill_data_files/
    __init__.py
    plugin.py
    skills/
      data-files/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
