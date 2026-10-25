"""EA travel-flights skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-travel-flights",
        display_name="Travel Flights",
        icon="plane-takeoff",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: travel-flights.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "travel"],
        provides_skills=["travel-flights"],
        activation_events=[],
        permissions=[],
    )
