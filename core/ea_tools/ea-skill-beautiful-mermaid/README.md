# ea-skill-beautiful-mermaid

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `beautiful-mermaid` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-beautiful-mermaid/
  pyproject.toml
  ea_skill_beautiful_mermaid/
    __init__.py
    plugin.py
    skills/
      beautiful-mermaid/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
