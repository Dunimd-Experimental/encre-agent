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

"""Encre gateway session routing: structured source identity plus persistence.

This module answers one question -- "which agent session does this platform
message belong to?" -- and remembers the answer across restarts.

It provides:

- :class:`SessionSource` -- the structured origin of a message (platform,
  chat_id, chat_type, user, thread, scope).  Adapters build one per inbound
  event; it is the object that flows from
  :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`
  through :class:`encre.gateway.authz.AuthorizationChecker` and into the key
  builder below.
- :func:`build_session_key` -- the single source of truth that turns a
  :class:`SessionSource` into a deterministic key
  (``agent:main:{platform}:{chat_type}:{chat_id}[:{thread_id}][:{user_id}]``).
  Routing identity is derived from *where the message came from*, not from an
  adapter-chosen id, so a conversation keeps its session across adapter
  restarts and across adapter implementations.
- :class:`SessionStore` -- a SQLite-backed map from session_key to the agent
  loop's ``session_id`` (the transcript identity).  It persists the routing
  decision so a follow-up message from the same conversation resumes the same
  agent session.

Design notes:

- Encre's adapters identify their platform by a plain ``name`` string (not a
  :class:`encre.gateway.config.Platform` enum), so :attr:`SessionSource.platform`
  is ``str`` here.
- Platform-specific canonicalization (e.g. WhatsApp JID/LID flip) is deferred;
  the contract leaves room for it via :meth:`SessionSource.canonicalize`.
- :class:`SessionStore` is sync (a single locked connection) -- the inbound
  path is not hot enough to justify aiosqlite, and keeping it sync avoids
  event-loop coupling for a routing lookup.  This is the one gateway surface
  backed by a database file rather than the JSON/JSONL layout used elsewhere.

Public API:
    * :class:`SessionSource`, :func:`build_session_key`, :class:`SessionStore`.
"""

import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from encre.config import get_data_dir

logger = logging.getLogger("encre.gateway.session")

# Default session-key namespace.  A non-default profile namespaces
# multiplexed gateways (``agent:{profile}``).
_DEFAULT_NAMESPACE = "agent:main"


def _session_key_namespace(profile: str | None) -> str:
    """Resolve the session-key namespace for a profile.

    ``None`` (the common case) yields the legacy ``agent:main`` namespace.

    Args:
        profile: The agent profile name, or ``None`` for the default profile.

    Returns:
        The namespace prefix used as the first segment of a session key.
    """
    if profile is None:
        return _DEFAULT_NAMESPACE
    return f"agent:{profile}"


