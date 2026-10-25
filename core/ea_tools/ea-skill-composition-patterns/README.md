# ea-skill-composition-patterns

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `composition-patterns` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-composition-patterns/
  pyproject.toml
  ea_skill_composition_patterns/
    __init__.py
    plugin.py
    skills/
      composition-patterns/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
