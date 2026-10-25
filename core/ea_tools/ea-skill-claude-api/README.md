# ea-skill-claude-api

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `claude-api` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-claude-api/
  pyproject.toml
  ea_skill_claude_api/
    __init__.py
    plugin.py
    skills/
      claude-api/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
