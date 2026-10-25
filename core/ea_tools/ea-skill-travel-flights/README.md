# ea-skill-travel-flights

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-flights` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-flights/
  pyproject.toml
  ea_skill_travel_flights/
    __init__.py
    plugin.py
    skills/
      travel-flights/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
