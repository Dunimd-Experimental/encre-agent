# ea-skill-systematic-literature-review

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `systematic-literature-review` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-systematic-literature-review/
  pyproject.toml
  ea_skill_systematic_literature_review/
    __init__.py
    plugin.py
    skills/
      systematic-literature-review/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
