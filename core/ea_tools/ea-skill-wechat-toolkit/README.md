# ea-skill-wechat-toolkit

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `wechat-toolkit` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-wechat-toolkit/
  pyproject.toml
  ea_skill_wechat_toolkit/
    __init__.py
    plugin.py
    skills/
      wechat-toolkit/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
