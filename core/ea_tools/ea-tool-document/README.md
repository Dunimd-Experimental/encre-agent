# ea-tool-document

EA (Encre Agent) system-default tool package.

| Tool | Factory var | Tier |
| --- | --- | --- |
| `document` | document_tool | system-default |

Auto-discovered by `encre.plugins.ea_scan` from `core/ea_tools/` (deployed into the plugin install dir before scanning). The tool
implementations are verbatim copies of the corresponding `encre.tools.builtin`
modules, adapted only by renaming the factory variable for the plugin system.
