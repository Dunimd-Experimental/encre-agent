# ea-skill-byted-web-search

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `byted-web-search` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-byted-web-search/
  pyproject.toml
  ea_skill_byted_web_search/
    __init__.py
    plugin.py
    skills/
      byted-web-search/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
