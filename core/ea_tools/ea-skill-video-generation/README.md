# ea-skill-video-generation

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `video-generation` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-video-generation/
  pyproject.toml
  ea_skill_video_generation/
    __init__.py
    plugin.py
    skills/
      video-generation/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
