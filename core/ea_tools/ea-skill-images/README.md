# ea-skill-images

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `images` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-images/
  pyproject.toml
  ea_skill_images/
    __init__.py
    plugin.py
    skills/
      images/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
