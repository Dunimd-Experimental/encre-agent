# ea-skill-goplaces

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `goplaces` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-goplaces/
  pyproject.toml
  ea_skill_goplaces/
    __init__.py
    plugin.py
    skills/
      goplaces/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
