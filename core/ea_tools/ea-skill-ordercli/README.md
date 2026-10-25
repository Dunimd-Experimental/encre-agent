# ea-skill-ordercli

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `ordercli` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-ordercli/
  pyproject.toml
  ea_skill_ordercli/
    __init__.py
    plugin.py
    skills/
      ordercli/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
