# ea-skill-redis-semantic-cache

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `redis-semantic-cache` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-redis-semantic-cache/
  pyproject.toml
  ea_skill_redis_semantic_cache/
    __init__.py
    plugin.py
    skills/
      redis-semantic-cache/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
