# ea-skill-hyperframes-registry

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `hyperframes-registry` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-hyperframes-registry/
  pyproject.toml
  ea_skill_hyperframes_registry/
    __init__.py
    plugin.py
    skills/
      hyperframes-registry/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
