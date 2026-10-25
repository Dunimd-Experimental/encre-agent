# ea-skill-mgrep-code-search

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `mgrep-code-search` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-mgrep-code-search/
  pyproject.toml
  ea_skill_mgrep_code_search/
    __init__.py
    plugin.py
    skills/
      mgrep-code-search/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
