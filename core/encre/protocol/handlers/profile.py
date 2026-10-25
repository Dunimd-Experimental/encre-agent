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

"""Profile domain handlers: memory, rules, hooks, the knowledge library and usage stats.

Owned client-message domain for the settings-panel data planes: the memory
graph, global and project rules, project hooks, the knowledge library, the
user profile, and aggregated usage statistics. :class:`ProfileHandlers` is
mixed into :class:`encre.transport.ws.EncreWSHandler` and backs the
``ClientGetMemory*`` / ``Client*GlobalRule*`` / ``ClientListProject*`` /
``ClientGetProfile`` / ``ClientLibrary*`` entries of that handler's
``_DISPATCH`` table.

The structure is snapshot builder + thin handler. Every builder
(:meth:`_build_memory_list`, :meth:`_build_global_rules_list`,
:meth:`_build_project_rules_list`, :meth:`_build_project_hooks_list`,
:meth:`_build_library_tree`, :meth:`_build_usage_stats`) has two callers:
the on-demand response handler and the unified full-data snapshot push in
:class:`encre.protocol.handlers.base.HandlerBase`, which pushes config,
sessions, workspaces and models on connect. Sharing the builders is what keeps
a settings refresh and the connection-time snapshot from disagreeing about the
same memory entry, and it means a builder that gains a field automatically
appears in both.

Backends these handlers read from:

- :class:`encre.memdir.layers.MemoryLayerStore` -- the single durable memory
  store; the settings panel lists the graph rather than scanning files again.
- ``<data_dir>/rules/*.md`` -- global rules, encrypted at rest like soul and
  memory.
- the workspace directory -- project rules from the convention files of other
  assistants (cursor, windsurf, cline, claude, copilot, codex) which Encre
  reads at a declared priority.
- :class:`encre.capabilities.document_manager.EncreLibraryManager`
  for the knowledge-library tree.
- :class:`encre.telemetry.EncreTelemetry` for usage stats.

Routing into :class:`encre.server.session_manager.SessionManager` is indirect:
the builders read config from ``self._default_config`` and workspace state from
``self._workspace_path`` / ``self._info``, which the manager-mediated handlers
set. These handlers do not create or mutate sessions.

Public API:

- :class:`ProfileHandlers`
"""

import asyncio
import contextlib
import logging
import os
import threading
from typing import Any

from encre.server.protocol import (
    ClientDeleteGlobalRule,
    ClientGetGlobalRuleContent,
    ClientGetMemoryDetail,
    ClientGetMemoryGraph,
    ClientGetMemoryList,
    ClientGetProfile,
    ClientLibraryAddClip,
    ClientLibraryAddFile,
    ClientLibraryCreateNode,
    ClientLibraryDeleteNode,
    ClientLibraryGetContent,
    ClientLibraryList,
    ClientLibraryMoveNode,
    ClientLibraryRenameNode,
    ClientLibrarySaveContent,
    ClientListGlobalRules,
    ClientListProjectHooks,
    ClientListProjectRules,
    ClientSaveGlobalRule,
)

logger = logging.getLogger("encre.transport.ws")


