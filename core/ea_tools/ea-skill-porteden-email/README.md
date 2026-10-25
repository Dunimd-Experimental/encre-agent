# ea-skill-porteden-email

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `porteden-email` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-porteden-email/
  pyproject.toml
  ea_skill_porteden_email/
    __init__.py
    plugin.py
    skills/
      porteden-email/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
