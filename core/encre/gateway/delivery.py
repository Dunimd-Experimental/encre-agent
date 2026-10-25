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

"""Gateway outbound delivery routing.

This is the *out-of-band* send path.  The live chat loop (adapter receives a
message -> runner resolves a session -> agent runs ->
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer` edits a preview
message in place) handles conversational replies.  Anything that is not a reply
to the message currently being answered -- a cron firing, a heartbeat, a
``send_message`` tool call aimed at another chat -- goes through here instead.

A single router takes a content blob plus a list of target strings and delivers
to each, handling:

- **Explicit targets** -- ``"platform:chat_id"`` strings (e.g.
  ``"telegram:123456789"``), the canonical cron send form.
- **Adapter-id targets** -- bare adapter names (e.g. ``"telegram"``), delivered
  to the adapter's auto-detected push target (``default_push_chat_id``).
- **Origin fallback** -- when no targets are given, deliver back to the
  originating chat (the live-reply path).
- **Truncation** -- outputs over :data:`MAX_PLATFORM_OUTPUT` chars are saved in
  full to an audit file and the adapter receives either the full payload (when
  it advertises ``splits_long_messages``) or a truncated payload with a
  ``"... [truncated, full output saved to <path>]"`` note.

This is independent of the live chat stream (which is driven by
:meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`): cron
outputs and explicit-target sends flow through here and are deliberately NOT
mirrored into session history.

Public API:
    * :class:`DeliveryRouter` -- the router the runner owns.
    * :class:`DeliveryTarget` -- one parsed target string.
    * :class:`DeliveryResult` -- the per-target outcome.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encre.config import get_data_dir

logger = logging.getLogger("encre.gateway.delivery")

# Soft cap for platform output. Outputs exceeding this threshold are saved to
# disk in full, and the adapter receives a truncated payload with a pointer
# to the saved file (unless the adapter handles its own chunking).
MAX_PLATFORM_OUTPUT = 4000
# Suffix appended to truncated payloads to indicate the full output location.
_TRUNCATION_NOTE = "... [truncated, full output saved to {path}]"


@dataclass(frozen=True)
class DeliveryTarget:
    """A resolved delivery destination.

    Frozen because targets are parsed once per :meth:`DeliveryRouter.deliver`
    call and then only read.  It carries both the human-entered form and the
    split form, which keeps failure reporting able to quote what the caller
    actually typed.

    Attributes:
        raw: The original target string (e.g. ``"telegram:123"`` or
            ``"telegram"``).  Surfaced verbatim in every
            :class:`DeliveryResult`.
        platform: The adapter/platform name (e.g. ``"telegram"``).
        chat_id: The explicit chat id, or ``None`` when the target was a bare
            adapter id (resolved at send time via ``default_push_chat_id``).
    """

    raw: str
    platform: str
    chat_id: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "DeliveryTarget":
        """Parse a target string into a :class:`DeliveryTarget`.

        Accepted forms:
        - ``"platform:chat_id"`` -- explicit chat.
        - ``"platform"`` -- adapter id, resolved to its push target at send.

        Args:
            raw: The target as the caller wrote it.  Surrounded whitespace is
                stripped; an empty chat id after the colon is treated as a
                bare adapter-id target rather than an error.

        Returns:
            The parsed target.

        Note:
            ``split(":", 1)`` keeps any further colons inside the chat id
            intact, which matters for platforms that namespace ids with colons.
        """
        raw = raw.strip()
        if ":" in raw:
            platform, chat_id = raw.split(":", 1)
            return cls(raw=raw, platform=platform.strip(), chat_id=chat_id.strip() or None)
        return cls(raw=raw, platform=raw, chat_id=None)


@dataclass
class DeliveryResult:
    """Per-target outcome of a :meth:`DeliveryRouter.deliver` call.

    One result is produced per input target string, in input order, so callers
    can zip the two lists to report partial success.

    Attributes:
        target: The raw target string this result is for.
        success: Whether the adapter reported a successful send.
        message_id: Platform message id, when the adapter returned one.
        error: Failure description; only populated on failure.
        truncated: Whether the payload was truncated before sending.
        saved_path: Where the full untruncated output was written.
    """

    target: str
    success: bool
    message_id: str | None = None
    error: str | None = None
    truncated: bool = False
    saved_path: str | None = None


class DeliveryRouter:
    """Routes outbound content to one or more platform targets.

    Owned by :class:`encre.gateway.run.GatewayRunner` (which holds the
    live adapter instances). Callers (automation/cron push, the send engine)
    pass a content blob and a list of target strings; the router resolves each
    to a live adapter, applies truncation, and sends.

    Lifecycle: constructed once with the adapter manager -> ``deliver()`` as
    many times as needed -> discarded at gateway shutdown.  The router itself
    holds no state beyond configuration, so it is safe to share across tasks.

    Failure isolation: a missing adapter, an unresolvable chat id, or a send
    exception is recorded on that target's :class:`DeliveryResult` and the loop
    moves on.  One bad target never suppresses the good ones, which is what
    makes multi-target cron sends reportable instead of all-or-nothing.
    """

    def __init__(
        self,
        adapter_manager: Any,
        *,
        max_output: int = MAX_PLATFORM_OUTPUT,
        audit_dir: str | Path | None = None,
    ) -> None:
        """Initialize the delivery router with an adapter manager and optional config.

        Args:
            adapter_manager: The gateway's adapter manager instance, holding
                live adapter references keyed by platform name.  The router
                reads its private ``_instances`` mapping directly, so this is
                a gateway-internal contract rather than a public API.
            max_output: Maximum output length before truncation is applied.
                Defaults to :data:`MAX_PLATFORM_OUTPUT`.
            audit_dir: Directory for saving truncated output files. Defaults
                to ``<data_dir>/delivery_audit`` if not provided.
        """
        self._am = adapter_manager
        self._max_output = max_output
        if audit_dir is None:
            audit_dir = Path(get_data_dir()) / "delivery_audit"
        self._audit_dir = Path(audit_dir)

    # ---- Adapter lookup ----

    def _get_adapter(self, platform: str) -> Any | None:
        """Look up a live adapter instance by platform name.

        Args:
            platform: The platform/adapter name to look up.

        Returns:
            The adapter instance if it is running, or ``None`` with a warning
            logged if not found.  ``None`` is not an exception: a cron target
            pointing at a platform that failed to start is a normal operating
            condition that should report per-target failure, not abort the
            whole delivery.
        """
        instances = getattr(self._am, "_instances", {}) or {}
        adapter = instances.get(platform)
        if adapter is None:
            logger.warning("[delivery] adapter '%s' not running", platform)
        return adapter

    def _resolve_chat_id(self, adapter: Any, target: DeliveryTarget) -> str | None:
        """Resolve the chat id for a target, falling back to the adapter's
        auto-detected push target (most recent inbound chat).
        """
        if target.chat_id:
            return target.chat_id
        push = getattr(adapter, "default_push_chat_id", None)
        if push is None:
            logger.warning("[delivery] target '%s' has no explicit chat_id and "
                           "adapter has no default push target", target.raw)
        return push

    # ---- Output truncation ----

    def _maybe_truncate(self, content: str, adapter: Any) -> tuple[str, bool, str | None]:
        """Apply truncation per the adapter's capability bits.

        Returns ``(payload, truncated, saved_path)``:
        - If the adapter self-chunks (``splits_long_messages``), the full
          content is returned unchanged.
        - Otherwise, over-cap content is saved to an audit file and a
          truncated payload with a note is returned.
        - Under-cap content passes through unchanged.

        Args:
            content: The full output text.
            adapter: The target adapter, consulted for its
                ``splits_long_messages`` capability bit.

        Returns:
            Tuple of payload to send, whether truncation happened, and the
            audit file path when it did.

        Note:
            The cut point is computed as ``max_output - len(note) - 1`` so that
            ``payload + newline + note`` lands exactly on the cap.  This keeps
            the delivered payload inside the platform limit while still
            pointing at the full output.
        """
        if not content or len(content) <= self._max_output:
            return content, False, None
        if getattr(adapter, "splits_long_messages", False):
            # Adapter handles its own chunking -- pass the full payload.
            return content, False, None
        # Truncate and save the full output to disk for audit.
        saved = self._save_audit(content)
        note = _TRUNCATION_NOTE.format(path=saved)
        cut = self._max_output - len(note) - 1
        if cut < 0:
            cut = 0
        payload = content[:cut] + "\n" + note
        logger.info("[delivery] truncated %d -> %d chars, full saved to %s",
                    len(content), len(payload), saved)
        return payload, True, saved

    def _save_audit(self, content: str) -> str:
        """Save the full content to an encrypted audit file and return its path."""
        from encre.secure_io import write_text

        try:
            name = f"output_{int(time.time() * 1000)}.txt"
            path = self._audit_dir / name
            write_text(path, content)
            return str(path)
        except Exception as e:
            logger.warning("[delivery] failed to save audit output: %s", e)
            return "<save failed>"

    # ---- Delivery ----

    async def deliver(
        self,
        content: str,
        targets: list[str] | None = None,
        *,
        origin: tuple[str, str] | None = None,
    ) -> list[DeliveryResult]:
        """Deliver content to each target.

        Args:
            content: The text to deliver.  Truncation is applied per target, so
                the same blob may be shortened for one platform and passed in
                full to another.
            targets: Target strings (``"platform:chat_id"`` or ``"platform"``).
                When ``None`` or empty, falls back to ``origin`` (the
                originating platform + chat id) if provided.
            origin: Optional ``(platform, chat_id)`` fallback when no explicit
                targets are given (the live-reply or cron-home case).

        Returns:
            One :class:`DeliveryResult` per target, in target order.  An empty
            list when there was nowhere to deliver.
        """
        if not targets and origin is not None:
            plat, chat = origin
            targets = [f"{plat}:{chat}" if chat else plat]
        if not targets:
            logger.warning("[delivery] no targets and no origin -- nothing to deliver")
            return []

        results: list[DeliveryResult] = []
        for raw in targets:
            target = DeliveryTarget.parse(raw)
            adapter = self._get_adapter(target.platform)
            if adapter is None:
                results.append(DeliveryResult(target=raw, success=False,
                                               error=f"adapter '{target.platform}' not running"))
                continue
            chat_id = self._resolve_chat_id(adapter, target)
            if not chat_id:
                results.append(DeliveryResult(target=raw, success=False,
                                               error="no resolvable chat_id"))
                continue
            payload, truncated, saved = self._maybe_truncate(content, adapter)
            try:
                res = await adapter.send(chat_id, payload)
                results.append(DeliveryResult(
                    target=raw,
                    success=bool(getattr(res, "success", False)),
                    message_id=getattr(res, "message_id", None),
                    error=getattr(res, "error", None) if not getattr(res, "success", False) else None,
                    truncated=truncated,
                    saved_path=saved,
                ))
            except Exception as e:
                logger.warning("[delivery] send to '%s' failed: %s", raw, e)
                results.append(DeliveryResult(target=raw, success=False, error=str(e)))
        return results
