"""EA code-documentation skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-code-documentation",
        display_name="Code Documentation",
        icon="file-code",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: code-documentation.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "dev"],
        provides_skills=["code-documentation"],
        activation_events=[],
        permissions=[],
    )
