# ea-skill-doc-coauthoring

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `doc-coauthoring` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-doc-coauthoring/
  pyproject.toml
  ea_skill_doc_coauthoring/
    __init__.py
    plugin.py
    skills/
      doc-coauthoring/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
