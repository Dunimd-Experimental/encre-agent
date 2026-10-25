#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
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

"""Plugin lifecycle handlers: install and uninstall.

Every install path converges on ONE artefact: the standard plugin package —
a self-contained EA Python package (``pyproject.toml`` + importable module
exposing a ``create_plugin`` factory) under ``<data_dir>/plugins/``, the
exact layout the ``encre.plugins.ea_scan`` directory scanner reads.  Three
install routes feed it:

* manual configuration (sub-agent / MCP server) is converted into a
  generated declarative package (``definition.json`` + factory) by
  :meth:`PluginHandlers._write_spec_plugin`;
* a legacy zip (``encre-plugin.json`` at its root) is converted into a
  standard package by :func:`_convert_legacy_package`;
* a standard package ships as-is (a ``.whl`` wheel, unpacked by
  :func:`_install_wheel_package`, or a zip of an existing package, handled
  by :func:`_install_ea_package_zip`).

Standard packages are tier ``user``, so discovery registers them PENDING
and the install click itself confirms activation (the only point where
plugin code is imported) — identical to the market-install flow.  After
install the lifecycle snapshot + UI sync are broadcast so every renderer
updates immediately.  Uninstalling removes the package directory from
whichever root contains it, drops it from the registry (deactivating first
when needed) and broadcasts again.

Code in an installed package is NOT imported during install — discovery
keeps third-party plugins inert until the dynamic-authorisation flow
approves them, exactly like directory-scanned plugins.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from encre.server.protocol import (
    ClientMarketInstall,
    ClientPluginDisable,
    ClientPluginEnable,
    ClientPluginInstall,
    ClientPluginInstallAgent,
    ClientPluginInstallMCP,
    ClientPluginUninstall,
)

logger = logging.getLogger("encre.transport.ws")

# Size guards: an installed plugin package must stay well below these.
_MAX_ZIP_BYTES = 32 * 1024 * 1024
_MAX_UNPACKED_BYTES = 128 * 1024 * 1024


def _user_plugins_dir() -> Path:
    """The user-level plugin install root."""
    from encre.config import get_data_dir

    d = get_data_dir() / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plugin_project_dirs() -> list[Path]:
    """Every directory root a plugin package may live in (user + project)."""
    roots = [_user_plugins_dir()]
    project = (Path("./.encre/plugins/")).resolve()
    roots.append(project)
    return roots


def _find_plugin_dir(name: str) -> Path | None:
    """Locate the installed directory of plugin *name* (user root first)."""
    for root in _plugin_project_dirs():
        if not root.is_dir():
            continue
        candidate = root / name
        if candidate.is_dir():
            return candidate
        # Central-repository installs unpack into ea-plugin-<name> (the
        # directory-scan prefix; the manifest name is the bare <name>).
        candidate = root / f"ea-plugin-{name}"
        if candidate.is_dir():
            return candidate
        # The directory name may differ from the manifest name (e.g. zip
        # "myplugin-1.0.0.zip" unpacks to "myplugin-1.0.0"); scan manifests.
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            manifest = entry / "encre-plugin.json"
            if not manifest.is_file():
                continue
            try:
                import json

                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data.get("name") == name:
                    return entry
            except Exception:
                continue
    return None


# ── Standard plugin package (EA Python package) conversion ─────────────

#: Directory/project prefixes the ``ea_scan`` scanner recognizes.
_EA_PKG_PREFIXES = ("ea-tool-", "ea-skill-", "ea-plugin-", "ea-connector-")


def _ea_names(safe: str) -> tuple[str, str]:
    """Normalize a package suffix into ``(project_name, module_name)``.

    The standard plugin package is an EA Python package: the directory /
    ``pyproject`` project name must carry an ``ea-`` prefix the scanner
    recognizes, and the importable module is the same name underscored
    (``ea_scan`` resolves modules only inside the ``ea_`` namespace).
    """
    proj = safe.strip().strip(".-")
    if not proj.startswith(_EA_PKG_PREFIXES):
        proj = f"ea-plugin-{proj}"
    return proj, proj.replace("-", "_")


def _unpack_zip_into(zf: zipfile.ZipFile, dest: Path, strip_top: bool) -> None:
    """Extract every regular file of *zf* into *dest* (zip-slip guarded).

    ``strip_top`` removes one common top-level folder (zips made from a
    directory) so package metadata lands directly inside *dest*.
    """
    names = [n.replace("\\", "/") for n in zf.namelist() if n.strip()]
    top: str | None = None
    if strip_top:
        tops = {n.split("/")[0] for n in names}
        if len(tops) == 1:
            top = next(iter(tops))
    for member in zf.infolist():
        if member.is_dir():
            continue
        rel = member.filename.replace("\\", "/")
        if top and rel.startswith(top + "/"):
            rel = rel[len(top) + 1:]
        if not rel or rel.endswith("/"):
            continue
        out = (dest / rel).resolve()
        if not str(out).startswith(str(dest.resolve())):
            raise ValueError("package contains an unsafe path")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(member))


def _toml_scalar(value: Any) -> str:
    """Escape *value* as a TOML basic string (JSON string literals are TOML)."""
    return json.dumps(str(value), ensure_ascii=False)


def _gen_ea_pyproject(
    project: str, module: str, version: str, description: str,
    display: str, kind: str,
) -> str:
    """Render the ``pyproject.toml`` of a generated standard plugin package.

    ``pyproject.toml`` is the directory scanner's single source of metadata
    truth: ``[project]`` carries identity, the ``ea.plugins`` entry point
    maps the package onto its plugin factory, and ``[tool.ea]`` records the
    tier (``user`` — the dynamic-authorisation gate applies) plus a
    provenance ``kind``.
    """
    return "\n".join([
        "[build-system]",
        'requires = ["setuptools>=68", "wheel"]',
        'build-backend = "setuptools.build_meta"',
        "",
        "[project]",
        f"name = {_toml_scalar(project)}",
        f"version = {_toml_scalar(version)}",
        f"description = {_toml_scalar(description)}",
        'requires-python = ">=3.11"',
        "",
        '[project.entry-points."ea.plugins"]',
        f"{module} = {_toml_scalar(f'{module}.plugin:create_plugin')}",
        "",
        "[tool.ea]",
        'tier = "user"',
        f"kind = {_toml_scalar(kind)}",
        (
            f"display_name = {_toml_scalar(display)}"
            if display
            else ""
        ),
        "",
        "[tool.setuptools.packages.find]",
        'where = ["."]',
        f"include = [{json.dumps(module + '*')}]",
        "",
        "[tool.setuptools.package-data]",
        f"{module} = [\"*.json\", \"skills/**/*.md\"]",
    ]).replace("\n\n\n", "\n\n") + "\n"


def _gen_wheel_pyproject(project: str, factory_target: str, version: str) -> str:
    """Render the ``pyproject.toml`` re-rendered beside an unpacked wheel.

    Wheels do not carry one, but the directory scanner reads identity and
    the factory mapping exclusively from ``pyproject.toml`` — so the
    wheel's own single ``ea.plugins`` entry point is transcribed verbatim.
    """
    module = factory_target.split(":", 1)[0].split(".", 1)[0]
    return "\n".join([
        "[build-system]",
        'requires = ["setuptools>=68", "wheel"]',
        'build-backend = "setuptools.build_meta"',
        "",
        "[project]",
        f"name = {_toml_scalar(project)}",
        f"version = {_toml_scalar(version)}",
        'description = ""',
        'requires-python = ">=3.11"',
        "",
        '[project.entry-points."ea.plugins"]',
        f"{module} = {_toml_scalar(factory_target)}",
        "",
        "[tool.ea]",
        'tier = "user"',
        'kind = "wheel"',
    ]) + "\n"


def _gen_spec_plugin_py(
    project: str, version: str, description: str, display: str, kind: str,
) -> str:
    """Render ``plugin.py`` for a declarative spec package.

    The plugin carries no executable logic of its own: its manifest loads
    ``definition.json`` (packaged beside it) and re-exposes the recorded
    agent / MCP contributions, which the registry reads off
    ``provides_agent`` / ``provides_mcp`` at activation.
    """
    return f'''# -*- coding: utf-8 -*-
"""Generated declarative plugin for Encre Agent — do not edit by hand.

