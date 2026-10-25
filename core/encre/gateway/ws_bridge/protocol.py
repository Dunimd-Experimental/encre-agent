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

"""Wire vocabulary of the remote-adapter WebSocket bridge.

This module is the bridge's own message protocol: it defines
:class:`GatewayOp` (the opcodes) and
:class:`GatewayMessage` (the envelope) exchanged between
:class:`encre.gateway.ws_bridge.server.WsBridgeServer` on the agent
side and :class:`encre.gateway.ws_bridge.client.GatewayClient` on the
remote-adapter side.

It is deliberately NOT :mod:`encre.server.protocol`, which defines the
desktop client protocol over
:class:`encre.transport.ws.EncreWSHandler`.  The two protocols serve
unrelated peers -- one carries gateway traffic to a platform adapter in
another process, the other carries the desktop renderer's UI traffic --
and share nothing beyond the transport.

Every message serialises to a flat JSON object of the shape
``{"op": <str>, "d": <dict>, "seq": <int>}``:

* ``op``  -- the opcode, the string value of a :class:`GatewayOp`.
* ``d``   -- the operation-specific payload; the key contract of each
  field lives in the factory that builds that operation.
* ``seq`` -- a monotonic per-sender counter, used for log correlation
  only.  It never routes a message; routing is by ``request_id`` inside
  ``d``.

Keeping one shared definition on both sides means a client and a server
cannot silently disagree about the wire format.  Both transports are
single-WebSocket, so there is no framing or chunking logic here.

Public API: :class:`GatewayOp`, :class:`GatewayMessage` and its factory
constructors, :meth:`GatewayMessage.to_dict`,
:meth:`GatewayMessage.from_dict`.
"""

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any


class GatewayOp(enum.Enum):
    """Opcodes of the gateway bridge wire protocol.

    Each member's value is the exact string written into the ``op``
    field of a :class:`GatewayMessage`.  The set covers five groups:

    * Connection lifecycle: :attr:`HELLO` (adapter announces itself),
      :attr:`HEARTBEAT` / :attr:`HEARTBEAT_ACK` (application-level
      liveness ping and reply).
    * Request flow: :attr:`SUBMIT` (collect the whole reply) and
      :attr:`SUBMIT_STREAM` (stream reply events back).
    * Streaming payload: :attr:`TEXT_DELTA`, :attr:`TOOL_RESULT`,
      :attr:`FINISH` (terminal) and :attr:`ERROR` (terminal).
    * Adapter metadata: :attr:`ADAPTER_INFO` (full state push) and
      :attr:`ADAPTER_UPDATE` (incremental status/capability change).
    * Control: :attr:`CANCEL` (abort an in-flight session) and
      :attr:`SHUTDOWN` (remote requests a clean teardown).

    A terminal opcode (:attr:`FINISH` or :attr:`ERROR`) must close a
    request; the bridge client treats anything else after the terminal
    message of a ``request_id`` as belonging to a later request.
    """

    HELLO = "hello"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    SUBMIT = "submit"
    SUBMIT_STREAM = "submit_stream"
    TEXT_DELTA = "text_delta"
    TOOL_RESULT = "tool_result"
    FINISH = "finish"
    ERROR = "error"
    ADAPTER_INFO = "adapter_info"
    ADAPTER_UPDATE = "adapter_update"
    CANCEL = "cancel"
    SHUTDOWN = "shutdown"


