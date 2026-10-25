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

"""Encre session manager.

Session authority for the Python half of the Encre stack.

:class:`SessionManager` owns the mapping between client-visible session ids
and the agents that execute turns, and keeps that mapping in agreement with
what is on disk.  Every domain handler under :mod:`encre.protocol.handlers`,
every adapter in :mod:`encre.gateway.platforms`, the channel router, and
:class:`encre.server.app.EncreServer` route their lookups through this module,
so the id a client holds, the live :class:`~encre.agent.EncreAgent`, and the
persisted transcript cannot disagree about identity.

Three stores are bridged:

    * memory -- ``_sessions`` holds live :class:`SessionInfo` records, each
      wrapping an agent whose asyncio task may still be running.  This is
      what makes streaming output and mid-run approval prompts possible.
    * disk -- ``data/sessions/<uuid>/meta.json`` plus the JSONL transcript,
      written by :class:`~encre.session.EncreSession`.  The directory is
      selected per mode: the global one, one per iWork workspace
      (``data/iwork/<ws>/sessions``), or ``data/iclaw/sessions`` for the
      embedded in-CLI agent client.
    * index -- ``<sessions_dir>/index.json``, an encrypted summary table that
      lets the sidebar render in a single read instead of opening every
      session.  It is a derived cache only;
      :meth:`SessionManager._bootstrap_index_from_disk` can rebuild it from
      the real directory listing.

Direction of flow: a handler creates or resolves a session, the agent runs in
the Encre Agent Core over the PyO3 binding, a hook persists the user message, the
owning index entry is refreshed, and
:meth:`SessionManager.notify_session_completed` fans the change out to
subscribers so the renderer and the gateway adapters update.

Public API
----------
* Lifecycle: :meth:`SessionManager.create_session`,
  :meth:`SessionManager.load_or_create_session`,
  :meth:`SessionManager.remove_session`,
  :meth:`SessionManager.delete_session_from_disk`,
  :meth:`SessionManager.try_resume_most_recent`.
* Listing and metadata: :meth:`SessionManager.list_sessions`,
  :meth:`SessionManager.query_index`,
  :meth:`SessionManager.list_archived_sessions`,
  :meth:`SessionManager.rename_session`,
  :meth:`SessionManager.archive_session`,
  :meth:`SessionManager.unarchive_session`.
* Inspection: :meth:`SessionManager.get_session`,
  :meth:`SessionManager.get_session_state`,
  :meth:`SessionManager.set_session_state`,
  :meth:`SessionManager.is_session_running`,
  :meth:`SessionManager.iter_sessions`, :meth:`SessionManager.touch`,
  :attr:`SessionManager.active_count`.
* Capacity and teardown: :meth:`SessionManager.acquire_slot`,
  :meth:`SessionManager.release_slot`, :meth:`SessionManager.cleanup_idle`,
  :meth:`SessionManager.shutdown`.
* Storage switching: :meth:`SessionManager.set_workspace`,
  :meth:`SessionManager.set_iclaw`.
* Change notification: :meth:`SessionManager.on_sessions_changed`,
  :meth:`SessionManager.notify_session_completed`.
"""


import asyncio
import threading
import contextlib
import json
import logging
import os
import pathlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from encre.config import EncreConfig, get_data_dir
from encre.crypto import decrypt, encrypt
from encre.model_selection import resolve_model_index
from encre.session import EncreSession, _atomic_write_text
from encre.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from encre.agent import EncreAgent

logger = logging.getLogger("encre.server.session")


class SessionState(str, Enum):
    """Three-state lifecycle for a single agent session.

    Derived from :class:`str` so a state value serializes straight into the
    index JSON and across the WebSocket protocol without any conversion step.
    Callers compare ``info.state`` against these members; the string values are
    what the desktop renderer and :mod:`encre.gateway.session` consume on the
    wire.

    The stored state is a hint, not an authority:
    :meth:`SessionManager.get_session_state` reconciles it against the actual
    asyncio task, so a cancelled or crashed task can never leave a session
    reporting ``RUNNING`` forever.
    """

    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"


@dataclass
class SessionInfo:
    """In-memory record pairing one session id with its live agent.

    Invariants:

        * ``session_id`` is the client-visible identity and is never
          reassigned.  The on-disk directory name and the index key are always
          the same value, which is what makes "open this row" unambiguous.
        * ``agent`` is owned by this record.  Removing the record from
          ``SessionManager._sessions`` is what ends the agent's lifetime.
        * ``sessions_dir`` records which physical directory owns the session
          at creation or load time.  It is stored explicitly instead of being
          inferred at save time, because a session loaded from the flat iWork
          listing may belong to a different workspace than the one currently
          selected.

    :class:`SessionManager` is the only writer.  Readers should treat
    ``state``, ``last_active``, ``pending_runs`` and ``metadata`` as mutable
    bookkeeping that may be updated concurrently; snapshot operations take
    ``SessionManager._lock`` before iterating the records.

    Attributes:
        session_id: Client-visible id; doubles as the on-disk directory name.
        agent: Live :class:`~encre.agent.EncreAgent` for this session.
        agent_task: Asyncio task driving the agent loop, or ``None`` while
            the session is idle.  Cancelled explicitly on delete and shutdown
            so a terminated session cannot keep appending to its transcript.
        created_at: Epoch seconds of creation; persisted to ``meta.json`` and
            mirrored into the index as the sort tie-breaker.
        last_active: Epoch seconds of the most recent activity; drives idle
            eviction and the fallback sort key.
        state: Current :class:`SessionState`.
        pending_runs: Runs queued behind the executing one, so the loop can
            drain follow-up prompts instead of discarding them.
        metadata: Session-level bag holding ``name``, ``channel``,
            ``workspace``, ``archived``, ``temp_chat``,
            ``name_manually_renamed`` and ``source``.  Mirrored into
            ``meta.json`` on save; the index is the authoritative fallback
            when the last save predates a rename.
        sessions_dir: Sessions directory that owns this session, or empty
            when it must be resolved lazily.
    """

    session_id: str
    agent: EncreAgent
    agent_task: asyncio.Task[None] | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    state: SessionState = SessionState.IDLE
    pending_runs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    sessions_dir: str = ""


def _create_default_tool_registry() -> ToolRegistry:
    """Construct a fresh, empty tool registry.

    No tools are attached here: agents populate the registry through their
    plugin system, so two sessions built under different plugin bundles can
    expose different tool surfaces without this module needing to know about
    any concrete tool implementation.

    Returns:
        An empty :class:`~encre.tools.registry.ToolRegistry`.
    """
    return ToolRegistry()


# Process-wide prototype registry, materialised once on first use and treated
# as read-only thereafter.  It exists so that every session, whenever it is
# created in the process' lifetime, clones the same starting tool surface --
# including tools that a plugin registers lazily after construction.
_SHARED_DEFAULT_TOOL_REGISTRY: ToolRegistry | None = None


def _get_shared_default_tool_registry() -> ToolRegistry:
    """Lazily materialise the process-wide prototype tool registry.

    The global is assigned exactly once and the same object is returned
    forever.  Callers must clone it with :func:`_clone_tool_registry` before
    handing a registry to an agent, because conversation-scoped state such as
    tool unlocks and overrides is written directly onto a registry instance
    and must not be shared.

    Returns:
        The shared :class:`~encre.tools.registry.ToolRegistry` prototype.
    """
    global _SHARED_DEFAULT_TOOL_REGISTRY
    if _SHARED_DEFAULT_TOOL_REGISTRY is None:
        _SHARED_DEFAULT_TOOL_REGISTRY = _create_default_tool_registry()
    return _SHARED_DEFAULT_TOOL_REGISTRY


def _clone_tool_registry() -> ToolRegistry:
    """Clone the shared prototype registry into a session-private copy.

    The copy is shallow over the tool map, which is the correct level of
    indirection: individual tool objects carry no per-conversation state,
    while the container-level state (which tools are unlocked, overridden or
    hidden) lives on the registry and must be isolated per session.  Without
    the clone, unlocking a tool in one conversation would silently unlock it
    for every other session in the process.

    Returns:
        A new :class:`~encre.tools.registry.ToolRegistry` holding the same
        tool entries as the shared prototype.
    """
    registry = ToolRegistry()
    # Reaching into the private tool map is deliberate: the registry exposes no
    # copy constructor, and rebuilding it through the public registration API
    # would re-run registration side effects on every session start.
    registry._tools = dict(_get_shared_default_tool_registry()._tools)
    return registry


