# ea-skill-travel-trains

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-trains` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-trains/
  pyproject.toml
  ea_skill_travel_trains/
    __init__.py
    plugin.py
    skills/
      travel-trains/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
