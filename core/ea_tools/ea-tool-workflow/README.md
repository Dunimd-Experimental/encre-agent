# ea-tool-workflow

EA (Encre Agent) system-default tool package.

| Tool | Factory var | Tier |
| --- | --- | --- |
| `workflow` | workflow_tool | system-default |

Auto-discovered by `encre.plugins.ea_scan` from `core/ea_tools/`. This is an
independent plugin implementation orchestrating sub-agents through the shared
harness engines (`encre.dag.executor.DagExecutor` for light mode,
`encre.swarm.EncreSwarmSession` for team mode).
