# ea-skill-slack

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `slack` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-slack/
  pyproject.toml
  ea_skill_slack/
    __init__.py
    plugin.py
    skills/
      slack/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
