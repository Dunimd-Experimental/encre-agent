# ea-skill-redis-connections

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `redis-connections` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-redis-connections/
  pyproject.toml
  ea_skill_redis_connections/
    __init__.py
    plugin.py
    skills/
      redis-connections/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
