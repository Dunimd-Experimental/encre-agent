# ea-skill-web-design-guidelines

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `web-design-guidelines` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-web-design-guidelines/
  pyproject.toml
  ea_skill_web_design_guidelines/
    __init__.py
    plugin.py
    skills/
      web-design-guidelines/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
