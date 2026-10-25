# ea-skill-wechat-auto-reply

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `wechat-auto-reply` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-wechat-auto-reply/
  pyproject.toml
  ea_skill_wechat_auto_reply/
    __init__.py
    plugin.py
    skills/
      wechat-auto-reply/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
