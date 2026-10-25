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

"""Typed wire model for the Encre client protocol.

This module is the framing layer of the Python ``encre`` package: it defines the
shape of every JSON frame exchanged between a client (the Electron + React 19
desktop renderer, or a headless driver such as
:mod:`encre.headless`) and the agent runtime reached through
:class:`encre.transport.ws.EncreWSHandler`.  The handler parses an incoming
WebSocket frame with :func:`parse_client_message` into a ``Client*``
dataclass, looks the result up in
:attr:`encre.transport.ws.EncreWSHandler._DISPATCH` to find the owning mixin
method from :mod:`encre.protocol.handlers`, and serialises the reply with
:func:`encode_server_message` (or one of the :func:`encode_*` helpers below)
back through the same connection.  :mod:`encre.transport.http.openai_api`
reuses the server-side encoders
reuses the client-side types for its driver-facing surface.

The protocol contract:

- Every frame is a flat JSON object whose ``type`` field is a discriminator.
  Client frames carry the names in :data:`ClientMessageType`; server frames
  carry the names in :data:`ServerMessageType`.
- Frames are exchanged bidirectionally as UTF-8 text.  The transport may be
  AES-256-GCM encrypted at the frame level (see
  :func:`_parse_client_encrypted` and :func:`encode_server_encrypted`); when
  encryption is in play the wire text is base64 ciphertext instead of JSON.
- Version negotiation happens through an optional ``protocol_version`` field
  that either side may include.  Clients that omit it are treated as current
  (backwards compatible); a client declaring a newer version than
  :data:`PROTOCOL_VERSION` is accepted with a logged warning, because unknown
  fields are ignored by the older peer rather than rejected.
- Unknown or malformed frames are dropped rather than raising.  This module
  never raises on untrusted input: :func:`parse_client_message` returns
  ``None`` for anything it cannot decode, and the handler decides how to
  report the failure.
- Fields default to empty values (``""``, ``0``, ``False``, ``None``) so a
  sender may omit anything it does not set.  Receivers that need a real
  distinction coerce explicitly in ``from_dict`` (e.g. ``bool(...)``,
  ``str(...) or None``); fields left as raw ``d.get`` calls are trusted
  because they were produced by our own renderers.
- Server encoders are best-effort broadcast helpers.  The single send path in
  :meth:`encre.protocol.handlers.base.HandlerBase._send` wraps every encode
  call in a try/except and logs the failure, so one malformed frame cannot
  interrupt the rest of a broadcast fan-out.

Public API:

- :data:`PROTOCOL_VERSION` -- the version this server speaks.
- :data:`ClientMessageType`, :data:`ServerMessageType` -- the frame-name
  vocabularies, used as ``Literal`` annotations for the dispatch tables.
- ``Client*`` dataclasses -- one per inbound frame, each with a
  :meth:`from_dict` classmethod that is tolerant of missing keys.
- :data:`ClientMessage` -- the union of every inbound type.
- :func:`parse_client_message` -- the single entry point from wire text to a
  typed message.
- :func:`encode_server_message` and the ``encode_*`` helpers -- the outbound
  serialisers.
- :func:`_parse_client_encrypted` and :func:`encode_server_encrypted` -- the
  frame-level crypto wrappers.  Transport encryption is mandatory: no server
  frame is ever emitted as plaintext.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from encre.crypto import decrypt, encrypt

# The protocol revision this server speaks.  Both sides may announce the value
# in a ``protocol_version`` JSON field; the handler latches the first value it
# sees so later frames in the same connection do not re-negotiate.
PROTOCOL_VERSION = 1


# ── Frame-level transport encryption ─────────────────────────────────────────

def _parse_client_encrypted(
    raw: str | bytes,
    *,
    require_encryption: bool = False,
) -> str | bytes | None:
    """Recover the JSON text of an inbound frame, decrypting if required.

    The transport is opportunistic while the handshake is in progress: a frame
    is either a base64 AES-256-GCM ciphertext produced by
    :func:`encre.crypto.encrypt`, or plain JSON.  Both are accepted on the same
    connection because a client's crypto capability is only known after its
    first frame is decoded.

    Once the connection's first frame has proven encryption
    (``require_encryption=True``), plaintext and undecryptable frames are
    rejected instead of being parsed.  Otherwise the "encryption is mandatory"
    handshake could be bypassed by simply sending plaintext on the second
    frame.

    The plaintext probe is ``str.strip().startswith("{")`` -- a JSON object
    always opens with that character.  base64 ciphertext opens with a random
    alphabet entry, so a collision with ``"{"`` costs one frame that the JSON
    decoder then rejects; it never turns a real message into the wrong shape.

    Args:
        raw: The frame as delivered by the WebSocket layer.  ``str`` for text
            frames, ``bytes`` for binary frames.

    Returns:
        The plaintext JSON text when decryption succeeded, the decoded input
        when it was already plaintext, the original ``raw`` unchanged when it is
        empty or contains no valid UTF-8, and ``None`` when the input is ``None``.
    """
    if not raw:
        return raw
    try:
        decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError:
        # Undecodable bytes are handed back untouched so the caller can log the
        # frame size and drop it, instead of losing the failure here.
        return raw

    # A JSON frame always opens with "{", so that character after stripping is
    # treated as plaintext.  base64 ciphertext is nonce (12) + ciphertext (>=1)
    # + tag (16) = at least 29 bytes, and its first character is a random base64
    # alphabet entry; it matches "{" about once in 64.  When it does, the frame
    # is misread as plaintext and the JSON decoder downstream rejects it, which
    # the handler reports rather than silently dropping.
    stripped = decoded.strip()
    if not stripped:
        return decoded
    if stripped.startswith("{"):
        if require_encryption:
            raise ValueError("plaintext frame rejected after encrypted handshake")
        return decoded

    try:
        return decrypt(stripped)
    except Exception:
        # A decrypt failure means the frame was not ciphertext after all (or the
        # key rotated mid-connection).  Return the text and let the JSON decoder
        # downstream decide; it will raise and the handler will report the frame.
        if require_encryption:
            raise
        return decoded


def serialize_server_message(msg_type: str, **kwargs: Any) -> str:
    """Serialise one server frame to plaintext JSON.

    Kept separate from encryption so the producer can snapshot the payload
    cheaply and hand it to the connection's writer task, which encrypts and
    writes it.  The returned string never leaves the transport layer.

    Args:
        msg_type: The value written to the ``type`` field.
        **kwargs: Payload fields merged into the same object.

    Returns:
        The JSON text of the frame.
    """
    return json.dumps({"type": msg_type, **kwargs}, ensure_ascii=False)


def encode_server_encrypted(msg_type: str, **kwargs: Any) -> str:
    """Serialise and AES-256-GCM-encrypt one server frame.

    Args:
        msg_type: The value written to the ``type`` field.
        **kwargs: Payload fields merged into the same object.

    Returns:
        A base64 ciphertext string ready to be sent as a WebSocket text frame.
    """
    return encrypt(serialize_server_message(msg_type, **kwargs))


# ── Client -> Server message types ───────────────────────────────────────────

ClientMessageType = Literal[
    "run", "respond_permission", "cancel", "resume", "configure", "ping",
    "list_models", "list_sessions", "new_session",
    "get_config", "update_providers", "set_active_model", "delete_model",
    "fetch_models",
    "update_skills", "install_skill", "uninstall_skill", "update_skill", "get_skill_detail",
    "get_skill_file",
    "update_mcp", "update_agent",
    "search",
    "rollback_log", "rollback_checkout",
    "validate_model",
    "edit_message", "delete_message",
    "delete_session", "export_session", "rename_session",
    "archive_session", "unarchive_session", "list_archived_sessions",
    "agent_create", "agent_delete", "agent_update", "agent_list", "agent_set_active",
    "update_sub_agents",
    "get_memory_list",
    "get_memory_detail",
    "get_global_rules",
    "get_global_rule_content",
    "save_global_rule",
    "delete_global_rule",
    "get_profile",
    "get_gitignore",
    "set_gitignore",
    "terminal_spawn",
    "terminal_write",
    "terminal_resize",
    "terminal_kill",
    "terminal_list_shells",
    "retry",
    "switch_branch",
    "rollback",
    "get_usage_stats",
    "replay_get_session",
    "automation_list_jobs",
    "automation_create_job",
    "automation_cancel_job",
    "automation_get_history",
    "steer",
    "spec_approve",
    "spec_reject",
    "plan_approve",
    "plan_reject",
    "set_mode",
    "browser_cdp_url",
    "export_data",
    "import_data",
    "plugin_install",
    "plugin_uninstall",
    "market_install",
]


@dataclass
class ClientSteer:
    """Inject an instruction into a turn that is already running.

    Unlike :class:`ClientRun`, this does not open a new turn.  The instruction
    is pushed into the steering queue on the agent's loop and is picked up
    before the next model call, so it can redirect an in-flight run without
    cancelling and resubmitting it.  Emitted by the chat composer when the send
    button is pressed again while a turn is active.

    Attributes:
        session_id: The turn to steer.  ``None`` lets the handler resolve the
            currently active session of this connection.
        prompt: The instruction text injected into the running loop.
    """

    type: str = "steer"
    session_id: str | None = None
    prompt: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSteer":
        """Build a :class:`ClientSteer` from decoded wire JSON."""
        return cls(
            type="steer",
            session_id=d.get("session_id"),
            prompt=d.get("prompt", "") or "",
        )


@dataclass
class ClientSpecApprove:
    """Approve the spec currently on the table.

    The spec mode (:class:`ClientSetMode` with ``mode="spec"``) makes the agent
    describe the change before implementing it.  The handler blocks on this
    frame; receiving an approval releases the gate and lets the run continue
    into implementation.  Consumed by
    :class:`~encre.protocol.handlers.permissions.PermissionHandlers`.
    """

    type: str = "spec_approve"
    session_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSpecApprove":
        """Build a :class:`ClientSpecApprove` from decoded wire JSON."""
        return cls(type="spec_approve", session_id=d.get("session_id"))


@dataclass
class ClientSpecReject:
    """Reject the spec on the table and send it back for revision.

    The ``feedback`` text is folded into the session so the agent can revise
    the spec instead of guessing, and the gate keeps blocking rather than
    releasing.
    """

    type: str = "spec_reject"
    session_id: str | None = None
    feedback: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSpecReject":
        """Build a :class:`ClientSpecReject` from decoded wire JSON."""
        return cls(
            type="spec_reject",
            session_id=d.get("session_id"),
            feedback=d.get("feedback", "") or "",
        )


@dataclass
class ClientPlanApprove:
    """Approve the execution plan currently under review.

    The plan gate is distinct from the spec gate: it covers the step-by-step
    work plan the agent produced for a multi-step task, and it is addressed by
    :attr:`review_id` rather than by session, because one session can have
    several overlapping plan reviews.
    """

    type: str = "plan_approve"
    session_id: str | None = None
    review_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPlanApprove":
        """Build a :class:`ClientPlanApprove` from decoded wire JSON."""
        return cls(
            type="plan_approve",
            session_id=d.get("session_id"),
            review_id=d.get("review_id", "") or "",
        )


@dataclass
class ClientPlanReject:
    """Reject the plan under review, requesting a revision with feedback."""

    type: str = "plan_reject"
    session_id: str | None = None
    review_id: str = ""
    feedback: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPlanReject":
        """Build a :class:`ClientPlanReject` from decoded wire JSON."""
        return cls(
            type="plan_reject",
            session_id=d.get("session_id"),
            review_id=d.get("review_id", "") or "",
            feedback=d.get("feedback", "") or "",
        )


@dataclass
class ClientRun:
    """Submit a prompt to the agent; the primary user action.

    Starts one turn of the agent loop.  The handler creates or reuses the
    target session, runs the turn through the Encre Agent Core agent loop, and streams
    the resulting ``text_delta`` / ``tool_call_*`` frames back to this
    connection.  Consumed by :class:`~encre.protocol.handlers.run.RunHandlers`.

    Attributes:
        session_id: Target session.  ``None`` means "create a fresh session"
            and the reply carries the new id in ``session_ready``.
        specialty: Named persona variant; selects which of the configured
            agents is bound to the turn.
        mode: Slash-command mode for this turn only (``"plan"`` or ``"spec"``).
            Distinct from the persisted mode set by :class:`ClientSetMode`.
        mode_prompt: Extra prompt body appended to the selected mode for this
            single turn, so a run can carry a one-off instruction without
            touching stored settings.
        channel: Route selector.  ``"iclaw"`` sends the run through the iClaw
            bridge instead of the local agent loop.
        temp_chat: Mark the session as ephemeral; ephemeral sessions are never
            written to ``data/sessions/``, so a run here leaves no trace on disk.
    """

    type: str = "run"
    prompt: str = ""
    system_prompt: str | None = None
    session_id: str | None = None
    specialty: str = "general"
    attachments: list[dict[str, Any]] | None = None
    mode: str | None = None
    mode_prompt: str | None = None
    channel: str | None = None  # "iclaw" when in iClaw mode
    temp_chat: bool = False  # True = ephemeral, never persist to disk

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRun":
        """Build a :class:`ClientRun` from decoded wire JSON."""
        return cls(
            type="run",
            prompt=d.get("prompt", ""),
            system_prompt=d.get("system_prompt"),
            session_id=d.get("session_id"),
            specialty=d.get("specialty", "general"),
            attachments=d.get("attachments"),
            mode=d.get("mode"),
            mode_prompt=d.get("mode_prompt"),
            channel=d.get("channel"),
            temp_chat=bool(d.get("temp_chat", False)),
        )


@dataclass
class ClientQueueList:
    """Request one conversation's queued prompts (queue panel refresh).

    Attributes:
        session_id: The conversation whose queue to report.
    """

    type: str = "queue_list"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientQueueList":
        """Build a :class:`ClientQueueList` from decoded wire JSON."""
        return cls(type="queue_list", session_id=d.get("session_id", ""))


@dataclass
class ClientQueuePop:
    """Pop the head of one conversation's queue and run it immediately.

    Backs the queue panel's "send now" action.

    Attributes:
        session_id: The conversation whose queue head to take.
    """

    type: str = "queue_pop"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientQueuePop":
        """Build a :class:`ClientQueuePop` from decoded wire JSON."""
        return cls(type="queue_pop", session_id=d.get("session_id", ""))


@dataclass
class ClientQueueRemove:
    """Delete one queued prompt by index.

    Attributes:
        session_id: The conversation whose queue to edit.
        index: Zero-based position of the entry to drop.
    """

    type: str = "queue_remove"
    session_id: str = ""
    index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientQueueRemove":
        """Build a :class:`ClientQueueRemove` from decoded wire JSON."""
        return cls(type="queue_remove", session_id=d.get("session_id", ""),
                   index=int(d.get("index", 0)))


@dataclass
class ClientQueueMove:
    """Reorder one queued prompt (drag-and-drop support).

    Attributes:
        session_id: The conversation whose queue to edit.
        from_index: Zero-based position of the entry to relocate.
        to_index: Target position; clamped into range server-side.
    """

    type: str = "queue_move"
    session_id: str = ""
    from_index: int = 0
    to_index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientQueueMove":
        """Build a :class:`ClientQueueMove` from decoded wire JSON."""
        return cls(type="queue_move", session_id=d.get("session_id", ""),
                   from_index=int(d.get("from_index", 0)),
                   to_index=int(d.get("to_index", 0)))


@dataclass
class ClientQueueSteer:
    """Turn a queued prompt into a mid-run steer instruction.

    Backs the queue panel's "guide" action: the entry is popped from the
    queue and pushed into the running loop's steer queue so it lands as
    the next model call's context injection rather than as a new turn.
    When no loop is live the entry is dropped but the queue is still
    rebroadcast so the panel stays in sync.

    Attributes:
        session_id: The conversation whose queue entry to guide.
        index: Zero-based position of the entry to consume.
    """

    type: str = "queue_steer"
    session_id: str = ""
    index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientQueueSteer":
        """Build a :class:`ClientQueueSteer` from decoded wire JSON."""
        return cls(type="queue_steer", session_id=d.get("session_id", ""),
                   index=int(d.get("index", 0)))


@dataclass
class ClientRespondPermission:
    """Answer a permission request the agent raised while running a tool.

    The handler pairs this with the outstanding ``permission_request`` frame
    and resumes the blocked tool call.  ``tool_name`` addresses the request and
    ``decision`` carries the boolean answer.

    Attributes:
        remember: When true the decision is persisted to the session's policy
            table so the same tool is never asked again in this session.
            Without it the choice applies to this single call only.
    """

    type: str = "respond_permission"
    tool_name: str = ""
    decision: bool = False
    # When True the decision is persisted into the session policy table so
    # the same tool is not asked again during this session ("always allow").
    remember: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRespondPermission":
        """Build a :class:`ClientRespondPermission` from decoded wire JSON.

        The decision is coerced with ``bool`` because a renderer may send a
        string such as ``"true"`` and a non-empty string is truthy.
        """
        return cls(
            type="respond_permission",
            tool_name=d.get("tool_name", ""),
            decision=d.get("decision", False),
            remember=bool(d.get("remember", False)),
        )


@dataclass
class ClientRespondPlan:
    """Answer a plan approval request by proposal id.

    ``proposal_id`` addresses the outstanding proposal and ``approved`` carries
    the decision, so a client can acknowledge an older proposal after a newer
    one has already superseded it without creating ambiguity.
    """

    type: str = "respond_plan"
    proposal_id: str = ""
    approved: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRespondPlan":
        """Build a :class:`ClientRespondPlan` from decoded wire JSON."""
        return cls(
            type="respond_plan",
            proposal_id=str(d.get("proposal_id", "")),
            approved=bool(d.get("approved", False)),
        )


@dataclass
class ClientPluginConfirm:
    """User decision on a pending third-party plugin (dynamic authorisation).

    Sent by the frontend after the activation-confirmation popup.  ``name``
    identifies the pending plugin; ``approved`` selects confirm (import +
    activate) versus deny (drop the inert shell — the plugin's code is
    never imported).
    """

    type: str = "plugin_confirm"
    name: str = ""
    approved: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPluginConfirm":
        """Build a :class:`ClientPluginConfirm` from decoded wire JSON."""
        return cls(
            type="plugin_confirm",
            name=str(d.get("name", "")),
            approved=bool(d.get("approved", False)),
        )


@dataclass
class ClientSetPlanMode:
    """Toggle the session-level plan gate.

    Legacy boolean form of :class:`ClientSetMode`.  The handler maps
    ``active`` onto the unified mode string (``"plan"`` or ``""``) and routes
    it through the same entry point, so config, the session metadata mirror,
    and the ``mode_changed`` / ``plan_mode_changed`` broadcasts all agree
    instead of drifting.  ``reason`` is carried for the wire but is not read
    by the handler.
    """

    type: str = "set_plan_mode"
    active: bool = False
    reason: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetPlanMode":
        """Build a :class:`ClientSetPlanMode` from decoded wire JSON."""
        return cls(
            type="set_plan_mode",
            active=bool(d.get("active", False)),
            reason=str(d.get("reason", "")),
        )


@dataclass
class ClientSetMode:
    """Set or clear the persistent slash-command mode (plan/spec).

    Unlike ``ClientRun.mode`` which is per-run, this persists the mode
    in session metadata so it stays active across multiple runs.
    The frontend sends this when the user clicks the mode chip's X button
    or activates a mode via shortcut.
    """

    type: str = "set_mode"
    session_id: str | None = None
    mode: str = ""  # "plan", "spec", or "" to clear

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetMode":
        """Build a :class:`ClientSetMode` from decoded wire JSON."""
        return cls(
            type="set_mode",
            session_id=str(d.get("session_id", "")) or None,
            mode=str(d.get("mode", "")),
        )


@dataclass
class ClientExportData:
    """Request a full plaintext zip of the entire data directory.

    The server builds the archive on disk and returns its path (plus progress
    via ``data_export_progress`` events) through a ``data_exported_zip`` event.
    ``request_id`` lets the caller correlate the response with the original
    request.
    """

    type: str = "export_data"
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientExportData":
        """Build a :class:`ClientExportData` from decoded wire JSON."""
        return cls(type="export_data", request_id=str(d.get("request_id", "")))


@dataclass
class ClientImportData:
    """Import a plaintext backup zip into this machine's data directory.

    ``zip_path`` is a local filesystem path to the archive produced by
    :func:`encre.migration.export_all` (the desktop renderer passes the path
    the user picked, avoiding a large base64 round-trip over the WebSocket).
    The server re-encrypts with this machine's key and reports via
    ``data_import_progress`` events, then ``data_import_done``.

    ``mode`` controls how imported files interact with data already on this
    machine: ``"overwrite"`` (merge, default), ``"replace"`` (wipe the data dir
    first for a faithful clone) or ``"skip"`` (never overwrite existing files).
    """

    type: str = "import_data"
    zip_path: str = ""
    mode: str = "overwrite"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientImportData":
        """Build a :class:`ClientImportData` from decoded wire JSON."""
        return cls(
            type="import_data",
            zip_path=str(d.get("zip_path", "")),
            mode=str(d.get("mode", "overwrite")),
        )


@dataclass
class ClientSetCdpUrl:
    """Point the session's browser tools at a running DevTools instance.

    The agent's browser skill drives a Chromium page over the Chrome DevTools
    Protocol.  Instead of spawning its own browser, it can attach to one the
    Encre Agent Desktop already started and expose here, which keeps cookies and login
    state alive across agent runs.
    """

    type: str = "browser_cdp_url"
    url: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetCdpUrl":
        """Build a :class:`ClientSetCdpUrl` from decoded wire JSON."""
        return cls(
            type="browser_cdp_url",
            url=str(d.get("url", "")),
            session_id=str(d.get("session_id", "")),
        )


@dataclass
class ClientSetCommand:
    """Set or clear the persistent slash *command* (a sticky prompt injection).

    A command is NOT a mode: it re-injects its prompt body every turn while
    active but triggers no tool interception and no spec gate.  Unlike
    ``ClientRun.mode_prompt`` (one-shot, per-run), this persists the command
    in session metadata so it stays active across runs until cleared.
    The frontend sends this when the user activates a command chip (built-in
    action or user-defined).  An empty ``name`` clears the active command.
    """

    type: str = "set_command"
    session_id: str | None = None
    name: str = ""
    prompt: str = ""  # rendered prompt body ({{args}} already substituted)
    icon: str = ""
    title: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetCommand":
        """Build a :class:`ClientSetCommand` from decoded wire JSON."""
        return cls(
            type="set_command",
            session_id=str(d.get("session_id", "")) or None,
            name=str(d.get("name", "")),
            prompt=str(d.get("prompt", "")),
            icon=str(d.get("icon", "")),
            title=str(d.get("title", "")),
        )


@dataclass
class ClientRespondQuestion:
    """Answer a question an interactive tool asked on its behalf.

    Pairs with a tool call that used the ask-question behaviour: the tool is
    suspended with an outstanding question, and this frame supplies the user's
    answer, keyed by the ``tool_call_id`` the renderer displayed alongside the
    prompt.  ``answers`` is a plain string for a single question, or a list of
    strings (one entry per question, in the order the questions were asked)
    when the tool asked several questions at once.
    """

    type: str = "respond_question"
    tool_call_id: str = ""
    answers: str | list[str] = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRespondQuestion":
        """Build a :class:`ClientRespondQuestion` from decoded wire JSON."""
        raw = d.get("answers", "")
        if isinstance(raw, list):
            answers: str | list[str] = [str(a) for a in raw]
        else:
            answers = str(raw)
        return cls(
            type="respond_question",
            tool_call_id=str(d.get("tool_call_id", "")),
            answers=answers,
        )


@dataclass
class ClientEngineInstallResponse:
    """User decision for a pending runtime-engine install prompt.

    The handler waits for this frame before attempting to install an optional
    engine dependency, so the install never happens without consent.  ``choice``
    is the selected option string rather than a boolean because the prompt can
    offer more than two outcomes.
    """

    type: str = "engine_install_response"
    request_id: str = ""
    choice: str = "cancelled"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientEngineInstallResponse":
        """Build a :class:`ClientEngineInstallResponse` from decoded wire JSON."""
        return cls(
            type="engine_install_response",
            request_id=d.get("request_id", ""),
            choice=d.get("choice", "cancelled"),
        )


@dataclass
class ClientCancel:
    """Cancel the turn currently running in a session.

    The handler marks the run as cancelled; the agent loop observes the flag at
    its next checkpoint and stops, so the in-flight tool call finishes cleanly
    rather than being killed mid-write.  An empty ``session_id`` addresses the
    connection's current session.
    """

    type: str = "cancel"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientCancel":
        """Build a :class:`ClientCancel` from decoded wire JSON."""
        return cls(
            type="cancel",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientToolStop:
    """Stop a single running tool without cancelling the whole turn.

    Narrower than :class:`ClientCancel`: the surrounding turn keeps running and
    the agent continues past the aborted tool call, which is what a user wants
    when one long-running command hangs but the rest of the plan is sound.
    """

    type: str = "tool_stop"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientToolStop":
        """Build a :class:`ClientToolStop` from decoded wire JSON."""
        return cls(
            type="tool_stop",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientResume:
    """Resume a session that was suspended waiting for user input.

    The handler re-enters the session at the point where it parked (after a
    permission prompt, a question, or a plan/spec gate) and continues the run.
    ``request_id`` correlates the resume with the request that parked the
    session, which lets the renderer discard a stale resume.
    """

    type: str = "resume"
    session_id: str = ""
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientResume":
        """Build a :class:`ClientResume` from decoded wire JSON."""
        return cls(
            type="resume",
            session_id=d.get("session_id", ""),
            request_id=d.get("request_id", ""),
        )


@dataclass
class ClientConfigure:
    """Push a settings update to the running server.

    ``config`` is a partial patch, not a full settings dump: the handler routes
    each key to its real sink (live session config, runtime tool state, gateway
    adapter configs, persistence).  Empty-string and ``None`` values are
    skipped, so a partially filled form cannot zero out working settings.
    One persistence call runs at the end, so a half-applied patch never
    reaches disk.
    """

    type: str = "configure"
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientConfigure":
        """Build a :class:`ClientConfigure` from decoded wire JSON."""
        return cls(
            type="configure",
            config=d.get("config", {}),
        )


@dataclass
class ClientWechatScan:
    """Request the QR-code login flow for a scan-login platform adapter.

    Covers every adapter implementing the QR handshake (``weixin`` via iLink,
    ``qqbot`` via the official bind-task flow).  The handler asks the adapter
    for a fresh code and relays it as a ``wechat_scan_result`` frame.  A QR
    URL can be fetched without a live, fully connected adapter, so if no
    matching instance is running the handler builds a temporary one from
    stored config.  Every failure mode replies with ``success=False`` rather
    than raising, so the login dialog always gets a usable answer.

    ``adapter_id`` selects the target platform; it defaults to ``weixin`` for
    backward compatibility with older clients.
    """

    type: str = "wechat_scan"
    adapter_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientWechatScan":
        """Build a :class:`ClientWechatScan` from decoded wire JSON."""
        return cls(
            type="wechat_scan",
            adapter_id=d.get("adapter_id", "weixin"),
        )


@dataclass
class ClientPing:
    """Application-level keepalive probe.

    Independent of the WebSocket protocol's own ping/pong, because some proxies
    terminate at the transport level and never forward them.  The handler
    replies with ``pong`` so a client can measure round-trip latency.
    """

    type: str = "ping"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientPing":
        """Build a :class:`ClientPing` from decoded wire JSON."""
        return cls(type="ping")


@dataclass
class ClientListModels:
    """Request the configured model list.

    The handler answers with ``models_list`` so a renderer that connected
    without a cached model list can repopulate its picker from the server's
    view rather than guessing.
    """

    type: str = "list_models"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListModels":
        """Build a :class:`ClientListModels` from decoded wire JSON."""
        return cls(type="list_models")


@dataclass
class ClientListSessions:
    """Request the session list for the current channel view.

    The reply carries a ``channel`` field (``"normal"`` or ``"iwork"``) so the
    renderer knows which view it is drawing; use
    :class:`ClientListAllSessions` when both channels are needed at once.
    """

    type: str = "list_sessions"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListSessions":
        """Build a :class:`ClientListSessions` from decoded wire JSON."""
        return cls(type="list_sessions")


@dataclass
class ClientListAllSessions:
    """List sessions across ALL channels (normal + iwork), unfiltered.

    Used by the tray popup which needs both mode's sessions at once.
    Returns a ``sessions_all`` event with ``{normal: [...], iwork: [...]}``.
    """

    type: str = "list_all_sessions"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListAllSessions":
        """Build a :class:`ClientListAllSessions` from decoded wire JSON."""
        return cls(type="list_all_sessions")


@dataclass
class ClientNewSession:
    """Create an empty session without running anything in it.

    The reply ``session_ready`` frame carries the new id.  ``request_id`` is
    echoed back so the caller can tell which creation produced it when several
    are in flight.
    """

    type: str = "new_session"
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientNewSession":
        """Build a :class:`ClientNewSession` from decoded wire JSON."""
        return cls(type="new_session", request_id=d.get("request_id", ""))


@dataclass
class ClientGetConfig:
    """Request the effective runtime configuration.

    The handler builds ``config_data`` on a worker thread because it does disk
    and file-system work, and falls back to serialising the live config object
    if the builder fails.  The reply also carries ``available_skills``, the
    workspace mode and path, and the per-session files directory, so the
    settings panel wires all of its views up in a single round trip rather
    than issuing one request per field.
    """

    type: str = "get_config"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetConfig":
        """Build a :class:`ClientGetConfig` from decoded wire JSON."""
        return cls(type="get_config")


@dataclass
class ClientUpdateProviders:
    """Replace the model provider list wholesale.

    ``providers`` is a full replacement of the provider table rather than a
    diff, because providers are cheap and a merge would have to decide how to
    reconcile removed entries.  ``active_model_index`` names the flattened
    index into the concatenated model lists that the next run should use.
    """

    type: str = "update_providers"
    # Full provider list (canonical replace, same semantics as update_mcp):
    # each entry = {backend_type, api_key, base_url, models: [{model_id, ...}]}.
    providers: list[dict[str, Any]] = field(default_factory=list)
    active_model_index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateProviders":
        """Build a :class:`ClientUpdateProviders` from decoded wire JSON."""
        return cls(
            type="update_providers",
            providers=d.get("providers", []),
            active_model_index=d.get("active_model_index", 0),
        )


@dataclass
class ClientSetActiveModel:
    """Switch which model the next run uses, by flattened index."""

    type: str = "set_active_model"
    model_index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetActiveModel":
        """Build a :class:`ClientSetActiveModel` from decoded wire JSON."""
        return cls(
            type="set_active_model",
            model_index=d.get("model_index", 0),
        )


@dataclass
class ClientDeleteModel:
    """Remove a model from the flattened provider table by index.

    Indexes are positional over the concatenated model lists, so a deletion
    shifts every later index; the reply ``models_updated`` frame re-sends the
    whole table so a client never has to compute the shift itself.
    """

    type: str = "delete_model"
    model_index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteModel":
        """Build a :class:`ClientDeleteModel` from decoded wire JSON."""
        return cls(
            type="delete_model",
            model_index=d.get("model_index", 0),
        )


@dataclass
class ClientFetchModels:
    """Probe a model backend for its advertised model list.

    Used by the settings panel to populate a provider before the user has added
    any models by hand.  ``headers`` carries extra authentication such as a
    bearer token or an organisation header that some gateways require.
    """

    type: str = "fetch_models"
    backend_type: str = ""
    api_key: str = ""
    base_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientFetchModels":
        """Build a :class:`ClientFetchModels` from decoded wire JSON."""
        return cls(
            type="fetch_models",
            backend_type=d.get("backend_type", ""),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()}
            if isinstance(d.get("headers"), dict) else {},
        )


@dataclass
class ClientUninstallSkill:
    """Remove a user-installed skill by name.

    The reply ``skill_uninstalled`` frame carries the surviving availability
    table so the renderer can redraw without a second round trip.
    """

    type: str = "uninstall_skill"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUninstallSkill":
        """Build a :class:`ClientUninstallSkill` from decoded wire JSON."""
        return cls(
            type="uninstall_skill",
            name=d.get("name", ""),
        )


@dataclass
class ClientUpdateSkill:
    """Replace the contents of an installed skill by name."""

    type: str = "update_skill"
    name: str = ""
    content: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateSkill":
        """Build a :class:`ClientUpdateSkill` from decoded wire JSON."""
        return cls(
            type="update_skill",
            name=d.get("name", ""),
            content=d.get("content", ""),
        )


@dataclass
class ClientGetSkillDetail:
    """Read one skill's own description and ``SKILL.md`` body by name.

    The market's plugin page lists the skills a package provides and opens
    their bodies on demand; the boot-time ``available_skills`` catalogue
    deliberately omits bundled and managed skills, so this is the path that
    reaches them.  One request per skill, answered once.
    """

    type: str = "get_skill_detail"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetSkillDetail":
        """Build a :class:`ClientGetSkillDetail` from decoded wire JSON."""
        return cls(type="get_skill_detail", name=d.get("name", ""))


@dataclass
class ClientGetSkillFile:
    """Read one file that lives inside a skill's own directory.

    A ``SKILL.md`` points at sibling documents (``references/x.md``, scripts,
    templates) and those are what the skill actually consists of, so the
    market's reader follows them.  ``path`` is always relative to the skill's
    directory; the handler resolves and confines it before reading.
    """

    type: str = "get_skill_file"
    name: str = ""
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetSkillFile":
        """Build a :class:`ClientGetSkillFile` from decoded wire JSON."""
        return cls(type="get_skill_file", name=d.get("name", ""), path=d.get("path", ""))


@dataclass
class ClientUpdateSkills:
    """Enable or disable skills for the agent.

    ``enabled_skills`` is the set the user wants active; skills absent from it
    are skipped at load time.  The handler answers with ``skills_updated``
    carrying both the resulting enabled set and the availability table.
    """

    type: str = "update_skills"
    enabled_skills: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateSkills":
        """Build a :class:`ClientUpdateSkills` from decoded wire JSON."""
        return cls(
            type="update_skills",
            enabled_skills=d.get("enabled_skills", []),
        )


@dataclass
class ClientInstallSkill:
    """Install a skill from inline content or from a file path.

    ``content`` is the skill body itself (for example a ``SKILL.md`` document)
    and ``file_path`` is an alternative source to read from disk instead of
    carrying the text over the wire.  The handler writes into the user skill
    directory and answers with ``skill_installed`` on success or
    ``skill_install_error`` on failure.
    """

    type: str = "install_skill"
    name: str = ""
    content: str = ""
    file_path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientInstallSkill":
        """Build a :class:`ClientInstallSkill` from decoded wire JSON."""
        return cls(
            type="install_skill",
            name=d.get("name", ""),
            content=d.get("content", ""),
            file_path=d.get("file_path", ""),
        )


@dataclass
class ClientPluginInstall:
    """Install a plugin package (base64 zip) into the user plugins directory.

    The zip must contain an ``encre-plugin.json`` manifest and/or a
    ``plugin.py`` at its root.  After extraction the shared registry is
    re-scanned so the new plugin is discovered (and lazily activated like
    any other); every client then receives a fresh lifecycle snapshot.
    """
    type: str = "plugin_install"
    data_b64: str = ""
    filename: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPluginInstall":
        """Build a :class:`ClientPluginInstall` from decoded wire JSON."""
        return cls(
            type="plugin_install",
            data_b64=d.get("data_b64", ""),
            filename=d.get("filename", ""),
        )


@dataclass
class ClientPluginUninstall:
    """Uninstall an installed plugin by name (removes its directory)."""
    type: str = "plugin_uninstall"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPluginUninstall":
        """Build a :class:`ClientPluginUninstall` from decoded wire JSON."""
        return cls(
            type="plugin_uninstall",
            name=d.get("name", ""),
        )


@dataclass
class ClientPluginEnable:
    """Enable a disabled installed plugin (re-activates it live)."""
    type: str = "plugin_enable"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPluginEnable":
        """Build a :class:`ClientPluginEnable` from decoded wire JSON."""
        return cls(
            type="plugin_enable",
            name=d.get("name", ""),
        )


@dataclass
class ClientPluginDisable:
    """Disable an installed plugin (persisted in installed.json)."""
    type: str = "plugin_disable"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPluginDisable":
        """Build a :class:`ClientPluginDisable` from decoded wire JSON."""
        return cls(
            type="plugin_disable",
            name=d.get("name", ""),
        )


@dataclass
class ClientMarketInstall:
    """Install a discovered plugin by name through the market.

    The package already ships with the app (bundled ``ea_tools`` tree) or
    lives in a plugin root; installing means writing its ledger entry in
    ``installed.json`` and running the dynamic-authorisation confirm path
    so its code is imported and activated.  Replies ``market_installed``
    on success or ``market_install_error`` on failure.
    """
    type: str = "market_install"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientMarketInstall":
        """Build a :class:`ClientMarketInstall` from decoded wire JSON."""
        return cls(
            type="market_install",
            name=d.get("name", ""),
        )


@dataclass
class ClientPluginInstallMCP:
    """Install an MCP server *as a plugin* (unified plugin pipeline).

    Generates a declarative plugin package under the user plugins root
    whose manifest declares ``contributes.mcp_servers``, then runs the
    regular plugin install flow (discovery + ledger + broadcast).
    """
    type: str = "plugin_install_mcp"
    name: str = ""
    server: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.server is None:
            self.server = {}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPluginInstallMCP":
        """Build a :class:`ClientPluginInstallMCP` from decoded wire JSON."""
        server = d.get("server") or d.get("config") or {}
        return cls(
            type="plugin_install_mcp",
            name=d.get("name", ""),
            server=server if isinstance(server, dict) else {},
        )


@dataclass
class ClientPluginInstallAgent:
    """Install a sub-agent *as a plugin* (unified plugin pipeline).

    Generates a declarative plugin package under the user plugins root
    whose manifest declares ``contributes.agents``, then runs the regular
    plugin install flow (discovery + ledger + broadcast).
    """
    type: str = "plugin_install_agent"
    agent: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.agent is None:
            self.agent = {}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientPluginInstallAgent":
        """Build a :class:`ClientPluginInstallAgent` from decoded wire JSON."""
        agent = d.get("agent") or d.get("definition") or {}
        return cls(
            type="plugin_install_agent",
            agent=agent if isinstance(agent, dict) else {},
        )


@dataclass
class ClientUpdateMCP:
    """Replace the MCP server configuration wholesale.

    ``mcp_servers`` is a canonical replacement rather than a diff, so a client
    can always send the full table it has in memory.  Accepting both shapes
    keeps older renderers working: a list of server entries and a map keyed by
    server name are both understood.
    """

    type: str = "update_mcp"
    # Accept either list[dict] (old format) or dict[str, dict] (standard map format)
    mcp_servers: Any = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateMCP":
        """Build a :class:`ClientUpdateMCP` from decoded wire JSON."""
        return cls(
            type="update_mcp",
            mcp_servers=d.get("mcp_servers", []),
        )


@dataclass
class ClientUpdateAgent:
    """Update the base agent configuration.

    Applies to the default agent rather than to a named sub-agent; use
    :class:`ClientAgentCreate` / :class:`ClientAgentUpdate` for those.  Any
    field left at its default value is left unchanged by the handler, so a
    partial payload is safe to send.
    """

    type: str = "update_agent"
    system_prompt: str = ""
    specialty: str = "general"
    permission_mode: str = ""
    max_turns: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateAgent":
        """Build a :class:`ClientUpdateAgent` from decoded wire JSON."""
        return cls(
            type="update_agent",
            system_prompt=d.get("system_prompt", ""),
            specialty=d.get("specialty", "general"),
            permission_mode=d.get("permission_mode", ""),
            max_turns=d.get("max_turns", 0),
        )


@dataclass
class ClientSearch:
    """Search conversation history across sessions.

    ``seq`` is echoed back unchanged in the ``search_results`` reply.  The
    desktop search box fires a request per keystroke, so the client discards
    any response whose ``seq`` trails the one it is waiting on; without the
    echo a slow older search could overwrite the results of a newer one.
    """

    type: str = "search"
    query: str = ""
    seq: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSearch":
        """Build a :class:`ClientSearch` from decoded wire JSON."""
        return cls(
            type="search",
            query=d.get("query", ""),
            seq=d.get("seq", 0),
        )


@dataclass
class ClientRollbackLog:
    """Request the list of rollback checkpoints available in a session."""

    type: str = "rollback_log"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRollbackLog":
        """Build a :class:`ClientRollbackLog` from decoded wire JSON."""
        return cls(
            type="rollback_log",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientRollbackCheckout:
    """Move a session back to a recorded checkpoint.

    The handler rewrites the session history to the state captured at
    ``commit_hash`` and answers with ``rollback_checkout`` carrying the
    resulting message list so the renderer can redraw without reloading.
    """

    type: str = "rollback_checkout"
    session_id: str = ""
    commit_hash: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRollbackCheckout":
        """Build a :class:`ClientRollbackCheckout` from decoded wire JSON."""
        return cls(
            type="rollback_checkout",
            session_id=d.get("session_id", ""),
            commit_hash=d.get("commit_hash", ""),
        )


@dataclass
class ClientValidateModel:
    """Probe whether a candidate model configuration is usable.

    The handler issues a minimal request to the backend with the supplied
    credentials and reports either ``model_validated`` or
    ``model_validation_error``.  ``model_index`` of ``-1`` means "add this as a
    new model" rather than "edit an existing one", so the same frame serves both
    the test-connection button and the save flow.

    Attributes:
        probe_reasoning: When true, the probe also asks the model to reason so
            the capability of the reasoning toggle is verified, not just
            connectivity.
        thinking_config: Reasoning parameters sent with the probe request.  The
            default enables extended thinking so a backend that rejects it is
            caught before the model is added.
    """

    type: str = "validate_model"
    backend_type: str = ""
    api_key: str = ""
    base_url: str = ""
    model_id: str = ""
    max_tokens: int = 4096
    name: str = ""
    # Index of the model being edited; -1 (default) means "add new".
    model_index: int = -1
    multimodal: bool = False
    # Custom HTTP headers required by gateways (merged into every probe).
    headers: dict[str, str] = field(default_factory=dict)
    # Run the live reasoning-level probe (capability toggle is on).
    probe_reasoning: bool = False
    thinking_config: dict[str, Any] | None = field(default_factory=lambda: {"type": "enabled", "enabled": True})
    # Caller-supplied correlation id echoed back on model_validated /
    # model_validation_error.  Model ids are NOT unique across providers
    # (two providers may both list "gpt-4o"), so responses are routed by
    # this id, falling back to model_id for legacy callers.
    request_id: str = ""
    # Per-model sampling overrides from the settings sub-page.  The probes
    # stamp them onto their backends so relays that force-require these
    # fields don't fail a perfectly usable model during "Save & Test".
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientValidateModel":
        """Build a :class:`ClientValidateModel` from decoded wire JSON."""
        return cls(
            type="validate_model",
            backend_type=d.get("backend_type", ""),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            model_id=d.get("model_id", ""),
            max_tokens=d.get("max_tokens", 4096),
            name=d.get("name", ""),
            model_index=d.get("model_index", -1),
            multimodal=d.get("multimodal", False),
            headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()}
            if isinstance(d.get("headers"), dict) else {},
            probe_reasoning=bool(d.get("probe_reasoning", False)),
            thinking_config=d.get("thinking_config") if d.get("thinking_config") is not None else {"type": "enabled", "enabled": True},
            request_id=str(d.get("request_id", "") or ""),
            temperature=float(d["temperature"]) if d.get("temperature") is not None else None,
            top_p=float(d["top_p"]) if d.get("top_p") is not None else None,
            top_k=int(d["top_k"]) if d.get("top_k") is not None else None,
        )


@dataclass
class ClientEditMessage:
    """Replace the content of an earlier message in a session.

    ``message_index`` is the position of the message to rewrite; the handler
    records the edit and answers with ``messages_updated`` so every connected
    renderer of that session redraws consistently.

    ``message_id`` is the server message id of the target and takes priority
    over ``message_index``: ids are stable across compaction, sibling
    branches and synthetic-message injection, while a frontend-computed
    index can misalign with the raw stored list and edit the wrong turn.
    """

    type: str = "edit_message"
    message_index: int = 0
    new_content: str = ""
    session_id: str = ""
    message_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientEditMessage":
        """Build a :class:`ClientEditMessage` from decoded wire JSON."""
        return cls(
            type="edit_message",
            message_index=d.get("message_index", 0),
            new_content=d.get("new_content", ""),
            session_id=d.get("session_id", ""),
            message_id=d.get("message_id", ""),
        )


@dataclass
class ClientDeleteMessage:
    """Remove one message from a session by index."""

    type: str = "delete_message"
    message_index: int = 0
    session_id: str = ""
    #: Optional server message id of the target. When present it takes
    #: priority over ``message_index``: ids are stable across compaction and
    #: synthetic-message injection, while a frontend-computed index can
    #: misalign with the raw stored list and delete the wrong turn.
    message_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteMessage":
        """Build a :class:`ClientDeleteMessage` from decoded wire JSON."""
        return cls(
            type="delete_message",
            message_index=d.get("message_index", 0),
            session_id=d.get("session_id", ""),
            message_id=d.get("message_id", ""),
        )


@dataclass
class ClientDeleteSession:
    """Delete a session and its on-disk state.

    The session record is removed from ``data/sessions/``, so this is
    irreversible.  The reply ``session_deleted`` frame lets other renderers
    drop the session from their lists.
    """

    type: str = "delete_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteSession":
        """Build a :class:`ClientDeleteSession` from decoded wire JSON."""
        return cls(
            type="delete_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientClearAllSessions:
    """Delete every session in every store (settings → storage danger zone).

    Irreversible. The reply is one ``sessions_cleared`` frame plus a fresh
    ``session_ready`` for a new empty conversation; the sidebar is refreshed
    by the normal sessions-list broadcast.
    """

    type: str = "clear_all_sessions"
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientClearAllSessions":
        """Build a :class:`ClientClearAllSessions` from decoded wire JSON."""
        return cls(
            type="clear_all_sessions",
            request_id=d.get("request_id", ""),
        )


@dataclass
class ClientExportSession:
    """Render one session to markdown for sharing or archiving."""

    type: str = "export_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientExportSession":
        """Build a :class:`ClientExportSession` from decoded wire JSON."""
        return cls(
            type="export_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientExportSessionsBatch:
    """Render several sessions to a single archive in one round trip.

    The handler answers with ``sessions_exported_zip`` carrying the archive as
    base64, so the renderer can trigger a download without opening each session
    individually.
    """

    type: str = "export_sessions_batch"
    session_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientExportSessionsBatch":
        """Build a :class:`ClientExportSessionsBatch` from decoded wire JSON."""
        return cls(
            type="export_sessions_batch",
            session_ids=d.get("session_ids", []),
        )


@dataclass
class ClientRenameSession:
    """Rename a session; the new title is stored in the session metadata."""

    type: str = "rename_session"
    session_id: str = ""
    new_name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRenameSession":
        """Build a :class:`ClientRenameSession` from decoded wire JSON."""
        return cls(
            type="rename_session",
            session_id=d.get("session_id", ""),
            new_name=d.get("new_name", ""),
        )


@dataclass
class ClientArchiveSession:
    """Archive a session: hide it from the normal sidebar (data untouched)."""

    type: str = "archive_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientArchiveSession":
        """Build a :class:`ClientArchiveSession` from decoded wire JSON."""
        return cls(
            type="archive_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientUnarchiveSession:
    """Restore an archived session back into the normal session lists."""

    type: str = "unarchive_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUnarchiveSession":
        """Build a :class:`ClientUnarchiveSession` from decoded wire JSON."""
        return cls(
            type="unarchive_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientListArchivedSessions:
    """Request the list of archived sessions for the archive manager view."""

    type: str = "list_archived_sessions"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListArchivedSessions":
        """Build a :class:`ClientListArchivedSessions` from decoded wire JSON."""
        return cls(type="list_archived_sessions")


@dataclass
class ClientIclawResume:
    """Resume the most recent iClaw session for this connection.

    iClaw conversations belong to the gateway's
    :class:`~encre.protocol.session_router.EventRouter` manager rather than the
    desktop session manager, so the resume runs inside the router's iClaw
    context to be indexed against the right store.  It replies with
    ``session_ready`` carrying the transcript; when there is nothing to resume
    it sends nothing and the client keeps its empty state.
    """

    type: str = "iclaw_resume"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientIclawResume":
        """Build a :class:`ClientIclawResume` from decoded wire JSON."""
        return cls(type="iclaw_resume")


@dataclass
class ClientAgentCreate:
    """Register a new named agent (persona) in the agent table.

    ``agent`` is the full agent definition; the handler assigns it a position
    and answers with ``agents_updated`` so the renderer can place it.
    """

    type: str = "agent_create"
    agent: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentCreate":
        """Build a :class:`ClientAgentCreate` from decoded wire JSON."""
        return cls(type="agent_create", agent=d.get("agent", {}))


@dataclass
class ClientAgentDelete:
    """Delete a named agent by its position in the agent table."""

    type: str = "agent_delete"
    index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentDelete":
        """Build a :class:`ClientAgentDelete` from decoded wire JSON."""
        return cls(type="agent_delete", index=d.get("index", 0))


@dataclass
class ClientAgentUpdate:
    """Replace the definition of a named agent at ``index``."""

    type: str = "agent_update"
    index: int = 0
    agent: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentUpdate":
        """Build a :class:`ClientAgentUpdate` from decoded wire JSON."""
        return cls(type="agent_update", index=d.get("index", 0), agent=d.get("agent", {}))


@dataclass
class ClientAgentList:
    """Request the current agent table."""

    type: str = "agent_list"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientAgentList":
        """Build a :class:`ClientAgentList` from decoded wire JSON."""
        return cls(type="agent_list")


@dataclass
class ClientAgentSetActive:
    """Select which agent the next run uses.

    ``index`` is the agent's position; ``-1`` clears the selection and falls
    back to the default base agent.
    """

    type: str = "agent_set_active"
    index: int = -1

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentSetActive":
        """Build a :class:`ClientAgentSetActive` from decoded wire JSON."""
        return cls(type="agent_set_active", index=d.get("index", -1))


@dataclass
class ClientUpdateSubAgents:
    """Replace the set of sub-agents available to the active agent."""

    type: str = "update_sub_agents"
    sub_agents: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateSubAgents":
        """Build a :class:`ClientUpdateSubAgents` from decoded wire JSON."""
        return cls(
            type="update_sub_agents",
            sub_agents=d.get("agents", d.get("sub_agents", [])),
        )


@dataclass
class ClientOpenWorkspace:
    """Register a directory as the active workspace for this connection.

    The workspace scopes the agent's file operations and its own configuration
    overlay, so switching workspaces changes which files the agent can touch.
    ``request_id`` is echoed in the reply so the caller can match it to the
    request that opened the workspace.
    """

    type: str = "open_workspace"
    path: str = ""
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientOpenWorkspace":
        """Build a :class:`ClientOpenWorkspace` from decoded wire JSON."""
        return cls(
            type="open_workspace",
            path=d.get("path", ""),
            request_id=d.get("request_id", ""),
        )


@dataclass
class ClientListWorkspaces:
    """Request the list of known workspaces."""

    type: str = "list_workspaces"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListWorkspaces":
        """Build a :class:`ClientListWorkspaces` from decoded wire JSON."""
        return cls(type="list_workspaces")


@dataclass
class ClientRemoveWorkspace:
    """Forget a workspace from the registry.

    Only the registration record is removed; the directory on disk is never
    touched, so a workspace can be re-registered at any time.
    """

    type: str = "remove_workspace"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRemoveWorkspace":
        """Build a :class:`ClientRemoveWorkspace` from decoded wire JSON."""
        return cls(
            type="remove_workspace",
            path=d.get("path", ""),
        )


@dataclass
class ClientCloseWorkspace:
    """Leave the active workspace without removing it from the registry.

    The next run falls back to the unscoped session, while the registration
    survives for a later :class:`ClientOpenWorkspace`.
    """

    type: str = "close_workspace"
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientCloseWorkspace":
        """Build a :class:`ClientCloseWorkspace` from decoded wire JSON."""
        return cls(type="close_workspace", request_id=d.get("request_id", ""))


@dataclass
class ClientUploadWorkspaceIcon:
    """Attach an icon to a workspace entry.

    ``icon_data`` is carried inline as a data URL rather than by path because
    the image is picked in the renderer's dialog and may live in a temporary
    location that is not stable enough to reference later.
    """

    type: str = "upload_workspace_icon"
    path: str = ""
    # Full data URL of the picked image: "data:image/png;base64,..."
    icon_data: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUploadWorkspaceIcon":
        """Build a :class:`ClientUploadWorkspaceIcon` from decoded wire JSON."""
        return cls(
            type="upload_workspace_icon",
            path=d.get("path", ""),
            icon_data=d.get("icon_data", ""),
        )


@dataclass
class ClientRenameWorkspace:
    """Set a display name for a registered workspace."""

    type: str = "rename_workspace"
    path: str = ""
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRenameWorkspace":
        """Build a :class:`ClientRenameWorkspace` from decoded wire JSON."""
        return cls(
            type="rename_workspace",
            path=d.get("path", ""),
            name=d.get("name", ""),
        )


@dataclass
class ClientGetWorkspaceConfig:
    """Request a workspace's configuration overlay."""

    type: str = "get_workspace_config"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetWorkspaceConfig":
        """Build a :class:`ClientGetWorkspaceConfig` from decoded wire JSON."""
        return cls(
            type="get_workspace_config",
            path=d.get("path", ""),
        )


