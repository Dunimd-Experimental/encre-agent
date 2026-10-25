# ea-skill-byted-sms-sender

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `byted-sms-sender` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-byted-sms-sender/
  pyproject.toml
  ea_skill_byted_sms_sender/
    __init__.py
    plugin.py
    skills/
      byted-sms-sender/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
