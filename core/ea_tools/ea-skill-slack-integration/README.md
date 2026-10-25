# ea-skill-slack-integration

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `slack-integration` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-slack-integration/
  pyproject.toml
  ea_skill_slack_integration/
    __init__.py
    plugin.py
    skills/
      slack-integration/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
