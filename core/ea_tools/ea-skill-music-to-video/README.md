# ea-skill-music-to-video

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `music-to-video` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-music-to-video/
  pyproject.toml
  ea_skill_music_to_video/
    __init__.py
    plugin.py
    skills/
      music-to-video/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
