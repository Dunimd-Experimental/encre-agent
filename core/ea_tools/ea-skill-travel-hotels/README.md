# ea-skill-travel-hotels

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `travel-hotels` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-travel-hotels/
  pyproject.toml
  ea_skill_travel_hotels/
    __init__.py
    plugin.py
    skills/
      travel-hotels/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
