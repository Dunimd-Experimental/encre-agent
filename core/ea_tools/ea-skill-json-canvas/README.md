# ea-skill-json-canvas

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `json-canvas` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-json-canvas/
  pyproject.toml
  ea_skill_json_canvas/
    __init__.py
    plugin.py
    skills/
      json-canvas/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
