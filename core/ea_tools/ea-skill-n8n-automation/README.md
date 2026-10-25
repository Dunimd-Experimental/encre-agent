# ea-skill-n8n-automation

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `n8n-automation` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-n8n-automation/
  pyproject.toml
  ea_skill_n8n_automation/
    __init__.py
    plugin.py
    skills/
      n8n-automation/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
