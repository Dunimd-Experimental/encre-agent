# ea-skill-security-best-practices

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `security-best-practices` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-security-best-practices/
  pyproject.toml
  ea_skill_security_best_practices/
    __init__.py
    plugin.py
    skills/
      security-best-practices/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
