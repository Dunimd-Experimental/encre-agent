# ea-skill-deep-research

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `deep-research` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-deep-research/
  pyproject.toml
  ea_skill_deep_research/
    __init__.py
    plugin.py
    skills/
      deep-research/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
