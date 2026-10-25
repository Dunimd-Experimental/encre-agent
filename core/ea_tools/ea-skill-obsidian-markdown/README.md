# ea-skill-obsidian-markdown

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `obsidian-markdown` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-obsidian-markdown/
  pyproject.toml
  ea_skill_obsidian_markdown/
    __init__.py
    plugin.py
    skills/
      obsidian-markdown/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
