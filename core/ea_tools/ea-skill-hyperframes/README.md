# ea-skill-hyperframes

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `hyperframes` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-hyperframes/
  pyproject.toml
  ea_skill_hyperframes/
    __init__.py
    plugin.py
    skills/
      hyperframes/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
