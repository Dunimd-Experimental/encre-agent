"""EA image Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_image.tool import image_tool


def create_plugin() -> EncrePlugin:
    return _ImagePlugin()


class _ImagePlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-image",
        display_name="Image",
        icon="image",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: image.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "media"],
        provides_tools=['image'],
        provides_skills=["tool-image"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [image_tool]
