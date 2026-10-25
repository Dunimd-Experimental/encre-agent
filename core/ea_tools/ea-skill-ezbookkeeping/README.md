# ea-skill-ezbookkeeping

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `ezbookkeeping` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-ezbookkeeping/
  pyproject.toml
  ea_skill_ezbookkeeping/
    __init__.py
    plugin.py
    skills/
      ezbookkeeping/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
