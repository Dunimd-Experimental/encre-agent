# ea-skill-travel-itinerary

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-itinerary` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-itinerary/
  pyproject.toml
  ea_skill_travel_itinerary/
    __init__.py
    plugin.py
    skills/
      travel-itinerary/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
