# ea-skill-code-review

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `code-review` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-code-review/
  pyproject.toml
  ea_skill_code_review/
    __init__.py
    plugin.py
    skills/
      code-review/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
