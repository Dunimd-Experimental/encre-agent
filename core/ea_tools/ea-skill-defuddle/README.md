# ea-skill-defuddle

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `defuddle` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-defuddle/
  pyproject.toml
  ea_skill_defuddle/
    __init__.py
    plugin.py
    skills/
      defuddle/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
