# ea-skill-gmail-cleaner

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `gmail-cleaner` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-gmail-cleaner/
  pyproject.toml
  ea_skill_gmail_cleaner/
    __init__.py
    plugin.py
    skills/
      gmail-cleaner/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
