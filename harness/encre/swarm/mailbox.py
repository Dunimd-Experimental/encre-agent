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

# Point-to-point message passing between swarm agents.
#
# ``EncreMailbox`` is an asyncio-queue-backed inbox owned by one agent.  Agents
# ``send`` messages to another mailbox (delivering to its queue and archiving a
# copy) and ``receive`` (blocking with a timeout).  ``peek``/``clear`` support
# inspection and reset.  It is the lightweight channel used for consensus
# voting and teammate-to-teammate communication.

import asyncio
import contextlib
import time
from dataclasses import dataclass, field


@dataclass
class MailboxMessage:
    """One message delivered through a mailbox.

    Carries the sender id, payload ``content``, an optional metadata dict, and
    an auto-assigned timestamp.
    """
    sender: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


class EncreMailbox:
    """Async inbox/outbox for a single swarm participant.

    Messages are queued (bounded by ``max_messages`` with oldest-eviction) and
    also archived in ``_received`` for auditing.  ``receive`` blocks up to the
    mailbox's ``timeout`` (overridable per call) and returns ``None`` on
    timeout.  ``peek`` non-destructively inspects pending messages; ``clear``
    empties everything.
    """
    def __init__(self, owner_id: str = "", max_messages: int = 100, timeout: float = 30.0):
        self.owner_id = owner_id
        self.max_messages = max_messages
        self.timeout = timeout
        self._queue: asyncio.Queue[MailboxMessage] = asyncio.Queue()
        self._received: list[MailboxMessage] = []

    async def send(self, recipient_mailbox: "EncreMailbox", content: str, metadata: dict | None = None) -> None:
        msg = MailboxMessage(
            sender=self.owner_id,
            content=content,
            metadata=metadata or {},
        )
        if recipient_mailbox._queue.qsize() >= recipient_mailbox.max_messages:
            with contextlib.suppress(asyncio.QueueEmpty):
                recipient_mailbox._queue.get_nowait()
        await recipient_mailbox._queue.put(msg)
        recipient_mailbox._received.append(msg)

    async def receive(self, timeout: float | None = None) -> MailboxMessage | None:
        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            msg = await asyncio.wait_for(self._queue.get(), timeout=effective_timeout)
            return msg
        except TimeoutError:
            return None

    def peek(self) -> list[MailboxMessage]:
        items: list[MailboxMessage] = []
        while not self._queue.empty():
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in items:
            self._queue.put_nowait(item)
        return items

    def clear(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._received.clear()
