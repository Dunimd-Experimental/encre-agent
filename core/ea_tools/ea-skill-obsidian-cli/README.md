# ea-skill-obsidian-cli

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `obsidian-cli` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-obsidian-cli/
  pyproject.toml
  ea_skill_obsidian_cli/
    __init__.py
    plugin.py
    skills/
      obsidian-cli/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
