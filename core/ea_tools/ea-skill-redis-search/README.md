# ea-skill-redis-search

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `redis-search` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-redis-search/
  pyproject.toml
  ea_skill_redis_search/
    __init__.py
    plugin.py
    skills/
      redis-search/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
