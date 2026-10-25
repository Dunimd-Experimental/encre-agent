# ea-skill-pptx

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `pptx` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-pptx/
  pyproject.toml
  ea_skill_pptx/
    __init__.py
    plugin.py
    skills/
      pptx/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
