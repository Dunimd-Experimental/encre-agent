# ea-skill-google-docs

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `google-docs` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-google-docs/
  pyproject.toml
  ea_skill_google_docs/
    __init__.py
    plugin.py
    skills/
      google-docs/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
