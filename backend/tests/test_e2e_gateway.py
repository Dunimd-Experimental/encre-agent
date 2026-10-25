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

"""End-to-end tests of the full message round trip: adapter -> gateway client -> server -> EventRouter -> back.

Simulates a QQ/Telegram-style flow: an adapter submits a message over the WS
bridge, the server resolves (or creates) a session and calls EventRouter with a
fake agent that yields canned events, and the streamed response flows back to
the adapter. These tests exist to catch the "message processed but never sent
back" class of bug, and are skipped until the WS bridge protocol is aligned.
"""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

# These e2e tests require full WS bridge protocol integration and are skipped
# until the ws_bridge client/server streaming protocol is fully aligned.
pytestmark = pytest.mark.skip(reason="WS bridge e2e requires protocol alignment")

from encre.gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from encre.gateway.session import SessionSource
from encre.config import EncreConfig
from encre.gateway.ws_bridge.client import GatewayClient
from encre.gateway.ws_bridge.server import WsBridgeServer
from encre.gateway.session import SessionStore
from encre.server.session_manager import SessionManager
from encre.utils.types import Finish, TextDelta


# ---- Stub adapter that records sends ----


class _StubAdapter:
    """Adapter stub that talks to a WsBridgeServer through a GatewayClient.

    Stands in for a remote adapter connected over WebSocket and records every
    response it receives in self.sent so assertions can inspect what was sent.
    """

    name = "test"

    def __init__(self, gateway_url="ws://127.0.0.1:18799/gateway"):
        """Create the outbound client and per-adapter state."""
        self._client = GatewayClient(gateway_url)
        self.sent: list[tuple[str, str]] = []
        self._message_handler = None

    def set_message_handler(self, handler):
        """Register an optional handler that intercepts incoming MessageEvents."""
        self._message_handler = handler

    async def send(self, chat_id, content, *, reply_to=None, metadata=None):
        """Record the outbound message and report success."""
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="m1")

    async def connect(self, *, is_reconnect=False) -> bool:
        """Open the WebSocket connection to the gateway."""
        await self._client.connect()
        return True

    async def disconnect(self) -> None:
        """Close the WebSocket connection to the gateway."""
        await self._client.disconnect()

    async def process_with_stream(self, text, chat_id, session_id=None, *, source=None):
        """Submit the text over the bridge and accumulate streamed responses.

        Concatenates every text delta and, if anything was produced, records the
        assembled response as a sent message for chat_id.
        """
        response_text = ""
        async for event in self._client.submit_stream(text, source=source.to_dict() if source else None):
            if hasattr(event, 'text'):
                response_text += event.text
        if response_text:
            self.sent.append((chat_id, response_text))

    async def handle_message(self, event):
        """Dispatch an incoming event to the handler, or fall back to streaming.

        If a custom handler is registered it is invoked; otherwise the adapter
        derives a chat id from the event source and streams the message itself.
        """
        source = event.source
        chat_id = source.chat_id if source else ""
        if self._message_handler:
            await self._message_handler(event)
        else:
            await self.process_with_stream(event.text, chat_id, source=source)


# ---- Fake agent that yields canned events ----


class _FakeAgent:
    """Minimal agent double covering everything EventRouter reads from EncreAgent.

    Provides the session, telemetry, and loop attributes plus run()/add_message()
    so the router can be exercised without contacting a real LLM.
    """

    def __init__(self, response_text: str = "Hello from agent"):
        """Store the canned response text that run() will stream back."""
        self._text = response_text

    class _FakeSession:
        """Stand-in session exposing the metadata dict the router mutates."""

        metadata = {"channel": "test"}

    session = _FakeSession()
    telemetry = type("obj", (object,), {"session_id": "test"})()
    loop = type("obj", (object,), {"cancel": lambda: None})()

    def add_message(self, role: str, content: str) -> None:
        """Accept a conversation turn and discard it, since no history is kept."""
        pass

    async def run(self, *, prompt, system_prompt=None, custom_instructions=None):
        """Yield a single TextDelta followed by a Finish event."""
        yield TextDelta(text=self._text)
        yield Finish(reason="done")


# ---- End-to-end tests ----


@pytest.mark.asyncio
async def test_verify_legacy_path_no_source_returns_response():
    """The legacy no-source path must still deliver the agent's response.

    Submits a message with no source object (the old QQ-style flow) and asserts
    the adapter records at least one sent message containing the canned reply,
    so the legacy path cannot silently drop responses.
    """
    import os

    # Fixed port; the three e2e tests use distinct ports to avoid conflicts.
    port = 18799

    # 1. Build the server-side routing stack.
    sm = SessionManager()
    config = EncreConfig()
    from encre.protocol.session_router import EventRouter

    router = EventRouter(sm, config)
    store = SessionStore(db_path=Path("/tmp/encre_test_routing.db"))

    # Replace adapter session creation so every session carries the fake agent.
    original_create = sm.create_session

    def fake_create(*a, **kw):
        """Create a real session, then swap in the fake agent."""
        info = original_create(*a, **kw)
        info.agent = _FakeAgent("Hello from agent")
        return info

    sm.create_session = fake_create

    class _Engine:
        """Runner stub exposing only what WsBridgeServer needs on this path."""

        _router = router
        _session_store = store

        async def ensure_adapter_session(self, name):
            """Allocate a session for the adapter and return its id."""
            info = sm.create_session(config=config)
            return info.session_id

    engine = _Engine()

    # 2. Start the gateway server.
    gw = WsBridgeServer(runner=engine, host="127.0.0.1", port=port)
    await gw.start()

    # 3. Create the adapter and connect its client.
    adapter = _StubAdapter(gateway_url=f"ws://127.0.0.1:{port}/gateway")
    await adapter._client.connect()
    await asyncio.sleep(0.5)  # let the handshake complete

    # 4. Simulate the legacy QQ-style call: no source argument at all.
    await adapter.process_with_stream("hello", "chat-1", session_id=None)

    # 5. Confirm the response came back through the bridge.
    assert len(adapter.sent) >= 1, f"Expected response sent, got {adapter.sent}"
    chat_id, text = adapter.sent[0]
    assert chat_id == "chat-1"
    assert "Hello from agent" in text

    # Cleanup.
    await adapter.disconnect()
    await gw.stop()
    store.close()


