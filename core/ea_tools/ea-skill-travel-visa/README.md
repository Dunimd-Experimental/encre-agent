# ea-skill-travel-visa

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-visa` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-visa/
  pyproject.toml
  ea_skill_travel_visa/
    __init__.py
    plugin.py
    skills/
      travel-visa/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
