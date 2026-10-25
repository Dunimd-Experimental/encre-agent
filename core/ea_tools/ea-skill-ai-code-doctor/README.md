# ea-skill-ai-code-doctor

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `ai-code-doctor` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-ai-code-doctor/
  pyproject.toml
  ea_skill_ai_code_doctor/
    __init__.py
    plugin.py
    skills/
      ai-code-doctor/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
