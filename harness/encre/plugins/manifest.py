#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

"""``encre-plugin.json`` declarative manifest support.

VSCode-style plugins ship a JSON manifest describing their static
contributions (skills, slash commands, templates) and their activation
events.  Two tracks are supported:

- **Declarative track**: a directory with only ``encre-plugin.json`` —
  static contributions (skill files, command definitions, templates)
  become visible at startup without executing any plugin code.
- **Hybrid track**: ``encre-plugin.json`` + ``plugin.py`` — the manifest
  drives metadata, permissions, engines, and lazy activation; the Python
  module provides the imperative :class:`EncrePlugin` implementation.

Both tracks share the same registry, so hand-built ``EncrePlugin``
subclasses keep working unchanged (9.1 dual track).
"""

import json
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

logger = get_logger("encre.plugins")

MANIFEST_FILENAME = "encre-plugin.json"

_SUPPORTED_ENGINES = {"python", "typescript", "rust"}

#: ``engines`` also carries the harness compatibility range under ``encre``
#: (VSCode-style).  It is a version constraint, not a language runtime, so it
#: is kept alongside :data:`_SUPPORTED_ENGINES` -- ``PluginManifest.encre_engine``
#: documents ``engines = {"encre": ">=0.5"}`` as the form new manifests
#: should declare, and dropping it here made that advice a no-op.
_ENGINE_KEYS = _SUPPORTED_ENGINES | {"encre"}

#: Upper bound for a plugin name — engine permission keys, composite
#: handler ids and log lines all embed it verbatim.
_NAME_MAX_LEN = 128


def is_valid_plugin_name(name: Any) -> bool:
    """True when *name* is safe to use as a plugin identity.

    The name keys the permission engine (resource / UI / tool ownership),
    composite handler ids (``plugin:source:event:ref``) and every log
    line, so it must not carry path separators, ``:`` (which would forge
    composite-key structure), control characters or whitespace padding.
    Length is capped to keep engine keys and log lines bounded.
    """
    if not isinstance(name, str):
        return False
    if not name or len(name) > _NAME_MAX_LEN:
        return False
    if name != name.strip():
        return False
    return not any(ch in (":", "\\", "/") or ord(ch) < 0x20 or ch == "\x7f" for ch in name)


