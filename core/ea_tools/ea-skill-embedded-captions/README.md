# ea-skill-embedded-captions

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `embedded-captions` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-embedded-captions/
  pyproject.toml
  ea_skill_embedded_captions/
    __init__.py
    plugin.py
    skills/
      embedded-captions/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
