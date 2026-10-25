"""EA generate_image Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_generate_image.tool import edit_image_tool, generate_image_tool, image_variation_tool


def create_plugin() -> EncrePlugin:
    return _GenerateImagePlugin()


class _GenerateImagePlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-generate-image",
        display_name="Generate Image",
        icon="image-plus",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: edit_image, generate_image, image_variation.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "media"],
        provides_tools=['edit_image', 'generate_image', 'image_variation'],
        provides_skills=["tool-edit_image", "tool-generate_image", "tool-image_variation"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [edit_image_tool, generate_image_tool, image_variation_tool]
