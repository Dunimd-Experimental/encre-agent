# ea-skill-verify

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `verify` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-verify/
  pyproject.toml
  ea_skill_verify/
    __init__.py
    plugin.py
    skills/
      verify/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
