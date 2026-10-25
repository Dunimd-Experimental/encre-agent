# ea-skill-gmail-2

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `gmail-2` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-gmail-2/
  pyproject.toml
  ea_skill_gmail_2/
    __init__.py
    plugin.py
    skills/
      gmail-2/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
