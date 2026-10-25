"""EA hash_crypto Plugin (system-default)."""
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_hash_crypto.tool import hash_crypto_tool


def create_plugin() -> EncrePlugin:
    return _HashCryptoPlugin()


class _HashCryptoPlugin(EncrePlugin):
    manifest = PluginManifest(
        name="ea-tool-hash-crypto",
        display_name="Hash Crypto",
        icon="hash",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: hash_crypto.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "dev"],
        provides_tools=['hash_crypto'],
        provides_skills=["tool-hash_crypto"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        return [hash_crypto_tool]
