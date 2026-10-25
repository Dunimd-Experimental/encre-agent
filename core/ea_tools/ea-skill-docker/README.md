# ea-skill-docker

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `docker` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-docker/
  pyproject.toml
  ea_skill_docker/
    __init__.py
    plugin.py
    skills/
      docker/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
