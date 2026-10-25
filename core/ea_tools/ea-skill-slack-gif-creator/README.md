# ea-skill-slack-gif-creator

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `slack-gif-creator` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-slack-gif-creator/
  pyproject.toml
  ea_skill_slack_gif_creator/
    __init__.py
    plugin.py
    skills/
      slack-gif-creator/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
