# ea-skill-data-analysis

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `data-analysis` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-data-analysis/
  pyproject.toml
  ea_skill_data_analysis/
    __init__.py
    plugin.py
    skills/
      data-analysis/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