@dataclass
class GatewayMessage:
    """One envelope exchanged over the bridge WebSocket.

    A message is intentionally dumb: one opcode, one payload dict, one
    sequence number.  All meaning is carried by the opcode plus the
    payload contract documented on each factory below, so a new opcode
    never requires an envelope change.

    Instances are mutated in place by the transports
    (:attr:`seq` is assigned at send time), so a message object should
    be used for a single send rather than shared across tasks.

    Attributes:
        op: What this message means.  Serialised as ``op.value``.
        data: Operation-specific payload.  Serialised under the short
            key ``"d"`` to keep frames small.  The exact keys depend on
            ``op``.
        seq: Monotonic send counter assigned by the sender for log
            correlation.  Defaults to ``0``; transports overwrite it
            before serialising.
    """

    op: GatewayOp
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire dictionary form.

        Returns:
            A ``{"op": <str>, "d": <dict>, "seq": <int>}`` mapping ready
            to hand to :func:`json.dumps`.  The payload dict is emitted
            by reference, not copied, so callers must not mutate it
            after this call if they still hold it.

        Raises:
            Nothing.
        """
        return {
            "op": self.op.value,
            "d": self.data,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GatewayMessage":
        """Reconstruct a message from one decoded JSON object.

        Args:
            raw: The parsed JSON object.  ``op`` is optional on the wire
                and defaults to an empty string, which fails enum
                resolution; ``d`` defaults to ``{}`` and ``seq`` to
                ``0`` so a partially-formed frame is still usable.

        Returns:
            The reconstructed message.

        Raises:
            ValueError: If ``raw["op"]`` is not a value of
                :class:`GatewayOp`.  Callers treat this as an untrusted
                frame and drop it rather than closing the socket.
        """
        return cls(
            op=GatewayOp(raw.get("op", "")),
            data=raw.get("d", {}),
            seq=raw.get("seq", 0),
        )

    @staticmethod
    def hello(adapter_name: str) -> "GatewayMessage":
        """Build the HELLO frame that opens a bridge connection.

        Sent by the remote adapter immediately after the TCP/WS
        handshake; the server replies with its own HELLO carrying the
        resolved adapter name.  This round trip is what promotes a raw
        socket into a registered adapter -- the connection is not added
        to the registry until it is answered.

        Args:
            adapter_name: The identifier the adapter wants to be known
                by.  The server may override it on its reply.

        Returns:
            A HELLO message with ``{"name": adapter_name}``.  Callers
            attach ``capabilities`` to ``data`` after construction.
        """
        return GatewayMessage(op=GatewayOp.HELLO, data={"name": adapter_name})

    @staticmethod
    def heartbeat() -> "GatewayMessage":
        """Build an empty HEARTBEAT ping.

        Application-level liveness, distinct from the WebSocket-protocol
        PING/PONG that :mod:`websockets` already performs.  The
        application ping lets each side confirm the *other side's*
        receive loop is alive, not just the TCP stack.

        Returns:
            A HEARTBEAT message with no payload.
        """
        return GatewayMessage(op=GatewayOp.HEARTBEAT)

    @staticmethod
    def heartbeat_ack() -> "GatewayMessage":
        """Build the reply to a received HEARTBEAT.

        Returns:
            A HEARTBEAT_ACK message with no payload.  Carrying no
            payload keeps the ack path allocation-free enough to run
            every few seconds per connection.
        """
        return GatewayMessage(op=GatewayOp.HEARTBEAT_ACK)

    @staticmethod
    def submit(
        prompt: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
        source: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> "GatewayMessage":
        """Build a one-shot SUBMIT request.

        The server is expected to run the turn and answer with a single
        ``TEXT_DELTA`` followed by a terminal ``FINISH`` (or an
        ``ERROR``), all under the same ``request_id``.

        Args:
            prompt: The user prompt to run.
            session_id: Optional conversation session id.  When absent
                the server creates an adapter-default session.
            system_prompt: Optional system prompt override.
            source: Optional origin metadata as a
                :class:`encre.gateway.session.SessionSource` dict.  When
                present the server resolves the session from it instead
                of using the adapter-default session, which is how a
                remote adapter reports a platform message.
            request_id: Optional caller-supplied correlation id.  A
                random one is generated when omitted; the server echoes
                it back on every reply frame.

        Returns:
            A SUBMIT message with ``request_id`` plus any provided
            optional fields.  Omitted optional fields are absent from
            ``data`` rather than present-but-``None``, so receivers can
            test key presence.
        """
        d: dict[str, Any] = {"prompt": prompt}
        d["request_id"] = request_id or uuid.uuid4().hex
        if session_id:
            d["session_id"] = session_id
        if system_prompt:
            d["system_prompt"] = system_prompt
        if source:
            d["source"] = source
        return GatewayMessage(op=GatewayOp.SUBMIT, data=d)

    @staticmethod
    def submit_stream(
        prompt: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
        source: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> "GatewayMessage":
        """Build a SUBMIT_STREAM request expecting streamed replies.

        Same payload contract as :meth:`submit`, but the server streams
        ``TEXT_DELTA`` / ``TOOL_RESULT`` frames under the same
        ``request_id`` until a terminal ``FINISH`` or ``ERROR``.  This
        is the path
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`-
        shaped traffic uses, since partial output must reach the remote
        adapter incrementally.

        Args:
            prompt: The user prompt to run.
            session_id: Optional conversation session id.
            system_prompt: Optional system prompt override.
            source: Optional origin metadata dict.
            request_id: Optional caller-supplied correlation id; a
                random one is generated when omitted.

        Returns:
            A SUBMIT_STREAM message with ``request_id`` plus any
            provided optional fields.
        """
        d: dict[str, Any] = {"prompt": prompt}
        d["request_id"] = request_id or uuid.uuid4().hex
        if session_id:
            d["session_id"] = session_id
        if system_prompt:
            d["system_prompt"] = system_prompt
        if source:
            d["source"] = source
        return GatewayMessage(op=GatewayOp.SUBMIT_STREAM, data=d)

    @staticmethod
    def text_delta(text: str, msg_id: str = "", session_id: str = "", request_id: str = "") -> "GatewayMessage":
        """Build a TEXT_DELTA frame carrying one incremental text chunk.

        Args:
            text: The incremental assistant text fragment.  Serialized
                under the key ``"text"``.
            msg_id: Optional message id this delta belongs to.
                Serialized under the key ``"id"``.
            session_id: Optional session id, added only when non-empty.
            request_id: Optional request id linking to the originating
                submit, added only when non-empty.

        Returns:
            A TEXT_DELTA message with ``text`` and ``id`` plus any
            provided optional fields.

        Note:
            Receiving a TEXT_DELTA is never terminal.  A remote adapter
            must keep accumulating until FINISH or ERROR arrives for the
            same ``request_id``.
        """
        d: dict[str, Any] = {"text": text, "id": msg_id}
        if session_id:
            d["session_id"] = session_id
        if request_id:
            d["request_id"] = request_id
        return GatewayMessage(op=GatewayOp.TEXT_DELTA, data=d)

    @staticmethod
    def tool_result(tool_id: str, content: str, is_error: bool = False, request_id: str = "") -> "GatewayMessage":
        """Build a TOOL_RESULT frame reporting one tool's output.

        Mirrors the ``ToolResult`` agent event on
        :mod:`encre.utils.types`; the bridge flattens it to plain JSON
        because the envelope has no nested object support.

        Args:
            tool_id: The identifier of the finished tool call.
            content: The textual result returned by the tool.
            is_error: ``True`` when the tool failed rather than returned
                a value.
            request_id: Optional request id linking to the originating
                submit, added only when non-empty.

        Returns:
            A TOOL_RESULT message with ``id``, ``content`` and
            ``is_error`` (plus ``request_id`` when provided).
        """
        d = {"id": tool_id, "content": content, "is_error": is_error}
        if request_id:
            d["request_id"] = request_id
        return GatewayMessage(op=GatewayOp.TOOL_RESULT, data=d)

    @staticmethod
    def finish(reason: str = "done", usage: dict[str, Any] | None = None, error: str = "", request_id: str = "", context_usage: dict[str, Any] | None = None) -> "GatewayMessage":
        """Build a terminal FINISH frame ending one request.

        Every request started by :meth:`submit` or
        :meth:`submit_stream` must end with either a FINISH or an
        ERROR; the bridge client breaks its read loop on whichever
        arrives first.

        Args:
            reason: A short completion reason, e.g. ``"done"`` or
                ``"cancelled"``.
            usage: Optional token-usage statistics dict, added only when
                non-empty.
            error: Optional error detail; present when the turn failed
                but the server still emitted FINISH rather than ERROR.
            request_id: Optional request id linking to the originating
                submit, added only when non-empty.
            context_usage: Optional per-request context composition
                breakdown (system prompt / messages / tools by connector
                kind / skills), added only when non-empty.

        Returns:
            A FINISH message carrying ``reason`` plus any provided
            optional fields.
        """
        d: dict[str, Any] = {"reason": reason}
        if usage:
            d["usage"] = usage
        if context_usage:
            d["context_usage"] = context_usage
        if error:
            d["error"] = error
        if request_id:
            d["request_id"] = request_id
        return GatewayMessage(op=GatewayOp.FINISH, data=d)

    @staticmethod
    def error(message: str, request_id: str = "") -> "GatewayMessage":
        """Build a terminal ERROR frame describing a failure.

        Args:
            message: A human-readable error description.
            request_id: Optional request id linking to the originating
                submit, added only when non-empty.

        Returns:
            An ERROR message with ``message`` (and ``request_id`` when
            provided).  Terminal like FINISH: the receiver must stop
            waiting for this request after it.
        """
        d = {"message": message}
        if request_id:
            d["request_id"] = request_id
        return GatewayMessage(op=GatewayOp.ERROR, data=d)

    @staticmethod
    def adapter_info(name: str, status: str, capabilities: list[str] | None = None) -> "GatewayMessage":
        """Build an ADAPTER_INFO frame describing an adapter's state.

        Used for full state pushes, as opposed to
        :class:`GatewayOp.ADAPTER_UPDATE` which carries only changed
        fields.

        Args:
            name: The adapter identifier.
            status: A status string, e.g. ``"online"`` or ``"offline"``.
            capabilities: Optional list of capability strings, added
                only when non-empty.

        Returns:
            An ADAPTER_INFO message with ``name`` and ``status`` (and
            ``capabilities`` when provided).
        """
        d: dict[str, Any] = {"name": name, "status": status}
        if capabilities:
            d["capabilities"] = capabilities
        return GatewayMessage(op=GatewayOp.ADAPTER_INFO, data=d)

    @staticmethod
    def cancel(session_id: str) -> "GatewayMessage":
        """Build a CANCEL frame aborting one in-flight session.

        Args:
            session_id: The session whose running request should be
                cancelled.  The server forwards this to the runner's
                event router rather than to the adapter.

        Returns:
            A CANCEL message carrying ``{"session_id": session_id}``.
        """
        return GatewayMessage(op=GatewayOp.CANCEL, data={"session_id": session_id})
