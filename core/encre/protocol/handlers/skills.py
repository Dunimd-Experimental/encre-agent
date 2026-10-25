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

from __future__ import annotations

"""Skills domain handlers: enable, install, uninstall, update.

Owned client-message domain for the user-skill lifecycle: turning skills
on and off, installing a skill from markdown or a zip, uninstalling one, and
re-saving its contents. :class:`SkillsHandlers` is mixed into
:class:`encre.transport.ws.EncreWSHandler` and backs the
``ClientUpdateSkills`` / ``ClientInstallSkill`` / ``ClientUninstallSkill`` /
``ClientUpdateSkill`` entries of that handler's ``_DISPATCH`` table.

Skills are files, not code objects: a skill is a directory under
``<data_dir>/skills/<name>/`` containing a ``SKILL.md`` with frontmatter. The
registry (:class:`encre.skills` registry attached to
``info.agent.skill_registry``) is a lazy view over that directory, so every
install, uninstall and update ends by calling ``load_from_dir`` again to make
the registry match the disk. The registry is shared across sessions, which is
why a skill installed from one window is available in every session.

Because the registry is shared and is normally filled by the startup plugin
warmup, :meth:`SkillsHandlers._build_skills_list` primes it on demand. Without
that a client that pulls config before warmup completes would see an empty
``available_skills`` list.

The install path accepts three input shapes in a fixed order: a zip file on
disk, a base64-encoded zip inline in the message body, or plain ``SKILL.md``
text. Installed zip packages are also recorded in an encrypted
``index.json`` next to the skill directories, because a zip's directory name
need not match the skill name declared in its frontmatter and the index is
what makes an uninstall findable by either.

Public API:

- :class:`SkillsHandlers`
- :func:`_find_skill_root`
- :func:`_copy_skill_tree`
- :func:`_is_auto_generated_skill`
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from encre.server.protocol import (
    ClientInstallSkill,
    ClientGetSkillFile,
    ClientUninstallSkill,
    ClientGetSkillDetail,
    ClientUpdateSkill,
    ClientUpdateSkills,
)

logger = logging.getLogger("encre.transport.ws")


def _resolve_skill(registry: Any, name: str) -> Any:
    """Find a registered skill by the name a manifest declares.

    The registry normalises skill names per spec (lowercase, hyphens, no
    underscores), while a plugin manifest may still declare the original
    file stem (``tool-generate_image``).  Looking that up verbatim used to
    miss every underscored skill — exactly the multi-skill tool packages —
    and the market then showed no "查看技能说明" entry for them.  So the
    lookup is by normalised name: exact first, then the hyphen/underscore
    agnostic comparison.
    """
    skills = dict(getattr(registry, "_skills", {}) or {})
    key = (name or "").strip()
    if not key:
        return None
    hit = skills.get(key)
    if hit is not None:
        return hit
    norm = key.lower().replace("_", "-")
    for candidate, skill in skills.items():
        if str(candidate).strip().lower().replace("_", "-") == norm:
            return skill
    return None

#: Largest skill document the reader is allowed to pull in one frame.  A
#: package can ship anything; the desktop reader is not a file browser for
#: megabytes of data, so a bigger file is reported rather than half-read.
_MAX_SKILL_FILE_BYTES = 256 * 1024


def _list_skill_files(skill: Any) -> list[str]:
    """Return every file of one skill's own directory, skill-relative.

    ``SKILL.md`` is excluded: it is the page the reader already has, and this
    list exists to show what else the package carries (``references/…``,
    templates, scripts).  Hidden entries and ``__pycache__`` are build noise,
    not part of the skill.

    Args:
        skill: The registry's ``BundledSkillDefinition``; its ``file_path`` is
            the absolute path of the skill's ``SKILL.md``.

    Returns:
        A sorted list of forward-slashed relative paths, empty when the skill
        has no directory on disk.
    """
    file_path = getattr(skill, "file_path", "") or ""
    if not file_path:
        return []
    path = Path(file_path)
    # A flat skill document (``skills/<name>.md``) IS the whole skill: its
    # directory is shared with every sibling skill, so walking it would list
    # other skills' documents as if they belonged to this one.
    if path.name.upper() != "SKILL.MD":
        return []
    root = path.parent
    try:
        if not root.is_dir():
            return []
        names: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            if rel.as_posix() == "SKILL.md":
                continue
            names.append(rel.as_posix())
        return sorted(names)
    except OSError:
        return []


def _read_skill_file(root: Path, rel: str) -> dict[str, Any]:
    """Read one text file inside a skill directory, confined to that directory.

    Args:
        root: The skill's own directory.
        rel: The client-supplied path, relative to ``root``.

    Returns:
        ``{"content": ..., "path": <normalized relative>}`` on success, or
        ``{"content": "", "error": ...}`` when the path leaves ``root``, is
        missing, oversized, or is not text.
    """
    try:
        resolved_root = root.resolve()
        target = (resolved_root / rel).resolve()
        if not str(target).startswith(str(resolved_root) + os.sep):
            return {"content": "", "error": "path escapes the skill directory"}
        if not target.is_file():
            return {"content": "", "error": "file not found"}
        if target.stat().st_size > _MAX_SKILL_FILE_BYTES:
            return {"content": "", "error": "file is too large to display"}
        raw = target.read_text("utf-8")
        content = raw
        if not raw.startswith("---"):
            from encre.crypto import decrypt

            with contextlib.suppress(Exception):
                content = decrypt(raw)
        return {"content": content, "path": target.relative_to(resolved_root).as_posix()}
    except (UnicodeDecodeError, OSError):
        return {"content": "", "error": "not a text file"}


def _find_skill_root(extracted_dir: str) -> str | None:
    """Find the directory that directly contains a ``SKILL.md``.

    Zip packages are not trusted to have the skill files at their root: most
    archives are wrapped in a single top-level folder, and some are not, so
    the install path has to locate the marker file instead of assuming a
    layout.

    Args:
        extracted_dir: The temporary directory the archive was unpacked into.

    Returns:
        The absolute path of the directory holding ``SKILL.md``, or ``None``
        when no marker file is present anywhere in the tree.

    Note:
        Directories starting with a dot are pruned in place, so VCS metadata
        shipped inside a package can neither be matched nor copied out. The
        comparison is case-insensitive because the marker is normally
        ``SKILL.md`` but zips coming from case-insensitive filesystems may
        spell it differently.
    """
    for root, dirs, files in os.walk(extracted_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.upper() == "SKILL.MD":
                return root
    return None


def _copy_skill_tree(src_dir: str, dest_dir: str) -> None:
    """Replace *dest_dir* with a copy of *src_dir*.

    Args:
        src_dir: The source tree, usually the skill root located inside an
            extracted archive.
        dest_dir: The destination skill directory.

    Returns:
        None.

    Note:
        The destination is removed outright rather than merged. A re-install
        of a skill must leave exactly the files the new package shipped,
        otherwise a file deleted upstream would survive an update and the
        registry would keep offering a stale skill. Metadata is preserved
        for individual files by using ``copy2`` rather than ``copy``.
    """
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(dest_dir, exist_ok=True)
    for item in os.listdir(src_dir):
        s = os.path.join(src_dir, item)
        d = os.path.join(dest_dir, item)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


# Subdirectory of the skills dir that the skill-evolution subsystem writes
# its own artifacts into: ``<data dir>/skills/auto_generated``.
_AUTO_GENERATED_DIR = "auto_generated"


def _is_auto_generated_skill(skill: Any) -> bool:
    """Report whether *skill* is an artifact of the skill-evolution subsystem.

    Auto-generated skills live under ``<data dir>/skills/auto_generated`` and
    declare ``source: auto_generated`` in their frontmatter, yet the registry
    registers them as :class:`SkillSource.USER` because provenance follows the
    directory that was walked rather than the frontmatter. Both markers are
    therefore checked, so the catalogue stays correct if either one changes.

    Args:
        skill: A registered skill exposing ``metadata`` and ``file_path``.

    Returns:
        ``True`` when the skill is auto-generated.

    Note:
        Hiding these from the settings panel is about presentation only. The
        skills stay registered and fully invocable -- the panel is a catalogue
        of skills the user installed, and mixed-in generated artifacts would
        make it impossible to tell what is removable.
    """
    metadata = getattr(skill, "metadata", None)
    if isinstance(metadata, dict):
        marker = str(metadata.get("source") or "")
        if marker.startswith("auto_generated"):
            return True
    file_path = str(getattr(skill, "file_path", "") or "")
    if file_path:
        parts = file_path.replace("\\", "/").split("/")
        if _AUTO_GENERATED_DIR in parts[:-1]:
            return True
    return False


class SkillsHandlers:
    """Skill lifecycle handlers plus the encrypted skills index.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`; carries no state of
    its own and reads ``_manager``, ``_persist_config``, ``_tasks`` and
    ``_send`` from the composed handler.

    Lifecycle: stateless, constructed once per server.

    Invariants:

    - The skill registry is a lazy view over ``<data dir>/skills``, so every
      write to that directory is followed by a ``load_from_dir`` re-scan.
      Skipping the re-scan leaves the registry advertising a skill that no
      longer exists on disk.
    - The registry is shared across sessions, so the catalogue is global and
      an install from one window is visible to every session.
    - Only user skills are listed: ``bundled`` and ``managed`` sources and
      auto-generated artifacts are filtered out, and hidden skills are too.
    - The index file is encrypted at rest, consistent with the other
      user-authored content under the data directory.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the composed
        handler.
    """

    @staticmethod
    async def _build_skills_list(info: Any) -> list[dict[str, Any]]:
        """Serialise the user-skill catalogue for one session.

        Builds the ``available_skills`` payload the settings panel renders,
        filtering the registry down to skills the user installed themselves.

        Args:
            info: The :class:`~encre.server.session_manager.SessionInfo` whose
                agent owns the skill registry to list.

        Returns:
            One entry per listed skill with its name, description, aliases,
            source, invocation hints, tool restrictions, context, model,
            license, compatibility, metadata and full ``body``. Never raises.

        Note:
            The body is taken from the skill if it is cached, otherwise it is
            rendered on demand via ``get_prompt_for_command``. Failing to
            render degrades to an empty string rather than dropping the skill,
            since the rest of the entry is still useful.
        """
        results: list[dict[str, Any]] = []
        try:
            registry = info.agent.skill_registry
            # The agent attaches the *shared* skill registry, which starts
            # empty when the session is built with ``defer_plugins=True`` and
            # is normally filled by the startup plugin warmup -- or, on
            # demand, by the first ``ensure_plugins_loaded`` / skill install.
            # Priming here (off the event loop, idempotent and cheap once
            # loaded) keeps the catalogue complete no matter when the client
            # asks for it.
            if not registry.list_all():
                from encre.agent import _get_shared_skill_registry
                await asyncio.to_thread(_get_shared_skill_registry)
            # Snapshot the mapping before iterating: the shared registry can
            # be concurrently primed by the server's plugin warmup in another
            # thread, and iterating a dict while it grows raises.
            for name, skill in list(registry._skills.items()):
                # The registry holds every source, but the panel lists only
                # what the user installed: bundled and managed skills come
                # with the build, hidden ones are deliberately invisible, and
                # auto-generated artifacts belong to the evolution subsystem.
                if getattr(skill, "hidden", False):
                    continue
                if skill.source in ("bundled", "managed"):
                    continue
                if _is_auto_generated_skill(skill):
                    continue
                entry: dict[str, Any] = {
                    "name": name,
                    "description": skill.description,
                    "aliases": skill.aliases,
                    "source": skill.source,
                    "argument_hint": skill.argument_hint,
                    "allowed_tools": skill.allowed_tools,
                    "when_to_use": skill.when_to_use,
                    "context": str(skill.context) if hasattr(skill, "context") else "inline",
                    "model": skill.model,
                    "disable_model_invocation": skill.disable_model_invocation,
                    "user_invocable": skill.user_invocable,
                    "license": getattr(skill, "license", ""),
                    "compatibility": getattr(skill, "compatibility", ""),
                    "metadata": getattr(skill, "metadata", {}),
                }
                if skill.body:
                    entry["body"] = skill.body
                else:
                    try:
                        entry["body"] = await skill.get_prompt_for_command(None, {})
                    except Exception:
                        entry["body"] = ""
                results.append(entry)
        except Exception as e:
            logger.error(f"_build_skills_list failed: {e}")
        return results

    # ---- Zip helpers -----------------------------------------------------

    @staticmethod
    def _looks_like_base64_zip(content: str) -> bool:
        """Decide whether *content* is a base64-encoded zip rather than markdown.

        Args:
            content: The skill body supplied by the client.

        Returns:
            ``True`` only when the text is a single line of base64 characters
            that decodes to a file starting with the zip magic bytes.

        Note:
            This is a deliberately conservative heuristic. An install payload
            is ambiguous on the wire -- the same field carries either the
            ``SKILL.md`` source or a base64 archive -- so the checks are
            stacked: multi-line text, frontmatter and anything outside the
            base64 alphabet are all treated as markdown. The final check on
            the ``PK\\x03\\x04`` signature is what makes a base64-looking
            string of something else fall through to the text path.
        """
        stripped = content.strip()
        if not stripped:
            return False
        if "\n" in stripped:
            return False
        if stripped.startswith("---"):
            return False
        # Only the base64 alphabet may appear; anything else means this is
        # prose or markdown, not an encoded archive.
        if not re.match(r"^[A-Za-z0-9+/=]+$", stripped):
            return False
        try:
            decoded = base64.b64decode(stripped)
            # The local-file-header signature is what actually confirms this
            # is a zip and not arbitrary base64 text.
            return decoded[:4] == b"PK\x03\x04"
        except Exception:
            return False

    @staticmethod
    def _install_skill_from_zip_data(content: str, skill_dir: Any) -> None:
        """Install a skill from a base64-encoded zip carried in the message.

        Args:
            content: The base64 archive body.
            skill_dir: The destination skill directory.

        Returns:
            None.

        Raises:
            ValueError: When the archive contains no ``SKILL.md``.

        Note:
            The archive is unpacked into a temporary directory that is always
            removed on exit, even when the copy fails. Extracting to a temp
            dir first rather than straight into the destination keeps a bad
            package from partially overwriting an existing skill.
        """
        decoded = base64.b64decode(content.strip())
        tmpdir = tempfile.mkdtemp(prefix="yim_skill_")
        try:
            with zipfile.ZipFile(zipfile.BytesIO(decoded), "r") as zf:
                zf.extractall(tmpdir)

            src = _find_skill_root(tmpdir)
            if src is None:
                raise ValueError("No SKILL.md found in zip package")
            _copy_skill_tree(src, str(skill_dir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _install_skill_from_zip_file(zip_path: str, skill_dir: Any) -> None:
        """Install a skill from a zip archive on disk.

        Args:
            zip_path: The archive to unpack.
            skill_dir: The destination skill directory.

        Returns:
            None.

        Raises:
            ValueError: When the archive contains no ``SKILL.md``.

        Note:
            Same temp-directory dance as the base64 path: unpack first, then
            copy, and always remove the temp dir so a failed install leaves no
            litter and never half-writes the destination.
        """
        tmpdir = tempfile.mkdtemp(prefix="yim_skill_")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir)

            src = _find_skill_root(tmpdir)
            if src is None:
                raise ValueError("No SKILL.md found in zip package")
            _copy_skill_tree(src, str(skill_dir))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- Client message handlers -----------------------------------------

    async def _h_update_skills(self, ws: Any, msg: ClientUpdateSkills) -> None:
        """Replace the set of enabled skills.

        Args:
            ws: The connection to answer.
            msg: ``ClientUpdateSkills`` with the full ``enabled_skills`` list.

        Returns:
            None. Sends ``skills_updated`` echoing the enabled list together
            with the refreshed catalogue.

        Note:
            The enabled set is a whole-replace, not a delta, so the client is
            responsible for sending every skill it wants to keep on. It is
            persisted through the shared config writer so the selection
            survives a restart.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        info.agent.config.enabled_skills = list(msg.enabled_skills)
        self._persist_config(info)
        available = await self._build_skills_list(info)
        await self._send(ws, "skills_updated",
                enabled_skills=msg.enabled_skills, available_skills=available)

    async def _h_get_skill_detail(self, ws: Any, msg: ClientGetSkillDetail) -> None:
        """Reply with one skill's own description and ``SKILL.md`` body.

        The boot catalogue (``available_skills``) intentionally lists only what
        the user installed, so the plugin market — which shows the skills a
        *bundled* package provides — asks for them by name instead.  The text
        returned is the skill's own, unedited: the market renders it and never
        writes a summary of its own.

        Handles :class:`encre.server.protocol.ClientGetSkillDetail`; replies
        ``skill_detail`` carrying ``name`` and either the fields or an
        ``error`` when the registry knows no such skill.
        """
        name = (msg.name or "").strip()
        try:
            if not name:
                raise ValueError("missing skill name")
            from encre.agent import _get_shared_skill_registry

            registry = await asyncio.to_thread(_get_shared_skill_registry)
            # Snapshot first: the shared registry can be re-scanned by the
            # plugin warmup in another thread while this reply is built.
            skill = _resolve_skill(registry, name)
            if skill is None:
                raise ValueError(f"skill '{name}' is not registered")
            body = getattr(skill, "body", "") or ""
            if not body.strip() and getattr(skill, "file_path", ""):
                from encre.skills.registry import _read_skill_source

                body = await asyncio.to_thread(_read_skill_source, skill.file_path)
            await self._send(
                ws,
                "skill_detail",
                name=name,
                description=getattr(skill, "description", ""),
                when_to_use=getattr(skill, "when_to_use", ""),
                argument_hint=getattr(skill, "argument_hint", ""),
                source=str(getattr(skill, "source", "") or ""),
                license=getattr(skill, "license", ""),
                compatibility=getattr(skill, "compatibility", ""),
                files=await asyncio.to_thread(_list_skill_files, skill),
                body=body,
            )
        except Exception as e:
            logger.error(f"[get_skill_detail] failed: {e}")
            await self._send(ws, "skill_detail", name=name, error=str(e))

    async def _h_get_skill_file(self, ws: Any, msg: ClientGetSkillFile) -> None:
        """Read one file out of a skill's own directory.

        ``SKILL.md`` is only the entry point: the documents it points at
        (``references/…``, templates, scripts) are the rest of the skill, and
        without them the reader shows one page of a package that is really a
        tree.  ``path`` is skill-relative.

        Args:
            ws: The connection to answer.
            msg: ``ClientGetSkillFile`` with the skill ``name`` and a
                skill-relative ``path``.

        Returns:
            None. Always sends ``skill_file`` carrying ``name``, ``path`` and
            ``content``, with an ``error`` instead of content when the skill is
            unknown, the path leaves its directory, the file is missing, too
            large, or not text.

        Note:
            Same confinement the memory reader uses: join, ``resolve``, then
            require the result to sit under the resolved skill directory — that
            is what stops ``..`` traversal in a client-supplied path.  Anything
            that is not obviously plaintext is decrypted best-effort, matching
            ``_read_skill_source`` so evolution-generated skills read back
            whole rather than as ciphertext.
        """
        name = (msg.name or "").strip()
        rel = (msg.path or "").strip().replace("\\", "/")
        try:
            if not name or not rel:
                raise ValueError("missing skill name or path")
            from encre.agent import _get_shared_skill_registry

            registry = await asyncio.to_thread(_get_shared_skill_registry)
            skill = _resolve_skill(registry, name)
            if skill is None:
                raise ValueError(f"skill '{name}' is not registered")
            root_s = getattr(skill, "file_path", "") or ""
            if not root_s:
                raise ValueError(f"skill '{name}' has no directory on disk")
            result = await asyncio.to_thread(_read_skill_file, Path(root_s).parent, rel)
            # ``result`` carries the normalized path on success, so the reply
            # tells the reader which file it actually got.
            await self._send(ws, "skill_file", **{"name": name, "path": rel, **result})
        except Exception as e:
            logger.error(f"[get_skill_file] failed: {e}")
            await self._send(ws, "skill_file", name=name, path=rel,
                             content="", error=str(e))

    async def _h_install_skill(self, ws: Any, msg: ClientInstallSkill) -> None:
        """Install or replace a skill *as a plugin* (unified pipeline).

        Every skill install becomes a declarative plugin package (manifest
        only, no code) under the user plugin root, so skills, MCP servers,
        sub-agents and full plugins all share one install / enable /
        uninstall pipeline. The package manifest declares
        ``contributes.skills`` pointing at the package's ``SKILL.md``.

        Args:
            ws: The connection to answer.
            msg: ``ClientInstallSkill`` with ``name``, ``content``, and an
                optional ``file_path`` pointing at a zip on the client's disk.

        Returns:
            None. Sends ``skill_installed`` with the refreshed catalogue, or
            ``skill_install_error`` on failure.

        Raises:
            Never.

        Note:
            The destination plugin package is deleted before writing, so
            installing a skill is always a replace. The three input shapes
            are tried in a fixed order (zip on disk, base64 zip inline,
            plain text) so a client that sends both a path and content gets
            the archive, which is the higher-fidelity form. The shared
            plugin registry is re-scanned after writing; the agent's plugin
            loader then registers the skill into the shared skill registry.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        from encre.agent import _get_shared_plugin_registry_raw
        from encre.config import get_data_dir
        from encre.protocol.handlers.base import safe_pkg_name

        registry = _get_shared_plugin_registry_raw()
        try:
            content = msg.content
            file_path = msg.file_path
            # Sanitise the package suffix so a hostile name cannot escape
            # the plugin root via path traversal.
            plugin_name = f"skill-{safe_pkg_name(msg.name)}"
            # Always install into a plugin package named after the skill.
            plugin_dir = get_data_dir("plugins", plugin_name)
            if plugin_dir.exists():
                shutil.rmtree(str(plugin_dir), ignore_errors=True)
            plugin_dir.mkdir(parents=True, exist_ok=True)

            if file_path and file_path.lower().endswith(".zip") and os.path.isfile(file_path):
                self._install_skill_from_zip_file(file_path, plugin_dir)
            elif self._looks_like_base64_zip(content):
                self._install_skill_from_zip_data(content, plugin_dir)
            else:
                (plugin_dir / "SKILL.md").write_text(content, encoding="utf-8")
            if not (plugin_dir / "SKILL.md").is_file():
                raise ValueError("skill package produced no SKILL.md")

            # Declarative manifest: static skill contribution, no plugin code.
            (plugin_dir / "encre-plugin.json").write_text(
                json.dumps({
                    "name": plugin_name,
                    "version": "1.0.0",
                    "description": f"Skill plugin: {msg.name}",
                    "tier": "user",
                    "contributes": {"skills": [{"name": msg.name, "path": "SKILL.md"}]},
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            registry.discover_all()
            registry.record_installed(plugin_name, "1.0.0", source="user",
                                      pkg_dir=str(plugin_dir))
            info.agent.reload_plugins()
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            available = await self._build_skills_list(info)
            await self._send(ws, "skill_installed", name=msg.name, available_skills=available)
        except Exception as e:
            logger.error(f"Skill install failed: {e}")
            await self._send(ws, "skill_install_error", name=msg.name, message=str(e))

    @staticmethod
    def _find_skill_plugin_dir(plugins_root: Any, skill_name: str) -> Any | None:
        """Locate a plugin package in *plugins_root* that provides *skill_name*.

        Matches either a package named ``skill-<skill_name>`` or any
        declarative manifest whose ``contributes.skills`` carries an entry
        with that name.  Returns the package directory, or ``None``.
        """
        if not os.path.isdir(str(plugins_root)):
            return None
        direct = plugins_root / f"skill-{skill_name}"
        if direct.is_dir():
            return direct
        for entry in sorted(os.listdir(str(plugins_root))):
            pkg = plugins_root / entry
            manifest_file = pkg / "encre-plugin.json"
            if not pkg.is_dir() or not manifest_file.is_file():
                continue
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            for s in ((data.get("contributes") or {}).get("skills") or []):
                s_name = s.get("name") if isinstance(s, dict) else str(s)
                if s_name == skill_name:
                    return pkg
        return None

    async def _h_uninstall_skill(self, ws: Any, msg: ClientUninstallSkill) -> None:
        """Remove a skill through the unified plugin pipeline.

        Args:
            ws: The connection to answer.
            msg: ``ClientUninstallSkill`` naming the skill to remove.

        Returns:
            None. Sends ``skill_uninstalled`` with the refreshed catalogue, or
            an ``error`` frame on failure.

        Raises:
            Never.

        Note:
            Every skill lives as a plugin package under the user plugin root
            and is removed through the shared plugin uninstall path
            (registry unregister + ledger drop + directory delete).  There
            is no legacy fallback: a skill that cannot be resolved to a
            plugin package is reported as an error.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        from encre.agent import _get_shared_plugin_registry_raw
        from encre.config import get_data_dir

        skill_name = msg.name
        registry = _get_shared_plugin_registry_raw()
        try:
            # Unified plugin pipeline: resolve the providing plugin package.
            plugins_root = get_data_dir("plugins")
            plugin_dir = self._find_skill_plugin_dir(plugins_root, skill_name)
            if plugin_dir is None or not plugin_dir.is_dir():
                await self._send(ws, "error",
                                 message=f"Skill '{skill_name}' is not installed as a plugin")
                return
            plugin_name = plugin_dir.name
            manifest_file = plugin_dir / "encre-plugin.json"
            try:
                plugin_name = str(json.loads(
                    manifest_file.read_text(encoding="utf-8-sig")).get("name")
                    or plugin_name)
            except Exception:
                pass
            registry.unregister(plugin_name)
            registry.record_uninstalled(plugin_name)
            shutil.rmtree(str(plugin_dir), ignore_errors=True)
            logger.info("[uninstall_skill] removed plugin package %s", plugin_dir)
            info.agent.reload_plugins()
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            available = await self._build_skills_list(info)
            await self._send(ws, "skill_uninstalled", name=skill_name,
                             available_skills=available)
        except Exception as e:
            logger.error(f"Skill uninstall failed: {e}")
            await self._send(ws, "error", message=f"Failed to uninstall skill: {e}")

    async def _h_update_skill(self, ws: Any, msg: ClientUpdateSkill) -> None:
        """Rewrite an existing skill's ``SKILL.md``.

        Args:
            ws: The connection to answer.
            msg: ``ClientUpdateSkill`` with the ``name`` and replacement
                ``content``.

        Returns:
            None. Sends ``skill_installed`` with the refreshed catalogue, or
            ``skill_install_error`` on failure.

        Raises:
            Never.

        Note:
            This overwrites only ``SKILL.md`` and leaves any other files the
            skill directory holds alone, so editing a skill's prompt does not
            destroy bundled assets. Re-installing is the path to replace the
            whole directory. The response type is shared with install because
            the outcome is the same for the client: the catalogue has changed.
            There is no legacy fallback: a skill that cannot be resolved to a
            plugin package is reported as an error.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        from encre.agent import _get_shared_plugin_registry_raw
        from encre.config import get_data_dir
        registry = _get_shared_plugin_registry_raw()
        try:
            # Skills installed as plugin packages live under the user plugin
            # root; rewrite the SKILL.md in place there.
            plugin_dir = self._find_skill_plugin_dir(
                get_data_dir("plugins"), msg.name)
            if plugin_dir is None or not (plugin_dir / "SKILL.md").is_file():
                await self._send(ws, "skill_install_error", name=msg.name,
                                 message=f"Skill '{msg.name}' is not installed as a plugin")
                return
            (plugin_dir / "SKILL.md").write_text(msg.content, encoding="utf-8")
            plugin_name = plugin_dir.name
            try:
                plugin_name = str(json.loads(
                    (plugin_dir / "encre-plugin.json").read_text(
                        encoding="utf-8-sig")).get("name") or plugin_name)
            except Exception:
                pass
            # Re-discover so the declarative plugin re-reads its SKILL.md,
            # then re-sync the live agent's injected skills.
            registry.reload_plugin(plugin_name)
            registry.discover_all()
            info.agent.reload_plugins()
            await self.broadcast_plugin_changed(registry.snapshot_states())
            await self.broadcast_plugin_ui_sync()
            available = await self._build_skills_list(info)
            await self._send(ws, "skill_installed", name=msg.name, available_skills=available)
        except Exception as e:
            await self._send(ws, "skill_install_error", name=msg.name, message=str(e))
