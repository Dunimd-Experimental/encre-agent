"""EA clawsignal skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-clawsignal",
        display_name="Clawsignal",
        icon="signal",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: clawsignal.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill"],
        provides_skills=["clawsignal"],
        activation_events=[],
        permissions=[],
    )
