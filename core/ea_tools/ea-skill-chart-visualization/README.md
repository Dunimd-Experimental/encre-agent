# ea-skill-chart-visualization

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `chart-visualization` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-chart-visualization/
  pyproject.toml
  ea_skill_chart_visualization/
    __init__.py
    plugin.py
    skills/
      chart-visualization/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
