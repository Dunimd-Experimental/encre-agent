"""EA database Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_database.tool import database_tool


def create_plugin() -> EncrePlugin:
    return _DatabasePlugin()


class _DatabasePlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-database",
        display_name="Database",
        icon="database",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: database.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "data"],
        provides_tools=['database'],
        provides_skills=["tool-database"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [database_tool]
