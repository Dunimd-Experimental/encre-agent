# ea-skill-web-artifacts-builder

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `web-artifacts-builder` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-web-artifacts-builder/
  pyproject.toml
  ea_skill_web_artifacts_builder/
    __init__.py
    plugin.py
    skills/
      web-artifacts-builder/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
