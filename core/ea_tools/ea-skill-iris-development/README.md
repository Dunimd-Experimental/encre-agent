# ea-skill-iris-development

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `iris-development` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-iris-development/
  pyproject.toml
  ea_skill_iris_development/
    __init__.py
    plugin.py
    skills/
      iris-development/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
