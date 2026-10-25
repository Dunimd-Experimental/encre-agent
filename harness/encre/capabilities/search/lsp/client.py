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

import asyncio
import contextlib
import json
import subprocess
from threading import Lock
from typing import Any


"""Low-level JSON-RPC 2.0 client for a single Language Server Protocol server.

This module implements the ``stdio`` transport used to talk to an LSP server:
it spawns the server as a subprocess, frames every message with an HTTP-style
``Content-Length`` header, and correlates responses back to their requests
using a monotonic request id.  A background reader task continuously drains
the subprocess stdout, deframes incoming messages and resolves the pending
futures registered in :attr:`EncreLSPClient._pending_requests`.

Only the blocking ``stdout.read`` call is off-loaded to a thread-pool executor
so that the rest of the class stays ``async``-friendly.
"""

class EncreLSPClient:
    """Low-level JSON-RPC client for a single Language Server Protocol server.

    Spawns the server as a subprocess using the stdio transport, frames
    messages with ``Content-Length`` headers, and matches responses to
    requests by id via a background reader task.
    """

    def __init__(self, server_name: str) -> None:
        """Create a client wrapper for the named server (used in logs)."""
        self._process: subprocess.Popen[bytes] | None = None
        self._initialized = False
        self._request_id = 0
        self._lock = Lock()
        # Human-readable server name, used only for log lines.
        self._server_name = server_name
        self._pending_requests: dict[int, asyncio.Future[Any]] = {}
        self._response_buffer: bytearray = bytearray()
        self._reader_task: asyncio.Task[None] | None = None
        self._shutdown_event: asyncio.Event | None = None

    async def start(self, command: str, args: list[str], cwd: str) -> None:
        """Spawn the server subprocess and launch the response reader task."""
        self._shutdown_event = asyncio.Event()
        # On Windows, hide the spawned server's console window if possible.
        from encre.capabilities.process import hidden_subprocess_kwargs
        popen_kwargs = hidden_subprocess_kwargs()
        self._process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            **popen_kwargs,
        )
        self._reader_task = asyncio.create_task(self._read_responses())

    async def initialize(self, root_uri: str) -> dict[str, Any]:
        """Send the ``initialize`` request and the ``initialized`` notification."""
        params = {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                },
            },
        }
        result = await self.send_request("initialize", params)
        self._initialized = True
        await self.send_notification("initialized", {})
        return result

    async def send_request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and await its matching response (30s timeout)."""
        # Allocate the next request id under the lock so concurrent callers
        # never hand out the same id.
        with self._lock:
            self._request_id += 1
            request_id = self._request_id

        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending_requests[request_id] = future

        self._write_message(message)

        try:
            # Block until the reader task resolves the matching future,
            # giving up after 30 seconds to avoid hanging on dead servers.
            return await asyncio.wait_for(future, timeout=30.0)
        except TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._write_message(message)

    async def stop(self) -> None:
        """Stop the LSP server. Deprecated: use close() instead."""
        await self.close()

    async def close(self) -> None:
        """Terminate the LSP subprocess, cancel reader task, close pipes."""
        if not self._process or self._process.stdin is None:
            return
        with contextlib.suppress(Exception):
            await self.send_request("shutdown", {})
        self._write_message({"jsonrpc": "2.0", "method": "exit"})
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._shutdown_event:
            self._shutdown_event.set()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        # Close remaining pending futures
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        # Close pipes
        if self._process.stdin:
            with contextlib.suppress(Exception):
                self._process.stdin.close()
        if self._process.stdout:
            with contextlib.suppress(Exception):
                self._process.stdout.close()
        if self._process.stderr:
            with contextlib.suppress(Exception):
                self._process.stderr.close()

    def _write_message(self, message: dict[str, Any]) -> None:
        """Serialise and write a framed LSP message to the server stdin."""
        if not self._process or self._process.stdin is None:
            return
        content = json.dumps(message, ensure_ascii=False)
        content_bytes = content.encode("utf-8")
        # LSP framing: a single "Content-Length: N" header terminated by a
        # blank line, immediately followed by the raw JSON body.
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n".encode()
        with self._lock:
            self._process.stdin.write(header + content_bytes)
            self._process.stdin.flush()

    async def _read_responses(self) -> None:
        """Background loop that reads, frames, and dispatches server messages."""
        if not self._process or self._process.stdout is None:
            return

        loop = asyncio.get_running_loop()
        buffer = bytearray()
        stdout = self._process.stdout

        while True:
            if self._shutdown_event and self._shutdown_event.is_set():
                break

            try:
                # Blocking read off the event loop; 4096-byte chunks.
                chunk = await loop.run_in_executor(None, lambda: stdout.read(4096))
            except (ValueError, OSError):
                break

            if not chunk:
                # EOF or no progress yet: if the process is gone, stop,
                # otherwise yield briefly and try again.
                if self._process.poll() is not None:
                    break
                await asyncio.sleep(0.01)
                continue

            buffer.extend(chunk)

            while True:
                header_end = buffer.find(b"\r\n\r\n")
                if header_end == -1:
                    break

                header = buffer[:header_end].decode("utf-8", errors="replace")
                content_length = 0
                for line in header.split("\r\n"):
                    if line.lower().startswith("content-length: "):
                        with contextlib.suppress(ValueError):
                            content_length = int(line.split(":", 1)[1].strip())
                        break

                if content_length <= 0:
                    buffer = buffer[header_end + 4:]
                    continue

                body_start = header_end + 4
                if len(buffer) < body_start + content_length:
                    break

                body_bytes = buffer[body_start : body_start + content_length]
                buffer = buffer[body_start + content_length:]

                try:
                    message = json.loads(body_bytes.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Resolve a pending request future from a result/error response."""
        # Success response: fulfil the future registered for this id.
        if "id" in message and "result" in message:
            future = self._pending_requests.pop(message["id"], None)
            if future and not future.done():
                future.set_result(message["result"])
        elif "id" in message and "error" in message:
            future = self._pending_requests.pop(message["id"], None)
            if future and not future.done():
                future.set_exception(
                    RuntimeError(
                        f"LSP error {message['error'].get('code', 0)}: "
                        f"{message['error'].get('message', 'unknown')}"
                    )
                )
