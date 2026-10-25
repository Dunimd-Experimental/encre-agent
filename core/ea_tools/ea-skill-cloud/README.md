# ea-skill-cloud

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `cloud` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-cloud/
  pyproject.toml
  ea_skill_cloud/
    __init__.py
    plugin.py
    skills/
      cloud/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