@dataclass
class ClientSaveWorkspaceConfig:
    """Replace a workspace's configuration overlay.

    ``config`` is the whole overlay rather than a patch, because workspace
    config is small and a full replacement is easier to reason about than
    merging partial updates of nested maps.
    """

    type: str = "save_workspace_config"
    path: str = ""
    # Partial per-workspace config: {"models": [model_id,...] | null,
    #                              "permissions": {...} | null,
    #                              "mcp": {...} | null}
    # null/absent fields mean "reuse the global config for this key".
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSaveWorkspaceConfig":
        """Build a :class:`ClientSaveWorkspaceConfig` from decoded wire JSON."""
        return cls(
            type="save_workspace_config",
            path=d.get("path", ""),
            config=d.get("config") or {},
        )


@dataclass
class ClientGetGitignore:
    """Request ``.encre/.gitignore`` for the active workspace.

    This is Encre's own project-local ignore file, keeping its generated
    artifacts out of the user's repository.  The handler replies with
    ``gitignore_content``; a fresh project returns an empty string because the
    directory is created on demand, and a read failure is surfaced inside
    ``content`` as a comment line so the editor's frame shape stays stable.
    """

    type: str = "get_gitignore"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetGitignore":
        """Build a :class:`ClientGetGitignore` from decoded wire JSON."""
        return cls(type="get_gitignore")