class ProfileHandlers:
    """Settings-panel data planes and their shared snapshot builders.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`; holds no state of
    its own and reads ``_default_config``, ``_workspace_path``, ``_info`` and
    ``_send`` from the composed handler.

    Lifecycle: stateless, constructed once per server.

    Invariants:

    - Every response builder is also the snapshot builder, so the on-demand
      response and the connection-time snapshot can never disagree about the
      same entry.
    - Builders are pure reads. Write handlers (:meth:`_h_save_global_rule`,
      :meth:`_h_delete_global_rule`, :meth:`_h_library_create_node`,
      :meth:`_h_library_delete_node`) rebuild the affected list through the same
      builder and push it, so the settings panel refreshes without a manual
      reload.
    - Only long-term memory nodes are listed. Working snapshots, archived
      nodes and superseded nodes are excluded from the UI on purpose.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the composed
        handler.
    """

    @staticmethod
    def _parse_memory_frontmatter(content: str) -> dict[str, Any] | None:
        """Parse the YAML-like frontmatter block of a memory markdown file.

        Memory files carry metadata as a ``---`` fenced block before the body.
        Only the small subset of YAML that this project writes is understood:
        scalar ``key: value`` pairs and simple ``- item`` lists under a key.

        Args:
            content: The full file text.

        Returns:
            The parsed fields, or ``None`` when there is no frontmatter block.

        Note:
            This is a deliberately tiny hand-rolled parser rather than a YAML
            import, because memory files are an internal format that Encre
            writes itself and adding a parser dependency for it is not worth
            the cost. Quotes around scalar values are stripped, and a
            comment-only line is ignored. List values are only emitted when the
            key has at least one item, so an empty list is omitted rather than
            reported as ``[]``.
        """
        import re
        pattern = r"^---\s*\n(.*?)\n---"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            return None
        yaml_block = match.group(1)
        result: dict[str, Any] = {}
        current_key: str | None = None
        current_list: list[str] = []
        for line in yaml_block.split("\n"):
            stripped = line.rstrip()
            if not stripped or stripped.startswith("#"):
                continue
            list_match = re.match(r"^\s+-\s+(.+)$", stripped)
            if list_match and current_key:
                current_list.append(list_match.group(1).strip().strip("\"'"))
                continue
            if current_key is not None and current_list:
                result[current_key] = current_list
                current_list = []
                current_key = None
            kv_match = re.match(r"^(\w[\w_-]*)\s*:\s*(.*)$", stripped)
            if kv_match:
                key = kv_match.group(1)
                value = kv_match.group(2).strip()
                if not value:
                    current_key = key
                    current_list = []
                else:
                    val = value.strip("\"'")
                    result[key] = val
        if current_key is not None and current_list:
            result[current_key] = current_list
        return result

    def _build_global_rules_list(self) -> list[dict[str, Any]]:
        """List every global rule as a settings-panel row.

        Scans ``<data_dir>/rules/*.md`` and reports the newest first.

        Returns:
            One dict per rule with ``name`` (the file stem), ``path`` relative
            to the rules directory, ``size`` and ``modified`` timestamps.

        Note:
            Shared by ``global_rules_list`` responses and by the full-data
            snapshot push. Unreadable files are skipped rather than failing the
            whole listing.
        """
        from encre.config import get_data_dir
        rules_dir = get_data_dir() / "rules"
        rules_list: list[dict[str, Any]] = []
        if rules_dir.is_dir():
            for fpath in sorted(rules_dir.glob("*.md"), key=lambda p:
                p.stat().st_mtime, reverse=True):
                try:
                    rules_list.append({
                        "name": fpath.stem,
                        "path": str(fpath.relative_to(rules_dir)),
                        "size": fpath.stat().st_size,
                        "modified": fpath.stat().st_mtime,
                    })
                except Exception:
                    continue
        return rules_list

    def _visible_memory_nodes(self, store: Any) -> list[Any]:
        """Return the memory nodes visible to the settings panel.

        Resolved through the store's canonical visibility rule
        (``visible_entries``): long-term plus buffered nodes, archived nodes
        excluded, and tombstoned nodes hidden only while their replacement
        lineage is alive.  A dangling ``superseded_by`` pointer -- successor
        missing or its chain dead-ending -- is healed, so an interrupted
        arbitration can no longer make panel content silently vanish.
        Working snapshots stay excluded everywhere (transient scratch).

        Args:
            store: A loaded :class:`encre.memdir.layers.MemoryLayerStore`.

        Returns:
            The visible nodes, in store order.
        """
        try:
            visible = list(store.visible_entries())
        except AttributeError:
            # Older store without the canonical rule: keep the legacy filter.
            entries: list[Any] = list(store.long_term())
            with contextlib.suppress(Exception):
                entries.extend(store.buffer())
            visible = [
                e for e in entries
                if not e.decay.archived and not e.superseded_by
            ]
        return [e for e in visible if e.memory_type.value != "working"]

    def _build_memory_list(self, store: Any = None) -> list[dict[str, Any]]:
        """List the memory graph nodes visible in the settings panel.

        The memory graph (:class:`encre.memdir.layers.MemoryLayerStore`) is the
        single durable store. Judged candidates and ingested ``.md`` files both
        live there as typed nodes, so the UI lists the graph -- including each
        node's kind and confidence -- instead of scanning files a second time
        and showing a different set of entries than the store holds.

        Args:
            store: An already-loaded :class:`encre.memdir.layers.MemoryLayerStore`
                to read from.  ``None`` (the default) loads a fresh store, which
                keeps the on-demand handler behaviour unchanged; the unified
                snapshot push passes one shared store so the list and graph
                planes never load the memory graph twice per push.

        Returns:
            Visible nodes, most recently touched first. Each row carries the
            display name, path, size, modified time, a one-line preview, title,
            tags, node type, confidence, importance and use count.

        Note:
            Invisible nodes are filtered out on purpose: the working snapshot is
            transient, archived nodes are forgotten, and superseded nodes lost
            a contradiction arbitration -- their replacement is what should be
            listed. An unreadable store degrades to an empty list, since a
            settings panel is more useful than an error frame.
        """
        entries: list[dict[str, Any]] = []
        if store is None:
            try:
                from encre.memdir.layers import MemoryLayerStore
                store = MemoryLayerStore()
            except Exception:
                return entries
        for entry in self._visible_memory_nodes(store):
            source = str(entry.source or "")
            name = (
                source[3:].removesuffix(".md")
                if source.startswith("md:") else entry.id[:12]
            )
            meta = entry.metadata or {}
            modified = float(entry.decay.last_access or entry.created_at)
            entries.append({
                "name": name,
                "path": str(meta.get("path", "") or ""),
                "size": len(entry.content.encode("utf-8")),
                "modified": modified,
                "preview": entry.content[:200].replace("\n", " ").strip(),
                "title": str(meta.get("description", "") or ""),
                "tags": [str(t) for t in (meta.get("tags") or [])]
                    if isinstance(meta.get("tags"), list | tuple) else [],
                "type": entry.type,
                "confidence": round(float(entry.confidence), 4),
                "importance": round(float(entry.importance), 4),
                "uses": int(entry.decay.access_count),
                "superseded": False,
            })
        entries.sort(key=lambda e: e["modified"], reverse=True)
        return entries

    def _build_memory_graph(self, store: Any = None) -> dict[str, Any]:
        """Build the semantic memory graph for the settings relationship view.

        Args:
            store: An already-loaded memory store (see
                :meth:`_build_memory_list`); ``None`` loads a fresh one.

        Returns:
            ``{"nodes": [...], "edges": [...]}``. Nodes carry the id, type,
            title, preview, confidence, use count and the evolution flags the
            graph control needs (weakened / skill_candidate); edges are the
            store's typed links (mentions / co_occurs / contradicts / ...)
            restricted to visible endpoints.

        Note:
            Same visibility rules as the list: the working snapshot, archived
            and superseded nodes are excluded, and an unreadable store
            degrades to an empty graph rather than an error frame.
        """
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        if store is None:
            try:
                from encre.memdir.layers import MemoryLayerStore
                store = MemoryLayerStore()
            except Exception:
                return {"nodes": nodes, "edges": edges}
        entries = self._visible_memory_nodes(store)
        visible: dict[str, dict[str, Any]] = {}
        for entry in entries:
            meta = entry.metadata or {}
            source = str(entry.source or "")
            title = str(meta.get("description", "") or "")
            if not title and source.startswith("md:"):
                title = source[3:].removesuffix(".md")
            if not title:
                # Fall back to the first meaningful content line so every
                # node shows something human-readable instead of a raw id
                # (entity hubs carry their token as content, so they show
                # the subject word itself).
                for line in entry.content.splitlines():
                    stripped = line.strip().lstrip("#-*> ").strip()
                    if stripped:
                        title = stripped[:20]
                        break
            if not title:
                title = entry.id[:8]
            nodes.append({
                "id": entry.id,
                "type": entry.type,
                "title": title,
                "preview": entry.content[:160].replace("\n", " ").strip(),
                "confidence": round(float(entry.confidence), 4),
                "uses": int(entry.decay.access_count),
                "updated": float(entry.decay.last_access or entry.created_at),
                "weakened": float(entry.confidence) < 0.45,
                "skill_candidate": bool(meta.get("skill_candidate")),
            })
            visible[entry.id] = nodes[-1]
        id_set = set(visible)
        for entry in entries:
            if entry.id not in id_set:
                continue
            for link in entry.links or []:
                target = str(link.target_id)
                if target not in id_set:
                    continue
                edges.append({
                    "from": entry.id,
                    "to": target,
                    "relation": str(link.relation),
                    "weight": round(float(link.weight), 3),
                })
        return {"nodes": nodes, "edges": edges}

    def _build_library_tree(self) -> list[dict[str, Any]]:
        """List the whole knowledge-library tree.

        Returns:
            Every tree node known to the library manager, straight from the
            manifest.

        Note:
            Shared by ``library_tree`` responses and by the full-data
            snapshot push, so any tree change in one client is reflected in
            every other immediately.
        """
        from encre.capabilities.document_manager import EncreLibraryManager

        from encre.config import get_data_dir
        mgr = EncreLibraryManager(str(get_data_dir()))
        return mgr.nodes()

    def _library_manager(self) -> Any:
        """Build a fresh :class:`EncreLibraryManager` for the data dir."""
        from encre.capabilities.document_manager import EncreLibraryManager

        from encre.config import get_data_dir
        return EncreLibraryManager(str(get_data_dir()))

    # Usage-stats cache: the telemetry aggregate re-reads every
    # ``data/telemetry/*.jsonl`` on each call, which used to run once per
    # full-data push.  The cache is keyed on the newest file mtime under the
    # telemetry directory, so a finished run (which appends to a jsonl)
    # invalidates it immediately while repeated pushes within the same
    # activity window reuse one aggregation.  Guarded by a lock because
    # parallel plane pushes now run the builder concurrently.
    _usage_stats_cache: dict[str, Any] | None = None
    _usage_stats_cache_key: tuple | None = None
    _usage_stats_lock = threading.Lock()

    @staticmethod
    def _telemetry_fingerprint() -> tuple:
        """Fingerprint the telemetry inputs cheaply (dir mtimes only).

        Returns:
            A ``(max_jsonl_mtime, cumulative_mtime)`` tuple, or ``()`` when
            the telemetry directory does not exist.  Reading one mtime per
            file costs orders of magnitude less than re-parsing the files.
        """
        from encre.config import get_data_dir
        telemetry_dir = get_data_dir() / "telemetry"
        if not telemetry_dir.is_dir():
            return ()
        newest = 0.0
        try:
            for fpath in telemetry_dir.glob("*.jsonl"):
                try:
                    newest = max(newest, fpath.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            return ()
        cumulative = 0.0
        try:
            cpath = get_data_dir() / "telemetry_cumulative.json"
            if cpath.is_file():
                cumulative = cpath.stat().st_mtime
        except OSError:
            pass
        return (newest, cumulative)

    def _build_usage_stats(self) -> dict[str, Any]:
        """Aggregate usage statistics, resolving raw model ids to display names.

        Telemetry records what the model actually was at run time as a raw
        model id. Users see friendlier names in config, and they also delete
        and rename models, so this method reconciles the three states the
        settings panel needs to distinguish:

        - the model is still configured -> show its configured display name
          with ``model_status="active"``;
        - it is unknown entirely -> ``(unknown model)`` with status ``unknown``;
        - it was configured before but is gone now -> keep the raw id so the
          historical record is preserved, marked ``model_status="deleted"``.

        Returns:
            The telemetry aggregate with ``sessions`` and ``model_breakdown``
            rewritten to display names, or a fully zeroed structure when
            telemetry is unavailable.

        Note:
            ``model_breakdown`` keys are merged by display name, so two model
            ids that shared a configured name fold into one row with summed
            tokens and turns. On any failure the return value is a complete
            zeroed shape rather than ``{}``, because the usage panel expects
            every field to be present.
        """
        # Cache check: a matching fingerprint returns the previously
        # aggregated (and name-resolved) stats without touching the jsonl
        # files.  The configured display-name mapping is part of the key so
        # renaming a model invalidates immediately instead of waiting for
        # the next telemetry write.  The lock only guards the check-and-store
        # pair; a worst case race re-computes the same aggregate, which is
        # harmless.
        model_names: dict[str, str] = {}
        if self._default_config:
            for mc in self._default_config.models:
                mid = (mc.model_id or "").strip()
                if mid and mc.name:
                    model_names[mid] = mc.name
        fingerprint = (
            self._telemetry_fingerprint(),
            tuple(sorted(model_names.items())),
        )
        with self._usage_stats_lock:
            if (
                self._usage_stats_cache is not None
                and self._usage_stats_cache_key == fingerprint
            ):
                return self._usage_stats_cache
        try:
            from encre.telemetry import EncreTelemetry
            stats = EncreTelemetry.get_all_sessions_usage()
            if stats.get("sessions"):
                for s in stats["sessions"]:
                    raw = (s.get("model", "") or "").strip()
                    if not raw or raw == "unknown":
                        s["model"] = "(unknown model)"
                        s["model_status"] = "unknown"
                    elif raw in model_names:
                        s["model"] = model_names[raw]
                        s["model_status"] = "active"
                    else:
                        # Model is no longer in the user's config: keep the
                        # raw id so the historical record is preserved.
                        s["model"] = raw
                        s["model_status"] = "deleted"
            if stats.get("model_breakdown"):
                mb: dict[str, dict[str, Any]] = {}
                for raw, data in stats["model_breakdown"].items():
                    if not raw or raw == "unknown":
                        display = "(unknown model)"
                    elif raw in model_names:
                        display = model_names[raw]
                    else:
                        display = raw
                    if display in mb:
                        for k in ("input_tokens", "output_tokens", "total_tokens", "turns"):
                            mb[display][k] = mb[display].get(k, 0) + data.get(k, 0)
                    else:
                        mb[display] = dict(data)
                stats["model_breakdown"] = mb
            with self._usage_stats_lock:
                self._usage_stats_cache = stats
                self._usage_stats_cache_key = fingerprint
            return stats
        except Exception:
            return {
                "total_sessions": 0, "total_tokens": 0,
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_tool_calls": 0,
                "tool_call_breakdown": {},
                "model_breakdown": {},
                "sessions": [],
            }

    def _build_project_rules_list(self) -> list[dict[str, Any]]:
        """List the project rule files found in the active workspace.

        Encre reads the convention files of other assistants so a project does
        not have to duplicate its instructions. Each source is declared with a
        priority: Encre's own ``.encre/rules.md`` wins, then cursor, windsurf,
        cline, claude and copilot in decreasing order. Codex-style
        instructions (the ``AGENTS.md`` chain plus
        ``.codex/config.toml`` developer instructions) are appended separately
        at a fixed priority.

        Returns:
            Rows with ``name``, ``path`` (relative to the workspace for the
            convention files, made workspace-relative for Codex paths),
            ``priority`` and ``modified``, sorted by priority descending.
            Empty when no workspace is open or it does not exist.

        Note:
            The workspace is taken from ``self._workspace_path`` when a
            workspace is registered, otherwise from the default config's
            ``workspace``. Codex paths are converted to be relative to the
            workspace because the panel renders them as clickable paths.
        """
        ws_path = (self._workspace_path or self._default_config.workspace) if self._default_config else ""
        rules_list: list[dict[str, Any]] = []
        if ws_path and os.path.isdir(ws_path):
            for rel_path, priority, name in [
                (".encre/rules.md", 100, "encre"),
                (".cursorrules", 90, "cursor"),
                (".windsurfrules", 85, "windsurf"),
                (".clinerules", 80, "cline"),
                ("CLAUDE.md", 75, "claude"),
                (".github/copilot-instructions.md", 60, "copilot"),
            ]:
                full_path = os.path.join(ws_path, rel_path)
                if os.path.isfile(full_path):
                    try:
                        st = os.stat(full_path)
                        rules_list.append({
                            "name": name,
                            "path": rel_path,
                            "priority": priority,
                            "modified": st.st_mtime,
                        })
                    except Exception:
                        continue
            # Codex instructions: AGENTS.md chain, .codex/config.toml
            # developer_instructions, and model_instructions_file.
            try:
                from encre.codex_compat import build_codex_context
                ctx = build_codex_context(ws_path)
                seen_codex: set[str] = set()
                for path, _ in ctx.instructions:
                    if path in seen_codex:
                        continue
                    seen_codex.add(path)
                    rel = path
                    if rel.startswith(ws_path + os.sep):
                        rel = rel[len(ws_path) + len(os.sep):]
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        mtime = 0.0
                    rules_list.append({
                        "name": "codex",
                        "path": rel,
                        "priority": 65,
                        "modified": mtime,
                    })
            except Exception:
                pass
            rules_list.sort(key=lambda r: -r["priority"])
        return rules_list

    def _build_project_hooks_list(self) -> list[dict[str, Any]]:
        """List the hooks attached to the connection's current agent.

        Hooks are read from the hook system owned by the session's agent, so
        this reflects exactly what the loop would actually fire -- not the
        files on disk, which may not all be enabled for this agent.

        Returns:
            One row per registered handler with its id, event type, source
            file, matcher, command, hook type and timeout in milliseconds.
            Empty when there is no session or it has no hook system yet.

        Note:
            ``self._info`` may be ``None`` before the first session is created,
            hence the chained ``getattr`` guards rather than direct attribute
            access.
        """
        from encre.hooks import EncreHookSystem
        info = self._info
        hook_system: EncreHookSystem | None = (
            getattr(getattr(info, "agent", None), "hook_system", None)
        )
        hooks_list: list[dict[str, Any]] = []
        if hook_system is not None:
            for h in hook_system.list_handlers():
                hooks_list.append({
                    "handler_id": h.get("handler_id", ""),
                    "event_type": h.get("event_type", ""),
                    "source_path": h.get("source_path", ""),
                    "matcher": h.get("matcher", ""),
                    "command": h.get("command", ""),
                    "hook_type": h.get("hook_type", "command"),
                    "timeout_ms": int(h.get("timeout_ms", 0) or 0),
                })
        return hooks_list

    # ---- Client message handlers -----------------------------------------
    # Thin responders over the builders above. Each one names the response
    # frame the desktop panel subscribes to.

    async def _h_get_memory_list(self, ws: Any, msg: ClientGetMemoryList) -> None:
        """Reply with every visible memory node.

        Args:
            ws: The connection to answer.
            msg: ``ClientGetMemoryList``; carries no payload.

        Returns:
            None. Sends ``memory_list``.
        """
        await self._send(ws, "memory_list", entries=self._build_memory_list())

    async def _h_get_memory_graph(self, ws: Any, _msg: ClientGetMemoryGraph) -> None:
        """Reply with the whole memory graph (nodes + typed edges).

        Args:
            ws: The connection to answer.
            _msg: ``ClientGetMemoryGraph``; carries no payload.

        Returns:
            None. Sends ``memory_graph`` with ``{"nodes", "edges"}``.
        """
        await self._send(ws, "memory_graph", graph=self._build_memory_graph())

    async def _h_get_memory_detail(self, ws: Any, msg: ClientGetMemoryDetail) -> None:
        """Return the decrypted content of one memory file.

        Args:
            ws: The connection to answer.
            msg: ``ClientGetMemoryDetail`` with the memory-relative ``path``.

        Returns:
            None. Always sends ``memory_detail``, with an ``error`` field when
            the path escapes the memory directory or does not exist.

        Raises:
            Never.

        Note:
            The path is joined to the memory root and then ``resolve``d before
            being compared against the resolved root. That check is what stops
            a ``..`` traversal in the client-supplied path from reading files
            elsewhere on disk. Plaintext files are returned as-is; anything
            that is not obviously frontmatter-plaintext is decrypted best-effort,
            and a decrypt failure degrades to empty content rather than an
            error frame.
        """
        from encre.config import get_data_dir
        mem_dir = get_data_dir() / "memory"
        # ``node:<id>`` paths fetch the detail-layer full text of one memory
        # graph node (used by the settings relationship view), bypassing the
        # file-path resolution below.
        if msg.path.startswith("node:"):
            node_id = msg.path[len("node:"):]
            content = ""
            try:
                from encre.memdir.layers import MemoryLayerStore
                store = MemoryLayerStore()
                entry = store.get(node_id)
                if entry is not None:
                    content = store.load_detail(entry) or entry.content
            except Exception:
                content = ""
            await self._send(ws, "memory_detail", path=msg.path, content=content)
            return
        file_path = mem_dir / msg.path
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(mem_dir.resolve())) or not file_path.is_file():
            await self._send(ws, "memory_detail", path=msg.path, content="", error="File not found or access denied")
        else:
            try:
                raw = file_path.read_text("utf-8")
                content = raw
                from encre.crypto import decrypt
                if not raw.startswith("---"):
                    with contextlib.suppress(Exception):
                        content = decrypt(raw)
            except Exception:
                content = ""
            await self._send(ws, "memory_detail", path=msg.path, content=content)

    async def _h_list_global_rules(self, ws: Any, msg: ClientListGlobalRules) -> None:
        """Reply with every global rule.

        Args:
            ws: The connection to answer.
            msg: ``ClientListGlobalRules``; carries no payload.

        Returns:
            None. Sends ``global_rules_list``.
        """
        await self._send(ws, "global_rules_list", rules=self._build_global_rules_list())

    async def _h_list_project_rules(self, ws: Any, msg: ClientListProjectRules) -> None:
        """Reply with the project rules for the active workspace.

        Args:
            ws: The connection to answer.
            msg: ``ClientListProjectRules``; carries no payload.

        Returns:
            None. Sends ``project_rules_list``.
        """
        await self._send(ws, "project_rules_list", rules=self._build_project_rules_list())

    async def _h_list_project_hooks(self, ws: Any, msg: ClientListProjectHooks) -> None:
        """Reply with the hooks registered on the current agent.

        Args:
            ws: The connection to answer.
            msg: ``ClientListProjectHooks``; carries no payload.

        Returns:
            None. Sends ``project_hooks_list``.
        """
        await self._send(ws, "project_hooks_list", hooks=self._build_project_hooks_list())

    async def _h_save_global_rule(self, ws: Any, msg: ClientSaveGlobalRule) -> None:
        """Write a global rule to disk encrypted, then push the refreshed list.

        Args:
            ws: The connection to answer.
            msg: ``ClientSaveGlobalRule`` with the rule ``name`` and ``content``.

        Returns:
            None. Sends ``global_rule_saved`` followed by the refreshed
            ``global_rules_list``, or an ``error`` frame on write failure.

        Note:
            Rules are encrypted at rest for the same reason the soul and memory
            directories are: they contain user-authored instructions that are
            read into prompts and may include secrets the user pasted in.
            Re-pushing the whole list from the shared builder means the panel
            never holds a stale view after an edit.
        """
        from encre.config import get_data_dir
        from encre.crypto import encrypt
        rules_dir = get_data_dir() / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        rule_path = rules_dir / f"{msg.name}.md"
        try:
            rule_path.write_text(encrypt(msg.content), encoding="utf-8")
            await self._send(ws, "global_rule_saved", name=msg.name)
            # Rebuild the list from the shared builder so this write is
            # reflected immediately rather than on the next snapshot.
            await self._send(ws, "global_rules_list", rules=self._build_global_rules_list())
        except Exception as e:
            await self._send(ws, "error", message=f"Failed to save global rule: {e}")

    async def _h_delete_global_rule(self, ws: Any, msg: ClientDeleteGlobalRule) -> None:
        """Remove a global rule file and push the refreshed list.

        Args:
            ws: The connection to answer.
            msg: ``ClientDeleteGlobalRule`` naming the rule to remove.

        Returns:
            None. Sends ``global_rule_deleted`` then the refreshed
            ``global_rules_list``, or an ``error`` frame on failure.

        Note:
            Deleting a missing name is not an error -- the file may have
            already been removed on another window -- and the list is still
            re-pushed so every panel converges. Pushing the list mirrors the
            save path, otherwise the deleted rule would stay visible until the
            next full snapshot.
        """
        from encre.config import get_data_dir
        rules_dir = get_data_dir() / "rules"
        rule_path = rules_dir / f"{msg.name}.md"
        try:
            if rule_path.is_file():
                rule_path.unlink()
            await self._send(ws, "global_rule_deleted", name=msg.name)
            # Push the full list so every settings window refreshes without a
            # manual reload, matching the save path.
            await self._send(ws, "global_rules_list", rules=self._build_global_rules_list())
        except Exception as e:
            await self._send(ws, "error", message=f"Failed to delete global rule: {e}")

    async def _h_get_global_rule_content(self, ws: Any, msg: ClientGetGlobalRuleContent) -> None:
        """Return the decrypted content of one global rule.

        Args:
            ws: The connection to answer.
            msg: ``ClientGetGlobalRuleContent`` naming the rule.

        Returns:
            None. Always sends ``global_rule_content``, with an ``error``
            field when the rule does not exist or cannot be read.

        Note:
            The traversal guard is the same as for memory detail: resolve the
            joined path and require it to still live under the rules root.
            Decryption goes through the shared rule reader rather than being
            duplicated here, so the prompt loader and this endpoint can never
            disagree about what a rule file contains.
        """
        from encre.config import get_data_dir
        rules_dir = get_data_dir() / "rules"
        rule_path = (rules_dir / f"{msg.name}.md").resolve()
        if not str(rule_path).startswith(str(rules_dir.resolve())) or not rule_path.is_file():
            await self._send(ws, "global_rule_content", name=msg.name, content="", error="File not found")
        else:
            try:
                # Single reader shared with the prompt loader, so at-rest
                # decryption stays in one place.
                from encre.rules.loader import read_global_rule_text
                content = read_global_rule_text(str(rule_path))
                await self._send(ws, "global_rule_content", name=msg.name, content=content)
            except Exception as e:
                await self._send(ws, "global_rule_content", name=msg.name, content="", error=str(e))

    def _build_profile_data(self) -> dict[str, Any]:
        """Return the current user profile as a payload dict.

        Shared by the ``get_profile`` handler and by the file-backed plane
        pushers (``memory_graph`` / ``profile_data`` frames), so the profile
        is never rebuilt twice with subtly different code.  The profile
        system is instantiated against the memory directory and loaded on
        demand rather than kept in server state, so an edit made through the
        memory panel is always reflected.  An unreadable profile degrades to
        an empty dict, since a settings panel is more useful than an error
        frame.
        """
        try:
            from encre.config import get_data_dir
            from encre.profile.system import EncreProfileSystem
            mem_dir = str(get_data_dir() / "memory")
            ps = EncreProfileSystem(mem_dir)
            ps.load()
            data = ps.get_data()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _h_get_profile(self, ws: Any, msg: ClientGetProfile) -> None:
        """Return the current user profile.

        Args:
            ws: The connection to answer.
            msg: ``ClientGetProfile``; carries no payload.

        Returns:
            None. Sends ``profile_data``.
        """
        await self._send(ws, "profile_data", profile=self._build_profile_data())

    async def _h_library_list(self, ws: Any, msg: ClientLibraryList) -> None:
        """Reply with the whole knowledge-library tree.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryList``; carries no payload.

        Returns:
            None. Sends ``library_tree``, or ``library_error`` if the
            manifest cannot be read.

        Note:
            Failures are reported as ``library_error`` rather than silence
            because the library panel distinguishes "empty tree" from
            "tree unreadable".
        """
        try:
            await self._send(ws, "library_tree", nodes=self._build_library_tree())
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_create_node(self, ws: Any, msg: ClientLibraryCreateNode) -> None:
        """Create a structural or self-contained node in the library tree.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryCreateNode`` with ``parent_id``, ``node_type``
                (``space`` / ``folder`` / ``doc`` / ``sheet``) and ``name``.

        Returns:
            None. Sends ``library_tree`` on success, or ``library_error``.
        """
        try:
            mgr = self._library_manager()
            mgr.create_node(msg.parent_id, msg.node_type, msg.name)
            await self._send(ws, "library_tree", nodes=self._build_library_tree())
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_add_file(self, ws: Any, msg: ClientLibraryAddFile) -> None:
        """Import a local file into the library as a markdown/attachment leaf.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryAddFile`` with ``parent_id``, ``name`` and
                ``file_path``.

        Returns:
            None. Sends ``library_tree`` on success, or ``library_error``.
        """
        try:
            mgr = self._library_manager()
            mgr.add_file(msg.parent_id, msg.name, msg.file_path)
            await self._send(ws, "library_tree", nodes=self._build_library_tree())
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_add_clip(self, ws: Any, msg: ClientLibraryAddClip) -> None:
        """Add a web clip (URL) to the library.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryAddClip`` with ``parent_id``, ``name`` and
                ``url``.

        Returns:
            None. Sends ``library_tree`` with the pending leaf immediately
            and again when the background crawl finishes, or
            ``library_error``.

        Note:
            The crawl runs as a tracked background task (see
            :meth:`_crawl_clip`) because fetching may outlast the panel's
            patience; the panel sees the loading entry at once and the
            refreshed tree when the body lands.
        """
        try:
            mgr = self._library_manager()
            node = mgr.add_clip_pending(msg.parent_id, msg.name, msg.url)
            await self._send(ws, "library_tree", nodes=self._build_library_tree())
            _t = asyncio.ensure_future(self._crawl_clip(ws, mgr, node["id"], node["name"], msg.url))
            self._tasks.add(_t)
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_move_node(self, ws: Any, msg: ClientLibraryMoveNode) -> None:
        """Move a library node (with its subtree) under a new parent.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryMoveNode`` with ``id``, ``parent_id`` and the
                sibling ``index`` to insert at.

        Returns:
            None. Sends ``library_tree`` on success, or ``library_error``.
        """
        try:
            mgr = self._library_manager()
            mgr.move(msg.id, msg.parent_id, msg.index)
            await self._send(ws, "library_tree", nodes=self._build_library_tree())
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_rename_node(self, ws: Any, msg: ClientLibraryRenameNode) -> None:
        """Rename a library tree node.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryRenameNode`` with ``id`` and ``name``.

        Returns:
            None. Sends ``library_tree`` on success, or ``library_error``.
        """
        try:
            mgr = self._library_manager()
            mgr.rename(msg.id, msg.name)
            await self._send(ws, "library_tree", nodes=self._build_library_tree())
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_delete_node(self, ws: Any, msg: ClientLibraryDeleteNode) -> None:
        """Delete a library node together with its whole subtree.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryDeleteNode`` with the node ``id``.

        Returns:
            None. Sends ``library_tree`` on success, or ``library_error``
            when the id is unknown or the manager fails.
        """
        try:
            mgr = self._library_manager()
            removed = mgr.delete(msg.id)
            if removed:
                await self._send(ws, "library_tree", nodes=self._build_library_tree())
            else:
                await self._send(ws, "library_error", message="Node not found")
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_get_content(self, ws: Any, msg: ClientLibraryGetContent) -> None:
        """Read a leaf node's stored body.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibraryGetContent`` with the node ``id``.

        Returns:
            None. Sends ``library_content`` with the body text, or
            ``library_error``.
        """
        try:
            mgr = self._library_manager()
            content = mgr.read_content(msg.id)
            await self._send(ws, "library_content", id=msg.id, content=content)
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _h_library_save_content(self, ws: Any, msg: ClientLibrarySaveContent) -> None:
        """Persist edited content back to a leaf node.

        Args:
            ws: The connection to answer.
            msg: ``ClientLibrarySaveContent`` with the node ``id`` and the
                new ``content``.

        Returns:
            None. Sends ``library_tree`` (size/status changed) on success,
            or ``library_error``.
        """
        try:
            mgr = self._library_manager()
            mgr.write_content(msg.id, msg.content)
            await self._send(ws, "library_tree", nodes=self._build_library_tree())
        except Exception as e:
            await self._send(ws, "library_error", message=str(e))

    async def _crawl_clip(self, ws: Any, mgr: Any, node_id: str, name: str, url: str) -> None:
        """Background crawl of a URL into a pending web-clip leaf.

        Spawned by :meth:`_h_library_add_clip` and tracked in ``self._tasks``
        so the server can await or discard it on shutdown.

        Args:
            ws: The connection that requested the crawl; updates go back to it.
            mgr: The library manager owning the leaf being updated.
            node_id: The pending clip node's id.
            name: The clip's display name.
            url: The URL to fetch.

        Returns:
            None.

        Note:
            The fetch runs in the default thread-pool executor because it is
            blocking network I/O and must not stall the event loop. On
            failure the pending leaf is dropped from the manifest, otherwise
            the library panel would keep showing a clip that will never be
            usable. Any disconnect between the crawl finishing and the tree
            frame sending degrades to ``_send`` returning silently, which is
            acceptable here.
        """
        from encre.capabilities.document_manager import crawl_url_to_text
        try:
            loop = asyncio.get_event_loop()
            full_text = await loop.run_in_executor(None, crawl_url_to_text, name, url)
            updated = mgr.finish_clip(node_id, full_text)
            if updated:
                await self._send(ws, "library_tree", nodes=self._build_library_tree())
        except Exception as e:
            try:
                mgr.delete(node_id)
            except Exception:
                pass
            await self._send(ws, "library_error", message=f"Crawl failed: {e}")
