# ea-skill-alipay-payment-integration

EA (Encre Agent) system-default skill package.

| Field | Value |
| --- | --- |
| Skill | `alipay-payment-integration` |
| Tier | `system-default` |
| Version | `0.5.0` |
| Author | Dunimd Team |

## Layout

```
ea-skill-alipay-payment-integration/
  pyproject.toml
  ea_skill_alipay_payment_integration/
    __init__.py
    plugin.py
    skills/
      alipay-payment-integration/   # SKILL.md + references/assets
```

Discovered automatically by the EA directory scan; the skill markdown is
loaded via `EncreSkillRegistry.load_from_dir`.
