# ea-skill-travel-transit

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-transit` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-transit/
  pyproject.toml
  ea_skill_travel_transit/
    __init__.py
    plugin.py
    skills/
      travel-transit/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
