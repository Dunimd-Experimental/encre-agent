# ea-skill-debug

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `debug` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-debug/
  pyproject.toml
  ea_skill_debug/
    __init__.py
    plugin.py
    skills/
      debug/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
