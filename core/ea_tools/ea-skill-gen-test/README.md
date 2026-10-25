# ea-skill-gen-test

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `gen-test` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-gen-test/
  pyproject.toml
  ea_skill_gen_test/
    __init__.py
    plugin.py
    skills/
      gen-test/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
