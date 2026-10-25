# ea-skill-code-documentation

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `code-documentation` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-code-documentation/
  pyproject.toml
  ea_skill_code_documentation/
    __init__.py
    plugin.py
    skills/
      code-documentation/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
