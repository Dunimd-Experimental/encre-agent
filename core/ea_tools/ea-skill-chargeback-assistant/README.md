# ea-skill-chargeback-assistant

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `chargeback-assistant` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-chargeback-assistant/
  pyproject.toml
  ea_skill_chargeback_assistant/
    __init__.py
    plugin.py
    skills/
      chargeback-assistant/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