@dataclass
class ClientSetGitignore:
    """Replace the content of the workspace's ``.encre/.gitignore``.

    Written verbatim with no sanitisation, because the editor owns the
    semantics of gitignore patterns.  The handler echoes the frame back as
    ``gitignore_content`` so the editor can confirm the save.
    """

    type: str = "set_gitignore"
    content: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetGitignore":
        """Build a :class:`ClientSetGitignore` from decoded wire JSON."""
        return cls(
            type="set_gitignore",
            content=d.get("content", ""),
        )


@dataclass
class ClientLibraryList:
    """Request the full knowledge-library tree."""

    type: str = "library_list"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientLibraryList":
        """Build a :class:`ClientLibraryList` from decoded wire JSON."""
        return cls(type="library_list")


@dataclass
class ClientLibraryCreateNode:
    """Create a node in the knowledge-library tree.

    ``node_type`` is one of ``space`` / ``folder`` / ``doc`` / ``sheet``
    (the structural and self-contained types).  URL clips and local files
    flow through :class:`ClientLibraryAddClip` / :class:`ClientLibraryAddFile`,
    which infer the leaf type and store the body.
    """

    type: str = "library_create_node"
    parent_id: str = ""
    node_type: str = "doc"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibraryCreateNode":
        """Build a :class:`ClientLibraryCreateNode` from decoded wire JSON."""
        return cls(
            type="library_create_node",
            parent_id=d.get("parent_id", ""),
            node_type=d.get("node_type", "doc"),
            name=d.get("name", ""),
        )


