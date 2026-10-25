# ea-tool-platform-api

EA (Encre Agent) system-default tool package.

| Tool | Factory var | Tier |
| --- | --- | --- |
| `batch_api` | batch_api_tool | system-default |
| `file_api` | file_api_tool | system-default |
| `fine_tuning_api` | fine_tune_api_tool | system-default |

Auto-discovered by `encre.plugins.ea_scan` from `core/ea_tools/` (deployed into the plugin install dir before scanning). The tool
implementations are verbatim copies of the corresponding `encre.tools.builtin`
modules, adapted only by renaming the factory variable for the plugin system.
