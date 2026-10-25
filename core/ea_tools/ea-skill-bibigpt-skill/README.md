# ea-skill-bibigpt-skill

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `bibigpt-skill` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-bibigpt-skill/
  pyproject.toml
  ea_skill_bibigpt_skill/
    __init__.py
    plugin.py
    skills/
      bibigpt-skill/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
