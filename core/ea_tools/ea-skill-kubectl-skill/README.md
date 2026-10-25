# ea-skill-kubectl-skill

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `kubectl-skill` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-kubectl-skill/
  pyproject.toml
  ea_skill_kubectl_skill/
    __init__.py
    plugin.py
    skills/
      kubectl-skill/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
