# ea-tool-agent

EA (Encre Agent) system-default tool package.

| Tool | Factory var | Tier |
| --- | --- | --- |
| `agent` | agent_tool | system-default |

Auto-discovered by `encre.plugins.ea_scan` from `core/ea_tools/`. This is an
independent plugin implementation delegating to the host loop's
`_run_sub_agent` (parallel fan-out, per-task timeouts, streaming transcripts).
