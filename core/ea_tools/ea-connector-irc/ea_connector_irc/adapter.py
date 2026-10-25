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

"""IRC platform adapter for Encre's IM gateway.

This module is one of the vendor adapters under
:mod:`encre.gateway.platforms`.  It is dependency-free: the IRC protocol is
implemented directly on top of Python's standard-library asyncio sockets, so
no third-party IRC client is required to run the platform.  The adapter
speaks RFC 1459 style messages over a single TCP (optionally TLS) socket.

Inbound, the receive loop reads CRLF-delimited protocol lines, answers PING
with PONG, watches for the ``001`` RPL_WELCOME reply that completes
registration, and routes PRIVMSG traffic through
:meth:`IRCAdapter._handle_line` into a
:class:`encre.gateway.platforms.base.MessageEvent` addressed to either the
channel name or the sender's nick.  Outbound, agent text is stripped of
markdown, split by byte count to respect the IRC line limit, and sent as
separate PRIVMSGs paced to stay clear of server flood limits.  The
:class:`encre.gateway.platforms.base.SendResult` value returned by
:meth:`IRCAdapter.send` is read by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

:class:`encre.gateway.run.GatewayRunner` owns the lifecycle and builds the
adapter from a :class:`encre.gateway.config.PlatformConfig`.  Configuration
reads the environment first and falls back to the config ``extra`` mapping,
so ``config.yaml`` and exported environment variables can both drive the
platform and environment always wins.  Recognised variables: ``IRC_SERVER``,
``IRC_PORT``, ``IRC_NICKNAME``, ``IRC_CHANNEL``, ``IRC_USE_TLS``,
``IRC_SERVER_PASSWORD``, ``IRC_NICKSERV_PASSWORD``,
``IRC_ALLOWED_USERS``/``IRC_ALLOW_ALL_USERS``, ``IRC_HOME_CHANNEL``, and
``IRC_HOME_CHANNEL_NAME``.

Example ``config.yaml``::

    gateway:
      platforms:
        irc:
          enabled: true
          extra:
            server: irc.libera.chat
            port: 6697
            nickname: encre-bot
            channel: "#encre"
            use_tls: true
            server_password: ""
            nickserv_password: ""
            allowed_users: []
            max_message_length: 450

An empty ``allowed_users`` list allows every nick.  This is deliberately
broad because IRC nicks are self-declared rather than authenticated, and an
allow-list is the only local defence against a spoofed sender.  Registration
is stateful: the adapter waits for ``001`` before considering itself joined,
and claims a scoped lock on ``server:nick`` so two profiles cannot share one
IRC identity.  Nick collisions (``433``) are recovered by retrying with an
incremented suffix, and channel messages must address the bot explicitly
(``nick:`` or ``nick,``) before they are answered, which keeps a busy public
channel from becoming an uncontrolled trigger.

Public API:
    * :class:`IRCAdapter` -- the adapter contract implementation.
    * :func:`check_requirements`, :func:`validate_config`,
      :func:`is_connected` -- readiness and configuration probes.
    * :func:`_env_enablement` -- environment seed for config loading.
    * :func:`_standalone_send` -- out-of-process delivery for callers with
      no live adapter instance.
"""

import asyncio
import logging
import os
import re
import ssl
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The base adapter and the config enum are imported here at module scope, but
# the gateway-only collaborators are imported lazily inside methods below.
# Those collaborators live further down the import graph, so importing them
# unconditionally would create a cycle at discovery time.


from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from encre.gateway.config import Platform
from encre.gateway.status import acquire_scoped_lock


# -- IRC protocol helpers --
# Minimal RFC 1459 line parsing shared by the adapter and the standalone
# sender, so both interpret the wire format identically.


def _parse_irc_message(raw: str) -> dict:
    """Split one raw IRC protocol line into its protocol components.

    IRC lines have an optional ``:prefix``, a command, space-separated
    parameters, and a single optional trailing parameter introduced by `` :``.
    The trailing form is what carries PRIVMSG bodies, because message text
    may itself contain spaces and must not be word-split.

    Args:
        raw: One protocol line with the CRLF terminator already removed.

    Returns:
        Dict with three keys: ``prefix`` (empty string when the line is
        unprefixed, which is common for commands we send), ``command``, and
        ``params`` (list; the trailing parameter, if present, is appended
        last so it keeps its internal whitespace intact).

    Raises:
        Nothing; malformed lines degrade to empty components rather than
        raising, since a single bad line must not terminate the receive loop.
    """
    prefix = ""
    trailing = ""

    if raw.startswith(":"):
        try:
            prefix, raw = raw[1:].split(" ", 1)
        except ValueError:
            # A bare colon with nothing after it is malformed but not fatal;
            # keep whatever came after the colon as the prefix and discard
            # the rest rather than crashing the receive loop.
            prefix = raw[1:]
            raw = ""

    # The " :" sequence starts the trailing parameter.  It must be located
    # before word-splitting, otherwise a PRIVMSG body containing spaces would
    # be torn apart into separate params and the message text would be lost.
    if " :" in raw:
        raw, trailing = raw.split(" :", 1)

    parts = raw.split()
    command = parts[0] if parts else ""
    params = parts[1:] if len(parts) > 1 else []
    if trailing:
        params.append(trailing)

    return {"prefix": prefix, "command": command, "params": params}


def _extract_nick(prefix: str) -> str:
    """Extract the nickname from an IRC source prefix.

    Prefixes are of the form ``nick!user@host``.  Only the part before the
    first ``!`` identifies the sender for authorization and echo suppression,
    so the rest is discarded.

    Args:
        prefix: Raw prefix string, possibly empty.

    Returns:
        The nickname when a ``!`` delimiter is present, otherwise the input
        unchanged.  An empty prefix therefore returns an empty string, which
        callers treat as an unidentifiable sender.
    """
    return prefix.split("!")[0] if "!" in prefix else prefix


# -- IRC Adapter --