@dataclass
class ClientLibraryAddFile:
    """Import a local file into the library as a markdown/attachment leaf."""

    type: str = "library_add_file"
    parent_id: str = ""
    name: str = ""
    file_path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibraryAddFile":
        """Build a :class:`ClientLibraryAddFile` from decoded wire JSON."""
        return cls(
            type="library_add_file",
            parent_id=d.get("parent_id", ""),
            name=d.get("name", ""),
            file_path=d.get("file_path", ""),
        )


@dataclass
class ClientLibraryAddClip:
    """Add a web clip (URL) to the library; the body is crawled async."""

    type: str = "library_add_clip"
    parent_id: str = ""
    name: str = ""
    url: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibraryAddClip":
        """Build a :class:`ClientLibraryAddClip` from decoded wire JSON."""
        return cls(
            type="library_add_clip",
            parent_id=d.get("parent_id", ""),
            name=d.get("name", ""),
            url=d.get("url", ""),
        )


@dataclass
class ClientLibraryMoveNode:
    """Move a library tree node (with its subtree) under a new parent."""

    type: str = "library_move_node"
    id: str = ""
    parent_id: str = ""
    index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibraryMoveNode":
        """Build a :class:`ClientLibraryMoveNode` from decoded wire JSON."""
        return cls(
            type="library_move_node",
            id=d.get("id", ""),
            parent_id=d.get("parent_id", ""),
            index=int(d.get("index", 0)),
        )


