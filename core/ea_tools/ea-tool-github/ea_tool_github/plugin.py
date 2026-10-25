"""EA github Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_github.tool import git_hub_tool


def create_plugin() -> EncrePlugin:
    return _GithubPlugin()


class _GithubPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-github",
        display_name="Github",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: github.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['github'],
        provides_skills=["tool-github"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [git_hub_tool]
