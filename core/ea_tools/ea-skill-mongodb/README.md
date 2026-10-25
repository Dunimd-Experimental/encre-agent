# ea-skill-mongodb

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `mongodb` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-mongodb/
  pyproject.toml
  ea_skill_mongodb/
    __init__.py
    plugin.py
    skills/
      mongodb/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
