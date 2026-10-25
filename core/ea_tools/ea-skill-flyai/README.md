# ea-skill-flyai

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `flyai` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-flyai/
  pyproject.toml
  ea_skill_flyai/
    __init__.py
    plugin.py
    skills/
      flyai/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
