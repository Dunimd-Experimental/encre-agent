"""EA oversized-cursor skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-oversized-cursor",
        display_name="Oversized Cursor",
        icon="mouse-pointer-click",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: oversized-cursor.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "dev"],
        provides_skills=["oversized-cursor"],
        activation_events=[],
        permissions=[],
    )
