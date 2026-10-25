"""EA env_manager Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_env_manager.tool import env_manager_tool


def create_plugin() -> EncrePlugin:
    return _EnvManagerPlugin()


class _EnvManagerPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-env-manager",
        display_name="Env Manager",
        icon="key-round",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: env_manager.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['env_manager'],
        provides_skills=["tool-env_manager"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [env_manager_tool]
