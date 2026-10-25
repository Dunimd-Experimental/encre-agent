# ea-tool-media-api

EA (Encre Agent) system-default tool package.

| Tool | Factory var | Tier |
| --- | --- | --- |
| `create_embeddings` | create_embeddings_tool | system-default |
| `create_moderation` | create_moderation_tool | system-default |
| `transcribe_audio` | transcribe_audio_tool | system-default |
| `translate_audio` | translate_audio_tool | system-default |

Auto-discovered by `encre.plugins.ea_scan` from `core/ea_tools/` (deployed into the plugin install dir before scanning). The tool
implementations are verbatim copies of the corresponding `encre.tools.builtin`
modules, adapted only by renaming the factory variable for the plugin system.
