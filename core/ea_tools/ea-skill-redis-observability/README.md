# ea-skill-redis-observability

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `redis-observability` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-redis-observability/
  pyproject.toml
  ea_skill_redis_observability/
    __init__.py
    plugin.py
    skills/
      redis-observability/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
