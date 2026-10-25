# ea-skill-whatsapp-styling-guide

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `whatsapp-styling-guide` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-whatsapp-styling-guide/
  pyproject.toml
  ea_skill_whatsapp_styling_guide/
    __init__.py
    plugin.py
    skills/
      whatsapp-styling-guide/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