def load_manifest(path: Path) -> PluginManifest | None:
    """Parse an ``encre-plugin.json`` file into a :class:`PluginManifest`.

    Returns ``None`` when the file is missing or invalid.  Unknown keys
    are ignored; required fields are ``name`` and ``version``.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Invalid manifest %s: %s", path, e)
        return None
    if not isinstance(raw, dict):
        logger.warning("Manifest %s must be a JSON object", path)
        return None
    name = raw.get("name")
    version = raw.get("version")
    if not name or not version:
        logger.warning("Manifest %s missing 'name' or 'version'", path)
        return None
    if not is_valid_plugin_name(name):
        logger.warning("Manifest %s has an invalid plugin name; rejected", path)
        return None
    # Required-field gate: identity, provenance and activation semantics
    # must all be stated explicitly — an empty-ish manifest is rejected so
    # the plugin market never shows half-baked entries.
    required = ("description", "icon", "author", "license", "tags", "tier", "engines", "activation", "permissions")
    missing = [k for k in required if k not in raw]
    if missing:
        logger.warning("Manifest %s missing required field(s): %s", path, ", ".join(missing))
        return None

    contributes = raw.get("contributes") or {}
    provides_skills = [
        s.get("name", Path(str(s.get("path", ""))).stem) if isinstance(s, dict) else Path(str(s)).stem
        for s in contributes.get("skills", [])
    ]
    provides_tools = [t.get("name", "?") if isinstance(t, dict) else str(t) for t in contributes.get("tools", [])]
    provides_backends = list((contributes.get("backends") or {}).keys())

    tools_meta = [entry for entry in (contributes.get("tools_meta") or []) if isinstance(entry, dict) and entry.get("name")]

    # MCP / agent contributions: ``contributes.mcp_servers`` entries are
    # MCP server configs ({"name", "command"|"url", ...}) and
    # ``contributes.agents`` entries are sub-agent definitions
    # (``SubAgentConfig`` dict shape).  Both are declarative — no plugin
    # code is required to provide an MCP server or a sub-agent.
    provides_mcp = [dict(e) for e in (contributes.get("mcp_servers") or []) if isinstance(e, dict)]
    provides_agent = [dict(e) for e in (contributes.get("agents") or []) if isinstance(e, dict)]

    # Declarative hook registrations: ``contributes.hooks`` entries are
    # ``{"event": ..., "handler": "module:func", "priority"?: ..., "timeout"?: ...}``.
    # Handlers are imported and registered onto the arbitration chain at
    # activation (see ``_register_manifest_contributions``).
    provides_hooks = [
        {
            "event": str(e.get("event", "")),
            "handler": str(e.get("handler", "")),
            "priority": e.get("priority", 100),
            "timeout": e.get("timeout", 5),
        }
        for e in (contributes.get("hooks") or [])
        if isinstance(e, dict) and str(e.get("event", "")).strip() and str(e.get("handler", "")).strip()
    ]

    # Per-tool danger levels (``tool_name`` -> safe|high|critical) and the
    # fail-closed resource grants.  Without these being read, a JSON-only
    # plugin would have no way to reach ``context.resources`` at all: the
    # engine denies everything it has not been granted.
    danger_levels = {
        str(k): str(v)
        for k, v in (raw.get("danger_levels") or {}).items()
        if isinstance(v, str)
    }
    resources = raw.get("resources") if isinstance(raw.get("resources"), dict) else {}
    ui_permissions = raw.get("ui_permissions") if isinstance(raw.get("ui_permissions"), dict) else {}
    resource_limits = raw.get("resource_limits") if isinstance(raw.get("resource_limits"), dict) else {}
    transport = str(raw.get("transport") or "local")

    manifest = PluginManifest(
        name=str(name),
        version=str(version),
        description=str(raw.get("description", "")),
        icon=str(raw.get("icon", "")),
        author=str(raw.get("author", "")),
        license=str(raw.get("license", "")),
        homepage=str(raw.get("homepage", "")),
        email=str(raw.get("email", "")),
        repository=str(raw.get("repository", "")),
        docs=str(raw.get("docs", "")),
        source=PluginSource.PROJECT,
        tier=str(raw.get("tier", "user")),
        dependencies=list(raw.get("dependencies", [])),
        min_ea_version=str(raw.get("min_ea_version", "") or raw.get("engines", {}).get("encre", "0.1.0")),
        tags=list(raw.get("tags", [])),
        provides_tools=provides_tools,
        provides_skills=provides_skills,
        provides_backends=provides_backends,
        activation=str(raw.get("activation", "") or "") or "deferred",
        activation_events=list(raw.get("activationEvents", [])),
        permissions=list(raw.get("permissions", [])),
        engines={k: str(v) for k, v in (raw.get("engines") or {}).items() if k in _ENGINE_KEYS},
        contributes=contributes,
        tools_meta=tools_meta,
        provides_mcp=provides_mcp,
        provides_agent=provides_agent,
        provides_hooks=provides_hooks,
        transport=transport,
        allow_local=bool(raw.get("allow_local", False)),
        endpoint=str(raw.get("endpoint", "")),
        resource_limits=resource_limits,
        danger_levels=danger_levels,
        resources=resources,
        ui_permissions=ui_permissions,
    )
    return manifest


class DeclarativePlugin(EncrePlugin):
    """A plugin defined purely by ``encre-plugin.json`` (no Python code).

    Static contributions are served straight from the manifest:
    - ``contributes.skills``: list of ``{name, path}`` relative SKILL.md paths
    - ``contributes.commands``: list of slash-command definitions
    - ``contributes.templates``: list of template definitions

    No side effects occur at load time; the instance is inert until the
    host activates it, and its code (if any) is only imported when an
    activation event fires (lazy activation, 9.4).
    """

    def __init__(self, manifest: PluginManifest, plugin_dir: Path) -> None:
        self.manifest = manifest
        self._dir = plugin_dir
        # Manifest UI entries may reference compiled files (`ui/plugin.js`);
        # the registry resolves those against the package directory.
        if not manifest.pkg_dir:
            manifest.pkg_dir = str(plugin_dir)

    def get_skills(self) -> list[Any]:
        """Load skill files declared in ``contributes.skills``."""
        from encre.skills.types import BundledSkillDefinition

        skills: list[Any] = []
        for entry in self.manifest.contributes.get("skills", []):
            try:
                if isinstance(entry, dict):
                    name = entry.get("name", "")
                    rel = entry.get("path", "")
                else:
                    name, rel = "", str(entry)
                skill_path = self._dir / rel
                if not rel or not skill_path.is_file():
                    skill_path = self._dir / "SKILL.md"
                if skill_path.is_file():
                    text = skill_path.read_text(encoding="utf-8-sig")
                    # Derive a short description from the first non-empty
                    # line, stripping a leading markdown heading marker.
                    description = ""
                    for line in text.splitlines():
                        stripped = line.strip().lstrip("#").strip()
                        if stripped:
                            description = stripped
                            break

                    async def _render_prompt(
                        _args: Any = None,
                        _ctx: Any = None,
                        _body: str = text,
                    ) -> str:
                        return _body

                    skills.append(
                        BundledSkillDefinition(
                            name=name or skill_path.parent.name,
                            description=description or name or skill_path.parent.name,
                            get_prompt_for_command=_render_prompt,
                            body=text,
                            source=f"plugin:{self.manifest.name}",
                            file_path=str(skill_path),
                        )
                    )
            except Exception as e:
                logger.warning("Plugin '%s' skill load failed: %s", self.manifest.name, e)
        return skills

    def get_commands(self) -> list[dict[str, Any]]:
        """Return slash-command definitions from ``contributes.commands``."""
        return list(self.manifest.contributes.get("commands", []))

    def get_templates(self) -> list[dict[str, Any]]:
        """Return template definitions from ``contributes.templates``."""
        return list(self.manifest.contributes.get("templates", []))

    def get_tool_meta(self) -> list[dict[str, Any]]:
        """Return tool presentation overrides from ``contributes.tools_meta``."""
        return list(self.manifest.contributes.get("tools_meta", []))
