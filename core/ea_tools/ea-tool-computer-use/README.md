# ea-tool-computer-use

EA (Encre Agent) system-default tool package.

| Tool | Factory var | Tier |
| --- | --- | --- |
| `computer` | computer_tool | system-default |

Auto-discovered by `encre.plugins.ea_scan` from `core/ea_tools/` (deployed into the plugin install dir before scanning). The tool
implementations are verbatim copies of the corresponding `encre.tools.builtin`
modules, adapted only by renaming the factory variable for the plugin system.

The `computer` tool is the single computer-control surface: it merges the
former `computer_use`, `vlm_computer_use`, `computer` and `device_display`
tools into one action schema, attaches screenshots to the model context as
images (never as base64 text), and adds an OCR digest so text-only models
can see the screen.
