# ea-skill-aihotel

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `aihotel` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-aihotel/
  pyproject.toml
  ea_skill_aihotel/
    __init__.py
    plugin.py
    skills/
      aihotel/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
