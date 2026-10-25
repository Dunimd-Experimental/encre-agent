"""EA json_tool Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_json_tool.tool import json_tool


def create_plugin() -> EncrePlugin:
    return _JsonToolPlugin()


class _JsonToolPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-json-tool",
        display_name="JSON Tool",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: json_tool.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['json_tool'],
        provides_skills=["tool-json_tool"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [json_tool]
