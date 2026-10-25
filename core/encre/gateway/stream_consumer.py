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

"""Gateway streaming consumer -- bridges agent events to platform delivery.

The agent runtime emits a stream of events (``TextDelta``, ``ToolResult``,
``Finish``) through ``EventRouter.submit_stream()``. ``GatewayStreamConsumer``
is the per-conversation object that turns that event stream into a smooth,
rate-limited delivery to a chat platform:

  1. ``feed()`` / ``feed_stream_event()`` receive events from the agent.
  2. Text deltas are buffered in memory as they arrive.
  3. Buffered text is pushed to the platform at a throttled cadence using
     progressive message editing, so the user sees the reply grow instead of
     appearing all at once.
  4. ``finalize()`` delivers the complete response (a final edit to strip the
     typing cursor, or a fresh send in buffer-only mode) and returns the full
     text.

This is the tail of the gateway's inbound pipeline.  The adapter that
received the user's message passes its own instance to the consumer, so the
reply is written back through the same vendor connection, thread, and reply
anchor that the request arrived on.

The consumer owns exactly one agent turn for exactly one chat.  It is created
per turn, fed, finalized, and discarded; it must not be reused, because
``_finalized`` is one-way.  Out-of-band sends (cron, explicit targets) do not
go through here -- they go through
:class:`encre.gateway.delivery.DeliveryRouter`.

Public API:
    * :class:`GatewayStreamConsumer` -- one consumer per agent turn.
    * :class:`StreamConsumerConfig` -- per-conversation tuning knobs.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from encre.gateway.config import (
    DEFAULT_STREAMING_BUFFER_THRESHOLD,
    DEFAULT_STREAMING_CURSOR,
    DEFAULT_STREAMING_EDIT_INTERVAL,
)
from encre.gateway.platforms.base import BasePlatformAdapter, SendResult
from encre.gateway.stream_events import (
    Commentary,
    GatewayNotice,
    MessageChunk,
    MessageStop,
    StreamEvent,
    ToolCallChunk,
    ToolCallFinished,
)
from encre.utils.types import AgentEvent, Finish, PermissionRequest, TextDelta, ToolResult

logger = logging.getLogger("encre.gateway.stream_consumer")


@dataclass
class StreamConsumerConfig:
    """Runtime configuration for a single stream consumer instance.

    These values tune how aggressively the consumer pushes incremental edits to
    the platform. They are sourced from the gateway config defaults but can be
    overridden per conversation.

    Attributes:
        edit_interval: Minimum seconds between two consecutive progressive
            edits to the same preview message. Prevents flooding the platform
            with an edit on every keystroke.
        buffer_threshold: Minimum number of accumulated characters before an
            edit is pushed, unless a preview message already exists. Keeps very
            short replies from triggering chatty edits.
        cursor: A short marker (e.g. a typed-text glyph) appended to the
            preview text to signal "still streaming". Removed on finalize.
        buffer_only: When True, the consumer only buffers text and never
            performs progressive editing. Used for platforms that do not
            support editing an already-sent message.
    """

    edit_interval: float = DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = DEFAULT_STREAMING_CURSOR
    # When True, only buffer text without progressive editing (used for
    # platforms that don't support message editing).
    buffer_only: bool = False


class GatewayStreamConsumer:
    """Bridges agent stream events to platform-specific progressive delivery.

    One instance handles one agent turn for one chat. It owns a text buffer and
    a "preview" message on the platform that is repeatedly edited as text
    arrives, giving the appearance of live typing. When the turn ends it
    finalizes the message and returns the complete text.

    Typical usage::

        consumer = GatewayStreamConsumer(adapter, chat_id, config)
        async for event in router.submit_stream(...):
            await consumer.feed(event)
        result = await consumer.finalize()

    Attributes worth knowing:
        ``_preview_message_id`` tracks the platform message being edited; it is
        ``None`` until the first edit is sent.
        ``_finalized`` guards against feeding or finalizing more than once.
        ``_finish_error`` captures an agent error reported via a ``Finish``
        event so it can be surfaced to the user.
    """

    def __init__(
        self,
        adapter: BasePlatformAdapter,
        chat_id: str,
        config: Optional[StreamConsumerConfig] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Bind one consumer to one chat and initialise the streaming state to "not started".

        The adapter and chat id are captured rather than re-derived because the
        consumer is constructed fresh for a single turn and never shares its
        output across conversations. Configuration falls back to a default
        :class:`StreamConsumerConfig` when omitted, which is what keeps callers
        that want the platform defaults from having to spell them out.

        Invariant: on return the buffer is empty, ``_preview_message_id`` is
        ``None``, ``_last_edit_time`` is ``0.0``, ``_finalized`` is ``False``,
        and ``_final_text`` is ``""`` -- i.e. no edit has been sent, no
        throttling window has started, and neither :meth:`feed` nor
        :meth:`finalize` has completed. The zero timestamp in particular is what
        lets the first edit bypass the ``edit_interval`` cooldown instead of
        waiting one full interval after a value that was never set.

        Args:
            adapter: Platform adapter used to send and edit the preview
                message; its ``send`` output is the only delivery path here.
            chat_id: Conversation the preview message belongs to.
            config: Tunables for edit pacing and buffering, defaulting to
                module-level defaults.
            reply_to: Optional platform message id to quote when sending.
            metadata: Optional per-conversation context passed through to the
                adapter's send call.
        """
        self._adapter = adapter
        self._chat_id = chat_id
        self._config = config or StreamConsumerConfig()
        self._reply_to = reply_to
        self._metadata = metadata

        # Accumulated full response text, kept as a list of string fragments
        # for cheap appends before a final join at read time.
        self._buffer: list[str] = []
        # The message_id of the "preview" message being progressively edited.
        self._preview_message_id: Optional[str] = None
        # Wall-clock timestamp of the last edit pushed to the platform.
        self._last_edit_time: float = 0.0
        # Whether the stream has been finalized (guards double finalize).
        self._finalized = False
        # Complete response text, populated only after finalize().
        self._final_text: str = ""
        # Agent error captured from a Finish event, if any.
        self._finish_error: str | None = None

    @property
    def accumulated_text(self) -> str:
        """Return the full text accumulated in the buffer so far.

        Returns:
            All buffered fragments joined.  Called frequently by the throttle
            check, so this is a join of a list of small strings rather than a
            growing concatenation.
        """
        return "".join(self._buffer)

    async def feed(self, event: AgentEvent) -> None:
        """Feed a raw agent event into the consumer.

        Routes the event kinds the agent emits during a turn:
        ``TextDelta`` (incremental reply text), ``ToolResult`` (tool output,
        informational only), ``PermissionRequest`` (pushed to the chat as an
        actionable approval prompt), and ``Finish`` (turn complete, possibly
        with an error). Buffering and progressive edits are driven here.

        Args:
            event: The agent event produced by the runtime stream.

        Returns:
            None.

        Raises:
            None. Errors from platform edits are logged by the adapter layer.
        """
        if self._finalized:
            return

        if isinstance(event, TextDelta) and event.text:
            self._buffer.append(event.text)
            await self._maybe_progressive_edit()

        elif isinstance(event, ToolResult):
            # Tool results are informational; we don't display them in the
            # streaming preview (they're part of the agent's internal state).
            pass

        elif isinstance(event, PermissionRequest):
            # The desktop renders a permission card for this event; IM users
            # would otherwise stare at a silent chat until the 300s timeout.
            # Push an actionable line so /approve and /deny can reach it.
            reason = f": {event.reason}" if event.reason else ""
            try:
                await self._adapter.send(
                    self._chat_id,
                    f"⚠️ Tool '{event.tool_name}' needs approval{reason}\n\n"
                    "Send /approve to allow (or /approve always to remember "
                    "for this session), /deny to refuse.",
                )
            except Exception as send_err:
                logger.warning(
                    "[stream-consumer] failed to push permission prompt: %s", send_err
                )

        elif isinstance(event, Finish):
            if event.error:
                self._finish_error = event.error
                logger.warning(
                    "[stream-consumer] finish with error: %s", event.error
                )

    async def feed_stream_event(self, event: StreamEvent) -> None:
        """Feed a typed gateway ``StreamEvent`` instead of a raw agent event.

        Provides an alternative ingestion path for callers that already produce
        typed stream events (``MessageChunk``, ``Commentary``, ``ToolCallChunk``,
        ``MessageStop``) rather than the lower-level ``AgentEvent`` union.

        Args:
            event: A typed stream event from :mod:`encre.gateway.stream_events`.

        Returns:
            None.

        Note:
            ``Commentary`` is delivered immediately as its own platform message
            instead of being merged into the preview buffer, so an
            intermediate note cannot be silently folded into the final reply.
        """
        if self._finalized:
            return

        if isinstance(event, MessageChunk):
            self._buffer.append(event.text)
            await self._maybe_progressive_edit()

        elif isinstance(event, Commentary):
            # Commentary is a complete intermediate message -- send it as-is
            # rather than merging it into the streaming preview buffer.
            await self._adapter.send(self._chat_id, event.text, metadata=self._metadata)

        elif isinstance(event, ToolCallChunk):
            # Optionally show tool progress (platform-dependent). Currently a
            # no-op; platforms can later render tool activity here.
            pass

        elif isinstance(event, MessageStop):
            if event.final:
                await self.finalize()

    async def finalize(self) -> str:
        """Finalize the stream and deliver the complete response.

        Performs the terminal delivery: if progressive editing was used it does
        a final edit that strips the typing cursor and shows the complete text;
        otherwise (buffer-only mode or no preview message) it sends the full
        message fresh. If the turn produced no real text but ended with an
        agent error, a warning message is delivered instead.

        Args:
            None.

        Returns:
            The complete response text (or the error notice) as a string.

        Raises:
            None. Platform send/edit failures are handled by the adapter.

        Note:
            ``_finalized`` is set before any await, so a second ``finalize()``
            call from a racing caller returns the already-final text instead of
            issuing a duplicate platform write.  The first call still owns the
            write.
        """
        if self._finalized:
            return self._final_text

        self._finalized = True
        self._final_text = self.accumulated_text

        if not self._final_text.strip():
            if self._finish_error:
                self._final_text = f"⚠️ Agent error: {self._finish_error}"
                await self._adapter.send(
                    self._chat_id,
                    self._final_text,
                    reply_to=self._reply_to,
                    metadata=self._metadata,
                )
            return self._final_text

        if self._config.buffer_only or self._preview_message_id is None:
            # No progressive editing was done; send the complete message.
            await self._adapter.send(
                self._chat_id,
                self._final_text,
                reply_to=self._reply_to,
                metadata=self._metadata,
            )
        else:
            # Final edit to remove the cursor and show the complete text.
            await self._adapter.edit_message(
                self._chat_id, self._preview_message_id, self._final_text
            )

        return self._final_text

    async def _maybe_progressive_edit(self) -> None:
        """Push a throttled progressive edit of the preview message if due.

        Decides whether enough time has elapsed and enough new text has
        accumulated to justify another edit. On the first qualifying edit it
        sends a brand-new message and records its id; afterwards it edits the
        existing preview message. Cancels silently when in buffer-only mode.

        Args:
            None.

        Returns:
            None.

        Note:
            The threshold check is only applied once a preview message exists,
            so the very first preview goes out as soon as the time gate opens,
            even for a reply under the threshold.  That is deliberate: it is
            what makes a short answer appear at all instead of waiting for
            enough characters to accumulate.
        """
        if self._config.buffer_only:
            return

        now = time.time()
        elapsed = now - self._last_edit_time
        text_so_far = self.accumulated_text

        # Only edit if enough time has passed AND enough new text accumulated.
        if elapsed < self._config.edit_interval:
            return
        if len(text_so_far) < self._config.buffer_threshold and self._preview_message_id is not None:
            return

        preview_text = text_so_far + self._config.cursor
        self._last_edit_time = now

        if self._preview_message_id is None:
            # First progressive message: send a new message and remember its id
            # so subsequent edits target the same platform message.
            result = await self._adapter.send(
                self._chat_id,
                preview_text,
                reply_to=self._reply_to,
                metadata=self._metadata,
            )
            if result.success and result.message_id:
                self._preview_message_id = result.message_id
        else:
            # Subsequent edits: mutate the existing preview message in place.
            await self._adapter.edit_message(
                self._chat_id, self._preview_message_id, preview_text
            )
