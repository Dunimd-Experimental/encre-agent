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

from encre.logging_config import get_logger

"""Chrome DevTools Protocol (CDP) transport layer.

Provides low-level WebSocket-based communication with Chromium-based
browsers (Chrome, Edge, etc.) and Firefox (via Remote Protocol).
"""

import asyncio
import contextlib
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any

import websockets

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# CDP Transport
# ---------------------------------------------------------------------------

class CDPTransport:
    """Low-level WebSocket transport for Chrome DevTools Protocol.

    Manages a single CDP WebSocket connection to a browser endpoint.
    Supports command/response (JSON-RPC 2.0) and event streaming.
    """

    def __init__(self) -> None:
        """Initialise empty connection, pending-request and handler state."""
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._msg_id: int = 0
        self._connected: bool = False
        self._event_handlers: dict[str, list[Callable]] = {}
        self._reader_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        """Return True while the WebSocket connection is open."""
        return self._connected

    async def connect(self, url: str) -> None:
        """Connect to a CDP WebSocket endpoint."""
        if self._connected:
            return
        self._ws = await websockets.connect(url, max_size=2**30)
        self._connected = True
        self._reader_task = asyncio.create_task(self._reader())

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._ws and self._connected:
            await self._ws.close()
        self._ws = None
        self._connected = False
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
        """Send a CDP command and wait for its result.

        Returns the ``result`` dict from the response.
        Raises :exc:`RuntimeError` if the command fails or times out.
        """
        if not self._connected or self._ws is None:
            raise RuntimeError("CDP transport not connected")
        self._msg_id += 1
        msg_id = self._msg_id
        body = {"id": msg_id, "method": method, "params": params or {}}
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(json.dumps(body))
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"CDP timeout ({method}): no response within {timeout}s")
        except asyncio.CancelledError:
            self._pending.pop(msg_id, None)
            raise
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"CDP error ({method}): {err.get('message', err)}")
        return resp.get("result", {})

    def on(self, method: str, handler: Callable) -> None:
        """Register a handler for CDP events (e.g. ``Page.screencastFrame``)."""
        self._event_handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler: Callable) -> None:
        """Remove a previously registered event handler."""
        handlers = self._event_handlers.get(method, [])
        if handler in handlers:
            handlers.remove(handler)

    async def _reader(self) -> None:
        """Continuously read messages from the WebSocket."""
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("CDP: invalid JSON received: %s", raw[:200])
                    continue
                msg_id = data.get("id")
                if msg_id is not None:
                    fut = self._pending.pop(msg_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                else:
                    method = data.get("method", "")
                    params = data.get("params", {})
                    await self._dispatch_event(method, params)
        except websockets.exceptions.ConnectionClosed:
            logger.info("CDP: connection closed")
        except asyncio.CancelledError:
            logger.debug("exception suppressed", exc_info=True)
        except Exception:
            logger.exception("CDP: reader error")
        finally:
            self._connected = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()

    async def _dispatch_event(self, method: str, params: dict[str, Any]) -> None:
        """Dispatch a CDP event to registered handlers."""
        handlers = self._event_handlers.get(method, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(params)
                else:
                    handler(params)
            except Exception:
                logger.exception("CDP: event handler error for %s", method)

    async def wait_for_event(self, method: str, timeout: float = 30) -> dict[str, Any]:
        """Wait for the next occurrence of a specific CDP event."""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        def handler(params: dict[str, Any]) -> None:
            if not fut.done():
                fut.set_result(params)

        self.on(method, handler)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            raise TimeoutError(f"Timed out waiting for CDP event: {method}") from None
        finally:
            self.off(method, handler)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class CDPSession:
    """A CDP session attached to a specific browser target (tab/page).

    Provides a convenient interface for common CDP operations.
    """

    def __init__(self, transport: CDPTransport) -> None:
        """Bind this session to a shared :class:`CDPTransport` (detached)."""
        self._transport = transport
        self._session_id: str | None = None
        self._target_id: str | None = None
        self._attached: bool = False

    @property
    def transport(self) -> CDPTransport:
        """Return the underlying CDP transport used by this session."""
        return self._transport

    @property
    def target_id(self) -> str | None:
        """Return the attached target (tab) id, or None if detached."""
        return self._target_id

    @property
    def attached(self) -> bool:
        """Return True while this session is attached to a target."""
        return self._attached

    async def attach(self, target_id: str) -> None:
        """Attach to the given target (creates a session)."""
        result = await self._transport.send("Target.attachToTarget", {
            "targetId": target_id,
            "flatten": True,
        })
        self._session_id = result.get("sessionId")
        self._target_id = target_id
        self._attached = True

    async def detach(self) -> None:
        """Detach from the current target."""
        if not self._attached or not self._session_id:
            return
        await self._transport.send("Target.detachFromTarget", {
            "sessionId": self._session_id,
        })
        self._session_id = None
        self._target_id = None
        self._attached = False

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a CDP command scoped to this session (tab)."""
        if not self._attached:
            raise RuntimeError("No target attached")
        # Flattened mode: commands that target a specific session use
        # ``Target.sendMessageToTarget`` under the hood, but with
        # ``flatten=True`` we can send commands scoped by ``sessionId``
        # directly on the browser WebSocket.
        result = await self._transport.send(method, {
            **(params or {}),
            "sessionId": self._session_id,
        })
        return result


# ---------------------------------------------------------------------------
# Browser detection & launch helpers
# ---------------------------------------------------------------------------

_CHROME_PATHS: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    ],
}


def _find_browser_executable() -> str | None:
    """Find an installed Chromium-based browser executable."""
    system = platform.system().lower()
    if system == "windows":
        key = "win32"
    elif system == "darwin":
        key = "darwin"
    else:
        key = "linux"

    for path in _CHROME_PATHS.get(key, []):
        if os.path.isfile(path):
            return path

    # Fallback: try ``where`` / ``which``
    exe = shutil.which("chrome") or shutil.which("chromium") or shutil.which("msedge") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if exe:
        return exe

    return None


async def _launch_browser(browser_path: str, port: int = 9222, headless: bool = False) -> subprocess.Popen:
    """Launch a Chromium browser with remote debugging enabled.

    Returns the subprocess handle (caller must manage lifetime).
    """
    user_data_dir = tempfile.mkdtemp(prefix="encre_chrome_")
    args = [
        browser_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=TranslateUI",
        "--disable-sync",
    ]
    if headless:
        args.append("--headless=new")

    logger.info("Launching browser: %s", " ".join(args))
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


async def _discover_or_launch_browser(port: int = 9222) -> tuple[str, subprocess.Popen | None]:
    """Try to connect to an existing debuggable browser, or launch one.

    Returns ``(cdp_ws_url, process)`` where *process* is ``None`` if an
    existing browser was used (caller should not kill it).
    """
    import urllib.request

    # Try connecting to an already-running instance
    try:
        req = urllib.request.Request(f"http://localhost:{port}/json/version")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            ws_url = data.get("webSocketDebuggerUrl")
            if ws_url:
                logger.info("Connected to existing browser at %s", ws_url)
                return ws_url, None
    except Exception:
        logger.debug("exception suppressed", exc_info=True)

    # Launch a new browser instance
    browser_path = _find_browser_executable()
    if browser_path is None:
        raise RuntimeError(
            "No Chromium-based browser found. "
            "Please install Chrome, Edge, or Chromium."
        )

    proc = await _launch_browser(browser_path, port=port)

    # Wait for the browser to start responding
    for _attempt in range(30):
        await asyncio.sleep(1)
        try:
            req = urllib.request.Request(f"http://localhost:{port}/json/version")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                ws_url = data.get("webSocketDebuggerUrl")
                if ws_url:
                    logger.info("Launched browser at %s", ws_url)
                    return ws_url, proc
        except Exception:
            continue

    proc.kill()
    raise RuntimeError("Browser started but did not respond in time")


async def _ensure_page_target(transport: CDPTransport) -> dict[str, Any]:
    """Find or create a page target. Returns ``{targetId, ...}``."""
    result = await transport.send("Target.getTargets")
    targets = result.get("targetInfos", [])

    # Find first page target (not service worker, etc.)
    for t in targets:
        if t.get("type") == "page":
            return t

    # Create a new blank tab
    result = await transport.send("Target.createTarget", {
        "url": "about:blank",
    })
    return result


async def _get_page_websocket_url(port: int, target_id: str) -> str:
    """Get the page-level WebSocket URL for a specific target."""
    import urllib.request

    req = urllib.request.Request(f"http://localhost:{port}/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        targets = json.loads(resp.read().decode())

    for t in targets:
        if t.get("id") == target_id:
            return t["webSocketDebuggerUrl"]

    raise RuntimeError(f"Target {target_id} not found")
