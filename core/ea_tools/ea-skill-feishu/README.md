# ea-skill-feishu

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `feishu` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-feishu/
  pyproject.toml
  ea_skill_feishu/
    __init__.py
    plugin.py
    skills/
      feishu/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
