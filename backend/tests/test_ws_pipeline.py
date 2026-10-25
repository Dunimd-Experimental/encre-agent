"""Outbound WebSocket pipeline: producers enqueue, one writer drains.

The pipeline exists so the agent loop never waits on socket I/O: `_send`
serialises and enqueues, a per-connection writer encrypts and writes in order.
These tests pin those two properties.
"""

from __future__ import annotations

import asyncio
import json

from encre.crypto import decrypt, encrypt
from encre.protocol.handlers.base import HandlerBase
from encre.protocol.handlers.connection import ConnectionState
from encre.server.protocol import _CLIENT_MESSAGE_PARSERS, ClientPing, parse_client_message


class _FakeWs:
    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.frames: list[str] = []
        self.delay = delay
        self.fail = fail

    async def send(self, frame: str) -> None:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("peer gone")
        self.frames.append(frame)


class _StubHandler(HandlerBase):
    """Binds just enough of HandlerBase to exercise `_send`/_writer_loop."""

    def __init__(self, ws: _FakeWs) -> None:
        self.state = ConnectionState(ws=ws, ws_id="test")
        self.cancelled = 0

    def _connection_for(self, ws):  # noqa: ANN001
        return self.state

    def _cancel_current_task(self, state):  # noqa: ANN001
        self.cancelled += 1


def test_send_does_not_wait_for_socket_writes_and_preserves_order():
    async def scenario() -> None:
        ws = _FakeWs(delay=0.2)
        stub = _StubHandler(ws)
        stub.state.writer_task = asyncio.create_task(stub._writer_loop(stub.state))

        loop = asyncio.get_running_loop()
        started = loop.time()
        for index in range(3):
            await stub._send(ws, "text_delta", text=str(index))
        enqueue_elapsed = loop.time() - started
        assert enqueue_elapsed < 0.1, "producers must not wait on the socket write"

        deadline = loop.time() + 3.0
        while len(ws.frames) < 3 and loop.time() < deadline:
            await asyncio.sleep(0.01)

        assert len(ws.frames) == 3
        assert stub.state.closed is False
        decoded = [json.loads(decrypt(frame)) for frame in ws.frames]
        assert [item["type"] for item in decoded] == ["text_delta"] * 3
        assert [item["text"] for item in decoded] == ["0", "1", "2"]
        assert all(not frame.startswith("{") for frame in ws.frames), "frames must be encrypted"

        stub.state.closed = True
        stub.state.writer_task.cancel()
        await asyncio.gather(stub.state.writer_task, return_exceptions=True)

    asyncio.run(scenario())


def test_writer_failure_closes_connection_and_cancels_run():
    async def scenario() -> None:
        ws = _FakeWs(fail=True)
        stub = _StubHandler(ws)
        stub.state.writer_task = asyncio.create_task(stub._writer_loop(stub.state))

        await stub._send(ws, "finish", reason="done")
        await asyncio.gather(stub.state.writer_task, return_exceptions=True)

        assert stub.state.closed is True
        assert stub.cancelled == 1
        await stub._send(ws, "text_delta", text="ignored")
        assert stub.state.outbox.empty(), "a closed connection must not accept more frames"

    asyncio.run(scenario())


def test_inbound_frames_require_ciphertext_after_handshake():
    """The handshake is not a one-off check: later plaintext frames are dropped."""

    plain = json.dumps({"type": "ping"})
    # Before the handshake (or on a permissive caller) plaintext still works.
    assert parse_client_message(plain) is not None
    # The server loop marks the connection encrypted after the first frame.
    assert parse_client_message(plain, require_encryption=True) is None
    assert parse_client_message("not-json", require_encryption=True) is None
    parsed = parse_client_message(encrypt(plain), require_encryption=True)
    assert type(parsed).__name__ == "ClientPing"


def test_client_parser_table_is_built_once():
    """`parse_client_message` must not rebuild the ~120-entry map per frame."""

    assert isinstance(_CLIENT_MESSAGE_PARSERS, dict)
    assert _CLIENT_MESSAGE_PARSERS["ping"] is ClientPing
