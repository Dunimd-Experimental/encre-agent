"""EA report-generator-skill skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-report-generator-skill",
        display_name="Report Generator Skill",
        icon="file-output",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: report-generator-skill.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "writing"],
        provides_skills=["report-generator-skill"],
        activation_events=[],
        permissions=[],
    )
