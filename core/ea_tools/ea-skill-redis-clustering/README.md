# ea-skill-redis-clustering

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `redis-clustering` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-redis-clustering/
  pyproject.toml
  ea_skill_redis_clustering/
    __init__.py
    plugin.py
    skills/
      redis-clustering/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
