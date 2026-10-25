# ea-skill-gsap-core

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `gsap-core` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-gsap-core/
  pyproject.toml
  ea_skill_gsap_core/
    __init__.py
    plugin.py
    skills/
      gsap-core/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
