# ea-skill-bilibili

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `bilibili` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-bilibili/
  pyproject.toml
  ea_skill_bilibili/
    __init__.py
    plugin.py
    skills/
      bilibili/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
