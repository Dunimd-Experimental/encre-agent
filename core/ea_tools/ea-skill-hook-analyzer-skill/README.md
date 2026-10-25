# ea-skill-hook-analyzer-skill

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `hook-analyzer-skill` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-hook-analyzer-skill/
  pyproject.toml
  ea_skill_hook_analyzer_skill/
    __init__.py
    plugin.py
    skills/
      hook-analyzer-skill/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
