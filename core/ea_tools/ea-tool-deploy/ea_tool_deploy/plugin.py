"""EA deploy Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_deploy.tool import deploy_tool


def create_plugin() -> EncrePlugin:
    return _DeployPlugin()


class _DeployPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-deploy",
        display_name="Deploy",
        icon="cloud-upload",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: deploy.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "cloud"],
        provides_tools=['deploy'],
        provides_skills=["tool-deploy"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [deploy_tool]
