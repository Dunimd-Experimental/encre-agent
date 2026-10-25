"""EA systematic-literature-review skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-systematic-literature-review",
        display_name="Systematic Literature Review",
        icon="book-marked",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: systematic-literature-review.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "research"],
        provides_skills=["systematic-literature-review"],
        activation_events=[],
        permissions=[],
    )