class SessionManager:
    """In-memory plus on-disk store for every agent session in the process.

    Purpose
    -------
    Own the id to agent mapping, own the encrypted index used for fast sidebar
    rendering, arbitrate run concurrency, and keep memory and disk consistent.
    It is a long-lived object: :class:`encre.server.app.EncreServer` constructs
    one per process and shares it with the per-connection protocol composition,
    the channel router, and the :mod:`encre.gateway` stack.

    Lifecycle
    ---------
    1. Construct with capacity and idle-tuning knobs; a ``sessions_dir``
       argument pins storage instead of letting it default.
    2. Every operation resolves storage lazily through
       :meth:`_get_sessions_dir`, which creates the directory and reads its
       encrypted index on first touch, so construction stays free of side
       effects.
    3. :meth:`shutdown` flushes pending debounced writes and persists every
       session in every pool, background ones included, before the process
       exits.

    Directory switching
    -------------------
    :meth:`set_workspace` and :meth:`set_iclaw` rebind storage without
    cancelling anything.  The leaving pool is parked in ``_dir_sessions``
    rather than closed, so agents bound to those sessions keep streaming into
    their original session objects, and switching back restores that exact
    pool (running tasks and all) instead of forking a second copy of a session
    that is already mid-run.  The corollary: a background session writes into
    *its own* directory's index, never the currently active one, so workspace
    sidebars never pick up foreign entries.

    Concurrency model
    -----------------
    * ``_lock`` is a re-entrant threading lock guarding snapshot reads of
      ``_sessions`` and ``_index``.  It is threading-based, not asyncio-based,
      because the startup resume inserts sessions from a worker thread while
      the event loop lists them.
    * ``_semaphore`` bounds how many agent runs may execute at once across all
      sessions and all channels, via :meth:`acquire_slot` /
      :meth:`release_slot`.
    * ``_save_tasks`` debounces disk writes per session, so a burst of
      mutations costs a single write.
    All three are keyed by session id or process-wide; none is per-request
    state.

    Safety
    ------
    Public listing methods snapshot under ``_lock`` before iterating, so a
    concurrent insert cannot raise "dictionary changed size during iteration".
    Blocking filesystem work is always dispatched to an executor through
    :meth:`_save_session_async`; no public method performs I/O on the event
    loop.

    Attributes:
        _sessions: Active pool for the currently selected directory.
        _dir_sessions: Parked pools keyed by sessions-directory path; keeps
            background agents alive across directory switches.
        _index: In-memory mirror of the active directory's ``index.json``.
        _index_dirty: Lazy-write flag; the index is re-encrypted and written
            only when at least one entry actually changed.
        _bootstrapped: Set once the active directory's index has been
            reconciled against the real directory listing.
        _sessions_changed_callbacks: Subscribers notified after any
            session-visible mutation; other layers use this to refresh
            sidebars and status badges.
    """

    def __init__(self, max_concurrent: int = 20, idle_timeout: float = 3600.0, sessions_dir: str | None = None) -> None:
        """Create an empty session store.

        Args:
            max_concurrent: Capacity of the run semaphore.  Bounds concurrent
                agent executions across desktop and gateway callers alike, not
                just this instance's direct callers.
            idle_timeout: Seconds of inactivity after which an ``IDLE`` session
                is evicted from memory by :meth:`cleanup_idle`.  The transcript
                stays on disk, so eviction is purely a memory decision and is
                invisible to the user.
            sessions_dir: Force a specific storage directory.  When omitted,
                storage defaults to ``<data_dir>/sessions`` and is resolved
                lazily; a per-workspace or per-iClaw directory is installed
                later through :meth:`set_workspace` / :meth:`set_iclaw`.
        """
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = threading.RLock()
        self._max_concurrent = max_concurrent
        self._idle_timeout = idle_timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._sessions_dir: str | None = None
        self._index: dict[str, dict[str, Any]] = {}
        self._index_dirty = False
        # Rows explicitly removed in memory whose removal has not yet been
        # persisted.  While a tombstone exists, disk-merge protection must
        # not resurrect the row from index.json.
        self._index_tombstones: set[str] = set()
        self._bootstrapped = False
        self._sessions_changed_callbacks: list[Callable[[], None]] = []
        self._dir_sessions: dict[str, dict[str, SessionInfo]] = {}
        self._dir_bootstrapped: dict[str, bool] = {}
        if sessions_dir:
            self._sessions_dir = sessions_dir
            pathlib.Path(sessions_dir).mkdir(parents=True, exist_ok=True)
            _store_started = time.perf_counter()
            self._load_index()
            # Ephemeral sessions must not survive a restart, so any leftovers
            # from the previous run (index rows and directories alike) are
            # removed here, before anything can list this directory.
            self._purge_temp_chat_leftovers()
            logger.info(
                "[perf] session store ready in %.0fms (%d indexed)",
                (time.perf_counter() - _store_started) * 1000.0,
                len(self._index),
            )

    # ----- session change callbacks -----

    def on_sessions_changed(self, callback: Callable[[], None]) -> None:
        """Register a callback fired whenever a session is created, updated, or deleted.

        Subscribers include the protocol handler base, which broadcasts a
        ``sessions_changed`` event to every connected renderer, and any gateway
        surface that needs to refresh its session list.  Callbacks are invoked
        synchronously in registration order on the calling thread.

        Args:
            callback: Zero-argument callable.  Exceptions it raises are caught
                and logged, so one misbehaving subscriber cannot prevent the
                remaining ones from running.

        Note:
            Callbacks never receive arguments; they must query the manager for
            current state, which keeps the event payload free of stale data.
        """
        self._sessions_changed_callbacks.append(callback)

    def notify_session_completed(self) -> None:
        """Notify subscribers that an agent run has finished and its session was saved.

        This is the completion edge: by the time it fires, the new transcript
        content is already flushed to disk, so subscribers may safely show the
        updated preview and message count without racing the writer.

        The number of callbacks is logged because callback registration is
        spread across several handlers and a missing subscription is the usual
        cause of a stale sidebar.
        """
        _fire_logger = logging.getLogger("encre.server.session")
        _fire_logger.info("[sessions_changed] notify_session_completed, %d callback(s)",
                          len(self._sessions_changed_callbacks))
        self._fire_sessions_changed()

    def _fire_sessions_changed(self) -> None:
        """Dispatch to every registered change callback, isolating failures.

        A raising subscriber is logged and skipped rather than propagated,
        because a dispatch error here would abort the caller's session
        mutation and leave the on-disk state and the UI out of step.

        Raises:
            Nothing; all subscriber exceptions are contained.
        """
        _fire_logger = logging.getLogger("encre.server.session")
        _fire_logger.info("[sessions_changed] firing %d callbacks",
                          len(self._sessions_changed_callbacks))
        for cb in self._sessions_changed_callbacks:
            try:
                cb()
            except Exception as exc:
                _fire_logger.warning("[sessions_changed] callback error: %s", exc)

    # ----- paths -----

    def _get_sessions_dir(self) -> str:
        """Return the active sessions directory, creating and loading it lazily.

        The first call does the real work: resolve the default
        ``<data_dir>/sessions`` path, create it, and read its encrypted
        ``index.json`` into memory.  Subsequent calls are a plain attribute
        read, so callers may treat this as a cheap pure lookup anywhere a
        directory path is needed.

        Returns:
            Absolute path of the active sessions directory.
        """
        if self._sessions_dir is None:
            _dir = get_data_dir() / "sessions"
            _dir.mkdir(parents=True, exist_ok=True)
            self._sessions_dir = str(_dir)
            self._load_index()
        return self._sessions_dir

    async def _switch_sessions_dir(self, _dir: pathlib.Path) -> None:
        """Rebind session storage to *dir* without stopping any running agent.

        The switch is non-destructive in two deliberate ways.  First, the
        leaving directory's index is flushed so its park state is not lost
        mid-flight.  Second, the leaving pool is parked in ``_dir_sessions``
        rather than closed: agents bound to those sessions keep running in the
        background and keep writing into their own directory's index, and
        switching back restores the identical pool so a resume cannot fork a
        second copy of a session that already owns a running task.

        Args:
            _dir: Target sessions directory, created if it does not exist.

        Note:
            The method is async for symmetry with its callers, but the work is
            synchronous; the index flush here is the only disk access and it is
            small (one encrypted summary file).
        """
        _dir_str = str(_dir)
        current_dir = self._get_sessions_dir()
        if current_dir == _dir_str:
            return
        _dir.mkdir(parents=True, exist_ok=True)

        # Persist the current directory's index before leaving.
        self._save_index()

        # Stash the current pool keyed by its own path; its tasks keep running.
        self._dir_sessions[current_dir] = self._sessions
        self._dir_bootstrapped[current_dir] = self._bootstrapped

        # Activate the target: reuse a parked pool if the user is switching
        # back to a directory they have visited, otherwise start with an empty
        # one that is populated on demand by load_or_create_session().
        self._sessions_dir = _dir_str
        self._sessions = self._dir_sessions.get(_dir_str, {})
        self._index = {}
        self._index_dirty = False
        self._bootstrapped = self._dir_bootstrapped.get(_dir_str, False)
        self._load_index()
        self._bootstrap_index_from_disk()

    async def set_workspace(self, ws_id: str | None) -> None:
        """Switch session storage to an iWork workspace, or back to the global directory.

        Workspace sessions are stored under ``<data_dir>/iwork/<ws_id>/sessions``
        so each workspace keeps its own history and its own sidebar.  Passing
        ``None`` returns to the global (non-workspace) store.

        Args:
            ws_id: Workspace id, or ``None`` for the global directory.

        Note:
            Running sessions in the directory being left are not cancelled; see
            :meth:`_switch_sessions_dir` for how they keep executing.
        """
        _dir = get_data_dir() / "iwork" / ws_id / "sessions" if ws_id else get_data_dir() / "sessions"
        await self._switch_sessions_dir(_dir)

    async def set_iclaw(self, active: bool) -> None:
        """Switch session storage to the embedded in-CLI client directory, or back to global.

        iClaw is the :mod:`encre.iclaw` agent client embedded in the harness
        loop; giving it a separate store keeps its conversations out of the
        desktop sidebar while still using the same agent and index machinery.

        Args:
            active: ``True`` to store under ``<data_dir>/iclaw/sessions``,
                ``False`` to return to ``<data_dir>/sessions``.
        """
        _dir = get_data_dir() / "iclaw" / "sessions" if active else get_data_dir() / "sessions"
        await self._switch_sessions_dir(_dir)

    def _index_path(self) -> pathlib.Path:
        """Return the on-disk path of the active directory's ``index.json``.

        Returns:
            Path of the encrypted index file for the active sessions directory.
        """
        return pathlib.Path(self._get_sessions_dir()) / "index.json"

    def _session_dir_path(self, session_id: str) -> pathlib.Path:
        """Resolve the on-disk directory of one session, searching past the active pool.

        Resolution order: the session's own recorded ``sessions_dir`` (fast
        path, correct for the common case), then any parked per-directory
        pool, then a filesystem-wide search.  The last step exists because the
        flat iWork listing shows history from every workspace at once; falling
        back to the active workspace's directory there would shadow the real
        session with a freshly created empty one and make the original vanish
        from the sidebar.

        Args:
            session_id: Client-visible session id.

        Returns:
            Directory holding this session's ``meta.json`` and transcript.
        """
        sessions_dir = self._get_sessions_dir()
        info = self._sessions.get(session_id)
        if info is not None and info.sessions_dir:
            sessions_dir = info.sessions_dir
        else:
            resolved = None
            for sess_dict in self._dir_sessions.values():
                cached = sess_dict.get(session_id)
                if cached is not None and cached.sessions_dir:
                    resolved = cached.sessions_dir
                    break
            if resolved is None:
                # Not tracked in memory, so it may live in a different
                # workspace's directory.  Search across all of them rather
                # than defaulting to the active workspace (see docstring).
                resolved = self._find_session_dir_anywhere(session_id, sessions_dir)
            if resolved is not None:
                sessions_dir = resolved
        return pathlib.Path(sessions_dir) / session_id

    def _find_session_dir_anywhere(
        self, session_id: str, current_sessions_dir: str
    ) -> str | None:
        """Locate a session directory across every workspace and the global directory.

        Builds a candidate list in a deliberate order -- every iWork workspace
        first, then the iClaw store, then the global store -- and returns the
        first candidate that actually contains ``session_id`` as a directory.
        Candidates are only probed with ``is_dir()``, so this is a handful of
        metadata calls rather than a full walk.

        Args:
            session_id: Session id to locate.
            current_sessions_dir: The already-active directory, excluded from
                the candidate list since the caller has checked it directly.

        Returns:
            The parent sessions directory containing the session, or ``None``
            if the session exists nowhere on disk.

        Note:
            Any filesystem error during candidate collection is swallowed; the
            method degrades to ``None`` and the caller falls back to creating
            a session, which is the safe outcome for a partial read.
        """
        candidates: list[str] = []
        try:
            iwork_root = get_data_dir() / "iwork"
            if iwork_root.is_dir():
                candidates.extend(
                    str(ws_dir / "sessions")
                    for ws_dir in iwork_root.iterdir()
                    if ws_dir.is_dir()
                )
        except Exception:
            pass
        # iClaw desktop sessions live under their own directory tree.
        try:
            iclaw_dir = get_data_dir() / "iclaw" / "sessions"
            if iclaw_dir.is_dir():
                candidates.append(str(iclaw_dir))
        except Exception:
            pass
        global_dir = str(get_data_dir() / "sessions")
        if global_dir != current_sessions_dir:
            candidates.append(global_dir)
        for sess_dir in candidates:
            if (pathlib.Path(sess_dir) / session_id).is_dir():
                return sess_dir
        return None

    # ----- index -----

    def _load_index_for_dir(self, sessions_dir: str) -> dict[str, Any]:
        """Read one directory's encrypted index without touching the active mirror.

        The file is normally ciphertext, but a plain-JSON prefix is accepted
        and tried as-is so an index written before encryption was mandatory
        still loads.  Decryption is attempted only when the raw text does not
        begin with ``{``, which is a cheap way to distinguish the two formats
        without ever attempting to parse ciphertext as JSON twice.

        Args:
            sessions_dir: Sessions directory whose ``index.json`` to read.

        Returns:
            The parsed index mapping, or ``{}`` on any failure.

        Note:
            Failing open to an empty index is safe: the index is a derived
            cache and :meth:`_bootstrap_index_from_disk` rebuilds it from the
            real directory listing.  An unparseable index must never block a
            listing call.
        """
        ip = pathlib.Path(sessions_dir) / "index.json"
        try:
            if ip.exists():
                raw = ip.read_text(encoding="utf-8").strip()
                if raw and not raw.startswith("{"):
                    with contextlib.suppress(Exception):
                        raw = decrypt(raw)
                index = json.loads(raw)
                if isinstance(index, dict):
                    return index
        except Exception:
            pass
        return {}

    def _save_index_for_dir(
        self,
        sessions_dir: str,
        index: dict[str, Any],
        *,
        preserve_disk_rows: bool = False,
    ) -> None:
        """Write one directory's index as an encrypted, compacted JSON blob.

        Args:
            sessions_dir: Sessions directory whose ``index.json`` to write.
            index: Full index mapping to persist; the whole table is rewritten
                rather than patched, so a single call always converges the
                file with memory.
            preserve_disk_rows: When ``True``, rows that exist only in the
                on-disk file (absent from *index*) are merged back before the
                write, except rows carrying a tombstone.  Every caller that
                writes WITHOUT delete intent -- single-session flush, rename,
                resume-sync -- must pass ``True``: a failed
                ``_load_index_for_dir`` (corrupt ciphertext, torn read, key
                mismatch) fails open to ``{}``, and writing a table built from
                that empty base would permanently destroy every row the memory
                mirror does not hold.  Callers with delete intent (temp-chat
                purge, disk delete) keep ``False`` so removed rows stay removed.

        Returns:
            ``True`` when the encrypted table was written to disk, ``False``
            when encryption or I/O failed and the file was left untouched.
        """
        ip = pathlib.Path(sessions_dir) / "index.json"
        try:
            if preserve_disk_rows:
                try:
                    if ip.exists():
                        raw = ip.read_text(encoding="utf-8").strip()
                        if raw and not raw.startswith("{"):
                            with contextlib.suppress(Exception):
                                raw = decrypt(raw)
                        disk = json.loads(raw)
                        if isinstance(disk, dict):
                            for sid, row in disk.items():
                                if sid not in index and sid not in self._index_tombstones:
                                    index[sid] = row
                except Exception:
                    pass
            payload = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
            encrypted = encrypt(payload)
            # Atomic (temp file + os.replace): a force-cancel or process kill
            # mid-write must never leave a truncated index behind -- a torn
            # JSON blob is exactly the "unclosed bracket" corruption that
            # breaks the session list on the next start.  Fail-closed on
            # encryption: the file is only ever replaced after ``encrypt``
            # succeeded, so a key or filesystem failure cannot leave session
            # titles and previews on disk in plaintext.
            _atomic_write_text(ip, encrypted)
            return True
        except Exception:
            return False

    def _load_index(self) -> None:
        """Load the active directory's index into the in-memory mirror.

        Used only during construction and directory switching, where the
        mirror is being repointed; per-session writes use the per-directory
        variant instead.
        """
        self._index = self._load_index_for_dir(self._get_sessions_dir())

    def _purge_temp_chat_leftovers(self) -> None:
        """Remove ephemeral session directories and index rows left by a previous run.

        Temp chats are meant to vanish with the process.  Because this cannot
        be guaranteed -- a crash or a process kill between the user message
        write and process exit would leave the artifacts behind -- startup
        removes them by scanning two orthogonal axes: directories whose
        ``meta.json`` carries the ``temp_chat`` marker, and index rows that
        carry it but no longer have a directory.  Both axes are needed, since
        each can outlive the other.

        The purge runs before any listing code can observe this directory, so
        a restarted app never shows a ghost session in the sidebar.  The index
        is only rewritten if something was actually removed, keeping the
        common case write-free.
        """
        import shutil
        sessions_dir = self._get_sessions_dir()
        repaired = False
        # Remove stray temp-chat directories on disk. Indexed rows already
        # carry the flag, so only unindexed directories need a meta read —
        # reading every session's meta here delayed server readiness by tens
        # of seconds on a large history.
        try:
            for entry in os.scandir(sessions_dir):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                row = self._index.get(entry.name)
                if isinstance(row, dict):
                    row_meta = row.get("metadata") or {}
                    if row_meta.get("temp_chat") or row.get("temp_chat"):
                        shutil.rmtree(entry.path, ignore_errors=True)
                        self._index.pop(entry.name, None)
                        self._index_tombstones.add(entry.name)
                        repaired = True
                    continue
                meta = EncreSession.read_meta(entry.path)
                if meta and isinstance(meta, dict):
                    meta_metadata = meta.get("metadata", {}) or {}
                    if meta_metadata.get("temp_chat"):
                        shutil.rmtree(entry.path, ignore_errors=True)
                        repaired = True
        except Exception:
            pass
        # Also drop index rows for temp chats whose directory is already gone.
        stale = [sid for sid, idx in list(self._index.items())
                 if idx.get("metadata", {}).get("temp_chat") or idx.get("temp_chat")]
        for sid in stale:
            self._index.pop(sid, None)
            self._index_tombstones.add(sid)
            repaired = True
        if repaired:
            self._save_index()

    def _save_index(self) -> None:
        """Flush the active directory's index if anything changed.

        The dirty flag makes the common path a single comparison: most
        lifecycle calls that touch the lock do not mutate an entry, and those
        must not re-encrypt and rewrite the whole table.

        Invariant:
            Called with the mirror already updated; this method never computes
            content, only decides whether a pending write is owed.
        """
        with self._lock:
            if not self._index_dirty:
                return
            # Disk-merge protection with tombstone exclusion: rows removed on
            # purpose (temp purge, _index_remove) stay removed, every other
            # disk-only row survives the rewrite even when the memory mirror
            # was seeded from a failed or aborted index load.
            saved = self._save_index_for_dir(
                self._get_sessions_dir(), self._index, preserve_disk_rows=True
            )
            self._index_dirty = False
            if saved:
                self._index_tombstones.clear()

    def _make_index_entry(self, info: SessionInfo, preview: str = "") -> dict[str, Any]:
        """Build one index row from a live session, carrying forward user-managed fields.

        The row is what the sidebar renders, so every field is chosen to match
        a persisted transcript rather than a transient runtime value.  Three
        fields deserve special care:

        * ``last_active`` is sourced from the session's ``last_message_at``,
          then ``updated_at``, then the in-memory clock.  It must reflect the
          real last conversation, never a creation time and never a touch from
          opening the session, or the sidebar would reorder on every click.
        * ``name`` and ``name_manually_renamed`` are read back out of the
          existing row when the live record has nothing.  These are
          user-managed, not conversation-derived, and this method wholesale
          replaces the row, so dropping them would silently un-rename a
          session after any save.
        * ``archived`` is the OR of the live metadata, the session metadata,
          and the previous row, so an archive flag can never be lost by an
          unrelated refresh.

        Args:
            info: Live session record to describe.
            preview: Optional preview string taken from a fresh summary; when
                empty, the cached summary of the session is used.

        Returns:
            A JSON-serializable row keyed by ``session_id``, ``created_at``,
            ``last_active``, ``preview``, ``channel``, ``workspace``,
            ``message_count``, ``turn_count``, ``name``,
            ``name_manually_renamed``, ``archived`` and ``temp_chat``.
        """
        sess = info.agent.session
        existing = self._index.get(info.session_id, {})
        msg_count, cached_preview = sess.get_summary()
        channel = sess.metadata.get("channel", info.metadata.get("channel", "normal"))
        # The workspace path is persisted so a workspace session can be
        # re-entered straight from disk after a restart.
        workspace = info.metadata.get("workspace") or sess.metadata.get("workspace", "")
        # Archived flag: hidden from normal lists, reachable from the archive view.
        archived = bool(
            info.metadata.get("archived")
            or sess.metadata.get("archived")
            or existing.get("archived", False)
        )
        last_active = (
            getattr(sess, "last_message_at", 0)
            or sess.updated_at
            or info.last_active
        )
        return {
            "session_id": info.session_id,
            "created_at": info.created_at,
            "last_active": last_active,
            "preview": preview or cached_preview,
            "channel": channel,
            "workspace": workspace,
            "message_count": msg_count,
            "turn_count": sess.turn_count,
            "name": info.metadata.get("name", existing.get("name", "")),
            # The manual-rename marker must survive restarts: the auto-namer
            # only defers to it, so losing it would overwrite a user's title
            # with a generated one on the first message after a restart.
            "name_manually_renamed": bool(
                sess.metadata.get("name_manually_renamed")
                or info.metadata.get("name_manually_renamed")
                or existing.get("name_manually_renamed", False)
            ),
            "archived": archived,
            # Ephemeral marker repeated on every row so that all index readers
            # (query_index, _list_all_sessions, startup purge) can exclude
            # temp chats even if some path ever writes one.
            "temp_chat": bool(
                info.metadata.get("temp_chat") or sess.metadata.get("temp_chat")
            ),
        }

    def _index_add(self, info: SessionInfo, preview: str = "") -> None:
        """Insert or replace a session's index row in the active directory.

        Args:
            info: Session to index.
            preview: Optional preview text; falls back to the session summary.

        Invariant:
            Ephemeral sessions never reach disk.  The check looks at both the
            record metadata and the underlying session metadata, because a
            temp chat is flagged on the session object itself and may not
            carry the flag on the wrapper.
        """
        sess = getattr(info.agent, "session", None)
        if info.metadata.get("temp_chat") or (
            sess is not None and sess.metadata.get("temp_chat")
        ):
            return
        self._index[info.session_id] = self._make_index_entry(info, preview)
        self._index_dirty = True

    def _index_remove(self, session_id: str) -> None:
        """Drop a session from the active directory's index mirror.

        A no-op for ids that were never indexed (for example, a session that
        was deleted before its first flush), which is why the dirty flag is
        only raised when a row actually existed.

        Args:
            session_id: Row to remove.
        """
        if session_id in self._index:
            self._index.pop(session_id, None)
            self._index_tombstones.add(session_id)
            self._index_dirty = True

    @staticmethod
    def _extract_preview_from_messages(messages: list[dict[str, Any]]) -> str:
        """Derive a sidebar preview from a raw transcript.

        Only user turns qualify, since an assistant preamble would be a
        spoiler and a system turn would be noise.  Content may be a plain
        string or the multi-block form ``[{"type": "text", "text": ...}]``
        used by the OpenAI-compatible transport, and both shapes are handled.
        The result is capped at 80 characters so a pasted file cannot blow up
        a single sidebar row.

        Args:
            messages: Ordered transcript, oldest first.

        Returns:
            The trimmed first non-empty user text, or ``""`` when no user
            message exists yet.
        """
        for m in messages:
            if m.get("role") != "user":
                continue
            c = m.get("content", "")
            if isinstance(c, str) and c.strip():
                return c.strip()[:80]
            if isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    text = b.get("text", "")
                    if isinstance(text, str) and text.strip() and b.get("type") == "text":
                        return text.strip()[:80]
        return ""

    def _bootstrap_index_from_disk(self) -> None:
        """Reconcile the active directory's index against the real directory listing.

        Self-guarded by ``_bootstrapped`` per directory, but the guard is a
        performance hint rather than a correctness gate: the method is also
        invoked from the listing paths, so a partially clobbered index file
        heals itself on startup without a dedicated repair command.

        Three classes of repair happen in one pass:

        1. Ephemeral leftovers are deleted from disk and from the index.
        2. Rows that are missing entirely, whose preview is empty, or whose
           message count disagrees with ``meta.json`` are rebuilt from the
           transcript.  The rebuilt row carries the persisted title and its
           manual-rename marker forward, because this method replaces rows
           wholesale and dropping them would un-rename a session on restart.
        3. Stale temp-chat rows whose directories no longer exist are purged.

        A directory with no readable ``meta.json`` is skipped rather than
        given a synthetic row: fabricating one would flood the sidebar with
        empty phantom entries for orphans left by crashes.

        Raises:
            Nothing; the whole pass is wrapped so a hostile or corrupt tree
            cannot break session listing.
        """
        if self._bootstrapped:
            return
        import shutil
        _boot_started = time.perf_counter()
        try:
            sessions_dir = self._get_sessions_dir()
            repaired = False
            for entry in os.scandir(sessions_dir):
                # Per-entry isolation: one hostile or corrupt directory must
                # not abort the whole scan.  An aborted scan left the remaining
                # rows unrepaired and let the next flush persist the shrunken
                # table, permanently losing every session behind the failure.
                try:
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    sid = entry.name
                    existing = self._index.get(sid)
                    if isinstance(existing, dict):
                        row_meta = existing.get("metadata") or {}
                        if row_meta.get("temp_chat") or existing.get("temp_chat"):
                            shutil.rmtree(entry.path, ignore_errors=True)
                            self._index.pop(sid, None)
                            self._index_tombstones.add(sid)
                            repaired = True
                            continue
                        # A complete row is written together with meta.json on
                        # every save; trusting it avoids reading thousands of
                        # meta files on the first listing (boot-long-task).
                        # Incomplete rows still fall through to the repair path.
                        if existing.get("preview", "") and existing.get("message_count") is not None:
                            continue
                    meta = EncreSession.read_meta(entry.path)
                    meta_metadata = meta.get("metadata", {}) if isinstance(meta, dict) else {}
                    if meta_metadata.get("temp_chat"):
                        shutil.rmtree(entry.path, ignore_errors=True)
                        if sid in self._index:
                            self._index.pop(sid, None)
                            self._index_tombstones.add(sid)
                            repaired = True
                        continue
                    # Rebuild only when there is something to fix: a missing row,
                    # an empty preview, or a count that predates the backend
                    # writing user-message counts into meta.json.
                    existing = self._index.get(sid)
                    meta_message_count = meta.get("message_count") if isinstance(meta, dict) else None
                    needs_repair = (
                        existing is None
                        or not existing.get("preview", "")
                        or (
                            meta_message_count is not None
                            and existing.get("message_count") != meta_message_count
                        )
                    )
                    if not needs_repair:
                        continue
                    channel = "normal"
                    if meta:
                        ch = meta_metadata.get("channel") or ""
                        if ch:
                            channel = ch
                    if meta is None:
                        # Orphan directory (crash or a previous encryption era):
                        # nothing recoverable, and no synthetic row may be made.
                        # An empty shell (only the per-session files/ folder a
                        # run preparation creates unconditionally) is garbage
                        # collected so hundreds never accumulate.
                        self._gc_orphan_shell(entry.path)
                        continue
                    preview = EncreSession.load_preview(entry.path) or ""
                    prev_name = (
                        (existing.get("name", "") if existing else "")
                        or (meta_metadata.get("name", "") if isinstance(meta_metadata, dict) else "")
                    )
                    prev_manual = bool(
                        (existing.get("name_manually_renamed", False) if existing else False)
                        or (meta_metadata.get("name_manually_renamed", False) if isinstance(meta_metadata, dict) else False)
                    )
                    ws_path = (
                        meta_metadata.get("workspace", "")
                        if isinstance(meta_metadata, dict)
                        else ""
                    )
                    # Prefer the persisted count; only scan the transcript when
                    # meta.json predates that field, which keeps a large tree fast.
                    if meta.get("message_count") is not None:
                        msg_count = meta.get("message_count")
                    else:
                        msg_count = EncreSession.count_user_messages(entry.path)
                    self._index[sid] = {
                        "session_id": sid,
                        "created_at": meta.get("created_at", time.time()),
                        "last_active": meta.get("updated_at", time.time()),
                        "preview": preview,
                        "message_count": msg_count,
                        "turn_count": meta.get("turn_count", 0),
                        "channel": channel,
                        "workspace": ws_path,
                        "name": prev_name,
                        "name_manually_renamed": prev_manual,
                        # Restoring the flag here is what keeps an archived
                        # session out of the normal sidebar after a rebuild.
                        "archived": bool(meta_metadata.get("archived", False)),
                    }
                    repaired = True
                except Exception:
                    continue
            stale_temp_sids = [
                sid for sid in list(self._index.keys())
                if self._index[sid].get("metadata", {}).get("temp_chat") or self._index[sid].get("temp_chat")
            ]
            for sid in stale_temp_sids:
                self._index.pop(sid, None)
                repaired = True
            if repaired:
                self._save_index()
            self._bootstrapped = True
            _boot_ms = (time.perf_counter() - _boot_started) * 1000.0
            if _boot_ms >= 200:
                logger.info("[perf] session index reconcile %.0fms", _boot_ms)
        except Exception:
            pass

    @staticmethod
    def _gc_orphan_shell(dir_path: str) -> None:
        """Delete an orphan session shell that holds no recoverable data.

        A run preparation creates the per-session ``files/`` folder
        unconditionally, so sessions that never produced a transcript leave
        behind directory shells that pile up on disk.  A shell is collected
        only when the directory is empty or contains nothing but an *empty*
        ``files/`` folder; any other content -- a transcript, a meta file,
        user artifacts -- keeps the directory on disk untouched.

        Args:
            dir_path: Candidate session directory to inspect and collect.
        """
        import shutil
        try:
            entries = list(os.scandir(dir_path))
        except Exception:
            return
        if not entries:
            shutil.rmtree(dir_path, ignore_errors=True)
            return
        if len(entries) == 1 and entries[0].name == "files" and entries[0].is_dir():
            try:
                inner = list(os.scandir(entries[0].path))
            except Exception:
                return
            if not inner:
                shutil.rmtree(dir_path, ignore_errors=True)

    # ----- session CRUD -----

    def _save_session(self, info: SessionInfo) -> None:
        """Persist one session and refresh the index row it owns.

        Ordering matters here: the transcript and ``meta.json`` are written
        first, then the index row is rewritten from the fresh summary.  If
        the order were reversed a crash between the two steps would leave a
        row pointing at older content, and a crash in the current order only
        leaves the index slightly stale, which
        :meth:`_bootstrap_index_from_disk` reconciles on the next start.

        The index update is written into the session's own directory rather
        than the currently active one, so a background session whose directory
        was parked by a workspace switch still lands its row where it
        belongs.  The title is re-read from that directory's index when the
        freshly built row has none, so a rename is not lost by a save that
        happens while a different workspace is in front.

        Args:
            info: Session record to flush.

        Raises:
            Nothing: every failure is logged and swallowed.  This runs in an
            executor thread, and a raised exception here would surface as an
            unhandled task failure rather than as a reportable error to the
            caller's coroutine.
        """
        import logging
        _log = logging.getLogger("encre.server.session")
        lock = self._save_io_locks.setdefault(info.session_id, threading.Lock())
        with lock:
            try:
                sess = info.agent.session
                # Ephemeral sessions must never touch disk or the index.
                if sess.metadata.get("temp_chat"):
                    return

                sessions_dir = info.sessions_dir or self._get_sessions_dir()
                dir_path = pathlib.Path(sessions_dir) / info.session_id
                sess.save_to_dir(str(dir_path))
                _, preview = sess.get_summary()

                index = self._load_index_for_dir(sessions_dir)
                entry = self._make_index_entry(info, preview)
                if not entry.get("name"):
                    entry["name"] = index.get(info.session_id, {}).get("name", "")
                index[info.session_id] = entry
                # No delete intent here: preserve rows the (possibly failed)
                # directory-index load did not return, so a corrupt index file
                # can never be overwritten with a shrunken table.
                self._save_index_for_dir(sessions_dir, index, preserve_disk_rows=True)
            except Exception:
                _log.exception("Failed to save session %s", info.session_id)

    def create_session(self, config: EncreConfig | None = None, session_id: str | None = None, mode: Any = None) -> SessionInfo:
        """Create a brand-new in-memory session with a cloned tool registry.

        Args:
            config: Optional configuration override; a shallow ``replace`` is
                taken so later per-session adjustments (workspace, channel)
                cannot mutate a caller's shared config object.
            session_id: Optional explicit id.  Defaults to a fresh UUID.  Allowing
                a caller-supplied id is what lets
                :meth:`load_or_create_session` and the gateway adapters reuse a
                known adapter session key on every fallback instead of minting a
                new one, which would otherwise make each retry look like a brand
                new "one-time" conversation.
            mode: Optional capability profile (``AgentMode``).  When omitted it
                is derived from the configuration: a session bound to a workspace
                gets the workspace profile (production gating plus the
                planner/architect contract loop) and an unbound one gets the
                general profile.  Deriving here keeps every workspace session
                creation path consistent without touching each call site.

        Returns:
            The new :class:`SessionInfo`, already inserted into the active pool
            and announced to change subscribers.

        Note:
            A placeholder title is assigned at creation time so the sidebar
            shows a readable label immediately; the auto-namer replaces it only
            once a real user message arrives and the session is not
            user-renamed.
        """
        config = replace(config) if config is not None else EncreConfig()
        if session_id is None:
            session_id = str(uuid.uuid4())
        if mode is None:
            from encre.modes.mode_profiles import AgentMode
            mode = AgentMode.WORKSPACE if (config.workspace or "").strip() else AgentMode.GENERAL
        tool_registry = _clone_tool_registry()
        from encre.agent import EncreAgent
        agent = EncreAgent(config=config, tool_registry=tool_registry, mode=mode, defer_plugins=True)
        agent.telemetry.session_id = session_id
        info = SessionInfo(session_id=session_id, agent=agent)
        info.sessions_dir = self._get_sessions_dir()
        from datetime import datetime
        placeholder_name = f"New Session {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        info.metadata["name"] = placeholder_name
        self._sessions[session_id] = info
        self._fire_sessions_changed()

        # Hook the flush to the moment a user message lands, so a kill before
        # the model answers still leaves a resumable transcript on disk.
        self._register_user_message_persist_hook(info)
        return info

    def _register_user_message_persist_hook(self, info: SessionInfo) -> None:
        """Attach an ``on_user_message_persisted`` handler that flushes the session.

        The hook schedules a fire-and-forget async save rather than awaiting
        one: the model loop must never stall on disk I/O, and the hook
        dispatch itself is synchronous, so only the write is detached.  The
        trade-off is that a process killed inside the write window loses the
        last assistant turn while keeping the user turn -- acceptable, since
        the user's prompt is exactly what makes a session worth resuming.

        Args:
            info: Session whose agent receives the handler.

        Raises:
            Nothing; registration failures are swallowed so a hook-system
            problem cannot block session creation.
        """
        import asyncio

        async def _persist(_session_id: str, _ctx: dict, _result: dict | None) -> dict:
            """Flush this session to disk the moment a user message is persisted.

            Registered as an ``on_user_message_persisted`` handler on
            ``info.agent.hook_system`` by the enclosing
            :meth:`_register_user_message_persist_hook`.  The hook fires on the
            model loop, immediately after the user turn has been appended to
            the :class:`~encre.session.EncreSession` transcript that backs
            ``data/sessions/<uuid>/meta.json`` and the JSONL log.

            The save is dispatched with :func:`asyncio.ensure_future` rather
            than awaited, because blocking the model loop on disk I/O would
            stall every concurrent WebSocket handler and gateway caller on this
            event loop.  The window of risk is one write: a process killed
            inside it keeps the user turn while losing the last assistant turn.

            The closure captures ``info`` directly, so the handler always
            flushes the record it was registered for even if the manager later
            parks the pool in a directory switch.

            Args:
                _session_id: Session id the hook fired for; redundant with
                    ``info.session_id`` and kept only to satisfy the hook
                    signature.
                _ctx: Hook context mapping; the flush is driven by the closure,
                    so no field is read here.
                _result: The persisted message payload, unused for the same
                    reason.

            Returns:
                ``{}``.  The hook system expects a dict back, and an empty one
                signals that no additional context is contributed.

            Raises:
                Nothing.  This runs inside the model loop, so a raised
                exception would propagate through the hook dispatcher and abort
                the turn; every failure is swallowed.  The same failure is
                retried by the next :meth:`_schedule_save` or by
                :meth:`shutdown`, so the swallowed error is not silent for
                long.
            """
            try:
                # Fire-and-forget: do not hold the model loop on disk I/O.
                asyncio.ensure_future(self._save_session_async(info))
            except Exception:
                pass
            return {}

        # Registration happens after the closure is fully built, so a handler
        # that was never installed cannot fire against a half-initialised
        # record; and the failure is dropped silently because _save_session
        # also runs from shutdown and remove_session, which cover the loss.
        try:
            info.agent.hook_system.register_handler(
                "on_user_message_persisted", _persist,
            )
        except Exception:
            pass

    def get_session(self, session_id: str) -> SessionInfo | None:
        """Return the live in-memory record for a session, or ``None``.

        Consults the active pool only.  Callers that need to reach parked
        sessions across a directory switch must go through
        :meth:`load_or_create_session`, which also promotes the record back
        into the active pool.

        Args:
            session_id: Client-visible session id.

        Returns:
            The :class:`SessionInfo` record, or ``None`` when not resident.
        """
        return self._sessions.get(session_id)

    def index_entry(self, session_id: str) -> dict[str, Any] | None:
        """Persisted index row for *session_id* from the current directory.

        Read-only view over the on-disk session index; lets callers label
        sessions that are not resident in memory (e.g. the gateway's
        ``/sessions`` listing) without promoting them into the active pool.
        """
        return self._index.get(session_id)

    def iter_sessions(self) -> list[SessionInfo]:
        """Return every in-memory session *record*.

        This is not a listing.  Unlike :meth:`list_sessions` it applies no
        ephemeral or archived filtering and performs no name or workspace
        fallback, because its callers (configuration reload, maintenance
        passes) need the live records themselves rather than a rendered
        summary.

        A snapshot is taken under the lock because the startup resume inserts
        records from a worker thread while this method may be iterating the
        same dict from the event loop.

        Returns:
            The live :class:`SessionInfo` records, one per resident session.
        """
        with self._lock:
            return list(self._sessions.values())

    def get_session_state(self, session_id: str) -> str:
        """Return the current state of a session as a ``SessionState`` value.

        The stored state is reconciled against the actual task before being
        reported: a session recorded as ``RUNNING`` whose task has completed
        or been cancelled is reported as ``IDLE`` instead.  Without this check
        a cancelled run would keep the session marked running forever, which
        both misleads the renderer and blocks idle eviction.

        Args:
            session_id: Session to inspect.

        Returns:
            One of ``"idle"``, ``"running"``, ``"awaiting_approval"``.  A
            session not resident anywhere reports ``"idle"`` rather than
            raising, so status polling is safe before and after a resume.
        """
        info = self._sessions.get(session_id)
        if info is None:
            for sess_dict in self._dir_sessions.values():
                info = sess_dict.get(session_id)
                if info is not None:
                    break
        if info is None:
            return SessionState.IDLE.value
        task_alive = info.agent_task is not None and not info.agent_task.done()
        if info.state == SessionState.RUNNING and not task_alive:
            return SessionState.IDLE.value
        return info.state.value

    def set_session_state(self, session_id: str, new_state: SessionState) -> None:
        """Transition a resident session to a new state.

        Silent no-op when the session is not resident, which keeps callers
        free to update state optimistically around a create or remove.

        Args:
            session_id: Session to transition.
            new_state: Target :class:`SessionState`.

        Note:
            State set here is advisory; see :meth:`get_session_state` for how
            it is reconciled with the real task before reporting.

            The lookup mirrors :meth:`get_session_state`: sessions parked in a
            per-directory pool must be reachable too. A silent no-op here is
            how a force-cancelled session ends up stuck in ``RUNNING`` --
            every later run is then refused with ``busy`` until a restart.
        """
        info = self._sessions.get(session_id)
        if info is None:
            for sess_dict in self._dir_sessions.values():
                info = sess_dict.get(session_id)
                if info is not None:
                    break
        if info is not None:
            info.state = new_state

    def schedule_cancel_finalization(self, info: SessionInfo) -> None:
        """Delegate a one-shot task that closes the interrupted turn's history.

        ``task.cancel()`` unwinds the run, but the async-generator ``finally``
        that closes orphan tool_use blocks is driven by the loop's asyncgen
        finalizer -- it runs on garbage collection, not eagerly, so it can be
        deferred indefinitely while the process stays alive.  Until it runs,
        the persisted history ends with an unanswered tool_use, which breaks
        the sidebar, rollback and the next model request.

        The delegated task waits for the cancelled agent task to unwind, then
        re-runs the idempotent finalize (synthesizes error tombstones for
        tool_calls without results, persists partially streamed text) and
        saves the session.  It deliberately does NOT touch the session state
        or the concurrency slot -- the cancel path already settled both.

        Args:
            info: The cancelled session whose history should be closed out.

        Returns:
            None.
        """

        async def _finalize() -> None:
            """Close out a cancelled turn's history once the run has unwound.

            Waits for the cancelled agent task to finish unwinding, then asks
            the loop to synthesise error tombstones for any orphan
            ``tool_use`` block and persists the partially streamed session.
            Without this, the persisted history ends on an unanswered
            ``tool_use``, which breaks rollback and the next model request.

            Args:
                None; everything is read from the enclosing ``info``.

            Returns:
                None.

            Note:
                The wait on ``info.agent_task`` is bounded at three seconds and
                the loop hook and the save are individually suppressed, so a
                hang or a raised cleanup step cannot strand the history. This
                task must not touch the session state or the concurrency slot;
                the cancel path already settled both.
            """
            try:
                task = info.agent_task
                if task is not None and not task.done():
                    # Wait (bounded) for the cancelled run to unwind so we
                    # never race its finally block while it is still writing.
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(task, timeout=3.0)
                loop = getattr(info.agent, "loop", None)
                if loop is not None and hasattr(loop, "_finalize_cancelled_turn"):
                    with contextlib.suppress(Exception):
                        loop._finalize_cancelled_turn()
                with contextlib.suppress(Exception):
                    await self._save_session_async(info)
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.getLogger("encre.server.session").warning(
                    "[session] deferred cancel finalization failed for %s",
                    info.session_id, exc_info=True,
                )

        finalizer = asyncio.ensure_future(_finalize())
        # Keep a strong reference until done -- the event loop only holds a
        # weak one, and a garbage-collected task vanishes mid-flight.
        pending = getattr(self, "_cancel_finalizers", None)
        if pending is None:
            pending = self._cancel_finalizers = set()
        pending.add(finalizer)
        finalizer.add_done_callback(pending.discard)

    def is_session_running(self, session_id: str) -> bool:
        """Report whether a session has live work in flight.

        True when the state is ``RUNNING`` or ``AWAITING_APPROVAL``, or when a
        task exists and has not finished.  The state terms are OR-ed with the
        task liveness check so a blocked-approval session counts as busy (its
        task is suspended but still owns resources) and a task that outlives a
        stale state still counts.

        Args:
            session_id: Session to probe.

        Returns:
            ``True`` when the session is executing or awaiting approval.
        """
        info = self._sessions.get(session_id)
        if info is None:
            for sess_dict in self._dir_sessions.values():
                info = sess_dict.get(session_id)
                if info is not None:
                    break
        if info is None:
            return False
        return info.state in (SessionState.RUNNING, SessionState.AWAITING_APPROVAL) or (info.agent_task is not None and not info.agent_task.done())

    def load_or_create_session(self, session_id: str, config: EncreConfig | None = None, mode: Any = None) -> SessionInfo:
        """Return the live session for *session_id*, loading it from disk if needed.

        Lookup order: the active pool, then every parked per-directory pool,
        then the on-disk session directory, then a fresh session.  Each step
        exists for a specific reason -- the first preserves running agent
        state, the second handles the case where a session belongs to a
        directory the user has since switched away from, the third restores a
        closed session, and the last covers an id that has never existed.

        Args:
            session_id: Session to resolve.
            config: Optional configuration; shallow-copied before use so
                per-session edits cannot leak back into a shared config.
            mode: Optional capability profile; derived from the restored
                workspace path when omitted.

        Returns:
            A resident :class:`SessionInfo`.  Never ``None``.

        Note:
            On disk load, workspace context is restored *before* the mode is
            derived, so a resumed workspace session resumes in the workspace
            profile instead of silently degrading to the general one.  The
            cumulative token cost is restored from the JSONL log so
            ``get_summary()`` reflects the whole session, not just the part
            executed after the resume.
        """
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing

        # The live instance may sit in a parked pool when the user is in one
        # mode and resumes a session from another; returning it keeps the
        # streaming task bound to the same session object so the disk view
        # never diverges from in-memory progress.
        for sess_dict in self._dir_sessions.values():
            stashed = sess_dict.get(session_id)
            if stashed is not None:
                # Promote to the active pool so later lookups are O(1).
                self._sessions[session_id] = stashed
                return stashed

        dir_path = self._session_dir_path(session_id)
        if dir_path.is_dir():
            try:
                cfg = replace(config) if config is not None else EncreConfig()
                tool_registry = _clone_tool_registry()
                meta = EncreSession.read_meta(str(dir_path))
                ws_path = (meta.get("metadata", {}) or {}).get("workspace") or meta.get("workspace") or ""
                if ws_path and os.path.isdir(ws_path):
                    cfg.workspace = ws_path
                # General-mode conversations remember their own model.  The
                # identity is resolved against the CURRENT model list, so a
                # provider edit between sessions cannot silently move this
                # conversation onto a different model.  Workspace sessions
                # skip this: their model comes from the workspace config and
                # must stay uniform across the project.
                if meta and not (cfg.workspace or "").strip():
                    saved_model = (meta.get("metadata", {}) or {}).get("active_model")
                    saved_index = resolve_model_index(cfg, saved_model)
                    if (
                        saved_index is not None
                        and getattr(cfg.models[saved_index], "enabled", True)
                    ):
                        cfg.active_model_index = saved_index
                        cfg.apply_active_model()
                if mode is None:
                    from encre.modes.mode_profiles import AgentMode
                    mode = AgentMode.WORKSPACE if (cfg.workspace or "").strip() else AgentMode.GENERAL
                from encre.agent import EncreAgent
                agent = EncreAgent(config=cfg, tool_registry=tool_registry, mode=mode, defer_plugins=True)
                agent.telemetry.session_id = session_id
                agent.session = EncreSession.load_from_dir(str(dir_path), cfg)
                agent.loop.session = agent.session
                agent.telemetry.session_id = session_id
                # Best-effort: never let a cost-restore failure abort a resume.
                with contextlib.suppress(Exception):
                    agent.telemetry.restore_session_cost_from_jsonl()
                info = SessionInfo(session_id=session_id, agent=agent)
                # Remember the directory the session came from -- it may be a
                # different workspace's when resumed from the flat iWork list.
                info.sessions_dir = str(dir_path.parent)
                if meta:
                    info.created_at = meta.get("created_at", time.time())
                    info.last_active = meta.get("updated_at", time.time())
                    if ws_path and os.path.isdir(ws_path):
                        info.metadata["workspace"] = ws_path
                    sess_channel = agent.session.metadata.get("channel") or ""
                    if sess_channel:
                        info.metadata["channel"] = sess_channel
                    _meta_metadata = meta.get("metadata", {}) or {}
                    # The index is the fallback when the last meta.json save
                    # predates the rename.
                    _idx_entry = (
                        self._load_index_for_dir(info.sessions_dir or self._get_sessions_dir()).get(session_id)
                        or {}
                    )
                    _restored_name = _meta_metadata.get("name") or _idx_entry.get("name") or ""
                    if _restored_name:
                        info.metadata["name"] = _restored_name
                    if _meta_metadata.get("name_manually_renamed") or _idx_entry.get("name_manually_renamed"):
                        info.metadata["name_manually_renamed"] = True
                self._sessions[session_id] = info
                # Sync the on-disk index with the loaded session so query_index()
                # and _list_all_sessions() report the same last_active as
                # list_sessions(); without this, sidebar ordering would use a
                # stale value and opening a session would appear to reset it.
                target_dir = info.sessions_dir or self._get_sessions_dir()
                index = self._load_index_for_dir(target_dir)
                index[session_id] = self._make_index_entry(info)
                # Resume-sync has no delete intent: never let a failed index
                # load shrink the on-disk table.
                self._save_index_for_dir(target_dir, index, preserve_disk_rows=True)
                if target_dir == self._get_sessions_dir():
                    # Merge instead of replace: the load may have failed open
                    # to {} and the memory mirror must not be wiped either.
                    for sid, row in self._index.items():
                        index.setdefault(sid, row)
                    self._index = index
                    self._index_dirty = False
                self._register_user_message_persist_hook(info)
                return info
            except Exception:
                # A corrupt transcript must not wedge the session: fall back to
                # a fresh session reusing the same id so the caller's handle stays valid.
                return self.create_session(config=config, session_id=session_id, mode=mode)

        return self.create_session(config=config, session_id=session_id, mode=mode)

    async def remove_session(self, session_id: str) -> None:
        """Evict a session from memory after cancelling its run and flushing it.

        The session is removed from the active pool first, so a concurrent
        lookup cannot reattach to it mid-teardown.  Its task is cancelled
        before the flush because a still-running loop could otherwise keep
        writing the transcript past the eviction point.  The final save runs
        in an executor so the event loop is not blocked by the transcript and
        index writes.

        Disk data is retained: the session remains openable and is only dropped
        from memory.  Use :meth:`delete_session_from_disk` for real removal.

        Args:
            session_id: Session to evict; a no-op when not resident.

        Note:
            Requires a running event loop because it awaits an executor call.
        """
        info = self._sessions.pop(session_id, None)
        if info is not None:
            if info.agent_task and not info.agent_task.done():
                info.agent_task.cancel()
            info.agent.telemetry.flush()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._save_session, info)

    def delete_session_from_disk(self, session_id: str) -> bool:
        """Physically delete a session from memory, disk, and every index.

        Steps, in dependency order:

        1. Cancel any pending debounced save.  This must be first: a queued
           write would otherwise re-create the transcript and index row that
           the rest of the method is about to delete.
        2. Remove the record from the active pool, cancelling its run task,
           then from any parked pool if it was not resident in the active one.
        3. Purge the session folder together with every derived store keyed by
           its id (telemetry, rollback, sub-agents, spillover) through
           :func:`encre.lifecycle.purge_session`.  Deleting only the folder
           leaves all four behind, which is how a deleted session otherwise
           keeps ghost data on disk.
        4. Scrub the id from every directory index that carries it, not just
           the owning one, because stale cross-workspace entries would
           resurrect the session in another workspace's sidebar.

        Args:
            session_id: Session to delete.

        Returns:
            ``True`` if the session was found anywhere (memory, disk, or an
            index) and something was removed; ``False`` otherwise.

        Note:
            The method is synchronous and performs unbounded I/O across every
            known sessions directory; call it from a handler path that can
            tolerate that, or wrap it in an executor.
        """
        removed = False
        pending_save = self._save_tasks.pop(session_id, None)
        if pending_save is not None and not pending_save.done():
            pending_save.cancel()

        sessions_dir = ""
        info = self._sessions.pop(session_id, None)
        if info is not None:
            removed = True
            sessions_dir = info.sessions_dir
            if info.agent_task and not info.agent_task.done():
                info.agent_task.cancel()

        if not sessions_dir:
            for sess_dict in self._dir_sessions.values():
                cached = sess_dict.pop(session_id, None)
                if cached is not None:
                    removed = True
                    sessions_dir = cached.sessions_dir
                    if cached.agent_task and not cached.agent_task.done():
                        cached.agent_task.cancel()
                    break

        if not sessions_dir:
            sessions_dir = self._get_sessions_dir()

        from encre.lifecycle import purge_session

        purge_report = purge_session(session_id, sessions_dir=sessions_dir)
        if purge_report.removed:
            removed = True

        active_dir = self._get_sessions_dir()
        had_index = False
        for dir_path in self._all_sessions_dirs():
            index = self._load_index_for_dir(dir_path)
            if session_id not in index:
                continue
            index.pop(session_id, None)
            self._save_index_for_dir(dir_path, index)
            if dir_path == active_dir:
                self._index = index
                self._index_dirty = False
            had_index = True
            removed = True

        self._fire_sessions_changed()
        if had_index:
            removed = True
        if not removed:
            import logging
            logging.getLogger("encre.server.session").warning(
                "delete_session_from_disk: session %s not found anywhere", session_id
            )
        return removed

    # ----- archive / unarchive -----

    def archive_session(self, session_id: str) -> bool:
        """Hide a session from the normal sidebar by flagging it as archived.

        Archiving never touches session data; it only flips a flag that is
        persisted in both the session's ``meta.json`` metadata (so an index
        rebuild restores it) and the index row (so listing paths can filter
        cheaply).  Both copies are needed because they serve different readers
        -- the metadata copy survives index loss, the index copy serves readers
        that never open the transcript.

        Args:
            session_id: Session to archive.

        Returns:
            ``True`` when the session was found and flagged.
        """
        return self._set_archived(session_id, True)

    def unarchive_session(self, session_id: str) -> bool:
        """Restore a previously archived session to the normal listing paths.

        Args:
            session_id: Session to unarchive.

        Returns:
            ``True`` when the session was found and the flag was cleared.
        """
        return self._set_archived(session_id, False)

    def _set_archived(self, session_id: str, archived: bool) -> bool:
        """Core flag flip shared by :meth:`archive_session` and :meth:`unarchive_session`.

        Four write targets are updated so no reader can observe a different
        answer to "is this archived":

        1. The resident record (active or parked pool) and its underlying
           session object, so a later ``_save_session`` flush carries the flag.
        2. The owning directory, resolved through
           :meth:`_find_session_dir_anywhere` for disk-only sessions.
        3. The session's ``meta.json`` metadata, re-encrypted and written
           atomically.
        4. Every directory index that carries the id.

        Args:
            session_id: Session to flag.
            archived: ``True`` to hide the session, ``False`` to restore it.

        Returns:
            ``True`` when at least one authoritative copy was found and
            updated; ``False`` when the session exists nowhere.
        """
        import json as _json

        from encre.crypto import encrypt as _encrypt

        found = False
        sessions_dir = ""
        info = self._sessions.get(session_id)
        if info is None:
            for sess_dict in self._dir_sessions.values():
                info = sess_dict.get(session_id)
                if info is not None:
                    break
        if info is not None:
            found = True
            sessions_dir = info.sessions_dir or self._get_sessions_dir()
            info.metadata["archived"] = archived
            if hasattr(info.agent, "session") and info.agent.session is not None:
                info.agent.session.metadata["archived"] = archived

        if not sessions_dir:
            # The active directory is checked directly first: a disk-only
            # session in the global store is not covered by
            # _find_session_dir_anywhere, which only scans the other trees.
            active_dir = self._get_sessions_dir()
            if (pathlib.Path(active_dir) / session_id).is_dir():
                sessions_dir = active_dir
                found = True
            else:
                resolved = self._find_session_dir_anywhere(session_id, active_dir)
                if resolved is None:
                    return False
                sessions_dir = resolved
                found = True

        # Only rewrite meta.json when the on-disk flag actually differs, which
        # avoids touching the file for repeated archive/unarchive calls.
        dir_path = pathlib.Path(sessions_dir) / session_id
        if dir_path.is_dir():
            meta = EncreSession.read_meta(str(dir_path))
            if meta is not None:
                meta_metadata = meta.get("metadata") or {}
                if bool(meta_metadata.get("archived", False)) != archived:
                    meta_metadata["archived"] = archived
                    meta["metadata"] = meta_metadata
                    payload = _json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
                    # Fail-closed: session metadata is never written plaintext.
                    payload = _encrypt(payload)
                    _atomic_write_text(dir_path / "meta.json", payload)
                found = True

        active_dir = self._get_sessions_dir()
        for dir_path in self._all_sessions_dirs():
            index = self._load_index_for_dir(dir_path)
            if session_id not in index:
                continue
            index[session_id]["archived"] = archived
            # Flag flip has no delete intent: preserve rows a failed load
            # did not return.
            self._save_index_for_dir(dir_path, index, preserve_disk_rows=True)
            if dir_path == active_dir:
                self._index = index
                self._index_dirty = False
        self._fire_sessions_changed()
        return found

    def list_archived_sessions(self) -> list[dict[str, Any]]:
        """Return archived sessions across every sessions directory.

        Mirrors the cross-directory scan used by the sidebar listing but keeps
        only rows flagged ``archived``, whether the session is resident or
        disk-only, sorted by most recent activity first.

        Returned rows use the same shape as :meth:`list_sessions` so the
        archive manager can render them with the same row component; in
        particular the canonical workspace path is mirrored into ``metadata``
        so the owning-workspace icon renders.  A ``seen`` set deduplicates ids
        that legitimately appear in both a resident pool and an on-disk row.

        Returns:
            Archived session summaries, newest activity first.

        Note:
            Orphan disk rows whose session directory does not live in that
            directory are skipped: a session belongs to exactly one workspace,
            and an orphan row would report the wrong owning workspace.
        """
        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _entry(info: SessionInfo | None, idx: dict[str, Any]) -> None:
            """Render one index row into *result*, de-duplicating by session id.

            Shared by both scan passes of :meth:`list_archived_sessions` so the
            row shape stays identical whether the session is resident in memory
            or only on disk.  Rows are appended, never inserted, because the
            caller sorts *result* by ``last_active`` before returning; keeping
            this a pure append avoids interleaving order logic with rendering.

            Args:
                info: Live :class:`SessionInfo` for a resident session, or
                    ``None`` for a disk-only row.  When ``None``, ``state`` is
                    forced to ``"idle"`` -- a parked or evicted session has no
                    live task, and reporting ``"running"`` for it would block
                    idle eviction and mislead the renderer.
                idx: The index row to render, either freshly built by
                    :meth:`_make_index_entry` or loaded from a directory's
                    encrypted ``index.json``.

            Note:
                *seen* is closed over from the enclosing method and is the
                single de-duplication point across both passes.  A session that
                is resident in one pool and also has a row in another
                directory's index is archived exactly once; skipping the
                check would double it in the sidebar.

                The workspace path is resolved from three keys -- the top-level
                ``workspace``, then ``metadata.workspace``, then the alternate
                ``metadata.workspace_path`` spelling -- and mirrored back under
                both ``workspace`` and ``workspace_path``.  Mirroring is what
                makes
                the canonical path visible to consumers of
                :class:`encre.server.app.EncreServer`'s admin endpoints and to
                the renderer, which reads either spelling.
            """
            sid = idx.get("session_id", "")
            if not sid or sid in seen:
                return
            seen.add(sid)
            meta = dict(idx.get("metadata", {}) or {})
            ws = idx.get("workspace") or meta.get("workspace") or meta.get("workspace_path") or ""
            if ws:
                meta["workspace"] = ws
                meta["workspace_path"] = ws
            result.append({
                "session_id": sid,
                "created_at": idx.get("created_at", 0),
                "last_active": idx.get("last_active", 0),
                "state": self.get_session_state(sid) if info is not None else SessionState.IDLE.value,
                "metadata": meta,
                "preview": idx.get("preview", ""),
                "name": idx.get("name", ""),
                "channel": idx.get("channel", "normal"),
                "message_count": idx.get("message_count", 0),
            })

        # Resident archived sessions, across the active and all parked pools.
        # Pooled rather than locked: _entry() reads only, and the seen set
        # makes a concurrent insert harmless (it is either included once or
        # skipped), whereas list_sessions must snapshot because it yields the
        # live records to a caller that may mutate them.
        for pool in [self._sessions, *self._dir_sessions.values()]:
            for sid, info in pool.items():
                if not (info.metadata.get("archived") or (
                    hasattr(info.agent, "session")
                    and info.agent.session
                    and info.agent.session.metadata.get("archived")
                )):
                    continue
                idx = self._make_index_entry(info)
                _entry(info, idx)

        # Disk-only archived rows across every known sessions directory.
        # _load_index_for_dir reads the file directly instead of using the
        # active mirror, so parked-workspace archives still surface here.
        for dir_path in self._all_sessions_dirs():
            index = self._load_index_for_dir(dir_path)
            for sid, entry in index.items():
                if not entry.get("archived"):
                    continue
                if not os.path.isdir(os.path.join(dir_path, sid)):
                    continue
                # Prefer the transcript's real last-message time over the
                # possibly stale index value.
                fallback_active = entry.get("last_active", 0)
                last_active = EncreSession.read_meta_last_active(
                    os.path.join(dir_path, sid), fallback_active
                )
                entry = dict(entry)
                entry["last_active"] = last_active
                entry["metadata"] = {"workspace": entry.get("workspace", "")} if entry.get("workspace") else {}
                _entry(None, entry)

        result.sort(key=lambda e: e.get("last_active", e.get("created_at", 0)), reverse=True)
        return result

    def _all_sessions_dirs(self) -> list[str]:
        """Return every sessions directory that can hold desktop sessions.

        The union is: the active directory, the global directory, the iClaw
        directory, and one directory per registered iWork workspace.  Both
        :meth:`delete_session_from_disk` and :meth:`_set_archived` iterate this
        so a session cannot be resurrected through a directory that still
        carries a stale entry for it.

        Returns:
            De-duplicated list of directory paths, active directory first.
        """
        dirs: list[str] = []
        active = self._get_sessions_dir()
        for d in (active, str(get_data_dir() / "sessions"), str(get_data_dir() / "iclaw" / "sessions")):
            if d not in dirs:
                dirs.append(d)
        try:
            iwork_root = get_data_dir() / "iwork"
            if iwork_root.is_dir():
                for ws_dir in iwork_root.iterdir():
                    if not ws_dir.is_dir():
                        continue
                    d = str(ws_dir / "sessions")
                    if d not in dirs:
                        dirs.append(d)
        except Exception:
            pass
        return dirs

    def rename_session(self, session_id: str, new_name: str, *, manual: bool = True) -> bool:
        """Rename a session by updating its display name in the index.

        The row lives in the session's *own* directory -- the workspace or
        global store it belongs to -- so the write must go there, never into
        the currently active directory.  Writing to the active one would inject
        a foreign entry into a different workspace's sidebar, and the rename
        would appear in the wrong place while the original title remained
        everywhere else.

        For ephemeral sessions the name is updated in memory only: temp chats
        must never appear in any on-disk index, and writing one would make it
        unreachable by the ``temp_chat`` filters in
        :meth:`query_index` and :meth:`list_sessions`, which key off that
        marker rather than off a rename.

        Args:
            session_id: Session to rename.
            new_name: New display title.
            manual: ``True`` for a human-initiated rename, which also sets the
                ``name_manually_renamed`` marker so the auto-namer leaves the
                title alone.  Programmatic renames may pass ``False`` to stay
                overwritable.

        Returns:
            ``True`` when the rename was recorded (index, memory, or session
            metadata); ``False`` when the session could not be located.

        Note:
            The title and the marker are mirrored into both the wrapper
            metadata and the underlying session metadata, because only the
            latter reaches ``meta.json`` on the next save; the wrapper copy
            alone dies with the process.
        """
        target_dir = ""
        info = self._sessions.get(session_id)
        if info is not None:
            target_dir = info.sessions_dir or self._get_sessions_dir()
        else:
            for sess_dict in self._dir_sessions.values():
                cached = sess_dict.get(session_id)
                if cached is not None:
                    target_dir = cached.sessions_dir or self._get_sessions_dir()
                    info = cached
                    break
        if info is not None and (
            info.metadata.get("temp_chat")
            or (
                hasattr(info.agent, "session")
                and info.agent.session
                and info.agent.session.metadata.get("temp_chat")
            )
        ):
            if not manual and info.metadata.get("name_manually_renamed"):
                # A programmatic rename (auto-namer and friends) must never
                # override a title the user set by hand, even for temp chats.
                return True
            info.metadata["name"] = new_name
            if manual:
                info.metadata["name_manually_renamed"] = True
            return True
        if not target_dir:
            active_dir = self._get_sessions_dir()
            if (pathlib.Path(active_dir) / session_id).is_dir():
                target_dir = active_dir
            else:
                resolved = self._find_session_dir_anywhere(session_id, active_dir)
                if resolved is None:
                    return False
                target_dir = resolved
        index = self._load_index_for_dir(target_dir)
        if session_id not in index:
            # Resident but never flushed to a directory index: build a row so
            # the name has somewhere to live on disk.
            if info is None:
                return False
            index[session_id] = self._make_index_entry(info)
        # Structural defense against silent un-renames: a programmatic rename
        # (the background auto-namer being the usual caller) must never
        # replace a title the user set by hand.  The manual marker may live
        # on the persisted index row or on the resident wrapper metadata --
        # either one wins over a ``manual=False`` write.  Returning ``True``
        # (not ``False``) keeps the caller's success path quiet: nothing was
        # written, so no rename broadcast should announce a change.
        if not manual and (
            index[session_id].get("name_manually_renamed")
            or (info is not None and info.metadata.get("name_manually_renamed"))
        ):
            return True
        index[session_id]["name"] = new_name
        # No delete intent: a rename must never destroy rows a failed index
        # load did not return.
        self._save_index_for_dir(target_dir, index, preserve_disk_rows=True)
        if target_dir == self._get_sessions_dir():
            # Merge instead of replace: a load that failed open to {} would
            # otherwise wipe the memory mirror and poison every later flush.
            for sid, row in self._index.items():
                index.setdefault(sid, row)
            self._index = index
            self._index_dirty = False
        if info is not None:
            info.metadata["name"] = new_name
            if manual:
                info.metadata["name_manually_renamed"] = True
            sess = getattr(info.agent, "session", None)
            if sess is not None:
                sess.metadata["name"] = new_name
                if manual:
                    sess.metadata["name_manually_renamed"] = True
        self._fire_sessions_changed()
        return True

    def try_resume_most_recent(self, config: EncreConfig | None = None) -> SessionInfo | None:
        """Resume the most recently active session, or report that none exists.

        Intended for startup: it warms the index, reconciles it with the
        directory listing, picks the session with the greatest ``last_active``
        (falling back to ``created_at``), and materialises it through
        :meth:`load_or_create_session`.

        Args:
            config: Optional configuration handed to the resumed session.

        Returns:
            The resumed :class:`SessionInfo`, or ``None`` when the store is
            empty.

        Note:
            Bootstrap is invoked even when the index already has rows, so a
            partially clobbered index file heals here at startup.
        """
        self._get_sessions_dir()  # ensures index loaded
        self._bootstrap_index_from_disk()
        if not self._index:
            return None

        best_sid = max(
            self._index.keys(),
            key=lambda sid: self._index[sid].get("last_active", self._index[sid].get("created_at", 0)),
        )
        return self.load_or_create_session(best_sid, config=config)

    def touch(self, session_id: str) -> None:
        """Refresh the in-memory activity clock for a session.

        Updates only ``SessionInfo.last_active``, which feeds idle eviction and
        the fallback sort key.  Sidebar ordering deliberately reads the
        session's own last-message time instead, so merely opening a session
        does not reorder the list.

        Args:
            session_id: Session to touch; a no-op when not resident.
        """
        info = self._sessions.get(session_id)
        if info:
            info.last_active = time.time()

    @property
    def active_count(self) -> int:
        """Number of sessions resident in the active pool.

        Reported by :mod:`encre.server.admin`, the transport status endpoints,
        and the iClaw profile as ``active_sessions``.  Counts only the active
        pool, so sessions parked by a workspace switch are excluded; use
        :meth:`iter_sessions` combined with the parked pools when a full count
        is required.
        """
        return len(self._sessions)

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return resident sessions as renderer-ready rows.

        Two filters apply: ephemeral sessions never appear in any listing, and
        archived sessions are hidden here (see
        :meth:`list_archived_sessions`).  The snapshot is taken under the lock
        because the startup resume inserts records from a worker thread.

        The canonical workspace path is mirrored into ``metadata`` as both
        ``workspace`` and ``workspace_path``, with a best-effort disk fallback
        for records whose in-memory metadata lacks it.  That mirror is what
        lets the frontend attribute a still-resident session to exactly one
        workspace; without it, a session from another workspace would be
        included in every workspace filter.

        Rows also expose ``source``, the structured routing origin set by the
        gateway when an adapter forwards a session source; it is ``None`` for
        desktop sessions and drives the platform badge in the renderer.

        Returns:
            Session summary rows, in insertion order of the active pool.
        """
        result: list[dict[str, Any]] = []
        with self._lock:
            infos = list(self._sessions.values())
        for info in infos:
            if hasattr(info.agent, "session") and info.agent.session and info.agent.session.metadata.get("temp_chat"):
                continue
            if info.metadata.get("archived") or (
                hasattr(info.agent, "session")
                and info.agent.session
                and info.agent.session.metadata.get("archived")
            ):
                continue
            msg_count = 0
            preview = ""
            if hasattr(info.agent, "session") and info.agent.session:
                msg_count, preview = info.agent.session.get_summary()
            meta = dict(info.metadata)
            ws = meta.get("workspace") or (
                info.agent.session.metadata.get("workspace")
                if hasattr(info.agent, "session") and info.agent.session
                else None
            )
            if not ws:
                try:
                    sid_dir = self._session_dir_path(info.session_id)
                    if sid_dir.is_dir():
                        _meta = EncreSession.read_meta(str(sid_dir))
                        if isinstance(_meta, dict):
                            ws = (
                                ((_meta.get("metadata", {}) or {}).get("workspace"))
                                or _meta.get("workspace")
                                or ""
                            )
                except Exception:
                    ws = ""
            if ws and "workspace_path" not in meta:
                meta["workspace"] = ws
                meta["workspace_path"] = ws
            result.append({
                "session_id": info.session_id,
                "created_at": info.created_at,
                "last_active": getattr(info.agent.session, "last_message_at", info.agent.session.updated_at) if hasattr(info.agent, "session") and info.agent.session else info.last_active,
                "state": info.state.value,
                "metadata": meta,
                "preview": preview,
                "name": info.metadata.get("name", self._index.get(info.session_id, {}).get("name", "")),
                "channel": (info.agent.session.metadata.get("channel") if hasattr(info.agent, "session") and info.agent.session and info.agent.session.metadata
                            else info.metadata.get("channel", "normal")),
                "source": info.metadata.get("source"),
                "message_count": msg_count,
            })
        return result

    def query_index(self, include_archived: bool = False) -> list[dict[str, Any]]:
        """Return all sessions known from the on-disk index.

        Archived rows are excluded by default; pass ``include_archived=True``
        for the full table (used by the archive manager view).  Ephemeral
        rows and orphan rows -- ones whose session directory does not exist and
        which are not resident in memory -- are dropped, so a stale index can
        never resurrect sessions that were already deleted.

        ``last_active`` is re-read from ``meta.json`` rather than trusted from
        the index, so a displayed timestamp does not get rewritten by every
        index refresh.

        Args:
            include_archived: ``False`` to keep only non-archived sessions.

        Returns:
            Index rows sorted newest-first, with sessions carrying no messages
            removed.  Rows are disk views: ``state`` is always ``"idle"``.
        """
        self._get_sessions_dir()
        self._bootstrap_index_from_disk()
        sessions_dir = self._get_sessions_dir()
        entries: list[dict[str, Any]] = []
        # Snapshot the table under the lock, then drop it: the loop below does
        # blocking per-session meta.json reads, and holding a threading RLock
        # across them would stall every snapshot reader for the whole pass.
        with self._lock:
            index_items = list(self._index.items())
        for sid, entry in index_items:
            if entry.get("temp_chat"):
                continue
            with self._lock:
                in_memory = sid in self._sessions or any(
                    sid in d for d in self._dir_sessions.values()
                )
            if not in_memory and not os.path.isdir(os.path.join(sessions_dir, sid)):
                continue
            fallback_active = entry.get("last_active", 0)
            last_active = EncreSession.read_meta_last_active(
                os.path.join(sessions_dir, sid), fallback_active
            )
            entries.append({
                "session_id": sid,
                "created_at": entry.get("created_at", 0),
                "last_active": last_active,
                "state": SessionState.IDLE.value,
                "metadata": (
                    {"workspace": entry.get("workspace", "")}
                    if entry.get("workspace")
                    else {}
                ),
                "preview": entry.get("preview", ""),
                "name": entry.get("name", ""),
                "channel": entry.get("channel", "normal"),
                "message_count": entry.get("message_count", 0),
                "archived": entry.get("archived", False),
            })
        entries.sort(key=lambda e: e.get("last_active", e.get("created_at", 0)), reverse=True)
        # Archived rows are filtered *after* sorting so the archived slice is
        # still in recency order for the archive manager view.
        if not include_archived:
            entries = [e for e in entries if not e.get("archived")]
        # Only sessions with at least one message count as history: a
        # brand-new empty shell must not surface in "All tasks".  The session
        # is still resolvable by id via load_or_create_session.
        entries = [e for e in entries if (e.get("message_count") or 0) > 0]
        return entries

    async def cleanup_idle(self) -> int:
        """Evict in-memory sessions that have been idle past the timeout.

        Only ``IDLE`` sessions qualify, so a running or approval-waiting
        session is never reclaimed even if it has not been touched recently.
        Each eviction goes through :meth:`remove_session`, which cancels the
        run task and flushes the transcript before dropping the record.

        Args:
            Nothing; the threshold comes from the ``idle_timeout`` constructor
            argument.

        Returns:
            Number of sessions evicted.

        Note:
            Eviction is memory-only; every evicted session remains openable
            from disk via :meth:`load_or_create_session`.
        """
        now = time.time()
        # Two-phase: select first, then evict.  remove_session() is async and
        # mutates _sessions, so awaiting inside the selection loop would raise
        # "dictionary changed size during iteration".
        to_remove: list[str] = []
        for sid, info in self._sessions.items():
            if now - info.last_active > self._idle_timeout and info.state == SessionState.IDLE:
                to_remove.append(sid)
        for sid in to_remove:
            await self.remove_session(sid)
        return len(to_remove)

    async def acquire_slot(self) -> bool:
        """Acquire a run slot, waiting at most 30 seconds.

        Binds concurrent agent executions process-wide.  The bounded wait is
        what makes a saturated server degrade into a clean rejection instead
        of an unbounded queue of queued runs that would never finish.

        Returns:
            ``True`` when a slot was obtained; the caller must later call
            :meth:`release_slot`.  ``False`` when the server is at capacity.
        """
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=30.0)
            return True
        except TimeoutError:
            return False

    def release_slot(self) -> None:
        """Release a previously acquired run slot.

        A ``ValueError`` from an over-release (more releases than acquires) is
        suppressed so an imbalanced caller pair cannot raise out of a run's
        ``finally`` block and hide the real cleanup.  Callers are still
        expected to balance acquire and release.
        """
        with contextlib.suppress(ValueError):
            self._semaphore.release()

    # ----- async session persistence -----

    # Process-wide debounced-save table, keyed by session id.  It is a ClassVar
    # rather than instance state so that a pending write cannot be resurrected
    # by a different code path: delete_session_from_disk pops it before purging,
    # and shutdown drains it before exiting.
    _save_tasks: ClassVar[dict[str, asyncio.Task[None]]] = {}
    # Per-session write lock for the executor-side persistence path. A
    # debounced save cancelled while its executor write is already running
    # cannot abort that write, so an explicit save could otherwise interleave
    # the transcript and index rewrites with it.
    _save_io_locks: ClassVar[dict[str, threading.Lock]] = {}

    async def _save_session_async(self, info: SessionInfo) -> None:
        """Persist a session off the event loop.

        The blocking transcript and index writes are dispatched to the default
        executor so a slow disk or a large transcript cannot stall the agent
        loop or the WebSocket handler sharing it.

        Args:
            info: Session record to persist.

        Note:
            Requires a running event loop.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_session, info)

    def _schedule_save(self, info: SessionInfo) -> None:
        """Schedule a debounced save, coalescing rapid mutations of one session.

        A new request cancels any not-yet-complete task for the same session id
        and starts a fresh timer, so a burst of updates during one turn costs a
        single disk write rather than one write per message.  This is a process
        -wide ClassVar table keyed by session id.

        Args:
            info: Session to persist after the debounce window elapses.
        """
        sid = info.session_id
        existing = self._save_tasks.get(sid)
        if existing and not existing.done():
            existing.cancel()
        self._save_tasks[sid] = asyncio.create_task(self._debounced_save(sid, info))

    async def _cancel_pending_save(self, session_id: str) -> None:
        """Cancel a debounced save for ``session_id``, if one is queued.

        Call this before an explicit save so the debounced write cannot run
        concurrently with it and interleave the transcript/index rewrites.

        Args:
            session_id: Session whose pending debounced save is dropped.
        """
        task = self._save_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _debounced_save(self, sid: str, info: SessionInfo) -> None:
        """Wait out the debounce window, then persist the session once.

        Cancelling the previous task for the same session id resets the timer,
        which is what makes a burst of mutations collapse into one write.  The
        bookkeeping slot is cleared before the write starts, so a cancellation
        delivered after the wait but before the clear cannot leave a stale task
        registered.

        Args:
            sid: Session id used as the table key.
            info: Session record to write.

        Raises:
            Nothing; a superseding save request surfaces here as a
            ``CancelledError`` and is swallowed.
        """
        try:
            await asyncio.sleep(2.0)
            self._save_tasks.pop(sid, None)
            await self._save_session_async(info)
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        """Flush pending saves and persist every session before the process exits.

        Ordering is deliberate:

        1. Drained first are the pending debounced writes, gathered so a save
           that is mid-flight actually completes instead of being abandoned
           with its bookkeeping slot still registered.
        2. Every session is then collected across the active pool and all
           parked pools, so background sessions from other workspaces are
           flushed too and no transcript is lost because the user happened to
           be in a different workspace at shutdown time.
        3. Each run task is cancelled and telemetry flushed before its save,
           so the final write captures the last turn and nothing keeps
           appending after it.

        Args:
            Nothing.

        Note:
            Requires a running event loop, and every session is written
            sequentially through the executor; this method is expected to be
            called once, on teardown.
        """
        pending = [t for t in self._save_tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._save_tasks.clear()

        all_infos: list[SessionInfo] = list(self._sessions.values())
        seen_ids: set[str] = {info.session_id for info in all_infos}
        for sess_dict in self._dir_sessions.values():
            for info in sess_dict.values():
                if info.session_id not in seen_ids:
                    seen_ids.add(info.session_id)
                    all_infos.append(info)

        loop = asyncio.get_running_loop()
        for info in all_infos:
            if info.agent_task and not info.agent_task.done():
                info.agent_task.cancel()
            info.agent.telemetry.flush()
            await loop.run_in_executor(None, self._save_session, info)
