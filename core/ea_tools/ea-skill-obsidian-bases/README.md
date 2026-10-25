# ea-skill-obsidian-bases

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `obsidian-bases` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-obsidian-bases/
  pyproject.toml
  ea_skill_obsidian_bases/
    __init__.py
    plugin.py
    skills/
      obsidian-bases/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
