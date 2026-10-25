# ea-skill-writing-guidelines

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `writing-guidelines` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-writing-guidelines/
  pyproject.toml
  ea_skill_writing_guidelines/
    __init__.py
    plugin.py
    skills/
      writing-guidelines/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
