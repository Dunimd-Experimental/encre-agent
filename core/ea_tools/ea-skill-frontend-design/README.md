# ea-skill-frontend-design

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `frontend-design` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-frontend-design/
  pyproject.toml
  ea_skill_frontend_design/
    __init__.py
    plugin.py
    skills/
      frontend-design/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
