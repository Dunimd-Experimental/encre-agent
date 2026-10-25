# ea-skill-iga-pages

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `iga-pages` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-iga-pages/
  pyproject.toml
  ea_skill_iga_pages/
    __init__.py
    plugin.py
    skills/
      iga-pages/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
