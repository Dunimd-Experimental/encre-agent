"""EA newsletter-generation skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-newsletter-generation",
        display_name="Newsletter Generation",
        icon="newspaper",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: newsletter-generation.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "writing"],
        provides_skills=["newsletter-generation"],
        activation_events=[],
        permissions=[],
    )
