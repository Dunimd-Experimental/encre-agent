# ea-skill-pytest-forge

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `pytest-forge` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-pytest-forge/
  pyproject.toml
  ea_skill_pytest_forge/
    __init__.py
    plugin.py
    skills/
      pytest-forge/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
