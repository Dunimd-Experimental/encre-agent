# ea-skill-revolut

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `revolut` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-revolut/
  pyproject.toml
  ea_skill_revolut/
    __init__.py
    plugin.py
    skills/
      revolut/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
