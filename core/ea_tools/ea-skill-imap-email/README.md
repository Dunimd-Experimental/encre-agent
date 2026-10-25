# ea-skill-imap-email

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `imap-email` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-imap-email/
  pyproject.toml
  ea_skill_imap_email/
    __init__.py
    plugin.py
    skills/
      imap-email/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
