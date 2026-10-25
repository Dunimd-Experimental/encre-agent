# ea-skill-travel-destination

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-destination` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-destination/
  pyproject.toml
  ea_skill_travel_destination/
    __init__.py
    plugin.py
    skills/
      travel-destination/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
