# ea-skill-faceless-explainer

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `faceless-explainer` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-faceless-explainer/
  pyproject.toml
  ea_skill_faceless_explainer/
    __init__.py
    plugin.py
    skills/
      faceless-explainer/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
