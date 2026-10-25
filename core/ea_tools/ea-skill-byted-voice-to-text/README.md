# ea-skill-byted-voice-to-text

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `byted-voice-to-text` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-byted-voice-to-text/
  pyproject.toml
  ea_skill_byted_voice_to_text/
    __init__.py
    plugin.py
    skills/
      byted-voice-to-text/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
