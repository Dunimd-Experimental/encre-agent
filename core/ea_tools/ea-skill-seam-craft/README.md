# ea-skill-seam-craft

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `seam-craft` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-seam-craft/
  pyproject.toml
  ea_skill_seam_craft/
    __init__.py
    plugin.py
    skills/
      seam-craft/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
