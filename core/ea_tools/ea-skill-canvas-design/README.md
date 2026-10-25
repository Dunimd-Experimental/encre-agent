# ea-skill-canvas-design

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `canvas-design` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-canvas-design/
  pyproject.toml
  ea_skill_canvas_design/
    __init__.py
    plugin.py
    skills/
      canvas-design/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
