"""EA pr-to-video skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-pr-to-video",
        display_name="Pr To Video",
        icon="assets/icon.svg",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: pr-to-video.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "media"],
        provides_skills=["pr-to-video"],
        activation_events=[],
        permissions=[],
    )