class IRCAdapter(BasePlatformAdapter):
    """Async IRC adapter implementing the base platform adapter contract.

    Instantiated by the adapter factory registered at the bottom of this
    module and driven by
    :class:`encre.gateway.run.GatewayRunner`.  It owns one TCP (optionally
    TLS) socket for the lifetime of the connection: the same writer is used
    to send registration commands and PRIVMSGs, and the same reader feeds
    :meth:`_receive_loop`.

    Invariants:
        * The socket is only written after registration has completed.  Until
          ``_registered`` is set, sending PRIVMSG would race the server's
          setup replies and could be silently dropped.
        * ``_current_nick`` is the nick the server reports we hold, not
          necessarily the configured ``nickname``.  Nick collisions and a
          later ``NICK`` self-change are both folded into it, so all
          addressing and echo suppression compare against it.
        * ``_allowed_users_lower`` is always lower-cased.  IRC nick identity
          is case-insensitive at the protocol level, so a case-sensitive
          comparison would let a spoofed sender bypass the allow-list.

    Lifecycle:
        Constructed with a :class:`encre.gateway.config.PlatformConfig`;
        :meth:`connect` dials, registers, joins, and claims the scoped nick
        lock; :meth:`disconnect` releases that lock, quits, and tears the
        socket down.

    Async-safety:
        All state is touched only from the adapter's event loop.  The receive
        loop and :meth:`send` share the single writer, and the loop is the
        only place that mutates ``_current_nick``, so no locking is needed.

    Attributes:
        server: IRC server hostname to dial.
        port: Server port, defaulting to the TLS port 6697.
        nickname: Nick requested at registration.
        channel: Channel to join, e.g. ``#encre``.
        use_tls: Whether to wrap the socket in TLS.
        server_password: Value for the ``PASS`` command, or empty.
        nickserv_password: NickServ identification password, or empty.
        allowed_users: Configured allow-list of nicks; empty means allow all.
        _allowed_users_lower: Case-folded form of the allow-list used for
            comparisons.
        max_message_length: Configured per-line character cap, applied on top
            of the protocol byte limit.
        _reader: Async stream reader for the connection.
        _writer: Async stream writer for the connection.
        _recv_task: Background task running the receive loop.
        _current_nick: Nick the server reports we currently hold.
        _registered: Whether ``001`` RPL_WELCOME has been received.
        _registration_event: Signalled by the receive loop to release
            :meth:`connect` once registration completes.
    """

    def __init__(self, config, **kwargs):
        """Resolve configuration and initialize adapter state.

        Construction performs no I/O: the runner wires collaborators and only
        then calls :meth:`connect`.  Every connection setting reads the
        environment first and falls back to the config ``extra`` mapping, so
        an exported environment variable always wins over ``config.yaml``.
        That precedence is intentional -- operators typically fix long-term
        values in the config file and override them per host with the
        environment.

        Args:
            config: Platform configuration.  Its ``extra`` mapping supplies
                ``server``, ``port``, ``nickname``, ``channel``, ``use_tls``,
                ``server_password``, ``nickserv_password``, ``allowed_users``
                and ``max_message_length``.
            **kwargs: Accepted and ignored for factory signature parity.

        Raises:
            Nothing.  An unset server or channel is not an error here; it is
            reported later by :meth:`connect`, which is the layer that owns
            fatal error recording.
        """
        platform = Platform("irc")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Connection settings.  Environment values override the config file.
        self.server = os.getenv("IRC_SERVER") or extra.get("server", "")
        try:
            self.port = int(os.getenv("IRC_PORT") or extra.get("port", 6697))
        except (ValueError, TypeError):
            # A mistyped port must not prevent the adapter from starting with
            # the standard TLS port rather than raising at construction time.
            self.port = 6697
        self.nickname = os.getenv("IRC_NICKNAME") or extra.get("nickname", "encre-bot")
        self.channel = os.getenv("IRC_CHANNEL") or extra.get("channel", "")
        self.use_tls = (
            os.getenv("IRC_USE_TLS", "").lower() in {"1", "true", "yes"}
            if os.getenv("IRC_USE_TLS")
            else extra.get("use_tls", True)
        )
        self.server_password = os.getenv("IRC_SERVER_PASSWORD") or extra.get("server_password", "")
        self.nickserv_password = os.getenv("IRC_NICKSERV_PASSWORD") or extra.get("nickserv_password", "")

        # Authorization policy.  An empty allow-list means allow everyone,
        # which is the correct reading because IRC nicks are self-declared
        # and cannot be authenticated by the client.
        self.allowed_users: list = extra.get("allowed_users", [])
        # IRC nicks are case-insensitive, so all lookups use folded copies.
        self._allowed_users_lower: set = {u.lower() for u in self.allowed_users if isinstance(u, str)}

        # Outbound line budget.  The configured value is applied on top of
        # the protocol byte limit, so it can only ever shrink the effective
        # cap.  The registry entry is a fallback for installs that set the
        # limit there rather than in the platform config.
        max_msg = extra.get("max_message_length")
        if max_msg is None:
            try:
                from encre.gateway.platform_registry import platform_registry
                entry = platform_registry.get("irc")
                if entry and entry.max_message_length:
                    max_msg = entry.max_message_length
            except Exception:
                pass
        self.max_message_length = int(max_msg or 450)

        # Runtime connection state, populated by connect() and cleared by
        # disconnect().
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._current_nick = self.nickname
        self._registered = False  # set once 001 RPL_WELCOME arrives
        self._registration_event = asyncio.Event()

    @property
    def name(self) -> str:
        """Canonical platform name used for routing and logging.

        Overrides the base implementation so the adapter reports a stable
        display label regardless of how the :class:`Platform` value was
        constructed.  This string is the platform component of session keys
        and of log prefixes, so it must not change across restarts.

        Args:
            None.

        Returns:
            ``"IRC"``.
        """
        return "IRC"

    # -- Connection lifecycle --

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Dial the IRC server, register, and join the configured channel.

        Registration is a stateful handshake with the server: after the
        socket opens the adapter sends ``PASS`` (when a server password is
        configured), ``NICK``, and ``USER``, then waits for the server's
        ``001`` RPL_WELCOME reply before it considers itself joined.  Only
        after that does it identify with NickServ and join the channel.
        Sending PRIVMSG before registration would be dropped by the server.

        Before dialing, the adapter claims a scoped lock keyed on
        ``server:nick``.  Two Encre profiles using the same identity on the
        same network would otherwise fight over the nick in a collision
        loop, so the second one is refused here rather than after the
        socket opens.

        Args:
            is_reconnect: Accepted for base-contract signature compatibility.
                Registration is a fresh handshake every time, so this flag is
                not consulted.

        Returns:
            ``True`` when the adapter is registered and has joined the
            channel.  ``False`` when configuration is missing, the identity
            lock is already held, the socket cannot be opened, or the
            server never confirmed registration.

        Raises:
            Nothing.  Every failure is recorded with
            :meth:`BasePlatformAdapter._set_fatal_error` so the runner can
            decide whether to retry, rather than propagating an exception.
        """
        if not self.server or not self.channel:
            logger.error("IRC: server and channel must be configured")
            self._set_fatal_error(
                "config_missing",
                "IRC_SERVER and IRC_CHANNEL must be set",
                retryable=False,
            )
            return False

        # Claim the identity before opening any socket.  Two profiles sharing
        # one nick on one network would otherwise trade 433 replies forever.
        lock_key = f"{self.server}:{self.nickname}"
        if not acquire_scoped_lock("irc", lock_key):
            logger.error("IRC: %s@%s already in use by another profile", self.nickname, self.server)
            self._set_fatal_error("lock_conflict", "IRC identity in use by another profile", retryable=False)
            return False
        self._lock_key = lock_key

        try:
            ssl_ctx = None
            if self.use_tls:
                ssl_ctx = ssl.create_default_context()

            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.server, self.port, ssl=ssl_ctx),
                timeout=30.0,
            )
        except Exception as e:
            logger.error("IRC: failed to connect to %s:%s 鈥?%s", self.server, self.port, e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        # Registration handshake.  Order is mandated by the protocol: the
        # password must precede the nick, and the nick must precede the user.
        if self.server_password:
            await self._send_raw(f"PASS {self.server_password}")
        await self._send_raw(f"NICK {self.nickname}")
        await self._send_raw(f"USER {self.nickname} 0 * :Encre Agent")

        # Start reading before waiting on registration so the 001 reply is
        # consumed and processed instead of sitting in the socket buffer.
        self._recv_task = asyncio.create_task(self._receive_loop())

        # Block until the server confirms registration, or give up.  Without
        # this wait a caller could send PRIVMSG while the adapter is still
        # unidentified, which servers silently ignore.
        try:
            await asyncio.wait_for(self._registration_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("IRC: registration timed out")
            await self.disconnect()
            self._set_fatal_error("registration_timeout", "IRC server did not send RPL_WELCOME", retryable=True)
            return False

        # Optional NickServ identification, after which the nick is protected
        # against taking.
        if self.nickserv_password:
            await self._send_raw(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")
            # Allow NickServ time to release the nick before we join.
            await asyncio.sleep(2)

        await self._send_raw(f"JOIN {self.channel}")

        self._mark_connected()
        logger.info("IRC: connected to %s:%s as %s, joined %s", self.server, self.port, self._current_nick, self.channel)
        return True

    async def disconnect(self) -> None:
        """Quit the server, release the identity lock, and close the socket.

        Idempotent by construction: every handle is checked and nulled as it
        is released, so a second call and a call before :meth:`connect` are
        both no-ops rather than errors.  The registration flag and event are
        reset so a later reconnect does not see a stale registration.

        Args:
            None.

        Returns:
            Nothing.
        """
        # Release the identity lock first so another profile can take the
        # nick as soon as we are done, not only after the socket closes.
        if getattr(self, "_lock_key", None):
            try:
                from encre.gateway.status import release_scoped_lock
                release_scoped_lock("irc", self._lock_key)
            except Exception:
                pass
        self._mark_disconnected()
        if self._writer and not self._writer.is_closing():
            try:
                await self._send_raw("QUIT :Encre Agent shutting down")
                # Give the server a moment to propagate the quit before the
                # socket disappears under it.
                await asyncio.sleep(0.5)
            except Exception:
                pass
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        self._reader = None
        self._writer = None
        self._registered = False
        self._registration_event.clear()

    # -- Sending --

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Send agent output to *chat_id* as IRC PRIVMSG lines.

        This is the single outbound method called by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.  The
        body is downgraded and split by :meth:`_split_message`, then each
        chunk is sent as its own PRIVMSG.  Chunks are paced with a short
        sleep because IRC servers enforce per-second message flood limits
        and reject or disconnect a client that exceeds them.

        There is no threading primitive on IRC, so *reply_to* is ignored: a
        reply cannot be nested under the message it answers.

        Args:
            chat_id: Target channel (``#name``) or nick for a direct message.
            content: Outbound text, still containing agent markdown.
            reply_to: Ignored; IRC has no message threading.
            metadata: Ignored; accepted for base-contract compatibility.

        Returns:
            :class:`encre.gateway.platforms.base.SendResult`.  ``success`` is
            ``False`` immediately when the connection is gone, or when any
            single line fails to write.  ``message_id`` is a millisecond
            timestamp because IRC servers do not assign durable message ids.

        Raises:
            Nothing; write failures are converted to a failed result so a
            broken socket cannot tear down the caller's turn.
        """
        if not self._writer or self._writer.is_closing():
            return SendResult(success=False, error="Not connected")

        # The chat id is already in IRC addressing form: a channel name for
        # group conversations, a bare nick for a direct message.
        target = chat_id
        lines = self._split_message(content, target)

        for line in lines:
            try:
                await self._send_raw(f"PRIVMSG {target} :{line}")
                # Servers enforce a per-second flood limit; pacing here
                # avoids being disconnected for bursting a whole reply.
                await asyncio.sleep(0.3)
            except Exception as e:
                return SendResult(success=False, error=str(e))

        return SendResult(success=True, message_id=str(int(time.time() * 1000)))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op typing indicator.

        IRC has no equivalent of a typing signal, so the empty implementation
        is correct.  Callers must treat a no-op as normal, not as a failure.

        Args:
            chat_id: Ignored; accepted for base-contract compatibility.
            metadata: Ignored; accepted for base-contract compatibility.

        Returns:
            Nothing.
        """
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Describe a chat for session routing and the desktop UI listing.

        IRC identifies conversation shape purely by the target prefix: a
        leading ``#`` (or a network-specific ``&``) means a channel,
        anything else is a bare nick and therefore a direct message.

        Args:
            chat_id: Channel name or nick to describe.

        Returns:
            Mapping with the ``name`` set to the identifier and ``type`` set
            to ``"group"`` for channels and ``"dm"`` for nicks.

        Note:
            ``+`` and ``!`` channel prefixes exist on some networks but are
            not treated as channels here, matching the split used in
            :meth:`_handle_line` when routing inbound traffic.
        """
        is_channel = chat_id.startswith("#") or chat_id.startswith("&")
        return {
            "name": chat_id,
            "type": "group" if is_channel else "dm",
        }

    # -- Message splitting --

    def _split_message(self, content: str, target: str) -> List[str]:
        """Split agent output into chunks each within the IRC line limit.

        IRC caps a line at roughly 512 bytes including the protocol framing,
        so the effective text budget is the cap minus the length of
        ``PRIVMSG <target> :`` and the CRLF terminator.  The configured
        :attr:`max_message_length` is applied as a second, tighter cap so an
        operator can shrink output on sensitive networks.

        Splitting is byte-aware rather than character-aware because multibyte
        characters can straddle the boundary.  A binary search finds the
        largest prefix that still fits in bytes, then the split point is
        backed up to the nearest space when doing so would not create a
        degenerate fragment, which keeps lines from breaking mid-word.

        Args:
            content: Outbound text, possibly containing markdown.
            target: Channel or nick the text is addressed to, needed to
                measure the exact protocol framing overhead.

        Returns:
            Ordered list of sendable lines.  Paragraphs are sent in order and
            blank paragraphs are dropped.  An empty result is never returned:
            a single empty string is yielded instead, so a caller iterating
            over the result always has something to send.
        """
        # IRC clients render text plain, so markup is stripped before any
        # byte accounting; measuring first would budget for characters that
        # are never actually sent.
        content = self._strip_markdown(content)

        overhead = len(f"PRIVMSG {target} :".encode("utf-8")) + 2  # +2 for \r\n
        max_bytes = 510 - overhead
        user_limit = self.max_message_length

        lines: List[str] = []
        for paragraph in content.split("\n"):
            if not paragraph.strip():
                continue
            while True:
                para_bytes = paragraph.encode("utf-8")
                limit = min(user_limit, max_bytes)
                if len(para_bytes) <= limit:
                    if paragraph.strip():
                        lines.append(paragraph)
                    break
                # Binary search for the largest character prefix that still
                # fits in the byte budget, so a multibyte character never
                # straddles the boundary.
                low, high = 1, len(paragraph)
                best = 0
                while low <= high:
                    mid = (low + high) // 2
                    if len(paragraph[:mid].encode("utf-8")) <= limit:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                split_at = best
                # Prefer breaking at a word boundary, but not so early that
                # the resulting chunk would be a stub fragment.
                space = paragraph.rfind(" ", 0, split_at)
                if space > split_at // 3:
                    split_at = space
                lines.append(paragraph[:split_at].rstrip())
                paragraph = paragraph[split_at:].lstrip()

        return lines if lines else [""]

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Downgrade markdown to plain text for an IRC client.

        IRC has no rendering, so markup would reach the reader as literal
        ``**bold**`` noise.  Link and image syntax is expanded to something
        readable rather than dropped: an image becomes just its URL, and a
        link becomes its label followed by the URL in parentheses, so the
        reader can still act on it.

        Args:
            text: Agent output, possibly containing markdown.

        Returns:
            Plain text with emphasis, code, heading, image and link markup
            removed or expanded.

        Note:
            The image rule must run before the link rule.  Both patterns can
            match the same text, and matching links first would consume the
            ``![alt](url)`` form and lose the image URL entirely.
        """
        # Emphasis markers are dropped and only the wrapped text survives.
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        # Italic markers; the second pattern needs word boundaries so that
        # a snake_case identifier is not misread as italic text.
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
        # Inline code backticks are dropped and the code body stays.
        text = re.sub(r"`(.+?)`", r"\1", text)
        # Fence lines are dropped so the code body survives inline.
        text = re.sub(r"```\w*\n?", "", text)
        # Images keep only the URL.  This must run before the link rule
        # below: both patterns match ``![alt](url)`` and matching links
        # first would consume the image form and lose its URL entirely.
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", text)
        # Links keep the label plus the URL in parentheses.
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        return text

    # -- Raw IRC I/O --

    async def _send_raw(self, line: str) -> None:
        """Write one IRC protocol line to the socket.

        IRC frames messages as bare lines terminated by CRLF.  Nothing else
        is appended, so callers must not include a terminator themselves.

        Args:
            line: Protocol line without a terminator, e.g. ``PRIVMSG #c :hi``.

        Returns:
            Nothing.

        Note:
            A closed or missing writer is treated as a silent no-op rather
            than raising.  A socket dropping mid-turn must not turn into an
            exception on every remaining send attempt; the receive loop is
            already responsible for reporting the loss.
        """
        if not self._writer or self._writer.is_closing():
            return
        encoded = (line + "\r\n").encode("utf-8")
        self._writer.write(encoded)
        await self._writer.drain()

    async def _receive_loop(self) -> None:
        """Read the socket and dispatch every protocol line.

        Runs as a single tracked task for the adapter's lifetime.  IRC does
        not length-prefix its messages, so a buffer accumulates raw bytes and
        is drained on CRLF boundaries before any line is handed off; a line
        larger than one read is reassembled across iterations.

        Args:
            None.

        Returns:
            Nothing.  Only a cancellation ends the loop.

        Raises:
            asyncio.CancelledError: Propagated to the caller so a shutdown
                cancellation is not swallowed and logged as an error.

        Note:
            If the loop exits while :attr:`is_connected` is still set, the
            connection was lost mid-flight.  That is recorded as a retryable
            fatal error so :class:`encre.gateway.run.GatewayRunner` can
            reconnect.
        """
        buffer = b""
        try:
            while self._reader and not self._reader.at_eof():
                data = await self._reader.read(4096)
                if not data:
                    break
                buffer += data
                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    try:
                        decoded = line.decode("utf-8", errors="replace")
                        await self._handle_line(decoded)
                    except Exception as e:
                        logger.warning("IRC: error handling line: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("IRC: receive loop error: %s", e)
        finally:
            if self.is_connected:
                logger.warning("IRC: connection lost, marking disconnected")
                self._set_fatal_error("connection_lost", "IRC connection closed unexpectedly", retryable=True)
                await self._notify_fatal_error()

    async def _handle_line(self, raw: str) -> None:
        """Route one parsed protocol line to its handler.

        Called per line by :meth:`_receive_loop`, so it must never raise on
        malformed input: a single hostile line must not terminate the receive
        loop.

        Args:
            raw: One protocol line with the CRLF terminator already removed.

        Returns:
            Nothing.  Outbound traffic is issued inline via :meth:`_send_raw`;
            inbound PRIVMSG traffic is handed to :meth:`_dispatch_message`.
        """
        msg = _parse_irc_message(raw)
        command = msg["command"]
        params = msg["params"]

        # PING is a liveness probe; answering nothing causes most servers to
        # disconnect the client, and the payload must be echoed back verbatim.
        if command == "PING":
            payload = params[0] if params else ""
            await self._send_raw(f"PONG :{payload}")
            return

        # 001 RPL_WELCOME marks registration complete.  Until it arrives the
        # client is still unidentified and PRIVMSG would be dropped.
        if command == "001":
            self._registered = True
            self._registration_event.set()
            if params:
                # The server reports the nick it actually granted us, which may
                # differ from the one requested; all addressing uses this value.
                self._current_nick = params[0]
            return

        # 433 ERR_NICKNAMEINUSE means another client holds our nick.  Take a
        # new one and retry; the server accepts the new NICK and re-issues 001.
        if command == "433":
            # Increment the numeric suffix rather than appending blindly, so a
            # collision chain stays bounded and never overflows NICKLEN.
            base = self.nickname.rstrip("_0123456789")
            suffix_match = re.search(r"_(\d+)$", self._current_nick)
            if suffix_match:
                next_num = int(suffix_match.group(1)) + 1
                self._current_nick = f"{base}_{next_num}"
            elif self._current_nick == self.nickname:
                self._current_nick = self.nickname + "_"
            else:
                self._current_nick = self.nickname + "_1"
            await self._send_raw(f"NICK {self._current_nick}")
            return

        # PRIVMSG is the only line carrying conversational content.
        if command == "PRIVMSG" and len(params) >= 2:
            sender_nick = _extract_nick(msg["prefix"])
            target = params[0]
            text = params[1]

            # Drop our own echo.  Nicks are compared case-insensitively because
            # IRC identity is case-insensitive, and the server may report our
            # nick with different casing than the one we requested.
            if sender_nick.lower() == self._current_nick.lower():
                return

            # CTCP ACTION is the /me action verb; render it as readable text so
            # the agent sees it as normal content rather than a control blob.
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                text = f"* {sender_nick} {text[8:-1]}"

            # Remaining CTCP (VERSION, USERINFO, DCC) is protocol chatter with
            # no conversational content, so it is discarded outright.
            if text.startswith("\x01"):
                return

            # A leading # or & means a channel; anything else is a nick and
            # therefore a private message addressed to us directly.
            is_channel = target.startswith("#") or target.startswith("&")
            chat_id = target if is_channel else sender_nick
            chat_type = "group" if is_channel else "dm"

            # Channels carry traffic from everyone in them.  Only react when the
            # message is explicitly addressed to our nick, matching the
            # conventions ``nick:``, ``nick,`` and a trailing space.
            if is_channel:
                addressed = False
                for prefix in (f"{self._current_nick}:", f"{self._current_nick},",
                               f"{self._current_nick} "):
                    if text.lower().startswith(prefix.lower()):
                        text = text[len(prefix):].strip()
                        addressed = True
                        break
                if not addressed:
                    return  # Not addressed to us; channel noise.

            # Enforce the allow-list when one is configured.  An empty list means
            # allow everyone, which is the only safe default here because IRC
            # nicks are self-declared and cannot be authenticated by the client.
            if self._allowed_users_lower and sender_nick.lower() not in self._allowed_users_lower:
                logger.debug("IRC: ignoring message from unauthorized user %s", sender_nick)
                return

            await self._dispatch_message(
                text=text,
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=sender_nick,
                user_name=sender_nick,
            )

        # A self-directed NICK means we changed our own nick out of band (for
        # example via an operator command), so tracking it keeps addressing and
        # echo suppression correct without a re-registration.
        if command == "NICK" and _extract_nick(msg["prefix"]).lower() == self._current_nick.lower():
            if params:
                self._current_nick = params[0]

    async def _dispatch_message(
        self,
        text: str,
        chat_id: str,
        chat_type: str,
        user_id: str,
        user_name: str,
    ) -> None:
        """Turn an inbound PRIVMSG into the gateway's common message event.

        :class:`encre.gateway.run.GatewayRunner` subscribes to the handler that
        :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`
        receives, so this method is the boundary between the IRC wire format
        and the gateway's platform-neutral message model.

        Args:
            text: Message body, already CTCP-stripped and de-addressed.
            chat_id: Conversation key; the channel name for groups, the sender
                nick for direct messages.
            chat_type: ``"group"`` or ``"dm"``, matching the gateway contract.
            user_id: Sender nick; IRC provides no stable user identifier.
            user_name: Sender nick, repeated for the display name slot.

        Returns:
            Nothing.  Returns early when no message handler is registered, which
            is the expected behaviour during tests that build the adapter
            without a full gateway.

        Note:
            IRC servers assign no durable message ids, so a millisecond
            timestamp is used and de-duplication cannot rely on it.
        """
        if not self._message_handler:
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(int(time.time() * 1000)),
            timestamp=__import__("datetime").datetime.now(),
        )

        await self.handle_message(event)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Report whether IRC can be enabled at all.

    Called by the platform registry before construction, so it must be cheap
    and must never raise: an IRC failure should disable the platform, not crash
    discovery.  The IRC protocol needs no third-party package, so this only
    checks that an operator configured a destination.

    Args:
        None.

    Returns:
        ``True`` when both a server and a channel are configured.
    """
    server = os.getenv("IRC_SERVER", "")
    channel = os.getenv("IRC_CHANNEL", "")
    # Also accept config.yaml-only configuration (no env vars).
    # The gateway passes PlatformConfig; we just check env for the
    # encre setup / requirements check path.
    return bool(server and channel)


def validate_config(config) -> bool:
    """Check that the configuration contains enough to open a connection.

    Reads the environment first and falls back to the config ``extra`` mapping,
    matching :class:`IRCAdapter.__init__` so validation and construction never
    disagree about which value wins.

    Args:
        config: Platform configuration carrying an ``extra`` mapping.

    Returns:
        ``True`` when both a server and a channel are present.
    """
    extra = getattr(config, "extra", {}) or {}
    server = os.getenv("IRC_SERVER") or extra.get("server", "")
    channel = os.getenv("IRC_CHANNEL") or extra.get("channel", "")
    return bool(server and channel)


def interactive_setup() -> None:
    """Interactive ``encre gateway setup`` flow for the IRC platform.

    Walks the operator through every setting :class:`IRCAdapter` reads and
    writes them to the data-dir ``.env`` so the configuration survives a
    restart.  Lazy-imports the ``encre.setup`` helpers so this plugin stays
    importable in non-CLI contexts (gateway runtime, tests) that have no
    terminal.

    Args:
        None.

    Returns:
        Nothing.  Returns early when the operator declines to reconfigure, or
        when a required field is left blank.

    Note:
        Clearing a prompt value writes an empty string rather than deleting the
        key, so an explicit "use the default" is distinguishable from "never
        set".
    """
    from encre.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("IRC")
    existing_server = get_env_value("IRC_SERVER")
    if existing_server:
        print_info(f"IRC: already configured (server: {existing_server})")
        if not prompt_yes_no("Reconfigure IRC?", False):
            return

    print_info("Connect Encre to an IRC network. Uses Python stdlib 鈥?no extra packages needed.")
    print_info("   Works with Libera.Chat, OFTC, your own ZNC/InspIRCd, etc.")
    print()

    server = prompt("IRC server hostname (e.g. irc.libera.chat)", default=existing_server or "")
    if not server:
        print_warning("Server is required 鈥?skipping IRC setup")
        return
    save_env_value("IRC_SERVER", server.strip())

    use_tls = prompt_yes_no("Use TLS (recommended)?", True)
    save_env_value("IRC_USE_TLS", "true" if use_tls else "false")

    default_port = "6697" if use_tls else "6667"
    port = prompt(f"Port (default {default_port})", default=get_env_value("IRC_PORT") or "")
    if port:
        try:
            save_env_value("IRC_PORT", str(int(port)))
        except ValueError:
            print_warning(f"Invalid port 鈥?using default {default_port}")
    elif get_env_value("IRC_PORT"):
        # User cleared the prompt; drop the override so the default applies.
        save_env_value("IRC_PORT", "")

    nickname = prompt(
        "Bot nickname (e.g. encre-bot)",
        default=get_env_value("IRC_NICKNAME") or "",
    )
    if not nickname:
        print_warning("Nickname is required 鈥?skipping IRC setup")
        return
    save_env_value("IRC_NICKNAME", nickname.strip())

    channel = prompt(
        "Channel to join (e.g. #encre 鈥?comma-separate for multiple)",
        default=get_env_value("IRC_CHANNEL") or "",
    )
    if not channel:
        print_warning("Channel is required 鈥?skipping IRC setup")
        return
    save_env_value("IRC_CHANNEL", channel.strip())

    print()
    print_info("馃攽 Optional authentication")
    print_info("   Leave blank to skip.")
    if prompt_yes_no("Configure a server password (PASS command)?", False):
        server_password = prompt("Server password", password=True)
        if server_password:
            save_env_value("IRC_SERVER_PASSWORD", server_password)

    if prompt_yes_no("Identify with NickServ on connect?", False):
        nickserv = prompt("NickServ password", password=True)
        if nickserv:
            save_env_value("IRC_NICKSERV_PASSWORD", nickserv)

    print()
    print_info("馃敀 Access control: restrict who can message the bot")
    print_info("   IRC nicks are not authenticated 鈥?anyone can claim any nick.")
    print_info("   For public channels, pair with NickServ-only mode on your network")
    print_info("   if you want stronger identity guarantees.")
    allow_all = prompt_yes_no("Allow all users in the channel to talk to the bot?", False)
    if allow_all:
        save_env_value("IRC_ALLOW_ALL_USERS", "true")
        save_env_value("IRC_ALLOWED_USERS", "")
        print_warning("鈿狅笍  Open access 鈥?any nick in the channel can command the bot.")
    else:
        save_env_value("IRC_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed nicks (comma-separated, leave empty to deny everyone)",
            default=get_env_value("IRC_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("IRC_ALLOWED_USERS", allowed.replace(" ", ""))
            print_success("Allowlist configured")
        else:
            save_env_value("IRC_ALLOWED_USERS", "")
            print_info("No nicks allowed 鈥?the bot will ignore all messages until you add nicks.")

    print()
    print_success("IRC configuration saved to data dir/.env")
    print_info("Restart the gateway for changes to take effect: encre gateway restart")


def is_connected(config) -> bool:
    """Report whether IRC is minimally configured.

    The environment is checked first and the config ``extra`` mapping second,
    matching :meth:`IRCAdapter.__init__`.

    Args:
        config: Platform configuration carrying an ``extra`` mapping.

    Returns:
        ``True`` when both a server and a channel are present.
    """
    extra = getattr(config, "extra", {}) or {}
    server = os.getenv("IRC_SERVER") or extra.get("server", "")
    channel = os.getenv("IRC_CHANNEL") or extra.get("channel", "")
    return bool(server and channel)


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry's env-enablement hook (landed in the
    generic-plugin-interface migration) BEFORE adapter construction, so
    ``gateway status`` and ``get_connected_platforms()`` reflect env-only
    configuration without instantiating the IRC client.  Returns ``None``
    when IRC isn't minimally configured; the caller skips auto-enabling.

    The special ``home_channel`` key in the returned dict is handled by the
    core hook: it becomes a proper ``HomeChannel`` dataclass on the
    ``PlatformConfig`` rather than being merged into ``extra``.
    """
    server = os.getenv("IRC_SERVER", "").strip()
    channel = os.getenv("IRC_CHANNEL", "").strip()
    if not (server and channel):
        return None
    seed: dict = {
        "server": server,
        "channel": channel,
    }
    port = os.getenv("IRC_PORT", "").strip()
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass
    nickname = os.getenv("IRC_NICKNAME", "").strip()
    if nickname:
        seed["nickname"] = nickname
    use_tls = os.getenv("IRC_USE_TLS", "").strip().lower()
    if use_tls:
        seed["use_tls"] = use_tls in {"1", "true", "yes"}
    # Passwords live in PlatformConfig.extra as well for back-compat with
    # existing config.yaml users; env-reads at construct time still win.
    if os.getenv("IRC_SERVER_PASSWORD"):
        seed["server_password"] = os.getenv("IRC_SERVER_PASSWORD")
    if os.getenv("IRC_NICKSERV_PASSWORD"):
        seed["nickserv_password"] = os.getenv("IRC_NICKSERV_PASSWORD")
    # Optional home-channel (usually the same as IRC_CHANNEL, but can be a
    # dedicated reports channel).  Defaults to IRC_CHANNEL so cron jobs
    # with ``deliver=irc`` have a sensible target without extra config.
    home = os.getenv("IRC_HOME_CHANNEL") or channel
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("IRC_HOME_CHANNEL_NAME", home),
        }
    return seed


def _strip_irc_control_chars(text: str) -> str:
    """Strip IRC line terminators and the NUL byte from ``text``.

    IRC commands are CRLF-delimited, so a bare ``\r`` or ``\n`` in user
    content lets an attacker inject arbitrary IRC commands (CTCP, JOIN,
    KICK).  ``\x00`` is a protocol-illegal byte.  Everything else is valid in
    PRIVMSG payloads, so only these three bytes are rewritten.

    Args:
        text: Untrusted text destined for an IRC protocol field.

    Returns:
        The input with ``\r`` and ``\n`` replaced by spaces and ``\x00``
        removed.
    """
    return text.replace("\r", " ").replace("\n", " ").replace("\x00", "")


def _is_irc_channel(target: str) -> bool:
    """Report whether ``target`` is an IRC channel rather than a nick.

    Used by :func:`_standalone_send` to decide whether to issue ``JOIN``
    before ``PRIVMSG``.  Networks with the default ``+n`` channel mode
    silently drop PRIVMSG from non-members, so skipping the join would
    produce an undeliverable message.

    Args:
        target: Channel name or nick.

    Returns:
        ``True`` when the first character is ``#``, ``&``, ``+`` or ``!``.
        The latter two cover network-specific channel prefix modes.
    """
    return bool(target) and target[0] in "#&+!"


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Open an ephemeral IRC connection, send a PRIVMSG, and quit.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (e.g. ``encre cron`` running as a
    separate process from ``encre gateway``).  Without this hook,
    ``deliver=irc`` cron jobs fail with ``No live adapter for platform``.

    The standalone client uses a distinct nick suffix (``-cron``) so it
    does not collide with the long-running gateway adapter that may already
    be holding the configured nickname on the same network.  When the
    target is a channel, the client JOINs it before sending PRIVMSG so
    networks with the default ``+n`` (no external messages) channel mode
    accept the delivery.

    ``thread_id`` and ``media_files`` are accepted for signature parity but
    are not meaningful on IRC: IRC has no native thread or attachment
    primitive.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    server = os.getenv("IRC_SERVER") or extra.get("server", "")
    channel = os.getenv("IRC_CHANNEL") or extra.get("channel", "")
    if not server or not channel:
        return {"error": "IRC standalone send: IRC_SERVER and IRC_CHANNEL must be configured"}

    port_value = os.getenv("IRC_PORT") or extra.get("port", 6697)
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        return {"error": f"IRC standalone send: invalid port {port_value!r}"}

    nickname = os.getenv("IRC_NICKNAME") or extra.get("nickname", "encre-bot")
    use_tls_env = os.getenv("IRC_USE_TLS")
    if use_tls_env is not None:
        use_tls = use_tls_env.lower() in {"1", "true", "yes"}
    else:
        use_tls = bool(extra.get("use_tls", True))

    server_password = os.getenv("IRC_SERVER_PASSWORD") or extra.get("server_password", "")
    nickserv_password = os.getenv("IRC_NICKSERV_PASSWORD") or extra.get("nickserv_password", "")

    # Reject control characters in chat_id to block IRC command injection.
    raw_target = chat_id or channel
    if any(ch in raw_target for ch in ("\r", "\n", "\x00", " ")):
        return {"error": "IRC standalone send: chat_id contains illegal IRC characters"}
    target = raw_target

    # Distinct nick prevents NICK collision with a live gateway adapter
    # that may already be holding the configured nickname.  Cap to 24 chars
    # so subsequent collision retries do not overflow the 30-char NICKLEN
    # most networks enforce.
    nick_base = nickname.rstrip("_0123456789-")[:24] or "encre-bot"
    standalone_nick = f"{nick_base}-cron"[:30]
    plain = IRCAdapter._strip_markdown(message)

    ssl_ctx = ssl.create_default_context() if use_tls else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(server, port, ssl=ssl_ctx),
            timeout=15.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"error": f"IRC standalone connect failed: {e}"}

    async def _raw(line: str) -> None:
        """Write one protocol line on the standalone socket.

        Args:
            line: Protocol line without a terminator.

        Returns:
            Nothing.
        """
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()

    nick_attempts = 0
    max_nick_attempts = 5
    try:
        if server_password:
            await _raw(f"PASS {_strip_irc_control_chars(server_password)}")
        await _raw(f"NICK {standalone_nick}")
        await _raw(f"USER {standalone_nick} 0 * :Encre Agent (cron)")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        registered = False
        while not registered:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"error": "IRC standalone send: registration timeout (no RPL_WELCOME)"}
            try:
                raw_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=remaining)
            except asyncio.TimeoutError:
                return {"error": "IRC standalone send: registration timeout (no RPL_WELCOME)"}
            except asyncio.IncompleteReadError:
                return {"error": "IRC standalone send: server closed connection during registration"}
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            msg = _parse_irc_message(decoded)
            cmd = msg["command"]
            if cmd == "PING":
                payload = msg["params"][0] if msg["params"] else ""
                await _raw(f"PONG :{payload}")
            elif cmd == "001":
                registered = True
            elif cmd in {"432", "433"}:
                nick_attempts += 1
                if nick_attempts > max_nick_attempts:
                    return {"error": "IRC standalone send: too many nick collisions"}
                # Build the next nick from the stable base, not the
                # mutated value, so the suffix stays bounded.
                standalone_nick = f"{nick_base}-cron-{nick_attempts}"[:30]
                await _raw(f"NICK {standalone_nick}")
            elif cmd in {"464", "465"}:
                return {"error": f"IRC standalone send: server rejected client ({cmd})"}

        if nickserv_password:
            await _raw(f"PRIVMSG NickServ :IDENTIFY {_strip_irc_control_chars(nickserv_password)}")
            await asyncio.sleep(2)

        # JOIN before PRIVMSG.  IRC channels with the default ``+n`` mode
        # (no external messages: Libera, OFTC, EFnet, IRCNet, undernet)
        # silently drop PRIVMSG from non-members.  Do not JOIN bare nicks
        # (DM target) or server queries.
        if _is_irc_channel(target):
            await _raw(f"JOIN {target}")
            join_deadline = loop.time() + 5.0
            joined = False
            while not joined:
                remaining = join_deadline - loop.time()
                if remaining <= 0:
                    # Timed out waiting for a JOIN ack: proceed anyway, the
                    # server may still deliver the PRIVMSG depending on mode.
                    break
                try:
                    raw_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=remaining)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                jmsg = _parse_irc_message(decoded)
                jcmd = jmsg["command"]
                if jcmd == "PING":
                    payload = jmsg["params"][0] if jmsg["params"] else ""
                    await _raw(f"PONG :{payload}")
                elif jcmd in {"366", "JOIN"}:
                    joined = True
                elif jcmd in {"403", "405", "471", "473", "474", "475"}:
                    return {"error": f"IRC standalone send: JOIN {target} rejected ({jcmd})"}

        # Bytes-aware per-line splitting so multi-line plain text never
        # exceeds the IRC 510-byte protocol limit.  Reuses the same
        # algorithm as IRCAdapter._split_message, with control-character
        # stripping per line to block CRLF injection from message content.
        overhead = len(f"PRIVMSG {target} :".encode("utf-8")) + 2
        max_bytes = 510 - overhead
        sent_any = False
        for paragraph in plain.split("\n"):
            paragraph = _strip_irc_control_chars(paragraph).rstrip()
            if not paragraph:
                continue
            while paragraph:
                encoded = paragraph.encode("utf-8")
                if len(encoded) <= max_bytes:
                    await _raw(f"PRIVMSG {target} :{paragraph}")
                    await asyncio.sleep(0.3)
                    sent_any = True
                    break
                # Binary search for largest prefix that fits within max_bytes
                low, high, best = 1, len(paragraph), 0
                while low <= high:
                    mid = (low + high) // 2
                    if len(paragraph[:mid].encode("utf-8")) <= max_bytes:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                split_at = best
                space = paragraph.rfind(" ", 0, split_at)
                if space > split_at // 3:
                    split_at = space
                await _raw(f"PRIVMSG {target} :{paragraph[:split_at].rstrip()}")
                await asyncio.sleep(0.3)
                sent_any = True
                paragraph = paragraph[split_at:].lstrip()

        if not sent_any:
            return {"error": "IRC standalone send: empty message after stripping"}

        await _raw("QUIT :delivered")
        try:
            await asyncio.wait_for(reader.read(1024), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        return {"success": True, "message_id": str(int(time.time() * 1000))}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("IRC standalone send raised", exc_info=True)
        return {"error": f"IRC standalone send failed: {e}"}
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass

def _build_adapter(config):
    """Construct the adapter for the plugin gateway-adapter spec.

    Wired as ``adapter_factory`` on the ``GatewayAdapterSpec`` in plugin.py
    and invoked by :class:`encre.gateway.run.GatewayRunner` once
    configuration is validated.

    Args:
        config: A :class:`encre.gateway.config.PlatformConfig` for IRC.

    Returns:
        A configured :class:`IRCAdapter` instance.
    """
    return IRCAdapter(config)


# -- Plugin registration glue ---------------------------------------------------
# The registry ``PlatformEntry`` was migrated into plugin.py's
# ``GatewayAdapterSpec``; the factory and probe below are the real values
# that spec mirrors. Registration happens at plugin activation time.

def _check_requirements():
    """Requirement check bound to the plugin gateway-adapter spec.

    The spec needs a callable taking no arguments, so this wrapper
    always reports the platform as available and leaves the judgement about
    configured values to :func:`validate_config`.

    Args:
        None.

    Returns:
        Always ``True``.
    """
    return True