@dataclass
class SessionSource:
    """Structured origin of an inbound message.

    Replaces the flat ``chat_id``/``user_id`` fields on
    :class:`encre.gateway.platforms.base.MessageEvent` with a single routing
    identity.  Used to:

    1. Build a deterministic :func:`build_session_key` for routing.
    2. Persist the routing decision in :class:`SessionStore`.
    3. Project platform origin into the system prompt and the desktop UI.

    ``platform`` is a plain ``str`` because adapters report their own
    ``name`` attribute, which is a string rather than an enum member.  That
    keeps :mod:`encre.gateway.platform_registry` and the adapters decoupled
    from :class:`encre.gateway.config.Platform` at the message boundary.

    ``is_bot`` is intentionally NOT serialized on the wire in v1 -- it stays a
    gateway-side attribute only (see the relay connector contract), because a
    bot flag computed on one host cannot be trusted after crossing the bridge.

    Field defaults are ordered so that adapters can build a source positionally
    with ``(platform, chat_id)`` and fill the rest only when the vendor
    exposes it.  That is why every field beyond ``chat_id`` is optional: most
    platforms never populate the thread, scope, or alternate-id fields at all.
    """

    platform: str
    chat_id: str
    chat_type: str = "dm"  # "dm" | "group" | "channel" | "thread" | "forum"
    chat_name: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    thread_id: str | None = None  # forum topics, Discord/Slack threads
    chat_topic: str | None = None  # channel description (Discord, Slack)
    user_id_alt: str | None = None  # stable alt id (Signal UUID, Feishu union_id)
    chat_id_alt: str | None = None  # alternate chat id (Signal group internal id)
    scope_id: str | None = None  # platform-neutral scope (Discord guild / Slack workspace)
    is_bot: bool = False  # gateway-side only; NOT serialized on the wire
    profile: str | None = None  # key-namespace for multiplexed gateways

    def canonicalize(self) -> "SessionSource":
        """Hook for platform-specific id canonicalization.

        WhatsApp JID/LID aliases are canonicalized here so a reshuffled
        alias form does not split one conversation into two sessions.  Encre
        returns ``self`` unchanged; adapters that need canonicalization can
        override this (or we add a per-platform hook later).

        Returns:
            A source whose ids are in canonical form.  The base
            implementation is the identity function.
        """
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the wire / persistence.

        Always sends the discriminator fields (``platform``/``chat_id``/
        ``chat_type``/``chat_name``/``user_id``/``user_name``/``thread_id``/
        ``chat_topic``); the rest are included only when set.  ``is_bot`` is
        deliberately omitted (gateway-side attribute only).

        Returns:
            A JSON-safe dict.  Omitting unset optional fields keeps the wire
            payload small and lets :meth:`from_dict` distinguish "absent" from
            "empty string" for downstream code.

        Note:
            ``profile`` is read by :meth:`from_dict` but not emitted here, so
            a ``to_dict`` -> ``from_dict`` round trip drops the profile.  Callers
            that need profile to survive serialization must carry it separately.
        """
        d: dict[str, Any] = {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "chat_name": self.chat_name,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "thread_id": self.thread_id,
            "chat_topic": self.chat_topic,
        }
        if self.user_id_alt is not None:
            d["user_id_alt"] = self.user_id_alt
        if self.chat_id_alt is not None:
            d["chat_id_alt"] = self.chat_id_alt
        if self.scope_id is not None:
            d["scope_id"] = self.scope_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionSource":
        """Reconstruct from a wire/persistence dict.

        Unknown keys are ignored (forward-compat).  Missing optional fields
        default to ``None``.

        Args:
            d: A dict as produced by :meth:`to_dict` on a possibly different
                Encre version.

        Returns:
            A rebuilt :class:`SessionSource`.  Ids are coerced to ``str`` so a
            numeric chat id from JSON cannot reach the key builder as an int.
        """
        return cls(
            platform=str(d.get("platform", "")),
            chat_id=str(d.get("chat_id", "")),
            chat_type=str(d.get("chat_type", "dm")),
            chat_name=d.get("chat_name"),
            user_id=d.get("user_id"),
            user_name=d.get("user_name"),
            thread_id=d.get("thread_id"),
            chat_topic=d.get("chat_topic"),
            user_id_alt=d.get("user_id_alt"),
            chat_id_alt=d.get("chat_id_alt"),
            scope_id=d.get("scope_id"),
            profile=d.get("profile"),
        )


def build_session_key(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
    profile: str | None = None,
) -> str:
    """Build a deterministic session key from a message source.

    This is the single source of truth for session-key construction.  Every
    component that needs to talk about "the session this chat is in" -- the
    gateway store, the session manager, the desktop UI, the desktop session
    router -- must go through this function rather than building the string
    itself, because a key-format change would otherwise silently orphan every
    persisted routing row.

    Format: ``{ns}:{platform}:{chat_type}[:{chat_id}][:{thread_id}][:{user_id}]``

    DM rules:
      - DMs include ``chat_id`` when present (each private conversation isolated).
      - ``thread_id`` further differentiates threaded DMs.
      - Without ``chat_id``, falls back to the sender's id (``user_id_alt`` or
        ``user_id``) so DMs without a chat id stay per-user isolated.
      - Without either, a bare per-platform DM sink is used.
    Group/channel rules:
      - ``chat_id`` identifies the parent group/channel.
      - ``user_id``/``user_id_alt`` isolates participants when
        ``group_sessions_per_user`` is enabled.
      - ``thread_id`` differentiates threads within the parent chat.  When
        ``thread_sessions_per_user`` is False (default), threads are *shared*
        across participants -- ``user_id`` is NOT appended, so every user in
        the thread shares one session (the expected UX for forum topics /
        Discord / Slack threads).

    Args:
        source: The message origin.
        group_sessions_per_user: If True, isolate group sessions per user.
        thread_sessions_per_user: If True, isolate thread sessions per user
            (default False = shared threads).
        profile: Optional key-namespace profile (multiplexed gateways).

    Returns:
        A deterministic ``:``-delimited session key.
    """
    ns = _session_key_namespace(profile)
    platform = source.platform

    if source.chat_type == "dm":
        if source.chat_id:
            if source.thread_id:
                return f"{ns}:{platform}:dm:{source.chat_id}:{source.thread_id}"
            return f"{ns}:{platform}:dm:{source.chat_id}"
        # No chat_id -- fall back to the sender before the bare per-platform sink.
        participant = source.user_id_alt or source.user_id
        if participant:
            if source.thread_id:
                return f"{ns}:{platform}:dm:{participant}:{source.thread_id}"
            return f"{ns}:{platform}:dm:{participant}"
        if source.thread_id:
            return f"{ns}:{platform}:dm:{source.thread_id}"
        return f"{ns}:{platform}:dm"

    # group / channel / thread / forum
    participant = source.user_id_alt or source.user_id
    parts: list[str] = [ns, platform, source.chat_type]
    if source.chat_id:
        parts.append(source.chat_id)
    if source.thread_id:
        parts.append(source.thread_id)

    # In threads, default to shared sessions (all participants see the same
    # conversation).  Per-user isolation only applies when explicitly enabled,
    # or when there is no thread (regular group).
    isolate_user = group_sessions_per_user
    if source.thread_id and not thread_sessions_per_user:
        isolate_user = False
    if isolate_user and participant:
        parts.append(str(participant))

    return ":".join(parts)


@dataclass
class _RoutingRow:
    """A row in the session_routing table (for in-memory access).

    Mirrors the SQLite schema one-to-one.  Used only as the return shape of
    :meth:`SessionStore.all_rows`; nothing writes through it.

    ``session_id`` is the agent transcript identity owned by
    :class:`encre.server.session_manager.SessionManager`; every other field is
    a copy of the :class:`SessionSource` that produced the key, kept on the
    row so operators can inspect a routing entry without re-deriving it.
    """

    session_key: str
    session_id: str
    platform: str
    chat_id: str
    chat_type: str
    user_id: str | None
    thread_id: str | None
    scope_id: str | None
    created_at: float
    last_active: float


class SessionStore:
    """SQLite-backed map from session_key to agent session_id.

    Persists the routing decision so a follow-up message from the same
    conversation resumes the existing agent session rather than starting a new
    one.

    The store is process-local (one ``gateway_routing.db`` under the Encre
    data dir) and thread-safe via a single connection guarded by a lock.  The
    agent session itself (the transcript) is owned by
    :class:`encre.server.session_manager.SessionManager`; this store only
    holds the *routing* mapping and delegates creation to a caller-supplied
    ``create_fn`` so it stays decoupled from the session manager.

    Lifecycle: constructed at gateway startup (creates the table) ->
    :meth:`get_or_create` on every inbound message -> :meth:`reset` on
    ``/new`` -> :meth:`close` at shutdown.

    Invariant: every public method acquires ``self._lock`` before touching the
    connection, which is why the connection is opened with
    ``check_same_thread=False`` -- the adapter callbacks arrive on arbitrary
    threads and would otherwise be refused by sqlite.

    Note:
        Every key derivation in this class passes ``profile=None``, so the
        store always uses the default ``agent:main`` namespace regardless of
        :attr:`SessionSource.profile`.  Multiplexed profiles are expected to
        use their own :class:`SessionStore` instance rather than relying on
        the source's profile field.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        """Open (or create) the routing database and ensure its schema exists.

        Resolves the database location first, then installs the lock *before*
        the connection is created so that the ``_ensure_table`` call made at the
        end of this constructor is already covered by the same serialisation
        every later public method relies on. The connection itself is deferred
        to :meth:`_connect`; only the table DDL runs here, because the schema
        is fixed at construction time and never migrated by this class.

        Invariant: on return ``session_routing`` exists and ``_conn`` is still
        ``None``, so the first real query pays the connection cost once on
        demand rather than holding a handle across the adapter-connect window.
        ``_ensure_table`` also commits, so the schema survives a process crash
        between create and first insert.

        Args:
            db_path: SQLite file to use.  Defaults to
                ``<data_dir>/gateway_routing.db``.
        """
        if db_path is None:
            db_path = Path(get_data_dir()) / "gateway_routing.db"
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        """Return the single process-wide SQLite connection, opening it lazily.

        The connection is created on first use rather than in the constructor
        so that merely building a :class:`SessionStore` never touches disk, and
        ``check_same_thread=False`` is required because adapter callbacks arrive
        on arbitrary worker threads -- sqlite would refuse the first cross-thread
        call without it. The lock is what makes that flag safe; nothing ever
        shares the connection outside this store, so it is never closed per call.

        Returns:
            The live :class:`sqlite3.Connection`. Never ``None`` on return.

        Raises:
            sqlite3.Error: If the database file cannot be opened. Not caught
                here because a store that cannot reach its database cannot
                serve any message and should fail loudly at startup.

        Invariant: callers must already hold ``self._lock``; this helper does
            not acquire it itself, since it is also called from
            :meth:`_ensure_table` while the lock is already held.
        """
        if self._conn is None:
            # check_same_thread=False: we guard all access with self._lock.
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        return self._conn

    def _ensure_table(self) -> None:
        """Create the ``session_routing`` table if it does not already exist.

        Runs once from the constructor to bring a fresh or upgraded data
        directory to the schema this store expects. ``IF NOT EXISTS`` makes the
        call idempotent, which is what lets every gateway restart reach this
        line without a separate migration check. The columns mirror
        :class:`_RoutingRow` one to one: the session key is the primary key,
        the session id is the non-null routing target, and the remaining fields
        are a denormalised copy of the :class:`SessionSource` plus the two
        timestamps used for recency tracking.

        The lock is taken here even though no other thread should be active
        during construction, so the invariant "every connection access holds the
        lock" holds from the very first call. The commit is what makes the
        schema survive a crash between create and first insert.

        Raises:
            sqlite3.Error: On DDL failure; propagates so a corrupt database is
                surfaced immediately rather than on the first message.
        """
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_routing (
                    session_key  TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL,
                    platform     TEXT,
                    chat_id      TEXT,
                    chat_type    TEXT,
                    user_id      TEXT,
                    thread_id    TEXT,
                    scope_id     TEXT,
                    created_at   REAL,
                    last_active  REAL
                )
                """,
            )
            # Per-conversation preferences (e.g. the model pinned via /model,
            # or a mode requested via /plan // /spec before the session exists).
            # A separate table keeps preference writes off the routing rows so
            # a preference change never disturbs session continuity.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_prefs (
                    session_key     TEXT PRIMARY KEY,
                    preferred_model TEXT,
                    active_mode     TEXT,
                    updated_at      REAL
                )
                """
            )
            # Conversation-owned session history: when /new drops a routing
            # row (or /resume rebinds one) the displaced session id lands
            # here, so /sessions + /resume can switch back within the same
            # platform+chat scope.  Desktop (normal/iwork) sessions never
            # enter the routing store, so they are unreachable from here.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_history (
                    session_key  TEXT NOT NULL,
                    session_id   TEXT NOT NULL,
                    archived_at  REAL,
                    PRIMARY KEY (session_key, session_id)
                )
                """
            )
            # Migration for stores created before active_mode existed.
            # The ALTER fails with "duplicate column" on fresh stores that
            # already created the table with the column above.
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE session_prefs ADD COLUMN active_mode TEXT")
            # Durable pending-message queue: messages that arrived while
            # their conversation was busy.  The adapter's in-memory queue is
            # the working copy; these rows are what a restart replays.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    adapter     TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    created_at  REAL,
                    payload     TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_messages_lookup "
                "ON pending_messages (adapter, session_key)"
            )
            conn.commit()

    # -- Durable pending-message queue --

    def add_pending_message(self, adapter: str, session_key: str, payload: dict) -> None:
        """Append one queued event to the durable pending queue.

        Args:
            adapter: Registry name of the adapter the event arrived on.
            session_key: The conversation key the event waits under.
            payload: JSON-serializable event snapshot built by the adapter.
        """
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO pending_messages (adapter, session_key, created_at, payload) VALUES (?, ?, ?, ?)",
                (adapter, session_key, time.time(), json.dumps(payload)),
            )
            conn.commit()

    def pop_pending_messages(self, adapter: str, session_key: str) -> list[dict]:
        """Take (and delete) every queued event waiting for one conversation.

        Pop semantics keep the durable queue aligned with the adapter's
        in-memory drain: rows are removed when their events are taken for
        processing, so a replay after a crash can never duplicate a turn.

        Args:
            adapter: Registry name of the adapter.
            session_key: The conversation key whose rows to take.

        Returns:
            The queued payload dicts, oldest first; empty when none.
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "SELECT id, payload FROM pending_messages WHERE adapter = ? AND session_key = ? ORDER BY id",
                (adapter, session_key),
            )
            rows = cur.fetchall()
            if not rows:
                return []
            conn.execute(
                "DELETE FROM pending_messages WHERE adapter = ? AND session_key = ?",
                (adapter, session_key),
            )
            conn.commit()
        payloads = []
        for _row_id, raw in rows:
            try:
                payloads.append(json.loads(raw))
            except (TypeError, ValueError):
                logger.warning("Dropped a malformed pending message row for %s", session_key)
        return payloads

    def pop_all_pending(self, adapter: str) -> list[tuple[str, dict]]:
        """Take (and delete) every queued event of one adapter, any conversation.

        Used by the adapter's startup replay: all rows are claimed up front
        so the replay is idempotent across crashes.

        Args:
            adapter: Registry name of the adapter.

        Returns:
            ``(session_key, payload)`` tuples, oldest first; empty when none.
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "SELECT id, session_key, payload FROM pending_messages WHERE adapter = ? ORDER BY id",
                (adapter,),
            )
            rows = cur.fetchall()
            if not rows:
                return []
            conn.execute("DELETE FROM pending_messages WHERE adapter = ?", (adapter,))
            conn.commit()
        entries = []
        for _row_id, session_key, raw in rows:
            try:
                entries.append((session_key, json.loads(raw)))
            except (TypeError, ValueError):
                logger.warning("Dropped a malformed pending message row for %s", session_key)
        return entries

    def replace_pending(self, adapter: str, session_key: str, payloads: list[dict]) -> None:
        """Rewrite the durable queue for one conversation with an ordered list.

        The in-memory backlog mutates in place (append, remove, reorder), so
        mirroring it with append-only inserts would drift.  This replaces the
        whole row set for one ``(adapter, session_key)`` pair atomically: the
        caller passes the full ordered backlog and the table ends up matching
        it exactly.

        Args:
            adapter: Registry name of the owning surface (an adapter name or
                a synthetic one such as ``"router"``/``"desktop"``).
            session_key: The conversation key whose rows to rewrite.
            payloads: The full ordered backlog; JSON-serializable dicts.
        """
        with self._lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM pending_messages WHERE adapter = ? AND session_key = ?",
                (adapter, session_key),
            )
            now = time.time()
            for payload in payloads:
                conn.execute(
                    "INSERT INTO pending_messages (adapter, session_key, created_at, payload) VALUES (?, ?, ?, ?)",
                    (adapter, session_key, now, json.dumps(payload)),
                )
            conn.commit()

    # -- Per-conversation preferences --

    def get_preferred_model(self, session_key: str) -> str | None:
        """Return the model pinned for this conversation, or None.

        Args:
            session_key: The stable conversation key from
                :func:`build_session_key`.

        Returns:
            The stored model id, or ``None`` when no preference is set (the
            normal auto-selection path applies).

        Note:
            A stored id may reference a model that has since been disabled or
            removed; callers must validate it against the live config before
            use rather than trusting the row blindly.
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "SELECT preferred_model FROM session_prefs WHERE session_key = ?",
                (session_key,),
            )
            row = cur.fetchone()
            if row is None or not row[0]:
                return None
            return str(row[0])

    def set_preferred_model(self, session_key: str, model_id: str | None) -> None:
        """Pin (or clear) the preferred model for one conversation.

        ``INSERT OR REPLACE`` keeps the upsert a single statement; passing
        ``None`` for *model_id* deletes the row so the conversation returns to
        automatic model selection instead of carrying a tombstone.

        Args:
            session_key: The stable conversation key.
            model_id: The model id to pin, or ``None`` to clear the pin.

        Raises:
            sqlite3.Error: Propagated; a failing preference write means the
                ``/model`` reply must report failure rather than pretend
                success.
        """
        with self._lock:
            conn = self._connect()
            if model_id is None:
                # Clear only this preference; other columns (active_mode)
                # belong to independent settings and must survive.
                conn.execute(
                    "UPDATE session_prefs SET preferred_model = NULL WHERE session_key = ?",
                    (session_key,),
                )
            else:
                # UPSERT so a row that already carries an active_mode keeps it.
                conn.execute(
                    """
                    INSERT INTO session_prefs
                        (session_key, preferred_model, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_key) DO UPDATE SET
                        preferred_model = excluded.preferred_model,
                        updated_at = excluded.updated_at
                    """,
                    (session_key, model_id, time.time()),
                )
            conn.commit()

    def get_active_mode(self, session_key: str) -> str | None:
        """Return the slash-command mode pending for this conversation, or None.

        A pending mode is one requested via ``/plan`` or ``/spec`` while the
        conversation had no live agent session yet; the gateway applies and
        clears it right before the next run starts.

        Args:
            session_key: The stable conversation key from
                :func:`build_session_key`.

        Returns:
            ``"plan"``, ``"spec"``, or ``None`` when nothing is pending.
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "SELECT active_mode FROM session_prefs WHERE session_key = ?",
                (session_key,),
            )
            row = cur.fetchone()
            if row is None or not row[0]:
                return None
            return str(row[0])

    def set_active_mode(self, session_key: str, mode: str | None) -> None:
        """Store (or clear) the pending slash-command mode for one conversation.

        Args:
            session_key: The stable conversation key.
            mode: ``"plan"``/``"spec"`` to pend, or ``None`` to clear.
        """
        with self._lock:
            conn = self._connect()
            if mode is None:
                conn.execute(
                    "UPDATE session_prefs SET active_mode = NULL WHERE session_key = ?",
                    (session_key,),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO session_prefs
                        (session_key, active_mode, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_key) DO UPDATE SET
                        active_mode = excluded.active_mode,
                        updated_at = excluded.updated_at
                    """,
                    (session_key, mode, time.time()),
                )
            conn.commit()

    def get(self, source: SessionSource) -> str | None:
        """Look up the agent session_id for a source, or None if unmapped.

        Touches ``last_active`` on hit so the routing row reflects recent use.

        Args:
            source: The message origin to look up.

        Returns:
            The existing agent session id, or ``None`` when this conversation
            has no routing row yet (first message from this chat).
        """
        key = build_session_key(source)
        now = time.time()
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "SELECT session_id FROM session_routing WHERE session_key = ?",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE session_routing SET last_active = ? WHERE session_key = ?",
                (now, key),
            )
            conn.commit()
            return str(row[0])

    def get_or_create(
        self,
        source: SessionSource,
        create_fn: Callable[[], str],
    ) -> str:
        """Return the session_id for ``source``, creating one if unmapped.

        ``create_fn`` is called only on a miss and must return a fresh agent
        session_id (typically ``SessionManager.create_session(...).session_id``).
        This keeps the store decoupled from the session manager.

        Args:
            source: The message origin.
            create_fn: Zero-argument callable returning a new session id.

        Returns:
            The session id, existing or newly created.

        Note:
            The create and the insert are not atomic with the lookup.  Two
            concurrent callers for the same new conversation can both invoke
            ``create_fn`` and the second insert replaces the first row; the
            discarded session id is orphaned but never used, because the caller
            that lost the race keeps its own returned value.  The gateway
            serializes inbound handling per adapter, so this is not reachable
            in practice.
        """
        existing = self.get(source)
        if existing is not None:
            return existing
        session_id = create_fn()
        if not session_id:
            logger.warning("[session-store] create_fn returned empty session_id for %s", source.platform)
        self.put(source, session_id)
        return session_id

    def put(self, source: SessionSource, session_id: str) -> None:
        """Insert or replace the routing mapping for ``source``.

        Args:
            source: The message origin that produced the row's copied fields.
            session_id: The agent transcript id to bind to the key.

        Note:
            ``INSERT OR REPLACE`` is what makes a re-derived key heal itself:
            if a key was previously bound to a now-deleted session, this call
            silently rebinds it instead of raising a primary-key conflict.
        """
        key = build_session_key(source)
        now = time.time()
        with self._lock:
            conn = self._connect()
            prev = conn.execute(
                "SELECT session_id FROM session_routing WHERE session_key = ?",
                (key,),
            ).fetchone()
            conn.execute(
                """
                INSERT OR REPLACE INTO session_routing
                    (session_key, session_id, platform, chat_id, chat_type,
                     user_id, thread_id, scope_id, created_at, last_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    session_id,
                    source.platform,
                    source.chat_id,
                    source.chat_type,
                    source.user_id,
                    source.thread_id,
                    source.scope_id,
                    now,
                    now,
                ),
            )
            if prev is not None and str(prev[0]) != session_id:
                self._archive(conn, key, str(prev[0]), now)
            conn.commit()

    _HISTORY_CAP = 20

    @classmethod
    def _archive(cls, conn: sqlite3.Connection, key: str, session_id: str, when: float) -> None:
        """Record a displaced session id under *key* and cap the per-key list.

        Called with the store lock already held and the surrounding write
        uncommitted, so archiving and the routing change land atomically.
        """
        conn.execute(
            "INSERT OR REPLACE INTO session_history (session_key, session_id, archived_at) VALUES (?, ?, ?)",
            (key, session_id, when),
        )
        conn.execute(
            """
            DELETE FROM session_history
            WHERE session_key = ? AND session_id NOT IN (
                SELECT session_id FROM session_history
                WHERE session_key = ?
                ORDER BY archived_at DESC LIMIT ?
            )
            """,
            (key, key, cls._HISTORY_CAP),
        )

    def touch(self, source: SessionSource) -> None:
        """Update ``last_active`` for the source's row (no-op if unmapped).

        Args:
            source: The message origin to refresh.

        Note:
            Unlike :meth:`get`, this does not return the session id and does
            not create a row.  It is for callers that already know the routing
            entry exists and only need the activity timestamp updated, such as
            after a completed agent turn.
        """
        key = build_session_key(source)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE session_routing SET last_active = ? WHERE session_key = ?",
                (time.time(), key),
            )
            conn.commit()

    def reset(self, source: SessionSource) -> None:
        """Drop the routing mapping for ``source`` (e.g. on ``/new``).

        Args:
            source: The message origin whose row should be deleted.

        Note:
            Only the routing row is removed.  The agent session itself is not
            deleted, so ``/new`` produces a fresh transcript for the next
            message rather than destroying history.  A missing row is not an
            error.
        """
        key = build_session_key(source)
        with self._lock:
            conn = self._connect()
            prev = conn.execute(
                "SELECT session_id FROM session_routing WHERE session_key = ?",
                (key,),
            ).fetchone()
            if prev is not None:
                self._archive(conn, key, str(prev[0]), time.time())
            conn.execute(
                "DELETE FROM session_routing WHERE session_key = ?",
                (key,),
            )
            conn.commit()

    def sessions_for(self, source: SessionSource) -> list[str]:
        """Every session id this conversation may switch between.

        Current routing row first, then archived ids newest-first.  The list
        is derived from the source's session key, so it is strictly scoped to
        this platform+chat (+user/thread): desktop normal/iwork sessions and
        other conversations' sessions can never appear, and a conversation
        that never ran ``/new`` legitimately has exactly one entry.
        """
        key = build_session_key(source)
        with self._lock:
            conn = self._connect()
            current = conn.execute(
                "SELECT session_id FROM session_routing WHERE session_key = ?",
                (key,),
            ).fetchone()
            hist = conn.execute(
                "SELECT session_id FROM session_history WHERE session_key = ?"
                " ORDER BY archived_at DESC",
                (key,),
            ).fetchall()
        ids: list[str] = []
        if current is not None:
            ids.append(str(current[0]))
        for (sid,) in hist:
            if str(sid) not in ids:
                ids.append(str(sid))
        return ids

    def all_rows(self) -> list[_RoutingRow]:
        """Return every routing row (for status/debug).

        Returns:
            All rows, most recently active first.  Nullable columns are
            coerced to safe defaults so a partially written row cannot surface
            as ``None`` where a string is expected.
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                SELECT session_key, session_id, platform, chat_id, chat_type,
                       user_id, thread_id, scope_id, created_at, last_active
                FROM session_routing
                ORDER BY last_active DESC
                """
            )
            return [
                _RoutingRow(
                    session_key=str(r[0]),
                    session_id=str(r[1]),
                    platform=str(r[2]) if r[2] is not None else "",
                    chat_id=str(r[3]) if r[3] is not None else "",
                    chat_type=str(r[4]) if r[4] is not None else "dm",
                    user_id=r[5],
                    thread_id=r[6],
                    scope_id=r[7],
                    created_at=float(r[8]) if r[8] is not None else 0.0,
                    last_active=float(r[9]) if r[9] is not None else 0.0,
                )
                for r in cur.fetchall()
            ]

    def close(self) -> None:
        """Release the SQLite connection (idempotent).

        Safe to call more than once and safe to call on a store whose
        connection was never opened.  After this call the store must not be
        used; the runner only calls it during shutdown.
        """
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
