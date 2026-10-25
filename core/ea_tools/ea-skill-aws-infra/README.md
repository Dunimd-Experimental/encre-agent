# ea-skill-aws-infra

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `aws-infra` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-aws-infra/
  pyproject.toml
  ea_skill_aws_infra/
    __init__.py
    plugin.py
    skills/
      aws-infra/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
