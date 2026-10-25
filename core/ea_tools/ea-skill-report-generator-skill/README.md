# ea-skill-report-generator-skill

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `report-generator-skill` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-report-generator-skill/
  pyproject.toml
  ea_skill_report_generator_skill/
    __init__.py
    plugin.py
    skills/
      report-generator-skill/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
