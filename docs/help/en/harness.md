---
sidebar:
  - text: Encre Agent Harness
    items:
      - text: Overview
        link: /en/harness
      - text: Plugin System
        link: /en/harness/plugins
---

# Encre Agent Harness

The Harness is the execution kernel of Encre: it drives every turn of the agent loop (receive input → call the model → run tools → produce a reply), together with the lifecycle, recovery and extension mechanisms built around that loop.

## Documentation

| Document | Contents |
| --- | --- |
| [Plugin System](/en/harness/plugins) | Writing and installing plugins; permission surfaces; intercepting the loop (arbitration semantics); lifecycle management |

More module docs (execution loop, recovery, skills, etc.) are on the way.
