# ea-skill-upstash-redis-kv

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `upstash-redis-kv` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-upstash-redis-kv/
  pyproject.toml
  ea_skill_upstash_redis_kv/
    __init__.py
    plugin.py
    skills/
      upstash-redis-kv/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