@pytest.mark.asyncio
async def test_verify_handle_message_no_handler_fallback_returns_response():
    """handle_message must fall back to streaming when no handler is registered.

    Without a custom _message_handler the adapter builds a source from the
    incoming event and streams it, so the adapter must still record a response
    for chat-2.
    """
    import os
    from pathlib import Path

    port = 18798
    sm = SessionManager()
    config = EncreConfig()
    from encre.protocol.session_router import EventRouter

    router = EventRouter(sm, config)
    store = SessionStore(db_path=Path("/tmp/encre_test_routing2.db"))

    original_create = sm.create_session

    def fake_create(*a, **kw):
        """Create a real session, then swap in the fake agent."""
        info = original_create(*a, **kw)
        info.agent = _FakeAgent("Response from handle_message")
        return info

    sm.create_session = fake_create

    class _Engine:
        """Runner stub that resolves sessions through the session store."""

        _router = router
        _session_store = store

        async def resolve_session(self, conn, source):
            """Look up or create a session for the given source."""
            def _create():
                """Allocate the session and stamp its metadata with the source."""
                info = sm.create_session(config=config)
                info.metadata["source"] = source.to_dict()
                info.metadata["channel"] = source.platform
                try:
                    info.agent.session.metadata["channel"] = source.platform
                except Exception:
                    pass
                return info.session_id

            return store.get_or_create(source, _create)

    engine = _Engine()

    gw = WsBridgeServer(runner=engine, host="127.0.0.1", port=port)
    await gw.start()

    adapter = _StubAdapter(gateway_url=f"ws://127.0.0.1:{port}/gateway")
    await adapter._client.connect()
    await asyncio.sleep(0.5)

    # No _message_handler set, so handle_message uses the fallback path with source.
    event = MessageEvent(
        text="hello from handle_message",
        source=SessionSource(platform="test", chat_id="chat-2", chat_type="dm", user_id="u1"),
    )
    await adapter.handle_message(event)

    assert len(adapter.sent) >= 1, f"Expected response sent, got {adapter.sent}"
    chat_id, text = adapter.sent[0]
    assert chat_id == "chat-2"
    assert "Response from handle_message" in text

    await adapter.disconnect()
    await gw.stop()
    store.close()


@pytest.mark.asyncio
async def test_verify_handle_message_with_handler_returns_response():
    """A registered handler must be invoked and must still produce a response.

    Registers a handler that delegates to process_with_stream, then asserts the
    handler ran and that the canned reply was recorded for chat-3, covering the
    normal adapter-driven path with custom pre/post processing.
    """
    port = 18797
    sm = SessionManager()
    config = EncreConfig()
    from encre.protocol.session_router import EventRouter

    router = EventRouter(sm, config)
    store = SessionStore(db_path=Path("/tmp/encre_test_routing3.db"))

    original_create = sm.create_session

    def fake_create(*a, **kw):
        """Create a real session, then swap in the fake agent."""
        info = original_create(*a, **kw)
        info.agent = _FakeAgent("Handler response")
        return info

    sm.create_session = fake_create

    class _Engine:
        """Runner stub that resolves sessions through the session store."""

        _router = router
        _session_store = store

        async def resolve_session(self, conn, source):
            """Look up or create a session for the given source."""
            def _create():
                """Allocate the session and stamp its metadata with the source."""
                info = sm.create_session(config=config)
                info.metadata["source"] = source.to_dict()
                info.metadata["channel"] = source.platform
                try:
                    info.agent.session.metadata["channel"] = source.platform
                except Exception:
                    pass
                return info.session_id

            return store.get_or_create(source, _create)

    engine = _Engine()

    gw = WsBridgeServer(runner=engine, host="127.0.0.1", port=port)
    await gw.start()

    adapter = _StubAdapter(gateway_url=f"ws://127.0.0.1:{port}/gateway")
    await adapter._client.connect()
    await asyncio.sleep(0.5)

    # Install a handler that runs the normal streaming flow.
    handler_called = False

    async def handler(event):
        """Flag the handler call and delegate to the adapter's streaming path."""
        nonlocal handler_called
        handler_called = True
        # Delegate to process_with_stream (the normal adapter-driven path).
        source = event.source
        await adapter.process_with_stream(event.text, source.chat_id if source else "", source=event.source)

    adapter.set_message_handler(handler)

    event = MessageEvent(
        text="hello with handler",
        source=SessionSource(platform="test", chat_id="chat-3", chat_type="dm", user_id="u1"),
    )
    await adapter.handle_message(event)

    assert handler_called, "handler was not called"
    assert len(adapter.sent) >= 1, f"Expected response sent, got {adapter.sent}"
    chat_id, text = adapter.sent[0]
    assert chat_id == "chat-3"
    assert "Handler response" in text

    await adapter.disconnect()
    await gw.stop()
    store.close()
