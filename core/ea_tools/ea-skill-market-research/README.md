# ea-skill-market-research

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `market-research` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-market-research/
  pyproject.toml
  ea_skill_market_research/
    __init__.py
    plugin.py
    skills/
      market-research/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
