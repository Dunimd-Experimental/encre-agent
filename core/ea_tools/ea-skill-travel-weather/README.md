# ea-skill-travel-weather

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-weather` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-weather/
  pyproject.toml
  ea_skill_travel_weather/
    __init__.py
    plugin.py
    skills/
      travel-weather/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
