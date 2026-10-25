# ea-skill-email-skill

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `email-skill` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-email-skill/
  pyproject.toml
  ea_skill_email_skill/
    __init__.py
    plugin.py
    skills/
      email-skill/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
