# ea-skill-yelp

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `yelp` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-yelp/
  pyproject.toml
  ea_skill_yelp/
    __init__.py
    plugin.py
    skills/
      yelp/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
