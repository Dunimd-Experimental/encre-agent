# ea-skill-bootstrap

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `bootstrap` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-bootstrap/
  pyproject.toml
  ea_skill_bootstrap/
    __init__.py
    plugin.py
    skills/
      bootstrap/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
