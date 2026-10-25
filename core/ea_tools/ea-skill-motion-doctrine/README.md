# ea-skill-motion-doctrine

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `motion-doctrine` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-motion-doctrine/
  pyproject.toml
  ea_skill_motion_doctrine/
    __init__.py
    plugin.py
    skills/
      motion-doctrine/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
