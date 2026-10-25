# ea-skill-media-use

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `media-use` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-media-use/
  pyproject.toml
  ea_skill_media_use/
    __init__.py
    plugin.py
    skills/
      media-use/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
