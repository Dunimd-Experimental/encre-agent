"""EA info Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_info.tool import info_tool


def create_plugin() -> EncrePlugin:
    return _InfoPlugin()


class _InfoPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-info",
        display_name="Info",
        icon="badge-info",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: info.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['info'],
            danger_levels={"info": "safe"},
        provides_skills=["tool-info"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [info_tool]
