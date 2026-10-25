# ea-skill-academic-paper-review

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `academic-paper-review` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-academic-paper-review/
  pyproject.toml
  ea_skill_academic_paper_review/
    __init__.py
    plugin.py
    skills/
      academic-paper-review/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
