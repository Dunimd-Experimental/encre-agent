# ea-skill-find-skills

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `find-skills` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-find-skills/
  pyproject.toml
  ea_skill_find_skills/
    __init__.py
    plugin.py
    skills/
      find-skills/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
