# ea-skill-webapp-testing

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `webapp-testing` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-webapp-testing/
  pyproject.toml
  ea_skill_webapp_testing/
    __init__.py
    plugin.py
    skills/
      webapp-testing/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
