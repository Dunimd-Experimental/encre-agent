# ea-skill-cut-the-curve

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `cut-the-curve` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-cut-the-curve/
  pyproject.toml
  ea_skill_cut_the_curve/
    __init__.py
    plugin.py
    skills/
      cut-the-curve/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
