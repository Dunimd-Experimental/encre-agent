# ea-skill-stuck

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `stuck` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-stuck/
  pyproject.toml
  ea_skill_stuck/
    __init__.py
    plugin.py
    skills/
      stuck/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
