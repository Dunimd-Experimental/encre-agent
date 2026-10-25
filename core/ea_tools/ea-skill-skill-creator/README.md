# ea-skill-skill-creator

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `skill-creator` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-skill-creator/
  pyproject.toml
  ea_skill_skill_creator/
    __init__.py
    plugin.py
    skills/
      skill-creator/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
