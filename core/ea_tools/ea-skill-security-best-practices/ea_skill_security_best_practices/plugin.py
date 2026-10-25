"""EA security-best-practices skill plugin."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-skill-security-best-practices",
        display_name="Security Best Practices",
        icon="shield-check",
        version="0.5.0",
        description="EA (Encre Agent) system-default skill: security-best-practices.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "skill", "dev"],
        provides_skills=["security-best-practices"],
        activation_events=[],
        permissions=[],
    )
