# ea-skill-docx

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `docx` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-docx/
  pyproject.toml
  ea_skill_docx/
    __init__.py
    plugin.py
    skills/
      docx/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