This module was produced by the unified install pipeline (kind: {kind}).
The plugin carries no logic of its own: it loads ``definition.json`` from
its package directory and re-exposes the recorded contributions through
the manifest, which the harness reads at activation.
"""

from __future__ import annotations

import json
from pathlib import Path

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

_DEFINITION = json.loads(
    Path(__file__).with_name("definition.json").read_text(encoding="utf-8")
)


class _SpecPlugin(EncrePlugin):
    """Declarative plugin: identity + recorded contributions, nothing else."""

    manifest = PluginManifest(
        name={json.dumps(project, ensure_ascii=False)},
        version={json.dumps(version, ensure_ascii=False)},
        description={json.dumps(description, ensure_ascii=False)},
        display_name={json.dumps(display, ensure_ascii=False)},
        source=PluginSource.INSTALLED,
        tier="user",
        provides_agent=[dict(e) for e in _DEFINITION.get("agents", [])],
        provides_mcp=[dict(e) for e in _DEFINITION.get("mcp_servers", [])],
    )


def create_plugin() -> EncrePlugin:
    """Factory the ``ea.plugins`` entry point resolves to."""
    return _SpecPlugin()
'''


def _staging_dir(name: str) -> Path:
    """Return a fresh staging directory under the plugin root."""
    staging = _user_plugins_dir() / f".{name}.staging"
    if staging.exists():
        shutil.rmtree(str(staging), ignore_errors=True)
    staging.mkdir(parents=True)
    return staging


def _promote_staging(staging: Path, target: Path) -> Path:
    """Atomically move a finished staging tree onto *target* (replacing it)."""
    if target.exists():
        shutil.rmtree(str(target), ignore_errors=True)
    staging.rename(target)
    return target


def _install_wheel_package(zf: zipfile.ZipFile, filename: str) -> tuple[Path, str, str]:
    """Install a standard plugin wheel by unpacking it as an EA package.

    Mirrors the market installer's pip-free track: the wheel's
    ``entry_points.txt`` must carry exactly one ``ea.plugins`` entry in
    ``module:factory`` form, the payload is unpacked into a staging
    directory, a ``pyproject.toml`` is re-rendered beside the code (the
    scanner's metadata source), and the tree lands under
    ``<data>/plugins/<project>``.

    Returns:
        ``(package_dir, plugin_name, version)``.

    Raises:
        ValueError: not a plugin wheel, unsafe paths, or a name outside
            the reserved ``ea-`` namespaces.
    """
    import configparser

    ep_member = next(
        (n for n in zf.namelist() if n.endswith("entry_points.txt") and ".dist-info" in n), None)
    if ep_member is None:
        raise ValueError("wheel carries no entry_points.txt")
    cp = configparser.ConfigParser()
    cp.read_string(zf.read(ep_member).decode("utf-8"))
    if not cp.has_section("ea.plugins") or len(cp.options("ea.plugins")) != 1:
        raise ValueError("wheel is not a single standard Encre Agent plugin package")
    factory_target = cp.get("ea.plugins", cp.options("ea.plugins")[0]).strip()
    if ":" not in factory_target:
        raise ValueError("plugin entry point target is not 'module:factory'")

    dist_name = ""
    dist_version = ""
    meta_member = next(
        (n for n in zf.namelist() if n.endswith("METADATA") and ".dist-info" in n), None)
    if meta_member is not None:
        for line in zf.read(meta_member).decode("utf-8", "replace").splitlines():
            if line.startswith("Name:") and not dist_name:
                dist_name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:") and not dist_version:
                dist_version = line.split(":", 1)[1].strip()
            if dist_name and dist_version:
                break
    if not dist_name:
        dist_name = Path(filename or "plugin").stem.split("-")[0]
    from encre.protocol.handlers.base import safe_pkg_name

    project, _module = _ea_names(safe_pkg_name(dist_name))
    if not project.startswith(_EA_PKG_PREFIXES):
        raise ValueError(f"'{dist_name}' is not a standard Encre Agent plugin package")

    staging = _staging_dir(project)
    try:
        for member in zf.infolist():
            if member.is_dir():
                continue
            out = (staging / member.filename.replace("\\", "/")).resolve()
            if not str(out).startswith(str(staging.resolve())):
                raise ValueError("wheel contains an unsafe path")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(member))
        (staging / "pyproject.toml").write_text(
            _gen_wheel_pyproject(project, factory_target, dist_version), encoding="utf-8")
        target = _promote_staging(staging, _user_plugins_dir() / project)
    finally:
        if staging.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
    logger.info("[plugin_install] unpacked standard wheel '%s' into %s", project, target)
    return target, project, dist_version or "0.0.0"


def _install_ea_package_zip(zf: zipfile.ZipFile) -> tuple[Path, str, str]:
    """Install a zip that already IS a standard EA Python package.

    The archive must carry ``pyproject.toml`` (at its root, or inside one
    common top folder).  Identity comes from the parsed ``[project]``
    table; a name outside the reserved ``ea-`` namespaces is rejected
    because the directory scanner would never see the package.

    Returns:
        ``(package_dir, plugin_name, version)``.
    """
    import tomllib

    names = [n.replace("\\", "/") for n in zf.namelist() if n.strip()]
    strip_top = not any(n == "pyproject.toml" for n in names)
    staging = _staging_dir("ea-zip")
    try:
        _unpack_zip_into(zf, staging, strip_top)
        pyproject = staging / "pyproject.toml"
        if not pyproject.is_file():
            raise ValueError("package carries no pyproject.toml")
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project_data = data.get("project") if isinstance(data.get("project"), dict) else {}
        raw_name = str((project_data or {}).get("name") or "").strip()
        if not raw_name or "/" in raw_name or "\\" in raw_name or ".." in raw_name:
            raise ValueError("package pyproject.toml carries no valid project name")
        if not raw_name.startswith(_EA_PKG_PREFIXES):
            raise ValueError(
                f"standard plugin package name must carry an ea- prefix (got '{raw_name}')")
        version = str((project_data or {}).get("version") or "0.0.0")
        eps = (project_data or {}).get("entry-points")
        eps = eps.get("ea.plugins") if isinstance(eps, dict) else None
        factory = next(iter(eps.values()), "") if isinstance(eps, dict) and eps else ""
        module = str(factory).split(":", 1)[0].split(".", 1)[0] if factory else raw_name.replace("-", "_")
        if not (staging / module).is_dir() and not (staging / f"{module}.py").is_file():
            raise ValueError(f"package module '{module}' not found in the archive")
        target = _promote_staging(staging, _user_plugins_dir() / raw_name)
    finally:
        if staging.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
    logger.info("[plugin_install] installed standard package '%s' into %s", raw_name, target)
    return target, raw_name, version


def _convert_legacy_package(zf: zipfile.ZipFile) -> tuple[Path, str, str]:
    """Convert a legacy manifest package into a standard EA Python package.

    Legacy zips carry ``encre-plugin.json`` (+ optional ``plugin.py``) at
    their root — the pre-standard format.  Everything except the manifest
    moves into the generated importable module; a hybrid ``plugin.py``
    keeps its ``create_plugin`` contract unchanged, a manifest-only
    package gets a generated declarative factory that re-exposes the
    recorded ``contributes`` (agents / MCP servers).  The result is
    indistinguishable from a hand-written standard package.

    Returns:
        ``(package_dir, plugin_name, version)``.
    """
    from encre.protocol.handlers.base import safe_pkg_name

    names = [n.replace("\\", "/") for n in zf.namelist() if n.strip()]
    strip_top = not any(n in ("encre-plugin.json", "plugin.py") for n in names)
    staging = _staging_dir("ea-convert")
    try:
        _unpack_zip_into(zf, staging, strip_top)
        manifest: dict[str, Any] = {}
        manifest_path = staging / "encre-plugin.json"
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except Exception:
                manifest = {}
        project, module = _ea_names(safe_pkg_name(str(manifest.get("name") or "plugin")))
        version = str(manifest.get("version") or "0.0.0")
        description = str(manifest.get("description") or "")
        display = str(manifest.get("display_name") or manifest.get("name") or "")
        contributes = manifest.get("contributes")
        contributes = contributes if isinstance(contributes, dict) else {}
        agents = [dict(e) for e in (contributes.get("agents") or []) if isinstance(e, dict)]
        mcps = [dict(e) for e in (contributes.get("mcp_servers") or []) if isinstance(e, dict)]

        pkg_dir = staging / module
        if pkg_dir.exists():
            shutil.rmtree(str(pkg_dir), ignore_errors=True)
        pkg_dir.mkdir(parents=True)
        legacy_plugin = staging / "plugin.py"
        had_code = legacy_plugin.is_file()
        for entry in list(staging.iterdir()):
            if entry.name in ("encre-plugin.json", "plugin.py", "pyproject.toml") or entry.name == module:
                continue
            shutil.move(str(entry), str(pkg_dir / entry.name))
        if had_code:
            shutil.move(str(legacy_plugin), str(pkg_dir / "plugin.py"))
        else:
            (pkg_dir / "definition.json").write_text(
                json.dumps({"agents": agents, "mcp_servers": mcps}, indent=2, ensure_ascii=False),
                encoding="utf-8")
            (pkg_dir / "plugin.py").write_text(
                _gen_spec_plugin_py(project, version, description, display, "converted"),
                encoding="utf-8")
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (staging / "pyproject.toml").write_text(
            _gen_ea_pyproject(project, module, version, description, display, "converted"),
            encoding="utf-8")
        target = _promote_staging(staging, _user_plugins_dir() / project)
    finally:
        if staging.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
    logger.info("[plugin_install] converted legacy package '%s' into %s", project, target)
    return target, project, version


class PluginHandlers:
    """Plugin install/uninstall handlers."""

    async def _h_plugin_install(self, ws: Any, msg: ClientPluginInstall) -> None:
        """Install an uploaded archive as a standard plugin package.

        Three upload shapes are recognised and all converge on the same
        artefact — the standard EA Python package under
        ``<data_dir>/plugins/<project>`` (the exact layout the
        ``encre.plugins.ea_scan`` directory scanner reads):

        * a ``.whl`` wheel (or any zip carrying ``.dist-info``) whose
          ``entry_points.txt`` declares exactly one ``ea.plugins`` entry
          is unpacked as-is (:func:`_install_wheel_package`);
        * a zip that already IS a standard package (root
          ``pyproject.toml``) is validated and installed as-is
          (:func:`_install_ea_package_zip`);
        * a legacy ``encre-plugin.json`` manifest package is converted
          into a standard package (:func:`_convert_legacy_package`).

        After the package lands, the shared registry re-discovers it
        (tier ``user`` → PENDING), the install click records the ledger
        entry and confirms activation — the only point where plugin code
        is imported — and a lifecycle snapshot plus a UI sync are
        broadcast so every connected renderer updates without a
        reconnect. Handles the
        :class:`encre.server.protocol.ClientPluginInstall` message type.

        Args:
            ws: The originating connection, used for the success and error
                reply frames.
            msg: The client message carrying ``data_b64`` (zip payload) and
                an optional ``filename`` (disambiguates wheels).

        Returns:
            None. Every outcome is reported to the client as a frame:
            ``plugin_installed`` on success, ``plugin_install_error`` on any
            failure.

        Raises:
            Nothing escapes. Any validation or I/O error is converted into
            the error frame, so a malformed upload can never take down the
            handler. A failed activation rolls the ledger entry back.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        try:
            if not msg.data_b64:
                raise ValueError("empty package payload")
            raw = base64.b64decode(msg.data_b64, validate=False)
            if len(raw) > _MAX_ZIP_BYTES:
                raise ValueError("package exceeds the 32 MiB limit")
            if not zipfile.is_zipfile(io.BytesIO(raw)):
                raise ValueError("package is not a zip archive")

            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                # Normalise backslash separators: some Windows archivers write
                # zip member names with "\" instead of the spec's "/", which
                # would blind every structural probe below.
                names = [n.replace("\\", "/") for n in zf.namelist() if n.strip()]
                total = sum(zi.file_size for zi in zf.infolist())
                if total > _MAX_UNPACKED_BYTES:
                    raise ValueError("unpacked size exceeds the 128 MiB limit")
                tops = {n.split("/")[0] for n in names}
                single_top = next(iter(tops)) if len(tops) == 1 else None
                # Route 1: a standard plugin wheel (.whl / dist-info payload).
                is_wheel = str(msg.filename or "").lower().endswith(".whl") or any(
                    ".dist-info/" in n for n in names)
                # Route 2: a zip that already IS a standard EA package.
                has_pyproject = any(n == "pyproject.toml" for n in names) or (
                    single_top is not None and f"{single_top}/pyproject.toml" in names)
                # Route 3: a legacy package awaiting conversion to standard.
                has_legacy = any(
                    n in ("encre-plugin.json", "plugin.py")
                    or n.endswith("/encre-plugin.json") or n.endswith("/plugin.py")
                    for n in names)
                if is_wheel:
                    target, plugin_name, version = _install_wheel_package(
                        zf, msg.filename or "")
                elif has_pyproject:
                    target, plugin_name, version = _install_ea_package_zip(zf)
                elif has_legacy:
                    target, plugin_name, version = _convert_legacy_package(zf)
                else:
                    preview = ", ".join(sorted(tops)[:5]) or "(empty)"
                    raise ValueError(
                        "package is neither a standard plugin package "
                        "(pyproject.toml) nor a legacy package "
                        f"(encre-plugin.json / plugin.py); top-level entries: {preview}")

            registry.discover_all()
            # Record the authoritative installed.json ledger entry: version
            # tracking + persisted enabled flag (survives restarts).
            registry.record_installed(
                plugin_name, version,
                source="user", pkg_dir=str(target),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
            # The install click IS the user approval: confirm the PENDING
            # package now (the only point its code is imported); a failed
            # activation rolls the ledger entry back — same as market flow.
            approved = registry.confirm_activation(
                plugin_name, host=registry.default_host_dict())
            if not approved:
                registry.record_uninstalled(plugin_name)
                reason = registry.list_failed().get(plugin_name, "activation failed")
                raise ValueError(f"plugin '{plugin_name}' failed to activate: {reason}")
            logger.info("[plugin_install] installed '%s' from upload", plugin_name)
            await self._resync_agent_plugins(mcp_changed=True)
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "plugin_installed", name=plugin_name)
        except Exception as e:
            logger.error("[plugin_install] failed: %s", e)
            await self._send(ws, "plugin_install_error", message=str(e))

    async def _h_market_install(self, ws: Any, msg: ClientMarketInstall) -> None:
        """Install a catalogue plugin by name (market one-click install).

        Two sources, in order.  A plugin already discovered locally (it
        ships with the app or lives in a scanned plugin root) installs by
        writing its ``installed.json`` ledger entry and running it through
        the dynamic-authorisation activation path
        (:meth:`PluginRegistry.confirm_activation` imports pending code
        only now, re-verifies integrity, then activates).  A plugin known
        only to the central index (ea-cwh catalog) is first pip-installed
        pinned to the index version with sha256 artifact verification
        (:func:`encre.market_catalog.install_from_index`), then follows the
        exact same ledger + activation path — clicking Install IS the user
        approval, and a failed activation rolls the ledger entry back.
        Replies ``market_installed`` / ``market_install_error``.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        name = msg.name
        try:
            if not name:
                raise ValueError("missing plugin name")
            manifest = registry.get_manifest(name)
            if manifest is None:
                registry.discover_all()
                manifest = registry.get_manifest(name)
            if manifest is not None:
                # Installed-but-stale: when the index pins a newer version
                # this click means UPDATE, so pull the pinned wheel instead
                # of re-activating the old copy sitting on disk.
                from encre.market_catalog import has_update

                if has_update(name, str(manifest.version or "")):
                    manifest = (await self._install_from_central_index(registry, name)) or manifest
            if manifest is None:
                manifest = await self._install_from_central_index(registry, name)
            if manifest is None:
                raise ValueError(f"plugin '{name}' is not in the catalogue")
            registry.record_installed(
                name, str(manifest.version or "0.0.0"),
                source="market", pkg_dir=str(getattr(manifest, "pkg_dir", "") or ""),
            )
            if not registry.confirm_activation(name, host=registry.default_host_dict()):
                registry.record_uninstalled(name)
                reason = registry.list_failed().get(name, "activation failed")
                raise ValueError(f"plugin '{name}' failed to activate: {reason}")
            await self._resync_agent_plugins(mcp_changed=True)
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "market_installed", name=name)
        except Exception as e:
            logger.error("[market_install] failed: %s", e)
            await self._send(ws, "market_install_error", name=name, message=str(e))

    async def _install_from_central_index(self, registry: Any, name: str) -> Any:
        """Install a central-repository plugin and return its manifest.

        Args:
            registry: The shared plugin registry (re-discovered after the
                pip install so the new entry point is picked up).
            name: Catalog plugin name from the market frame.

        Returns:
            The discovered ``PluginManifest``, or ``None`` when the index
            knows nothing under this name (the caller raises).

        Raises:
            Exception: pip/download/hash failures propagate verbatim into
                the ``market_install_error`` reply.
        """
        import asyncio

        from encre.market_catalog import install_from_index, load_catalog

        entry = next((e for e in load_catalog().get("plugins") or [] if e.get("name") == name), None)
        if entry is None:
            return None
        await asyncio.to_thread(install_from_index, entry)
        registry.discover_all()
        return registry.get_manifest(name)

    async def _h_plugin_uninstall(self, ws: Any, msg: ClientPluginUninstall) -> None:
        """Uninstall a plugin by name, removing both its registry entry and files.

        Deregisters the plugin from the shared registry -- which deactivates it
        first if it is live -- then deletes whichever plugin directory
        :func:`_find_plugin_dir` resolves, and broadcasts the resulting lifecycle
        snapshot and UI sync. Handles the
        :class:`encre.server.protocol.ClientPluginUninstall` message type.

        Args:
            ws: The originating connection, used for the success and error
                reply frames.
            msg: The client message carrying the plugin ``name``.

        Returns:
            None. Reports ``plugin_uninstalled`` on success or
            ``plugin_uninstall_error`` on failure.

        Raises:
            Nothing escapes; every failure is converted into the error frame.

        Note:
            Registry removal precedes directory deletion deliberately: removing
            the directory first would leave any loaded plugin code holding a
            reference into files that are about to disappear. If the name is
            neither registered nor installed anywhere, an error is raised rather
            than silently succeeding.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        name = msg.name
        try:
            if not name:
                raise ValueError("missing plugin name")
            # Drop from the registry first (deactivates when needed) so no
            # live code references the directory being removed.
            removed_registry = registry.unregister(name)
            registry.record_uninstalled(name)
            plugin_dir = _find_plugin_dir(name)
            if plugin_dir is not None:
                shutil.rmtree(str(plugin_dir), ignore_errors=True)
                logger.info("[plugin_uninstall] removed directory %s", plugin_dir)
            elif not removed_registry:
                raise ValueError(f"plugin '{name}' is not installed")
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "plugin_uninstalled", name=name)
        except Exception as e:
            logger.error("[plugin_uninstall] failed: %s", e)
            await self._send(ws, "plugin_uninstall_error", name=name, message=str(e))

    # ── Enable / disable (persisted in installed.json) ─────────────────

    async def _resync_agent_plugins(self, mcp_changed: bool = False) -> None:
        """Push the current plugin activation set into the live agent.

        Re-runs the agent's plugin loader so tool/skill/MCP/sub-agent
        injections reconcile against the registry; reconnects MCP when the
        plugin-provided server set may have changed.  Best-effort: a
        session-less server must not break the lifecycle broadcast.
        """
        try:
            info = self._get_or_create_session()
            self._manager.touch(info.session_id)
            info.agent.reload_plugins()
            if mcp_changed:
                await info.agent.reconnect_mcp()
        except Exception as e:
            logger.warning("[plugin_handlers] agent resync failed: %s", e)

    async def _h_plugin_enable(self, ws: Any, msg: ClientPluginEnable) -> None:
        """Enable a disabled installed plugin and re-activate it live.

        The enabled flag is persisted in the ``installed.json`` ledger, so
        the choice survives restarts: activation paths skip disabled
        plugins.  Handles the :class:`encre.server.protocol.ClientPluginEnable`
        message type; replies ``plugin_enabled`` or ``plugin_enable_error``.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        name = msg.name
        try:
            if not name:
                raise ValueError("missing plugin name")
            if not registry.set_plugin_enabled(name, True):
                raise ValueError(f"plugin '{name}' is not installed")
            await self._resync_agent_plugins(mcp_changed=True)
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "plugin_enabled", name=name)
        except Exception as e:
            logger.error("[plugin_enable] failed: %s", e)
            await self._send(ws, "plugin_enable_error", name=name, message=str(e))

    async def _h_plugin_disable(self, ws: Any, msg: ClientPluginDisable) -> None:
        """Disable an installed plugin (deactivates it; persisted across restarts).

        Handles the :class:`encre.server.protocol.ClientPluginDisable`
        message type; replies ``plugin_disabled`` or ``plugin_disable_error``.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        name = msg.name
        try:
            if not name:
                raise ValueError("missing plugin name")
            if not registry.set_plugin_enabled(name, False):
                raise ValueError(f"plugin '{name}' is not installed")
            await self._resync_agent_plugins(mcp_changed=True)
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "plugin_disabled", name=name)
        except Exception as e:
            logger.error("[plugin_disable] failed: %s", e)
            await self._send(ws, "plugin_disable_error", name=name, message=str(e))

    # ── Unified install pipeline: spec plugins (MCP / agent) ───────────

    def _write_spec_plugin(
        self, kind: str, name: str, definition: dict[str, Any],
        description: str = "", display: str = "", version: str = "1.0.0",
    ) -> tuple[Path, str]:
        """Convert a manual MCP/agent configuration into a standard package.

        The unified install pipeline's manual route: an MCP server or a
        sub-agent configured by hand is materialised as a declarative
        standard EA Python package under ``<data_dir>/plugins/<project>``
        — ``pyproject.toml`` + an importable module exposing a
        ``create_plugin`` factory — exactly what the directory scanner
        expects.  No legacy manifest is written: the recorded contribution
        lives in ``definition.json`` and is re-exposed through
        ``provides_agent`` / ``provides_mcp`` at activation.

        Args:
            kind: Provenance tag recorded in ``[tool.ea] kind`` (``mcp``
                or ``agent``).
            name: User-facing configuration name (sanitised into the
                package / module identity).
            definition: Contribution payload written to ``definition.json``
                (``agents`` and/or ``mcp_servers``).
            description: Package description.
            display: Human-friendly display name.
            version: Package version.

        Returns:
            ``(package_dir, plugin_name)`` — the directory the package
            landed in and the manifest name discovery registers.
        """
        from encre.protocol.handlers.base import safe_pkg_name

        project, module = _ea_names(safe_pkg_name(name))
        target = _user_plugins_dir() / project
        if target.exists():
            shutil.rmtree(str(target), ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        pkg = target / module
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "definition.json").write_text(
            json.dumps(definition, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (pkg / "plugin.py").write_text(
            _gen_spec_plugin_py(project, version, description, display, kind),
            encoding="utf-8",
        )
        (target / "pyproject.toml").write_text(
            _gen_ea_pyproject(project, module, version, description, display, kind),
            encoding="utf-8",
        )
        return target, project

    async def _h_plugin_install_mcp(self, ws: Any, msg: ClientPluginInstallMCP) -> None:
        """Install an MCP server *as a plugin* through the unified pipeline.

        The manual route: the server configuration is converted into a
        declarative standard plugin package whose ``definition.json``
        records ``mcp_servers`` — re-exposed as ``provides_mcp`` so the
        loader injects it into ``config.mcp_servers`` (and drops it again
        when the plugin is disabled or uninstalled).  The package is
        registered PENDING and the install click confirms activation; a
        failed activation rolls the ledger entry back.  Handles
        :class:`encre.server.protocol.ClientPluginInstallMCP`; replies
        ``plugin_installed_mcp`` or ``plugin_install_mcp_error``.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        try:
            name = (msg.name or "").strip()
            if not name:
                raise ValueError("missing MCP server name")
            if not isinstance(msg.server, dict) or not msg.server:
                raise ValueError("missing MCP server config")
            definition = {"mcp_servers": [{"name": name, **msg.server}]}
            target, plugin_name = self._write_spec_plugin(
                "mcp", name, definition,
                description=f"MCP server plugin: {name}", display=name,
            )
            registry.discover_all()
            registry.record_installed(
                plugin_name, "1.0.0", source="user", pkg_dir=str(target),
            )
            approved = registry.confirm_activation(
                plugin_name, host=registry.default_host_dict())
            if not approved:
                registry.record_uninstalled(plugin_name)
                reason = registry.list_failed().get(plugin_name, "activation failed")
                raise ValueError(f"plugin '{plugin_name}' failed to activate: {reason}")
            await self._resync_agent_plugins(mcp_changed=True)
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "plugin_installed_mcp", name=name)
        except Exception as e:
            logger.error("[plugin_install_mcp] failed: %s", e)
            await self._send(ws, "plugin_install_mcp_error", name=msg.name, message=str(e))

    async def _h_plugin_install_agent(self, ws: Any, msg: ClientPluginInstallAgent) -> None:
        """Install a sub-agent *as a plugin* through the unified pipeline.

        The manual route: the sub-agent definition is converted into a
        declarative standard plugin package whose ``definition.json``
        records ``agents`` — re-exposed as ``provides_agent`` so the
        loader appends it to ``config.sub_agents``, alongside the main
        agent and never altering its identity.  The package is registered
        PENDING and the install click confirms activation; a failed
        activation rolls the ledger entry back.  Handles
        :class:`encre.server.protocol.ClientPluginInstallAgent`; replies
        ``plugin_installed_agent`` or ``plugin_install_agent_error``.
        """
        from encre.agent import _get_shared_plugin_registry_raw

        registry = _get_shared_plugin_registry_raw()
        try:
            agent = msg.agent if isinstance(msg.agent, dict) else {}
            name = str(agent.get("name", "")).strip()
            if not name:
                raise ValueError("sub-agent definition requires a 'name'")
            target, plugin_name = self._write_spec_plugin(
                "agent", name, {"agents": [agent]},
                description=str(agent.get("description", ""))
                or f"Agent plugin: {name}",
                display=name,
            )
            registry.discover_all()
            registry.record_installed(
                plugin_name, "1.0.0", source="user", pkg_dir=str(target),
            )
            approved = registry.confirm_activation(
                plugin_name, host=registry.default_host_dict())
            if not approved:
                registry.record_uninstalled(plugin_name)
                reason = registry.list_failed().get(plugin_name, "activation failed")
                raise ValueError(f"plugin '{plugin_name}' failed to activate: {reason}")
            await self._resync_agent_plugins(mcp_changed=False)
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            await self._send(ws, "plugin_installed_agent", name=name)
        except Exception as e:
            logger.error("[plugin_install_agent] failed: %s", e)
            await self._send(ws, "plugin_install_agent_error", name="", message=str(e))
