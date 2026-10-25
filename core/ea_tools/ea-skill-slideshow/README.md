# ea-skill-slideshow

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `slideshow` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-slideshow/
  pyproject.toml
  ea_skill_slideshow/
    __init__.py
    plugin.py
    skills/
      slideshow/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
