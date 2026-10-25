# ea-skill-video-breakdown-skill

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `video-breakdown-skill` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-video-breakdown-skill/
  pyproject.toml
  ea_skill_video_breakdown_skill/
    __init__.py
    plugin.py
    skills/
      video-breakdown-skill/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
