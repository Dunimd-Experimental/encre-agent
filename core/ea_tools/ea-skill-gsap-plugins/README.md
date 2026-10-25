# ea-skill-gsap-plugins

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `gsap-plugins` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-gsap-plugins/
  pyproject.toml
  ea_skill_gsap_plugins/
    __init__.py
    plugin.py
    skills/
      gsap-plugins/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