@dataclass
class ClientLibraryRenameNode:
    """Rename a library tree node."""

    type: str = "library_rename_node"
    id: str = ""
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibraryRenameNode":
        """Build a :class:`ClientLibraryRenameNode` from decoded wire JSON."""
        return cls(
            type="library_rename_node",
            id=d.get("id", ""),
            name=d.get("name", ""),
        )


@dataclass
class ClientLibraryDeleteNode:
    """Delete a library tree node together with its whole subtree."""

    type: str = "library_delete_node"
    id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibraryDeleteNode":
        """Build a :class:`ClientLibraryDeleteNode` from decoded wire JSON."""
        return cls(
            type="library_delete_node",
            id=d.get("id", ""),
        )


@dataclass
class ClientLibraryGetContent:
    """Request the stored content of a library leaf node."""

    type: str = "library_get_content"
    id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibraryGetContent":
        """Build a :class:`ClientLibraryGetContent` from decoded wire JSON."""
        return cls(
            type="library_get_content",
            id=d.get("id", ""),
        )


@dataclass
class ClientLibrarySaveContent:
    """Persist edited content back to a library leaf node."""

    type: str = "library_save_content"
    id: str = ""
    content: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientLibrarySaveContent":
        """Build a :class:`ClientLibrarySaveContent` from decoded wire JSON."""
        return cls(
            type="library_save_content",
            id=d.get("id", ""),
            content=d.get("content", ""),
        )


@dataclass
class ClientGetMemoryList:
    """Request the memory tree for the session.

    Memory is the persistent note store written by the memory skill; this frame
    returns the tree so the settings panel can render it.
    """

    type: str = "get_memory_list"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetMemoryList":
        """Build a :class:`ClientGetMemoryList` from decoded wire JSON."""
        return cls(type="get_memory_list")


@dataclass
class ClientGetProfile:
    """Request the identity/profile document that describes the user to the agent."""

    type: str = "get_profile"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetProfile":
        """Build a :class:`ClientGetProfile` from decoded wire JSON."""
        return cls(type="get_profile")


@dataclass
class ClientGetMemoryDetail:
    """Read one memory entry by its path in the memory tree."""

    type: str = "get_memory_detail"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetMemoryDetail":
        """Build a :class:`ClientGetMemoryDetail` from decoded wire JSON."""
        return cls(type="get_memory_detail", path=d.get("path", ""))


@dataclass
class ClientGetMemoryGraph:
    """Request the whole memory graph (nodes + typed edges) for the
    settings relationship view."""

    type: str = "get_memory_graph"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> ClientGetMemoryGraph:
        """Build a :class:`ClientGetMemoryGraph` from decoded wire JSON."""
        return cls(type="get_memory_graph")


@dataclass
class ClientListGlobalRules:
    """Request the global rules that apply to every workspace."""

    type: str = "list_global_rules"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListGlobalRules":
        """Build a :class:`ClientListGlobalRules` from decoded wire JSON."""
        return cls(type="list_global_rules")


@dataclass
class ClientListProjectRules:
    """Request the rules scoped to the current workspace."""

    type: str = "list_project_rules"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListProjectRules":
        """Build a :class:`ClientListProjectRules` from decoded wire JSON."""
        return cls(type="list_project_rules")


@dataclass
class ClientListProjectHooks:
    """Request the workspace hooks that fire around tool calls."""

    type: str = "list_project_hooks"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListProjectHooks":
        """Build a :class:`ClientListProjectHooks` from decoded wire JSON."""
        return cls(type="list_project_hooks")


@dataclass
class ClientSaveGlobalRule:
    """Create or replace a global rule by name."""

    type: str = "save_global_rule"
    name: str = ""
    content: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSaveGlobalRule":
        """Build a :class:`ClientSaveGlobalRule` from decoded wire JSON."""
        return cls(
            type="save_global_rule",
            name=d.get("name", ""),
            content=d.get("content", ""),
        )


@dataclass
class ClientDeleteGlobalRule:
    """Delete a global rule by name."""

    type: str = "delete_global_rule"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteGlobalRule":
        """Build a :class:`ClientDeleteGlobalRule` from decoded wire JSON."""
        return cls(
            type="delete_global_rule",
            name=d.get("name", ""),
        )


@dataclass
class ClientGetGlobalRuleContent:
    """Read one global rule's body for editing."""

    type: str = "get_global_rule_content"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetGlobalRuleContent":
        """Build a :class:`ClientGetGlobalRuleContent` from decoded wire JSON."""
        return cls(
            type="get_global_rule_content",
            name=d.get("name", ""),
        )


@dataclass
class ClientTerminalSpawn:
    """Open a new pseudo-terminal for interactive shell use.

    The reply ``terminal_spawned`` frame carries the id that later
    ``terminal_write`` / ``terminal_resize`` / ``terminal_kill`` frames address.
    ``shell_args`` lets a caller request a specific interpreter or flags.
    """

    type: str = "terminal_spawn"
    shell: str = ""
    shell_args: list[str] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalSpawn":
        """Build a :class:`ClientTerminalSpawn` from decoded wire JSON."""
        return cls(
            type="terminal_spawn",
            shell=d.get("shell", ""),
            shell_args=d.get("shell_args"),
        )


@dataclass
class ClientTerminalWrite:
    """Write stdin bytes to an open terminal session."""

    type: str = "terminal_write"
    id: int = 0
    data: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalWrite":
        """Build a :class:`ClientTerminalWrite` from decoded wire JSON."""
        return cls(type="terminal_write", id=d.get("id", 0), data=d.get("data", ""))


@dataclass
class ClientTerminalResize:
    """Resize the pty of an open terminal session to match the rendered grid.

    Sent from the renderer when the terminal panel is resized, so the shell's
    line wrapping stays in step with the rendered grid.
    """

    type: str = "terminal_resize"
    id: int = 0
    cols: int = 80
    rows: int = 24

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalResize":
        """Build a :class:`ClientTerminalResize` from decoded wire JSON."""
        return cls(type="terminal_resize", id=d.get("id", 0), cols=d.get("cols", 80), rows=d.get("rows", 24))


@dataclass
class ClientTerminalKill:
    """Tear down an open terminal session and its child process."""

    type: str = "terminal_kill"
    id: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalKill":
        """Build a :class:`ClientTerminalKill` from decoded wire JSON."""
        return cls(type="terminal_kill", id=d.get("id", 0))


@dataclass
class ClientTerminalListShells:
    """List the shells available for :class:`ClientTerminalSpawn` to start.

    The handler answers with ``terminal_shells`` after probing the machine, so
    the spawn dialog offers only interpreters that are installed.
    """

    type: str = "terminal_list_shells"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientTerminalListShells":
        """Build a :class:`ClientTerminalListShells` from decoded wire JSON."""
        return cls(type="terminal_list_shells")


@dataclass
class ClientRetry:
    """Re-run a turn from an earlier message instead of appending a new one.

    The handler calls ``retry_at_user_index`` on the underlying
    :class:`~encre.session.EncreSession`, which forks a new branch off
    ``user_message_index`` rather than rewriting history in place, then
    spawns a retry run on it.  Forking keeps the abandoned continuation
    inspectable instead of destroying it.

    Attributes:
        mode: ``"normal"`` reuses the prompt as-is.  ``"detailed"`` and
            ``"concise"`` fold the previous assistant reply into the prompt so
            the model re-answers with the earlier attempt as context.
    """

    type: str = "retry"
    user_message_index: int = 0
    session_id: str = ""
    mode: str = "normal"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRetry":
        """Build a :class:`ClientRetry` from decoded wire JSON."""
        return cls(
            type="retry",
            user_message_index=d.get("user_message_index", 0),
            session_id=d.get("session_id", ""),
            mode=d.get("mode", "normal"),
        )


