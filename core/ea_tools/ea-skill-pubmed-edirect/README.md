# ea-skill-pubmed-edirect

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `pubmed-edirect` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-pubmed-edirect/
  pyproject.toml
  ea_skill_pubmed_edirect/
    __init__.py
    plugin.py
    skills/
      pubmed-edirect/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
