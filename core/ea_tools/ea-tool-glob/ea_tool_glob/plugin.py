"""EA glob Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_glob.tool import glob_tool


def create_plugin() -> EncrePlugin:
    return _GlobPlugin()


class _GlobPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-glob",
        display_name="Glob",
        icon="file-search",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: glob.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['glob'],
            danger_levels={"glob": "safe"},
        provides_skills=["tool-glob"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [glob_tool]
