# ea-skill-spotify

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `spotify` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-spotify/
  pyproject.toml
  ea_skill_spotify/
    __init__.py
    plugin.py
    skills/
      spotify/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
