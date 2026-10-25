# ea-skill-deploy-to-vercel

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `deploy-to-vercel` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-deploy-to-vercel/
  pyproject.toml
  ea_skill_deploy_to_vercel/
    __init__.py
    plugin.py
    skills/
      deploy-to-vercel/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
