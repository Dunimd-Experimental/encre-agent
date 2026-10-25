# ea-skill-product-launch-video

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `product-launch-video` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-product-launch-video/
  pyproject.toml
  ea_skill_product_launch_video/
    __init__.py
    plugin.py
    skills/
      product-launch-video/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