@dataclass
class ClientSwitchBranch:
    """Switch a session to a different conversation branch.

    A session keeps its alternative continuations as branches; switching makes
    one of them the live tail so subsequent runs continue from it.
    """

    type: str = "switch_branch"
    branch_id: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSwitchBranch":
        """Build a :class:`ClientSwitchBranch` from decoded wire JSON."""
        return cls(
            type="switch_branch",
            branch_id=d.get("branch_id", ""),
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientRollbackBranch:
    """Cut a branch back to an earlier message.

    Unlike :class:`ClientRetry`, which forks and re-executes, this truncates
    the active branch in place.  The handler cancels any running agent first:
    leaving a task alive would let its ``finally`` block write its in-memory
    state back over the restored branch.  The result is reported as a
    ``branch_switched`` frame so the client redraws without refetching.
    """

    type: str = "rollback"
    branch_id: str = ""
    message_id: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRollbackBranch":
        """Build a :class:`ClientRollbackBranch` from decoded wire JSON."""
        return cls(
            type="rollback",
            branch_id=d.get("branch_id", ""),
            message_id=d.get("message_id", ""),
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientGetUsageStats:
    """Request aggregate usage counters across sessions and models."""

    type: str = "get_usage_stats"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetUsageStats":
        """Build a :class:`ClientGetUsageStats` from decoded wire JSON."""
        return cls(type="get_usage_stats")


@dataclass
class ClientReplayGetSession:
    """Request a session's recorded telemetry for replay / scrubbing.

    Returns the full ordered event stream plus a summary and turn-boundary
    indices so the frontend can render a cursor-based scrubber without
    extra round-trips.
    """
    type: str = "replay_get_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientReplayGetSession":
        """Build a :class:`ClientReplayGetSession` from decoded wire JSON."""
        return cls(
            type="replay_get_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientAutomationListJobs:
    """Request the scheduled automation jobs."""

    type: str = "automation_list_jobs"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientAutomationListJobs":
        """Build a :class:`ClientAutomationListJobs` from decoded wire JSON."""
        return cls(type="automation_list_jobs")


@dataclass
class ClientAutomationCreateJob:
    """Register a scheduled automation job.

    ``cron`` is the schedule expression and ``prompt`` is the instruction the
    job runs on each firing.  ``model_index`` of ``-1`` means "use whatever the
    active model is at fire time" rather than pinning a specific model, which
    keeps a job valid when the model table changes.  ``push_gateways`` names the
    IM adapters the job's output should be pushed to when it is not run from
    the desktop UI.  ``workspace`` optionally pins the job to a registered
    workspace directory; an empty value keeps the default execution sandbox.
    ``permission_mode`` selects the same permission preset the safety engine
    dispatches on elsewhere (``default``/``auto``/``bypass``/``custom``);
    an empty value keeps the ambient agent permission.
    """
    type: str = "automation_create_job"
    name: str = ""
    prompt: str = ""
    cron: str = ""
    tag: str = ""
    model_index: int = -1
    workspace: str = ""
    permission_mode: str = ""
    push_gateways: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationCreateJob":
        """Build a :class:`ClientAutomationCreateJob` from decoded wire JSON."""
        return cls(
            type="automation_create_job",
            name=d.get("name", ""),
            prompt=d.get("prompt", ""),
            cron=d.get("cron", ""),
            tag=d.get("tag", ""),
            model_index=d.get("model_index", -1),
            workspace=d.get("workspace", ""),
            permission_mode=d.get("permission_mode", ""),
            push_gateways=d.get("push_gateways", []),
        )


@dataclass
class ClientAutomationCancelJob:
    """Stop a scheduled job from firing again."""

    type: str = "automation_cancel_job"
    job_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationCancelJob":
        """Build a :class:`ClientAutomationCancelJob` from decoded wire JSON."""
        return cls(
            type="automation_cancel_job",
            job_id=d.get("job_id", ""),
        )


@dataclass
class ClientAutomationGetHistory:
    """Request the execution history of the automation jobs."""

    type: str = "automation_get_history"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientAutomationGetHistory":
        """Build a :class:`ClientAutomationGetHistory` from decoded wire JSON."""
        return cls(type="automation_get_history")


@dataclass
class ClientAutomationToggleJob:
    """Enable or disable a job without deleting it."""

    type: str = "automation_toggle_job"
    job_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationToggleJob":
        """Build a :class:`ClientAutomationToggleJob` from decoded wire JSON."""
        return cls(
            type="automation_toggle_job",
            job_id=d.get("job_id", ""),
        )


@dataclass
class ClientAutomationUpdateJob:
    """Replace a job's definition.

    Fields left at their defaults are replaced by the caller's values, so a
    partial edit is expressed by sending the full row from the renderer.
    """
    type: str = "automation_update_job"
    job_id: str = ""
    name: str = ""
    prompt: str = ""
    cron: str = ""
    tag: str = ""
    model_index: int = -1
    workspace: str = ""
    permission_mode: str = ""
    push_gateways: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationUpdateJob":
        """Build a :class:`ClientAutomationUpdateJob` from decoded wire JSON."""
        return cls(
            type="automation_update_job",
            job_id=d.get("job_id", ""),
            name=d.get("name", ""),
            prompt=d.get("prompt", ""),
            cron=d.get("cron", ""),
            tag=d.get("tag", ""),
            model_index=d.get("model_index", -1),
            workspace=d.get("workspace", ""),
            permission_mode=d.get("permission_mode", ""),
            push_gateways=d.get("push_gateways", []),
        )


@dataclass
class ClientAutomationDeleteJob:
    """Delete a scheduled job and its history."""

    type: str = "automation_delete_job"
    job_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationDeleteJob":
        """Build a :class:`ClientAutomationDeleteJob` from decoded wire JSON."""
        return cls(
            type="automation_delete_job",
            job_id=d.get("job_id", ""),
        )


@dataclass
class ClientAutomationDeleteExecution:
    """Drop one execution record from a job's history."""

    type: str = "automation_delete_execution"
    entry_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationDeleteExecution":
        """Build a :class:`ClientAutomationDeleteExecution` from decoded wire JSON."""
        return cls(
            type="automation_delete_execution",
            entry_id=d.get("entry_id", ""),
        )


@dataclass
class ClientAutomationRenameExecution:
    """Rename an execution record so a history entry is easier to recognise."""

    type: str = "automation_rename_execution"
    entry_id: str = ""
    new_name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationRenameExecution":
        """Build a :class:`ClientAutomationRenameExecution` from decoded wire JSON."""
        return cls(
            type="automation_rename_execution",
            entry_id=d.get("entry_id", ""),
            new_name=d.get("new_name", ""),
        )


# The discriminated union of every client message type understood by the handler.
# ``parse_client_message`` returns one of these based on the wire ``type`` field.
ClientMessage = (
    ClientRun
    | ClientRespondPermission
    | ClientPluginConfirm
    | ClientCancel
    | ClientResume
    | ClientConfigure
    | ClientPing
    | ClientListModels
    | ClientListSessions
    | ClientListAllSessions
    | ClientNewSession
    | ClientGetConfig
    | ClientUpdateProviders
    | ClientSetActiveModel
    | ClientDeleteModel
    | ClientFetchModels
    | ClientUpdateSkills
    | ClientInstallSkill
    | ClientPluginInstall
    | ClientPluginUninstall
    | ClientPluginEnable
    | ClientPluginDisable
    | ClientPluginInstallMCP
    | ClientPluginInstallAgent
    | ClientUninstallSkill
    | ClientUpdateSkill
    | ClientUpdateMCP
    | ClientUpdateAgent
    | ClientSearch
    | ClientRollbackLog
    | ClientRollbackCheckout
    | ClientValidateModel
    | ClientEditMessage
    | ClientDeleteMessage
    | ClientDeleteSession
    | ClientExportSession
    | ClientExportSessionsBatch
    | ClientRenameSession
    | ClientAgentCreate
    | ClientAgentDelete
    | ClientAgentUpdate
    | ClientAgentList
    | ClientAgentSetActive
    | ClientUpdateSubAgents
    | ClientOpenWorkspace
    | ClientListWorkspaces
    | ClientRemoveWorkspace
    | ClientCloseWorkspace
    | ClientUploadWorkspaceIcon
    | ClientRenameWorkspace
    | ClientGetWorkspaceConfig
    | ClientSaveWorkspaceConfig
    | ClientGetGitignore
    | ClientSetGitignore
    | ClientGetMemoryList
    | ClientGetMemoryDetail
    | ClientListGlobalRules
    | ClientListProjectRules
    | ClientListProjectHooks
    | ClientSaveGlobalRule
    | ClientDeleteGlobalRule
    | ClientGetGlobalRuleContent
    | ClientGetProfile
    | ClientTerminalSpawn
    | ClientTerminalWrite
    | ClientTerminalResize
    | ClientTerminalKill
    | ClientTerminalListShells
    | ClientRetry
    | ClientSwitchBranch
    | ClientRollbackBranch
    | ClientGetUsageStats
    | ClientWechatScan
    | ClientIclawResume
    | ClientAutomationListJobs
    | ClientAutomationCreateJob
    | ClientAutomationCancelJob
    | ClientAutomationGetHistory
    | ClientAutomationToggleJob
    | ClientAutomationUpdateJob
    | ClientAutomationDeleteJob
    | ClientAutomationDeleteExecution
    | ClientAutomationRenameExecution
    | ClientSpecApprove
    | ClientSpecReject
    | ClientPlanApprove
    | ClientPlanReject
    | ClientSetMode
    | ClientSetCdpUrl
    | ClientExportData
    | ClientImportData
)


_CLIENT_MESSAGE_PARSERS: dict[str, type] = {
    "run": ClientRun,
    "queue_list": ClientQueueList,
    "queue_pop": ClientQueuePop,
    "queue_remove": ClientQueueRemove,
    "queue_move": ClientQueueMove,
    "queue_steer": ClientQueueSteer,
    "steer": ClientSteer,
    "respond_permission": ClientRespondPermission,
    "plugin_confirm": ClientPluginConfirm,
    "respond_plan": ClientRespondPlan,
    "set_plan_mode": ClientSetPlanMode,
    "set_command": ClientSetCommand,
    "respond_question": ClientRespondQuestion,
    "cancel": ClientCancel,
    "tool_stop": ClientToolStop,
    "resume": ClientResume,
    "configure": ClientConfigure,
    "ping": ClientPing,
    "list_models": ClientListModels,
    "list_sessions": ClientListSessions,
    "list_all_sessions": ClientListAllSessions,
    "new_session": ClientNewSession,
    "get_config": ClientGetConfig,
    "wechat_scan": ClientWechatScan,
    "iclaw_resume": ClientIclawResume,
    "update_providers": ClientUpdateProviders,
    "set_active_model": ClientSetActiveModel,
    "delete_model": ClientDeleteModel,
    "fetch_models": ClientFetchModels,
    "update_skills": ClientUpdateSkills,
    "get_skill_detail": ClientGetSkillDetail,
    "get_skill_file": ClientGetSkillFile,
    "install_skill": ClientInstallSkill,
    "plugin_install": ClientPluginInstall,
    "plugin_uninstall": ClientPluginUninstall,
    "plugin_enable": ClientPluginEnable,
    "plugin_disable": ClientPluginDisable,
    "plugin_install_mcp": ClientPluginInstallMCP,
    "plugin_install_agent": ClientPluginInstallAgent,
    "market_install": ClientMarketInstall,
    "uninstall_skill": ClientUninstallSkill,
    "update_skill": ClientUpdateSkill,
    "update_mcp": ClientUpdateMCP,
    "update_agent": ClientUpdateAgent,
    "search": ClientSearch,
    "rollback_log": ClientRollbackLog,
    "rollback_checkout": ClientRollbackCheckout,
    "validate_model": ClientValidateModel,
    "edit_message": ClientEditMessage,
    "delete_message": ClientDeleteMessage,
    "delete_session": ClientDeleteSession,
    "clear_all_sessions": ClientClearAllSessions,
    "export_session": ClientExportSession,
    "export_sessions_batch": ClientExportSessionsBatch,
    "rename_session": ClientRenameSession,
    "archive_session": ClientArchiveSession,
    "unarchive_session": ClientUnarchiveSession,
    "list_archived_sessions": ClientListArchivedSessions,
    "agent_create": ClientAgentCreate,
    "agent_delete": ClientAgentDelete,
    "agent_update": ClientAgentUpdate,
    "agent_list": ClientAgentList,
    "agent_set_active": ClientAgentSetActive,
    "update_sub_agents": ClientUpdateSubAgents,
    "open_workspace": ClientOpenWorkspace,
    "list_workspaces": ClientListWorkspaces,
    "remove_workspace": ClientRemoveWorkspace,
    "close_workspace": ClientCloseWorkspace,
    "upload_workspace_icon": ClientUploadWorkspaceIcon,
    "rename_workspace": ClientRenameWorkspace,
    "get_workspace_config": ClientGetWorkspaceConfig,
    "save_workspace_config": ClientSaveWorkspaceConfig,
    "get_gitignore": ClientGetGitignore,
    "set_gitignore": ClientSetGitignore,
    "library_list": ClientLibraryList,
    "library_create_node": ClientLibraryCreateNode,
    "library_add_file": ClientLibraryAddFile,
    "library_add_clip": ClientLibraryAddClip,
    "library_move_node": ClientLibraryMoveNode,
    "library_rename_node": ClientLibraryRenameNode,
    "library_delete_node": ClientLibraryDeleteNode,
    "library_get_content": ClientLibraryGetContent,
    "library_save_content": ClientLibrarySaveContent,
    "get_memory_list": ClientGetMemoryList,
    "get_memory_detail": ClientGetMemoryDetail,
    "get_memory_graph": ClientGetMemoryGraph,
    "list_global_rules": ClientListGlobalRules,
    "list_project_rules": ClientListProjectRules,
    "list_project_hooks": ClientListProjectHooks,
    "save_global_rule": ClientSaveGlobalRule,
    "delete_global_rule": ClientDeleteGlobalRule,
    "get_global_rule_content": ClientGetGlobalRuleContent,
    "get_profile": ClientGetProfile,
    "terminal_spawn": ClientTerminalSpawn,
    "terminal_write": ClientTerminalWrite,
    "terminal_resize": ClientTerminalResize,
    "terminal_kill": ClientTerminalKill,
    "terminal_list_shells": ClientTerminalListShells,
    "retry": ClientRetry,
    "switch_branch": ClientSwitchBranch,
    "rollback": ClientRollbackBranch,
    "get_usage_stats": ClientGetUsageStats,
    "replay_get_session": ClientReplayGetSession,
    "automation_list_jobs": ClientAutomationListJobs,
    "automation_create_job": ClientAutomationCreateJob,
    "automation_cancel_job": ClientAutomationCancelJob,
    "automation_get_history": ClientAutomationGetHistory,
    "automation_toggle_job": ClientAutomationToggleJob,
    "automation_update_job": ClientAutomationUpdateJob,
    "automation_delete_job": ClientAutomationDeleteJob,
    "automation_delete_execution": ClientAutomationDeleteExecution,
    "automation_rename_execution": ClientAutomationRenameExecution,
    "engine_install_response": ClientEngineInstallResponse,
    "spec_approve": ClientSpecApprove,
    "spec_reject": ClientSpecReject,
    "plan_approve": ClientPlanApprove,
    "plan_reject": ClientPlanReject,
    "set_mode": ClientSetMode,
    "browser_cdp_url": ClientSetCdpUrl,
    "export_data": ClientExportData,
    "import_data": ClientImportData,
}


def parse_client_message(
    raw: str | bytes,
    *,
    require_encryption: bool = False,
) -> ClientMessage | None:
    """Decode one inbound frame into its ``Client*`` dataclass.

    This is the single entry point from wire bytes to a typed message: it
    decrypts the frame when it is ciphertext, parses the JSON, and dispatches
    on the ``type`` field through :data:`_CLIENT_MESSAGE_PARSERS`.

    ``require_encryption`` is set by the server loop after the handshake proved
    the client encrypts: from then on a plaintext frame is dropped rather than
    accepted.

    Args:
        raw: The frame as delivered by the transport layer, either a text
            frame string or a binary frame byte string.

    Returns:
        The matching ``Client*`` instance, or ``None`` when the frame is empty,
        not valid JSON, not a mapping, or its ``type`` is unknown.  Callers
        treat ``None`` as "drop this frame and optionally report an error"
        rather than raising.

    Note:
        Unknown ``type`` values return ``None`` instead of raising so a badly
        behaved or mismatched client cannot crash the handler loop.  A newer
        client that sends a frame this server does not know about is degraded
        gracefully rather than being disconnected.
    """
    try:
        decrypted = _parse_client_encrypted(raw, require_encryption=require_encryption)
        if isinstance(decrypted, bytes):
            decrypted = decrypted.decode("utf-8")
        data = json.loads(decrypted)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        return None

    msg_type = data.get("type", "")

    cls = _CLIENT_MESSAGE_PARSERS.get(msg_type)
    if cls is None:
        return None
    return cls.from_dict(data)


# ── Server -> Client message types ───────────────────────────────────────────

ServerMessageType = Literal[
    "text_delta",
    "thinking_delta",
    "tool_call_start",
    "tool_call_delta",
    "tool_call_end",
    "tool_progress",
    "tool_result",
    "permission_request",
    "finish",
    "pong",
    "error",
    "session_ready",
    "data_begin",
    "data_shard",
    "data_end",
    "configured",
    "telemetry",
    "plan_update",
    "agent_state",
    "spec_update",
    "plan_review",
    "models_list",
    "sessions_list",
    "config_data",
    "config_patch",
    "models_updated",
    "models_fetched",
    "skills_updated",
    "skills_list",
    "skill_detail",
    "skill_installed",
    "skill_install_error",
    "skill_uninstalled",
    "mcp_updated",
    "agent_updated",
    "search_results",
    "compact",
    "compact_starting",
    "compact_failed",
    "retry",
    "rollback_log",
    "rollback_checkout",
    "model_validated",
    "model_validation_error",
    "messages_updated",
    "session_deleted",
    "session_exported",
    "sessions_exported_zip",
    "session_renamed",
    "archived_sessions_list",
    "memory_list",
    "memory_graph",
    "memory_detail",
    "global_rules_list",
    "project_rules_list",
    "global_rule_saved",
    "global_rule_deleted",
    "global_rule_content",
    "profile_data",
    "agents_list",
    "agents_updated",
    "sub_agents_updated",
    "gitignore_content",
    "library_tree",
    "library_content",
    "library_error",
    "artifacts_update",
    "assistant_boundary",
    "slash_commands",
    "terminal_data",
    "terminal_shells",
    "terminal_spawned",
    "branch_switched",
    "branch_rolled_back",
    "branch_updated",
    "usage_stats",
    "automation_jobs_list",
    "automation_job_created",
    "automation_job_cancelled",
    "automation_job_history",
    "automation_job_update",
    "automation_stream_event",
    "mode_changed",
    "plan_mode_changed",
    "command_changed",
    "data_exported_zip",
    "data_export_progress",
    "data_import_progress",
    "data_import_done",
]


def _make_message(msg_type: ServerMessageType, **kwargs: Any) -> dict[str, Any]:
    """Assemble the payload dict for a server frame.

    Args:
        msg_type: The value written to the ``type`` field.
        **kwargs: Payload fields merged into the same object.

    Returns:
        The assembled frame, ready to be JSON-serialised.

    Note:
        Kept separate from serialisation so the frame dict can be inspected or
        extended without an encoding pass, and so callers that only need the
        payload can reuse it.
    """
    msg: dict[str, Any] = {"type": msg_type}
    msg.update(kwargs)
    return msg


def encode_server_message(
    msg_type: ServerMessageType,
    **kwargs: Any,
) -> str:
    """Serialise one server frame, always encrypted.

    The single outbound serialiser every ``encode_*`` helper in this module
    funnels into, and the one :mod:`encre.protocol.handlers.base` uses through
    ``_send``.

    Encryption is mandatory and has no opt-out: every frame the server sends
    is AES-256-GCM ciphertext, including per-token streaming deltas.  The
    former ``encrypt=False`` escape hatch existed on the assumption that
    encrypting each token would dominate stream latency, but a 64-byte
    payload costs ~2.7 microseconds to encrypt and ~2.5 microseconds to
    decrypt, which is orders of magnitude below the inter-frame gap of a
    token stream -- so no frame is ever sent in the clear, on any client,
    GUI or TUI alike.

    Args:
        msg_type: The value written to the ``type`` field.
        **kwargs: Payload fields merged into the same object.

    Returns:
        A base64 ciphertext string ready to be sent as a WebSocket frame.
    """
    return encode_server_encrypted(msg_type, **kwargs)


# ── Convenience encoders ─────────────────────────────────────────────────────


def encode_text_delta(text: str) -> str:
    """Emit one chunk of the assistant's streamed reply text.

    Encrypted like every other frame.  This used to skip encryption on the
    assumption that a per-token round trip would dominate stream latency, but
    encryption of a token-sized payload costs single-digit microseconds, so the
    saving was never worth streaming cleartext to the client.

    Args:
        text: The text chunk to append.

    Returns:
        The encoded ``text_delta`` frame.
    """
    return encode_server_message("text_delta", text=text)


def encode_thinking_delta(text: str) -> str:
    """Emit one chunk of the model's reasoning trace.

    Mirrors :func:`encode_text_delta` but on the reasoning channel, so a UI can
    render thinking separately from the final answer.

    Args:
        text: The reasoning chunk to append.

    Returns:
        The encoded ``thinking_delta`` frame.
    """
    return encode_server_message("thinking_delta", text=text)


def encode_tool_call_start(name: str, call_id: str) -> str:
    """Announce that a tool call is beginning.

    Pairs with :func:`encode_tool_call_delta` and :func:`encode_tool_call_end`
    over the same ``call_id``, so a renderer can open a card, fill it in, and
    close it without guessing when each phase ends.

    Args:
        name: The tool being invoked.
        call_id: Identifier shared with the later delta and end frames.

    Returns:
        The encoded ``tool_call_start`` frame.
    """
    return encode_server_message("tool_call_start", name=name, id=call_id)


def encode_tool_call_delta(call_id: str, key: str, value: str) -> str:
    """Stream one argument of a tool call as it is assembled.

    Each call arrives as a series of key/value pairs rather than one final
    payload, which lets a UI watch a large argument being built instead of
    waiting for the whole call.

    Args:
        call_id: Identifier of the call being built.
        key: The argument name.
        value: The argument value being streamed.

    Returns:
        The encoded ``tool_call_delta`` frame.
    """
    return encode_server_message("tool_call_delta", id=call_id, key=key, value=value)


def encode_tool_call_end(call_id: str) -> str:
    """Mark the end of a streamed tool call.

    Args:
        call_id: Identifier of the call being closed.

    Returns:
        The encoded ``tool_call_end`` frame.
    """
    return encode_server_message("tool_call_end", id=call_id)


def encode_tool_progress(call_id: str, tool_name: str, status: str) -> str:
    """Report intermediate progress for a long-running tool call.

    Emitted between the call start and the result so a UI can show that a slow
    tool is still alive rather than looking hung.

    Args:
        call_id: Identifier of the running call.
        tool_name: The tool reporting progress.
        status: Human-readable status string.

    Returns:
        The encoded ``tool_progress`` frame.
    """
    return encode_server_message("tool_progress", id=call_id, tool_name=tool_name, status=status)


def encode_tool_result(
    call_id: str,
    content: str,
    is_error: bool = False,
    sub_agent_messages: list[dict[str, Any]] | None = None,
    sub_agent_session_id: str | None = None,
) -> str:
    """Deliver the final output of a tool call.

    ``content`` is the primary payload and ``is_error`` distinguishes a
    non-zero exit from a genuine result so a UI can render failures distinctly
    rather than treating every tool call as successful.

    Args:
        call_id: Identifier of the call that finished.
        content: The tool's output text.
        is_error: True when the call failed rather than returning data.
        sub_agent_messages: When the call delegated to a sub-agent, the
            sub-agent's transcript so the renderer can show the delegation.
        sub_agent_session_id: The sub-agent's session id, so its messages can
            be opened as a session.

    Returns:
        The encoded ``tool_result`` frame.
    """
    return encode_server_message(
        "tool_result", id=call_id,
        content=content,
        is_error=is_error,
        sub_agent_messages=sub_agent_messages,
        sub_agent_session_id=sub_agent_session_id,
    )


def encode_permission_request(tool_name: str, reason: str) -> str:
    """Ask the user to authorise a tool call before it runs.

    The handler blocks on the reply :class:`ClientRespondPermission`, so the
    ``reason`` string is what justifies the pause to a user who otherwise only
    sees a tool they did not explicitly ask for.

    Args:
        tool_name: The tool that needs approval.
        reason: Why the tool is being invoked and what it will do.

    Returns:
        The encoded ``permission_request`` frame.
    """
    return encode_server_message("permission_request", tool_name=tool_name, reason=reason)


def encode_finish(reason: str, usage: dict[str, Any] | None = None, error: str | None = None) -> str:
    """Signal that a turn has completed.

    Always the last frame of a turn, which lets a UI end its streaming spinner
    deterministically instead of inferring completion from silence.

    Args:
        reason: Why the turn ended, for example ``"done"`` or ``"cancelled"``.
        usage: Token and cost counters for the turn, when available.
        error: A message describing a failure, when the turn ended in error.

    Returns:
        The encoded ``finish`` frame.
    """
    return encode_server_message("finish", reason=reason, usage=usage, error=error)


def encode_pong() -> str:
    """Reply to a :class:`ClientPing`.

    Encrypted like every other frame, including the keepalive: a plaintext
    heartbeat would be a standing reminder that the transport tolerates
    cleartext, and it is the frame a leak-detecting client would probe first.

    Returns:
        The encoded ``pong`` frame.
    """
    return encode_server_message("pong")


def encode_library_tree(nodes: list[dict[str, Any]]) -> str:
    """Push the full knowledge-library tree to the client."""
    return encode_server_message("library_tree", nodes=nodes)


def encode_library_content(id: str, content: str) -> str:
    """Answer a library content read with the leaf node's stored body."""
    return encode_server_message("library_content", id=id, content=content)


def encode_library_error(message: str) -> str:
    """Report a knowledge-library operation failure.

    Kept separate from :func:`encode_error` so a UI can surface the failure in
    the library panel rather than as a global alert.

    Args:
        message: Human-readable description of the failure.

    Returns:
        The encoded ``library_error`` frame.
    """
    return encode_server_message("library_error", message=message)


def encode_error(message: str, code: str = "internal") -> str:
    """Report a handler-level error to the client.

    Sent when a frame could not be parsed or a handler raised, so the client
    sees a controlled message instead of a dropped connection.

    Args:
        message: Human-readable description of the failure.
        code: Machine-readable bucket the client can branch on.

    Returns:
        The encoded ``error`` frame.
    """
    return encode_server_message("error", message=message, code=code)


def encode_session_ready(
    session_id: str,
    messages: list[dict[str, Any]] | None = None,
    request_id: str = "",
) -> str:
    """Announce that a session is available and deliver its current transcript.

    Sent both when a new session is created and when an existing one is
    opened, which is what lets a freshly connected renderer render a whole
    conversation with a single round trip.

    Args:
        session_id: The session being announced.
        messages: The transcript to render; ``None`` when the session is empty.
        request_id: Echoed from the request that created the session, so the
            caller can match the reply to the request.

    Returns:
        The encoded ``session_ready`` frame.
    """
    payload: dict[str, Any] = {"session_id": session_id}
    if messages is not None:
        payload["messages"] = messages
    if request_id:
        payload["request_id"] = request_id
    return encode_server_message("session_ready", **payload)


def encode_configured(config: dict[str, Any]) -> str:
    """Report the resulting configuration after a :class:`ClientConfigure`."""
    return encode_server_message("configured", config=config)


def encode_telemetry(data: dict[str, Any]) -> str:
    """Forward a structured telemetry event for the replay view.

    Encrypted by default because the payload can contain arbitrary session
    content, unlike the per-token delta frames.
    """
    return encode_server_message("telemetry", data=data)


def encode_plan_update(plan_items: list[dict[str, Any]]) -> str:
    """Push a refresh of the agent's current work plan."""
    return encode_server_message("plan_update", plan_items=plan_items)


def encode_agent_state(state: dict[str, Any]) -> str:
    """Push the agent's state snapshot after a state-changing event.

    Sent when a session's run state changes so every renderer observing that
    session stays in step with the live loop.
    """
    return encode_server_message("agent_state", state=state)


def encode_spec_update(
    spec: dict[str, Any] | None,
    status: str = "",
    feedback: str = "",
) -> str:
    """Encode a spec update event for the frontend.

    Mirrors the spec gate: ``status`` carries the gate state (for example
    ``"pending"`` or ``"approved"``) and ``feedback`` carries the text a user
    sent with a rejection, so the renderer can show why the gate reopened.

    Args:
        spec: The parsed spec, or ``None`` when it is being cleared.
        status: The gate state.
        feedback: Reviewer text attached to the transition.

    Returns:
        The encoded ``spec_update`` frame.
    """
    return encode_server_message("spec_update", spec=spec, status=status, feedback=feedback)


def encode_plan_review(
    review: dict[str, Any] | None,
    status: str = "review",
    feedback: str = "",
) -> str:
    """Encode a plan review event for the frontend.

    Pairs with :class:`ClientPlanApprove` / :class:`ClientPlanReject`, so the
    ``status`` value drives whether the gate is shown as open or closed.

    Args:
        review: The plan under review, or ``None`` when cleared.
        status: The review state; ``"review"`` means the gate is awaiting input.
        feedback: Reviewer text attached to the transition.

    Returns:
        The encoded ``plan_review`` frame.
    """
    return encode_server_message("plan_review", review=review, status=status, feedback=feedback)


def encode_models_list(models: list[str]) -> str:
    """Reply to a :class:`ClientListModels` request with the known models."""
    return encode_server_message("models_list", models=models)


def encode_sessions_list(sessions: list[dict[str, Any]]) -> str:
    """Reply with the visible session list.

    Sent to every connection observing the session store whenever a session is
    created, renamed, or deleted, so the lists stay in sync across windows.
    """
    return encode_server_message("sessions_list", sessions=sessions)


def encode_archived_sessions_list(sessions: list[dict[str, Any]]) -> str:
    """Reply with the archived session list for the archive manager view."""
    return encode_server_message("archived_sessions_list", sessions=sessions)


def encode_config_data(config: dict[str, Any]) -> str:
    """Reply to a :class:`ClientGetConfig` request with the effective config."""
    return encode_server_message("config_data", config=config)


def encode_models_updated(models: list[dict[str, Any]], active_model_index: int) -> str:
    """Broadcast the refreshed model table and the active selection.

    Sent to every connection after a provider table change so all renderers
    agree on the flattened indexes, which is what a stale index would break.

    Args:
        models: The flattened model table.
        active_model_index: Position of the model the next run will use.

    Returns:
        The encoded ``models_updated`` frame.
    """
    return encode_server_message("models_updated", models=models, active_model_index=active_model_index)


def encode_models_fetched(models: list[str]) -> str:
    """Report the models discovered by a backend probe."""
    return encode_server_message("models_fetched", models=models)


def encode_skills_updated(enabled_skills: list[str], available_skills: list[dict[str, Any]]) -> str:
    """Broadcast the resulting skill state after an enable/disable change.

    Both lists travel together because a client cannot recompute availability
    from the enabled set alone: a skill may be present but unusable in the
    current workspace.

    Args:
        enabled_skills: Names of the skills now active.
        available_skills: Descriptions of every installed skill.

    Returns:
        The encoded ``skills_updated`` frame.
    """
    return encode_server_message("skills_updated", enabled_skills=enabled_skills, available_skills=available_skills)


def encode_skills_list(skills: list[dict[str, Any]]) -> str:
    """Reply with the installed skill catalogue."""
    return encode_server_message("skills_list", skills=skills)


def encode_skill_installed(name: str, available_skills: list[dict[str, Any]]) -> str:
    """Report a successful skill install and the resulting catalogue.

    Args:
        name: The installed skill's name.
        available_skills: The catalogue after the install, so the renderer does
            not need a second request to refresh.

    Returns:
        The encoded ``skill_installed`` frame.
    """
    return encode_server_message("skill_installed", name=name, available_skills=available_skills)


def encode_skill_install_error(name: str, message: str) -> str:
    """Report a failed skill install.

    Kept distinct from :func:`encode_error` so the settings panel can show the
    failure next to the skill row that caused it rather than as a global alert.

    Args:
        name: The skill the install attempt was for.
        message: What went wrong.

    Returns:
        The encoded ``skill_install_error`` frame.
    """
    return encode_server_message("skill_install_error", name=name, message=message)


def encode_skill_uninstalled(name: str, available_skills: list[dict[str, Any]]) -> str:
    """Report a successful skill uninstall and the resulting catalogue.

    Args:
        name: The removed skill's name.
        available_skills: The catalogue after the removal.

    Returns:
        The encoded ``skill_uninstalled`` frame.
    """
    return encode_server_message("skill_uninstalled", name=name, available_skills=available_skills)


def encode_mcp_updated(mcp_servers: list[dict[str, Any]]) -> str:
    """Broadcast the refreshed MCP server configuration."""
    return encode_server_message("mcp_updated", mcp_servers=mcp_servers)


def encode_agent_updated(config: dict[str, Any]) -> str:
    """Broadcast the refreshed base agent configuration."""
    return encode_server_message("agent_updated", config=config)


def encode_search_results(results: list[dict[str, Any]]) -> str:
    """Reply to a :class:`ClientSearch` request."""
    return encode_server_message("search_results", results=results)


def encode_rollback_log(session_id: str, commits: list[dict[str, Any]]) -> str:
    """Reply with the rollback checkpoints available in a session.

    Args:
        session_id: The session the checkpoints belong to.
        commits: The checkpoint records, oldest first.

    Returns:
        The encoded ``rollback_log`` frame.
    """
    return encode_server_message("rollback_log", session_id=session_id, commits=commits)


def encode_rollback_checkout(session_id: str, commit_hash: str, messages: list[dict[str, Any]], turn_count: int, plan_items: list[dict[str, Any]] | None = None, artifacts: list[dict[str, Any]] | None = None, references: list[dict[str, Any]] | None = None) -> str:
    """Deliver the session state as it stood at a rollback checkpoint.

    Sent after a checkout so the renderer can redraw the whole transcript
    without re-fetching it, and every field of the reconstructed state is
    included so nothing has to be re-derived.

    Args:
        session_id: The session that was checked out.
        commit_hash: The checkpoint that was restored to.
        messages: The transcript at that point.
        turn_count: Number of completed turns at that point.
        plan_items: The work plan as recorded at that point.
        artifacts: Artifacts recorded at that point.
        references: References recorded at that point.

    Returns:
        The encoded ``rollback_checkout`` frame.
    """
    return encode_server_message("rollback_checkout", session_id=session_id, commit_hash=commit_hash, messages=messages, turn_count=turn_count, plan_items=plan_items or [], artifacts=artifacts or [], references=references or [])


def encode_messages_updated(messages: list[dict[str, Any]], session_id: str, commit_hash: str = "") -> str:
    """Broadcast a refreshed transcript after an edit or delete.

    Sent to every connection watching the session, so concurrent editors see
    the same result rather than each keeping a divergent copy.

    Args:
        messages: The transcript after the change.
        session_id: The session that changed.
        commit_hash: The checkpoint the new state is recorded at, when one was
            created for the edit.

    Returns:
        The encoded ``messages_updated`` frame.
    """
    return encode_server_message("messages_updated", messages=messages, session_id=session_id, commit_hash=commit_hash)


def encode_session_deleted(session_id: str) -> str:
    """Broadcast that a session was deleted, so other renderers drop it."""
    return encode_server_message("session_deleted", session_id=session_id)


def encode_session_exported(session_id: str, markdown: str, filename: str) -> str:
    """Deliver a single session's markdown export inline.

    Args:
        session_id: The exported session.
        markdown: The rendered transcript.
        filename: Suggested download name.

    Returns:
        The encoded ``session_exported`` frame.
    """
    return encode_server_message("session_exported", session_id=session_id, markdown=markdown, filename=filename)


def encode_sessions_exported_zip(zip_base64: str, filename: str) -> str:
    """Deliver a multi-session export as a base64-encoded archive.

    Base64 keeps the archive inside the text-frame envelope of the protocol
    rather than requiring a separate binary channel.

    Args:
        zip_base64: The encoded archive.
        filename: Suggested download name.

    Returns:
        The encoded ``sessions_exported_zip`` frame.
    """
    return encode_server_message("sessions_exported_zip", zip_base64=zip_base64, filename=filename)


def encode_data_exported_zip(zip_path: str, filename: str, request_id: str = "") -> str:
    """Report that a full data-directory export is ready on disk.

    Unlike :func:`encode_sessions_exported_zip` this carries a path rather than
    inline bytes: the payload is the entire ``data/`` tree, which is far too
    large to ship over the wire when the desktop renderer already runs on the
    same machine.

    Args:
        zip_path: Filesystem path of the built archive.
        filename: Display name for the archive.
        request_id: Echoed from the originating :class:`ClientExportData`.

    Returns:
        The encoded ``data_exported_zip`` frame.
    """
    return encode_server_message("data_exported_zip", zip_path=zip_path, filename=filename, request_id=request_id)


def encode_data_export_progress(done: int, total: int, percent: int, file: str = "") -> str:
    """Report progress of a data-directory export.

    Emitted repeatedly while the archive is built so the renderer can show a
    real progress bar instead of an indeterminate spinner.

    Args:
        done: Entries processed so far.
        total: Entries expected in total.
        percent: Completion percentage.
        file: The entry currently being processed.

    Returns:
        The encoded ``data_export_progress`` frame.
    """
    return encode_server_message(
        "data_export_progress", done=done, total=total, percent=percent, file=file
    )


def encode_data_import_progress(done: int, total: int, percent: int, file: str = "") -> str:
    """Report progress of a data-directory import."""
    return encode_server_message(
        "data_import_progress", done=done, total=total, percent=percent, file=file
    )


def encode_data_import_done(files: int, restored: int, skipped: int, overwritten: int = 0, kept: int = 0) -> str:
    """Report the outcome of a data-directory import.

    The counters are separate because each describes a distinct user-visible
    outcome of the merge policy: files that were restored, files left alone
    because they already existed, files replaced, and files preserved.

    Args:
        files: Total entries in the archive.
        restored: Entries written to the data directory.
        skipped: Entries skipped by the merge policy.
        overwritten: Entries that replaced an existing file.
        kept: Existing files preserved instead of being replaced.

    Returns:
        The encoded ``data_import_done`` frame.
    """
    return encode_server_message(
        "data_import_done",
        files=files,
        restored=restored,
        skipped=skipped,
        overwritten=overwritten,
        kept=kept,
    )


def encode_session_renamed(session_id: str, new_name: str) -> str:
    """Broadcast a session rename to every connection watching it."""
    return encode_server_message("session_renamed", session_id=session_id, new_name=new_name)


def encode_agents_list(agents: list[dict[str, Any]], active_index: int) -> str:
    """Reply with the agent table and which agent is currently selected."""
    return encode_server_message("agents_list", agents=agents, active_index=active_index)


def encode_agents_updated(agents: list[dict[str, Any]], active_index: int) -> str:
    """Broadcast an agent table change so all renderers reindex together.

    Sent because indexes shift when an agent is added or removed, so a stale
    index would silently point at the wrong agent.
    """
    return encode_server_message("agents_updated", agents=agents, active_index=active_index)


def encode_sub_agents_updated(sub_agents: list[dict[str, Any]]) -> str:
    """Broadcast the refreshed sub-agent set."""
    return encode_server_message("sub_agents_updated", sub_agents=sub_agents)


def encode_artifacts_update(artifacts: list[dict[str, Any]]) -> str:
    """Push the current artifact list for the session.

    Artifacts are the files and links the agent produced during a turn; sending
    the full list rather than a delta keeps every renderer's panel identical
    without tracking per-artifact events.
    """
    return encode_server_message("artifacts_update", artifacts=artifacts)


def encode_assistant_boundary() -> str:
    """Mark where one assistant message ends and the next begins.

    Deliberately encrypted (the default) because it is rare and small, unlike
    the delta frames, so encryption is free here and it lets a client distinguish
    a real boundary from a stream artifact.

    Returns:
        The encoded ``assistant_boundary`` frame.
    """
    return encode_server_message("assistant_boundary")
