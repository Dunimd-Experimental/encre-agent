# ea-skill-discord-voice

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `discord-voice` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-discord-voice/
  pyproject.toml
  ea_skill_discord_voice/
    __init__.py
    plugin.py
    skills/
      discord-voice/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
