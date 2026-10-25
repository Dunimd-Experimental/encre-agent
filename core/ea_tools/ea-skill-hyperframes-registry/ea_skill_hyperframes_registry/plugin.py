"""EA hyperframes-registry skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-hyperframes-registry",
        display_name="Hyperframes Registry",
        icon="library",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: hyperframes-registry.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "web"],
        provides_skills=["hyperframes-registry"],
        activation_events=[],
        permissions=[],
    )
