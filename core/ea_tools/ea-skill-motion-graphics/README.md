# ea-skill-motion-graphics

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `motion-graphics` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-motion-graphics/
  pyproject.toml
  ea_skill_motion_graphics/
    __init__.py
    plugin.py
    skills/
      motion-graphics/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
