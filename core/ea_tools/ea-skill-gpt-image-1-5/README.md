# ea-skill-gpt-image-1-5

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `gpt-image-1-5` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-gpt-image-1-5/
  pyproject.toml
  ea_skill_gpt_image_1_5/
    __init__.py
    plugin.py
    skills/
      gpt-image-1-5/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
