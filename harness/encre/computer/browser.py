#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""CDP-based browser automation session.

Replaces the previous Playwright-based implementation with direct
Chrome DevTools Protocol (CDP) communication.  Supports Chrome,
Edge, and other Chromium-based browsers.
"""

from encre.logging_config import get_logger
import asyncio
import contextlib
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from encre.computer.cdp import (
    CDPSession,
    CDPTransport,
    _discover_or_launch_browser,
    _ensure_page_target,
    _get_page_websocket_url,
)

logger = get_logger(__name__)

_module_launched_process: Any | None = None


@dataclass
class BrowserState:
    """Snapshot of the current page: URL, title, HTML and visible text."""
    url: str = ""
    title: str = ""
    html: str = ""
    text: str = ""


@dataclass
class BrowserViewport:
    """Viewport dimensions, scroll offsets, and device pixel ratio."""
    width: int = 0
    height: int = 0
    scroll_x: int = 0
    scroll_y: int = 0
    device_pixel_ratio: float = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_css_selector(selector: str) -> str:
    """Remove pseudo-elements that CDP ``Runtime.evaluate`` can't handle."""
    return selector.replace("::before", "").replace("::after", "")


_CDP_KEYS: dict[str, str] = {
    "Enter": "Enter",
    "Tab": "Tab",
    "Escape": "Escape",
    "Backspace": "Backspace",
    "Delete": "Delete",
    "ArrowUp": "ArrowUp",
    "ArrowDown": "ArrowDown",
    "ArrowLeft": "ArrowLeft",
    "ArrowRight": "ArrowRight",
    "Home": "Home",
    "End": "End",
    "PageUp": "PageUp",
    "PageDown": "PageDown",
    "Control": "Control",
    "Alt": "Alt",
    "Shift": "Shift",
    "Meta": "Meta",
    " ": "Space",
}


def _translate_key(key: str) -> str:
    """Map a friendly key name to its CDP key identifier (identity if unknown)."""
    return _CDP_KEYS.get(key, key)


# ---------------------------------------------------------------------------
# Main session
# ---------------------------------------------------------------------------

class EncreBrowserSession:
    """CDP-based browser automation session.

    Manages a single browser instance (Chrome/Edge) through the Chrome
    DevTools Protocol.  Supports multiple tabs, navigation, clicking,
    typing, screenshots, and all standard browser automation actions.
    """

    def __init__(
        self,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        timeout: int = 30000,
    ) -> None:
        """Store connection defaults; the browser is launched lazily on use.

        Args:
            headless: Whether to launch the browser without a visible window.
            viewport_width: Emulated viewport width in pixels.
            viewport_height: Emulated viewport height in pixels.
            timeout: Default operation timeout in milliseconds.
        """
        self.headless = headless
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.timeout = timeout

        self._transport: CDPTransport | None = None
        self._session: CDPSession | None = None
        self._page_ws_url: str | None = None
        self._port: int = 9222
        self._proc: Any | None = None
        self._state = BrowserState()
        self._last_used = time.time()
        self._engine_requester: Any | None = None
        self._page_targets: list[dict[str, Any]] = []
        self._current_target_index: int = -1
        self._connected: bool = False
        self._tasks: set[asyncio.Task[Any]] = set()
        # Last logical pointer position, kept so consecutive gestures form
        # one continuous trajectory instead of a series of teleports.
        self._mouse_pos: tuple[float, float] = (0.0, 0.0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_engine_requester(self, requester: Any | None) -> None:
        """Install the engine-install requester used when no browser is found."""
        self._engine_requester = requester

    def is_idle(self, max_idle_seconds: int = 600) -> bool:
        """Return True if the session hasn't been used within the idle window."""
        return (time.time() - self._last_used) > max_idle_seconds

    async def _ensure_browser(self) -> None:
        """Ensure the CDP transport is connected to a browser."""
        if self._connected and self._transport is not None:
            return

        global _module_launched_process

        ws_url, proc = await _discover_or_launch_browser(port=self._port)
        if proc is not None:
            _module_launched_process = proc
        self._proc = proc

        # Connect transport to the browser-level WebSocket
        self._transport = CDPTransport()
        await self._transport.connect(ws_url)

        self._connected = True
        self._tick()

        # Get or create a page target
        if not self._page_targets:
            target = await _ensure_page_target(self._transport)
            page_ws = await _get_page_websocket_url(self._port, target["targetId"])

            # Reconnect transport to the page-level WS for simpler commands
            await self._transport.disconnect()
            self._transport = CDPTransport()
            await self._transport.connect(page_ws)

            self._page_ws_url = page_ws
            self._current_target_index = 0
            self._page_targets = [target]

        await self._apply_viewport()
        await self._enable_domains()

    async def connect(self, ws_url: str) -> None:
        """Connect to an existing CDP WebSocket endpoint (e.g. Electron webview relay).

        Skips browser launch and connects directly to the provided URL.
        This is used when the frontend provides a CDP relay for its embedded webview.
        """
        self._transport = CDPTransport()
        await self._transport.connect(ws_url)
        self._connected = True
        self._page_ws_url = ws_url
        self._proc = None
        self._tick()
        # Don't override viewport for internal webview — it uses its own size
        await self._enable_domains()

    async def _enable_domains(self) -> None:
        """Enable CDP domains needed for various operations."""
        with contextlib.suppress(Exception):
            await self._transport.send("Page.enable")
        with contextlib.suppress(Exception):
            await self._transport.send("Network.enable")
        with contextlib.suppress(Exception):
            await self._transport.send("DOM.enable")
        with contextlib.suppress(Exception):
            await self._transport.send("Runtime.enable")
        # Force prefers-color-scheme: light so web pages never inherit the
        # app's dark mode and show dark content.
        with contextlib.suppress(Exception):
            await self._transport.send("Emulation.setEmulatedMedia", {
                "features": [{"name": "prefers-color-scheme", "value": "light"}],
            })
        await self._apply_stealth()

    async def _apply_stealth(self) -> None:
        """Apply anti-detection measures to avoid being flagged as a bot."""
        # Override User-Agent to look like a real Chrome browser
        with contextlib.suppress(Exception):
            await self._transport.send("Network.setUserAgentOverride", {
                "userAgent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                "platform": "Windows",
            })

        # Inject anti-detection script before every page load
        with contextlib.suppress(Exception):
            await self._transport.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    // Hide webdriver flag
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined,
                        configurable: true,
                    });

                    // Fake plugins array
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5],
                        configurable: true,
                    });

                    // Fake languages
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['zh-CN', 'zh', 'en'],
                        configurable: true,
                    });

                    // Override chrome.runtime to look like a real browser
                    if (window.chrome) {
                        window.chrome.runtime = window.chrome.runtime || {};
                    }

                    // Remove CDP-specific detection properties
                    for (const key of Object.getOwnPropertyNames(window)) {
                        if (key.startsWith('$cdc_') || key.startsWith('$chrome_')) {
                            delete window[key];
                        }
                    }
                """,
            })

    async def _apply_viewport(self) -> None:
        """Set the viewport dimensions via CDP."""
        try:
            await self._transport.send("Emulation.setDeviceMetricsOverride", {
                "width": self.viewport_width,
                "height": self.viewport_height,
                "deviceScaleFactor": 1,
                "mobile": False,
            })
        except Exception:
            logger.debug("viewport apply skipped (page may not be ready)")

    def _tick(self) -> None:
        """Update the last-used timestamp to keep the session alive."""
        self._last_used = time.time()

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    async def _refresh_targets(self) -> list[dict[str, Any]]:
        """Re-fetch the target list from the browser."""
        try:
            # We need browser-level WS for target management.
            # Connect a temporary transport to the browser endpoint.
            browser_ws = f"ws://localhost:{self._port}/devtools/browser"
            temp = CDPTransport()
            await temp.connect(browser_ws)
            result = await temp.send("Target.getTargets")
            await temp.disconnect()
            targets = result.get("targetInfos", [])
            pages = [t for t in targets if t.get("type") == "page"]
            self._page_targets = pages
            return pages
        except Exception:
            logger.debug("_refresh_targets failed", exc_info=True)
            return self._page_targets

    async def _switch_to_target(self, target_id: str) -> None:
        """Disconnect from current page and connect to a new one."""
        if self._transport:
            await self._transport.disconnect()

        page_ws = await _get_page_websocket_url(self._port, target_id)
        self._transport = CDPTransport()
        await self._transport.connect(page_ws)
        self._page_ws_url = page_ws
        await self._apply_viewport()

    async def list_tabs(self) -> list[dict[str, Any]]:
        """List all open tabs with metadata."""
        await self._ensure_browser()
        await self._refresh_targets()
        result = []
        for i, t in enumerate(self._page_targets):
            tid = t.get("id", "")
            url = t.get("url", "")
            title = t.get("title", "")
            result.append({
                "index": i,
                "url": url,
                "title": title,
                "active": i == self._current_target_index,
                "targetId": tid,
            })
        return result

    async def switch_tab(self, index: int) -> bool:
        """Switch to the tab at *index*."""
        try:
            await self._ensure_browser()
            await self._refresh_targets()
            if index < 0 or index >= len(self._page_targets):
                return False
            tid = self._page_targets[index]["id"]
            await self._switch_to_target(tid)
            self._current_target_index = index
            self._tick()
            return True
        except Exception:
            logger.debug("switch_tab failed", exc_info=True)
            return False

    async def new_tab(self, url: str | None = None) -> dict[str, Any]:
        """Open a new tab, optionally navigating to *url*."""
        await self._ensure_browser()
        browser_ws = f"ws://localhost:{self._port}/devtools/browser"
        temp = CDPTransport()
        await temp.connect(browser_ws)
        result = await temp.send("Target.createTarget", {
            "url": url or "about:blank",
        })
        await temp.disconnect()
        new_target = result
        await self._refresh_targets()

        # Find the index of the new target
        new_id = new_target.get("targetId")
        for i, t in enumerate(self._page_targets):
            if t.get("id") == new_id:
                self._current_target_index = i
                break

        # Switch to the new tab
        if new_id:
            await self._switch_to_target(new_id)

        self._tick()
        return {"index": self._current_target_index, "url": url or "", "title": ""}

    async def close_tab(self, index: int | None = None) -> bool:
        """Close the tab at *index* (default: current tab)."""
        try:
            await self._ensure_browser()
            if index is None:
                index = self._current_target_index
            await self._refresh_targets()
            if index < 0 or index >= len(self._page_targets):
                return False
            tid = self._page_targets[index]["id"]

            browser_ws = f"ws://localhost:{self._port}/devtools/browser"
            temp = CDPTransport()
            await temp.connect(browser_ws)
            await temp.send("Target.closeTarget", {"targetId": tid})
            await temp.disconnect()

            # Re-anchor to first remaining tab
            await self._refresh_targets()
            if self._page_targets:
                await self._switch_to_target(self._page_targets[0]["id"])
                self._current_target_index = 0
            else:
                self._current_target_index = -1
            self._tick()
            return True
        except Exception:
            logger.debug("close_tab failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> BrowserState:
        """Navigate to *url* and return updated BrowserState."""
        await self._ensure_browser()
        result = await self._transport.send("Page.navigate", {"url": url})
        if "errorText" in result:
            raise RuntimeError(f"Navigation failed: {result['errorText']}")
        # Wait for DOM to be ready
        with contextlib.suppress(TimeoutError):
            await self._transport.wait_for_event("Page.frameStoppedLoading", timeout=30)
        self._tick()
        return await self.get_state()

    async def go_back(self) -> bool:
        """Navigate back in history."""
        try:
            await self._ensure_browser()
            await self._transport.send("Page.navigateToHistoryEntry", {
                "entryId": -1,
            })
            self._tick()
            return True
        except Exception:
            return False

    async def go_forward(self) -> bool:
        """Navigate forward in history."""
        try:
            await self._ensure_browser()
            await self._transport.send("Page.navigateToHistoryEntry", {
                "entryId": 1,
            })
            self._tick()
            return True
        except Exception:
            return False

    async def reload(self) -> bool:
        """Reload the current page."""
        try:
            await self._ensure_browser()
            await self._transport.send("Page.reload")
            self._tick()
            return True
        except Exception:
            return False

    async def wait(self, ms: int) -> None:
        """Wait for *ms* milliseconds."""
        if ms < 0:
            raise ValueError("ms must be non-negative")
        await asyncio.sleep(ms / 1000)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def screenshot(self, full_page: bool = False, selector: str | None = None) -> str:
        """Take a screenshot and return base64-encoded PNG."""
        await self._ensure_browser()

        if selector:
            # Clip to element bounding box
            el = await self._transport.send("Runtime.evaluate", {
                "expression": _build_js_find_element(_build_css_selector(selector)),
                "returnByValue": True,
            })
            if el.get("result", {}).get("type") == "object":
                rect = el["result"].get("value", {})
                if rect.get("x") is not None:
                    capture = await self._transport.send("Page.captureScreenshot", {
                        "format": "png",
                        "clip": {
                            "x": rect["x"],
                            "y": rect["y"],
                            "width": rect["width"],
                            "height": rect["height"],
                            "scale": 1,
                        },
                    })
                    return capture["data"]

        if full_page:
            # Get full page dimensions
            metrics = await self._transport.send("Page.getLayoutMetrics")
            content_size = metrics.get("contentSize", {})
            cw = content_size.get("width", self.viewport_width)
            ch = content_size.get("height", self.viewport_height)

            await self._transport.send("Emulation.setDeviceMetricsOverride", {
                "width": int(cw),
                "height": int(ch),
                "deviceScaleFactor": 1,
                "mobile": False,
            })
            try:
                result = await self._transport.send("Page.captureScreenshot", {
                    "format": "png",
                })
                return result["data"]
            finally:
                await self._apply_viewport()

        result = await self._transport.send("Page.captureScreenshot", {
            "format": "png",
        })
        self._tick()
        return result["data"]

    async def screenshot_viewport(self) -> dict[str, Any]:
        """Return viewport info + screenshot."""
        await self._ensure_browser()
        vp = await self.get_viewport()
        b64 = await self.screenshot()
        state = await self.get_state()
        return {
            "viewport": {
                "width": vp.width,
                "height": vp.height,
                "scroll_x": vp.scroll_x,
                "scroll_y": vp.scroll_y,
                "device_pixel_ratio": vp.device_pixel_ratio,
            },
            "url": state.url,
            "title": state.title,
            "screenshot_b64": b64,
        }

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    async def get_html(self) -> str:
        """Return the full page HTML."""
        await self._ensure_browser()
        result = await self._transport.send("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", "")

    async def get_text(self, selector: str | None = None) -> str:
        """Return visible text for *selector* or the whole page."""
        await self._ensure_browser()
        js = (_build_js_find_element(_build_css_selector(selector)) + ".innerText" if selector else "document.body?.innerText || ''")
        result = await self._transport.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", "")

    async def get_all_text(self, max_chars: int = 200_000) -> str:
        """Return visible body text, truncated at *max_chars*."""
        text = await self.get_text()
        text = " ".join(text.split())
        if len(text) > max_chars:
            text = text[:max_chars] + "...(truncated)"
        return text

    async def get_url(self) -> str:
        """Return the current page URL."""
        try:
            result = await self._transport.send("Runtime.evaluate", {
                "expression": "window.location.href",
                "returnByValue": True,
            })
            return result.get("result", {}).get("value", "")
        except Exception:
            return ""

    async def get_title(self) -> str:
        """Return the current page title."""
        try:
            result = await self._transport.send("Runtime.evaluate", {
                "expression": "document.title",
                "returnByValue": True,
            })
            return result.get("result", {}).get("value", "")
        except Exception:
            return ""

    async def get_state(self) -> BrowserState:
        """Return current BrowserState (url, title, html, text)."""
        try:
            self._state.url = await self.get_url()
            self._state.title = await self.get_title()
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        # html and text are expensive; only fetch if empty
        if not self._state.html:
            with contextlib.suppress(Exception):
                self._state.html = await self.get_html()
        if not self._state.text:
            with contextlib.suppress(Exception):
                self._state.text = await self.get_text()
        return self._state

    # ------------------------------------------------------------------
    # Mouse actions (coordinate-based)
    # ------------------------------------------------------------------

    # Human-like input dynamics. Pure math: bezier paths, smoothstep easing
    # and randomized pacing. The point is that every synthesized gesture
    # carries the statistical signature of a real hand — curved approach,
    # non-constant speed, sub-pixel jitter, occasional overshoot — so bot
    # heuristics that flag teleporting pointers, laser-straight lines or
    # metronome-regular wheel deltas find nothing to match on.

    _MOVE_FRAME_MS = (6, 14)  # pointer sampling interval, min/max
    _SCROLL_FRAME_MS = (12, 26)  # wheel frame interval, min/max

    @staticmethod
    def _smoothstep(t: float) -> float:
        """S-curve easing (3t^2 - 2t^3): slow start, fast middle, soft stop."""
        return t * t * (3.0 - 2.0 * t)

    async def _human_move_to(self, x: float, y: float, button: str | None = None) -> None:
        """Move the virtual pointer to (x, y) along a human-like trajectory.

        Quadratic bezier with a random perpendicular bow (never a straight
        laser line), smoothstep velocity profile, randomized frame pacing,
        sub-pixel hand jitter and a ~20% chance of overshoot-then-correct.
        """
        x0, y0 = self._mouse_pos
        dx, dy = x - x0, y - y0
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 2:
            ev: dict[str, Any] = {"type": "mouseMoved", "x": x, "y": y}
            if button:
                ev["button"] = button
            await self._transport.send("Input.dispatchMouseEvent", ev)
            self._mouse_pos = (x, y)
            return

        steps = max(6, min(28, int(dist / 22)))
        # Control point: midpoint nudged along the perpendicular; the bow
        # scales with distance (capped) and flips side at random.
        bow = random.uniform(-0.18, 0.18) * min(dist, 600)
        ux, uy = -dy / dist, dx / dist
        cx = (x0 + x) / 2.0 + ux * bow
        cy = (y0 + y) / 2.0 + uy * bow

        overshoot = random.random() < 0.2
        tx, ty = x, y
        if overshoot:
            tx = x + dx / dist * random.uniform(2.0, 6.0)
            ty = y + dy / dist * random.uniform(2.0, 6.0)

        for i in range(1, steps + 1):
            e = self._smoothstep(i / steps)
            u = 1.0 - e
            # Hand jitter on every frame except the final one: the pointer
            # must settle exactly on the target (no 0.5px edge-hover misses).
            jx = 0.0 if i == steps else random.uniform(-0.6, 0.6)
            jy = 0.0 if i == steps else random.uniform(-0.6, 0.6)
            px = u * u * x0 + 2.0 * u * e * cx + e * e * tx + jx
            py = u * u * y0 + 2.0 * u * e * cy + e * e * ty + jy
            ev = {"type": "mouseMoved", "x": px, "y": py}
            if button:
                ev["button"] = button
            await self._transport.send("Input.dispatchMouseEvent", ev)
            await asyncio.sleep(random.uniform(*self._MOVE_FRAME_MS) / 1000.0)

        if overshoot:
            ev: dict[str, Any] = {"type": "mouseMoved", "x": x, "y": y}
            if button:
                ev["button"] = button
            await self._transport.send("Input.dispatchMouseEvent", ev)
            await asyncio.sleep(random.uniform(0.02, 0.05))

        self._mouse_pos = (float(x), float(y))

    async def _human_scroll(self, delta_x: float, delta_y: float) -> None:
        """Send a wheel gesture for the given total delta.

        Split into frames with an ease-out velocity profile — a real fling
        starts fast and bleeds off — with randomized frame deltas and pacing
        so the series never looks metronome-regular.
        """
        if abs(delta_x) < 1 and abs(delta_y) < 1:
            return
        total = abs(delta_y) + abs(delta_x)
        frames = max(2, min(14, int(total / 110) + 2))
        px, py = self._mouse_pos
        for i in range(frames):
            prev = (1.0 - i / frames) ** 1.6
            cur = (1.0 - (i + 1) / frames) ** 1.6
            frac = prev - cur
            fx = delta_x * frac + (random.uniform(-6, 6) if abs(delta_x) > 30 else 0.0)
            fy = delta_y * frac + (random.uniform(-6, 6) if abs(delta_y) > 30 else 0.0)
            await self._transport.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": px, "y": py,
                "deltaX": fx, "deltaY": fy,
            })
            await asyncio.sleep(random.uniform(*self._SCROLL_FRAME_MS) / 1000.0)

    async def agent_control_request(self, request: str) -> dict[str, Any]:
        """Ask the user (via the takeover pill) for a control change.

        ``user_takeover`` requests that the USER operate the page (logins,
        CAPTCHAs, payments — anything the user must do by hand and the Agent
        must never see); ``agent_control`` requests that the user hand
        control back to the Agent. The host relay intercepts this method —
        it is not a real CDP command — surfaces the request on the pill and
        answers ``{"granted": bool, "reason": str}`` after the user responds
        (a timeout counts as denied). A request never changes control by
        itself: only the user's answer does.
        """
        await self._ensure_browser()
        try:
            result = await self._transport.send("Encre.agentControlRequest", {"request": request})
        except Exception:
            return {"granted": False, "reason": "unavailable"}
        return result if isinstance(result, dict) else {"granted": False, "reason": "unsupported"}

    async def _mouse_event(self, x: int, y: int, action: str = "click", button: str = "left", click_count: int = 1) -> None:
        """Send mouse events to the page, approaching the target with a
        human-like pointer trajectory and an aiming pause before pressing."""
        await self._human_move_to(x, y)
        # Aiming pause: a hand stops before it presses.
        await asyncio.sleep(random.uniform(0.04, 0.12))
        if action == "double":
            for _ in range(2):
                await self._transport.send("Input.dispatchMouseEvent", {
                    "type": "mousePressed",
                    "x": x, "y": y,
                    "button": button,
                    "clickCount": 2,
                })
                await self._transport.send("Input.dispatchMouseEvent", {
                    "type": "mouseReleased",
                    "x": x, "y": y,
                    "button": button,
                    "clickCount": 2,
                })
        else:
            await self._transport.send("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": x, "y": y,
                "button": button,
                "clickCount": click_count,
            })
            await self._transport.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": x, "y": y,
                "button": button,
                "clickCount": click_count,
            })

    async def click_at(self, x: int, y: int) -> bool:
        """Click at coordinates (x, y)."""
        try:
            await self._ensure_browser()
            await self._mouse_event(x, y, "click")
            self._tick()
            return True
        except Exception:
            logger.debug("click_at failed", exc_info=True)
            return False

    async def double_click_at(self, x: int, y: int) -> bool:
        """Double-click at coordinates (x, y)."""
        try:
            await self._ensure_browser()
            await self._mouse_event(x, y, "double")
            self._tick()
            return True
        except Exception:
            return False

    async def right_click_at(self, x: int, y: int) -> bool:
        """Right-click at coordinates (x, y)."""
        try:
            await self._ensure_browser()
            await self._mouse_event(x, y, "click", button="right")
            self._tick()
            return True
        except Exception:
            return False

    async def move_mouse(self, x: int, y: int) -> bool:
        """Move the mouse to (x, y) along a human-like trajectory."""
        try:
            await self._ensure_browser()
            await self._human_move_to(x, y)
            self._tick()
            return True
        except Exception:
            return False

    async def hover_at(self, x: int, y: int) -> bool:
        """Hover at coordinates (alias for move_mouse)."""
        return await self.move_mouse(x, y)

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> bool:
        """Drag from (x1, y1) to (x2, y2) along a human-like trajectory."""
        try:
            await self._ensure_browser()
            await self._human_move_to(x1, y1)
            # Grip pause before the press, then drag along a curved, eased
            # path with the button held, then a small settle before release.
            await asyncio.sleep(random.uniform(0.04, 0.1))
            await self._transport.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x1, "y": y1,
                "button": "left", "clickCount": 1,
            })
            await self._human_move_to(x2, y2, button="left")
            await asyncio.sleep(random.uniform(0.03, 0.08))
            await self._transport.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x2, "y": y2,
                "button": "left", "clickCount": 1,
            })
            self._tick()
            return True
        except Exception:
            return False

    async def type_at(self, x: int, y: int, text: str) -> bool:
        """Click at (x, y) then type text."""
        try:
            await self._ensure_browser()
            await self.click_at(x, y)
            # Use Input.insertText for non-ASCII text to avoid keyDown/keyUp
            # corruption with CJK characters.
            if text.isascii():
                for ch in text:
                    await self._transport.send("Input.dispatchKeyEvent", {
                        "type": "keyDown", "key": ch,
                    })
                    await self._transport.send("Input.dispatchKeyEvent", {
                        "type": "char", "text": ch,
                    })
                    await self._transport.send("Input.dispatchKeyEvent", {
                        "type": "keyUp", "key": ch,
                    })
            else:
                await self._transport.send("Input.insertText", {
                    "text": text,
                })
            self._tick()
            return True
        except Exception:
            return False

    async def press_key(self, key: str) -> None:
        """Press a keyboard key."""
        await self._ensure_browser()
        cdp_key = _translate_key(key)
        await self._transport.send("Input.dispatchKeyEvent", {
            "type": "rawKeyDown",
            "key": cdp_key,
        })
        # For Enter, also send a char event so the page processes the submission
        if cdp_key == "Enter":
            await self._transport.send("Input.dispatchKeyEvent", {
                "type": "char",
                "text": "\r",
                "key": "Enter",
            })
        await self._transport.send("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": cdp_key,
        })
        self._tick()

    async def hotkey(self, keys: list[str]) -> bool:
        """Press a hotkey combination (e.g. ['Control', 'c'])."""
        if not keys:
            return False
        try:
            await self._ensure_browser()
            # Press all modifier keys down
            for k in keys:
                await self._transport.send("Input.dispatchKeyEvent", {
                    "type": "rawKeyDown",
                    "key": _translate_key(k),
                    "windowsVirtualKeyCode": 0,
                })
            # Release in reverse order
            for k in reversed(keys):
                await self._transport.send("Input.dispatchKeyEvent", {
                    "type": "keyUp",
                    "key": _translate_key(k),
                })
            self._tick()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Selector-based actions
    # ------------------------------------------------------------------

    async def _find_element_rect(self, selector: str) -> dict | None:
        """Get bounding rect of the first element matching *selector*."""
        js = _build_js_find_element(_build_css_selector(selector))
        result = await self._transport.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        val = result.get("result", {}).get("value")
        if val and isinstance(val, dict) and val.get("x") is not None:
            return val
        return None

    async def click(self, selector: str) -> bool:
        """Click the first element matching CSS *selector*."""
        try:
            await self._ensure_browser()
            rect = await self._find_element_rect(selector)
            if rect is None:
                return False
            cx = rect["x"] + rect["width"] / 2
            cy = rect["y"] + rect["height"] / 2
            return await self.click_at(int(cx), int(cy))
        except Exception:
            return False

    async def type_text(self, selector: str, text: str) -> bool:
        """Type text into the element matched by *selector*."""
        try:
            await self._ensure_browser()
            rect = await self._find_element_rect(selector)
            if rect is None:
                return False
            cx = int(rect["x"] + rect["width"] / 2)
            cy = int(rect["y"] + rect["height"] / 2)
            await self.click_at(cx, cy)
            # Clear existing content via Ctrl+A + Delete
            await self._transport.send("Input.dispatchKeyEvent", {
                "type": "rawKeyDown", "key": "Control",
            })
            await self._transport.send("Input.dispatchKeyEvent", {
                "type": "rawKeyDown", "key": "a",
            })
            await self._transport.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "a",
            })
            await self._transport.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Control",
            })
            await self._transport.send("Input.dispatchKeyEvent", {
                "type": "rawKeyDown", "key": "Delete",
            })
            await self._transport.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Delete",
            })
            # Use Input.insertText for non-ASCII text (CJK, etc.) to avoid
            # keyDown/keyUp corruption; for pure ASCII use per-char events
            # so the page sees realistic keyboard typing.
            if text.isascii():
                for ch in text:
                    await self._transport.send("Input.dispatchKeyEvent", {
                        "type": "keyDown", "key": ch,
                    })
                    await self._transport.send("Input.dispatchKeyEvent", {
                        "type": "char", "text": ch,
                    })
                    await self._transport.send("Input.dispatchKeyEvent", {
                        "type": "keyUp", "key": ch,
                    })
            else:
                await self._transport.send("Input.insertText", {
                    "text": text,
                })
            self._tick()
            return True
        except Exception:
            return False

    async def hover(self, selector: str) -> bool:
        """Hover over the first element matching *selector*."""
        try:
            await self._ensure_browser()
            rect = await self._find_element_rect(selector)
            if rect is None:
                return False
            cx = int(rect["x"] + rect["width"] / 2)
            cy = int(rect["y"] + rect["height"] / 2)
            return await self.move_mouse(cx, cy)
        except Exception:
            return False

    async def fill_form(self, fields: dict[str, str]) -> bool:
        """Fill multiple form fields (selector -> value)."""
        try:
            await self._ensure_browser()
            for sel, val in fields.items():
                await self.type_text(sel, val)
            return True
        except Exception:
            return False

    async def select_option(self, selector: str, value: str | list[str], by: str = "value") -> bool:
        """Select an option in a <select> element.

        *by* can be ``"value"``, ``"label"``, or ``"index"``.
        """
        try:
            await self._ensure_browser()
            values = [value] if isinstance(value, str) else value
            if by == "index":
                js = f"""(s => {{
                    let idx = {json.dumps(values)};
                    idx.forEach(i => {{ if (s.options[i]) s.options[i].selected = true; }});
                    s.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}(document.querySelector({json.dumps(_build_css_selector(selector))})))"""
            elif by == "label":
                js = f"""(s => {{
                    let labels = {json.dumps(values)};
                    [...s.options].forEach(o => {{
                        if (labels.includes(o.label)) o.selected = true;
                    }});
                    s.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}(document.querySelector({json.dumps(_build_css_selector(selector))})))"""
            else:
                js = f"""(s => {{
                    let vals = {json.dumps(values)};
                    [...s.options].forEach(o => {{
                        if (vals.includes(o.value)) o.selected = true;
                    }});
                    s.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}(document.querySelector({json.dumps(_build_css_selector(selector))})))"""

            await self._transport.send("Runtime.evaluate", {
                "expression": js,
            })
            self._tick()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Wait / scroll
    # ------------------------------------------------------------------

    async def wait_for_selector(self, selector: str, timeout: int | None = None) -> bool:
        """Wait for *selector* to appear in the DOM."""
        timeout = timeout or self.timeout
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            try:
                rect = await self._find_element_rect(selector)
                if rect is not None:
                    return True
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
            await asyncio.sleep(0.2)
        return False

    async def scroll_to(self, x: int = 0, y: int = 0) -> None:
        """Scroll the page to absolute position (x, y) with a human-like
        wheel gesture.

        A real user cannot ``window.scrollTo`` — wheel events must fire in a
        fast-start/slow-stop series. If the gesture undershoots (wheel deltas
        can be clamped at the document edge), one programmatic correction
        finishes the remaining gap so the functional contract holds.
        """
        await self._ensure_browser()
        vp = await self.get_viewport()
        cur_x = float(getattr(vp, "scroll_x", 0) or 0)
        cur_y = float(getattr(vp, "scroll_y", 0) or 0)
        dx = float(x) - cur_x
        dy = float(y) - cur_y
        if abs(dx) >= 1 or abs(dy) >= 1:
            await self._human_scroll(dx, dy)
            try:
                vp2 = await self.get_viewport()
                gap_x = float(x) - float(getattr(vp2, "scroll_x", 0) or 0)
                gap_y = float(y) - float(getattr(vp2, "scroll_y", 0) or 0)
                if abs(gap_x) > 1 or abs(gap_y) > 1:
                    await self._transport.send("Runtime.evaluate", {
                        "expression": f"window.scrollTo({x}, {y})",
                    })
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        self._tick()

    # ------------------------------------------------------------------
    # JS execution
    # ------------------------------------------------------------------

    async def execute_js(self, code: str) -> Any:
        """Execute JavaScript and return the result."""
        await self._ensure_browser()
        result = await self._transport.send("Runtime.evaluate", {
            "expression": code,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value")

    async def evaluate_js(self, code: str) -> Any:
        """Alias for execute_js."""
        return await self.execute_js(code)

    # ------------------------------------------------------------------
    # Viewport
    # ------------------------------------------------------------------

    async def get_viewport(self) -> BrowserViewport:
        """Get current viewport dimensions and scroll position."""
        await self._ensure_browser()
        try:
            result = await self._transport.send("Runtime.evaluate", {
                "expression": """({
                    w: window.innerWidth,
                    h: window.innerHeight,
                    sx: window.scrollX || window.pageXOffset || 0,
                    sy: window.scrollY || window.pageYOffset || 0,
                    dpr: window.devicePixelRatio || 1,
                })""",
                "returnByValue": True,
            })
            val = result.get("result", {}).get("value", {})
            return BrowserViewport(
                width=val.get("w", self.viewport_width),
                height=val.get("h", self.viewport_height),
                scroll_x=val.get("sx", 0),
                scroll_y=val.get("sy", 0),
                device_pixel_ratio=val.get("dpr", 1.0),
            )
        except Exception:
            return BrowserViewport(
                width=self.viewport_width,
                height=self.viewport_height,
            )

    # ------------------------------------------------------------------
    # Attributes / properties
    # ------------------------------------------------------------------

    async def get_attribute(self, selector: str, name: str) -> str | None:
        """Get a DOM attribute from the first matching element."""
        try:
            await self._ensure_browser()
            js = _build_js_find_element(_build_css_selector(selector)) + f".getAttribute({json.dumps(name)})"
            result = await self._transport.send("Runtime.evaluate", {
                "expression": js,
                "returnByValue": True,
            })
            val = result.get("result", {}).get("value")
            return str(val) if val is not None else None
        except Exception:
            return None

    async def get_property(self, selector: str, name: str) -> Any:
        """Get a JS property from the first matching element."""
        try:
            await self._ensure_browser()
            js = _build_js_find_element(_build_css_selector(selector)) + f".{name}"
            result = await self._transport.send("Runtime.evaluate", {
                "expression": js,
                "returnByValue": True,
            })
            return result.get("result", {}).get("value")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Cookies / storage
    # ------------------------------------------------------------------

    async def save_cookies(self) -> list[dict]:
        """Get all cookies from the browser."""
        await self._ensure_browser()
        result = await self._transport.send("Network.getAllCookies")
        return result.get("cookies", [])

    async def load_cookies(self, cookies: list[dict]) -> None:
        """Set cookies in the browser."""
        await self._ensure_browser()
        for c in cookies:
            try:
                await self._transport.send("Network.setCookie", {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "url": c.get("url", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                    "sameSite": c.get("sameSite", "None"),
                })
            except Exception:
                logger.debug("load_cookies: failed to set cookie %s", c.get("name"))

    async def get_local_storage(self) -> dict[str, str]:
        """Get localStorage entries as a plain dict."""
        try:
            await self._ensure_browser()
            result = await self._transport.send("Runtime.evaluate", {
                "expression": """JSON.stringify(window.localStorage || {})""",
                "returnByValue": True,
            })
            raw = result.get("result", {}).get("value", "{}")
            return json.loads(raw)
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Dialogs / file choosers
    # ------------------------------------------------------------------

    async def set_dialog_handler(self, accept: bool = True, prompt_text: str = "") -> None:
        """Handle next dialog by accepting/dismissing with optional text."""
        await self._ensure_browser()
        # Intercept the next Page.javascriptDialogOpening event
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        def handler(params: dict[str, Any]) -> None:
            if not fut.done():
                fut.set_result(params)

        self._transport.on("Page.javascriptDialogOpening", handler)

        async def _respond() -> None:
            try:
                await asyncio.wait_for(fut, timeout=30)
            except (TimeoutError, Exception):
                return
            finally:
                self._transport.off("Page.javascriptDialogOpening", handler)
            with contextlib.suppress(Exception):
                await self._transport.send("Page.handleJavaScriptDialog", {
                    "accept": accept,
                    "promptText": prompt_text,
                })

        _t = asyncio.create_task(_respond())
        self._tasks.add(_t)

    async def set_file_chooser_handler(self, paths: list[str]) -> None:
        """Intercept file chooser dialog and provide files."""
        await self._ensure_browser()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        def handler(params: dict[str, Any]) -> None:
            if not fut.done():
                fut.set_result(params)

        self._transport.on("Page.fileChooserOpened", handler)

        async def _respond() -> None:
            try:
                evt = await asyncio.wait_for(fut, timeout=30)
            except (TimeoutError, Exception):
                return
            finally:
                self._transport.off("Page.fileChooserOpened", handler)
            with contextlib.suppress(Exception):
                await self._transport.send("DOM.setFileInputFiles", {
                    "objectId": evt.get("backendNodeId", ""),
                    "files": paths,
                })

        _t = asyncio.create_task(_respond())
        self._tasks.add(_t)

    # ------------------------------------------------------------------
    # Text-based interaction
    # ------------------------------------------------------------------

    async def find_text(
        self,
        text: str,
        *,
        fuzzy: bool = False,
        occurrence: int = 1,
        exact: bool = False,
    ) -> dict[str, Any]:
        """Find text on the page and return its bounding box + metadata."""
        await self._ensure_browser()
        match_type = "exact" if exact else "substring"
        js = _build_js_find_text(text, match_type, occurrence, fuzzy)
        result = await self._transport.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        val = result.get("result", {}).get("value", {})
        if not val or not isinstance(val, dict):
            return {"found": False}

        # Try to scroll into view
        if val.get("targetId"):
            with contextlib.suppress(Exception):
                await self._transport.send("Runtime.evaluate", {
                    "expression": f"document.querySelector('[data-encre-tid={val['targetId']}]')?.scrollIntoView({{block:'center'}})",
                })

        return val

    async def click_text(
        self,
        text: str,
        *,
        fuzzy: bool = False,
        occurrence: int = 1,
        exact: bool = False,
    ) -> bool:
        """Click the Nth occurrence of visible text."""
        info = await self.find_text(text, fuzzy=fuzzy, occurrence=occurrence, exact=exact)
        if not info.get("found"):
            return False
        cx = info.get("center_x") or info.get("x", 0) + info.get("width", 0) / 2
        cy = info.get("center_y") or info.get("y", 0) + info.get("height", 0) / 2
        return await self.click_at(int(cx), int(cy))

    async def get_by_text_count(self, text: str, exact: bool = False) -> int:
        """Count elements matching the given text."""
        await self._ensure_browser()
        safe_text = json.dumps(text)
        cond = f"el.textContent.trim() === {safe_text}" if exact else f"el.textContent.trim().includes({safe_text})"
        js = f"""(() => {{
            let count = 0;
            let walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            let node;
            while (node = walker.nextNode()) {{
                let t = node.textContent.trim();
                if (t && {cond}) count++;
            }}
            return count;
        }})()"""
        result = await self._transport.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", 0)

    # ------------------------------------------------------------------
    # Accessibility
    # ------------------------------------------------------------------

    async def a11y_snapshot(
        self,
        _interesting_only: bool = True,
        _root_selector: str | None = None,
    ) -> dict[str, Any]:
        """Build an accessibility tree via CDP."""
        await self._ensure_browser()
        try:
            return await self._transport.send("Accessibility.getFullAXTree", {})
        except Exception:
            return {}

    async def click_by_role(self, role: str, name: str, exact: bool = False) -> bool:
        """Click an element by ARIA role and accessible name."""
        await self._ensure_browser()
        match_op = "===" if exact else ".includes"
        js = f"""(() => {{
            let el = document.querySelector(`[role="{role}"]`);
            if (!el) return null;
            if (el.getAttribute('aria-label') {match_op} {json.dumps(name)}) {{
                let r = el.getBoundingClientRect();
                return {{ x: r.x, y: r.y, w: r.width, h: r.height }};
            }}
            // Search children
            let all = [...document.querySelectorAll(`[role="{role}"]`)];
            for (let e of all) {{
                let label = e.getAttribute('aria-label') || e.textContent?.trim() || '';
                if (label {match_op} {json.dumps(name)}) {{
                    let r = e.getBoundingClientRect();
                    return {{ x: r.x, y: r.y, w: r.width, h: r.height }};
                }}
            }}
            return null;
        }})()"""
        result = await self._transport.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        val = result.get("result", {}).get("value")
        if not val or val.get("x") is None:
            return False
        cx = int(val["x"] + val["w"] / 2)
        cy = int(val["y"] + val["h"] / 2)
        return await self.click_at(cx, cy)

    # ------------------------------------------------------------------
    # Page structure
    # ------------------------------------------------------------------

    async def get_page_structure(self) -> list[dict[str, Any]]:
        """Get all interactive elements with bounding boxes."""
        await self._ensure_browser()
        js = """(() => {
            const tags = ['a', 'button', 'input', 'select', 'textarea', 'details', 'summary',
                '[tabindex]', '[contenteditable]', '[onclick]', '[role=button]', '[role=link]',
                '[role=checkbox]', '[role=radio]', '[role=tab]', '[role=menuitem]'];
            const seen = new Set();
            const result = [];
            document.querySelectorAll(tags.join(',')).forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return;
                const key = `${r.x.toFixed(0)},${r.y.toFixed(0)},${r.width.toFixed(0)},${r.height.toFixed(0)}`;
                if (seen.has(key)) return;
                seen.add(key);
                result.push({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    role: el.getAttribute('role') || '',
                    text: (el.textContent || '').trim().slice(0, 100),
                    href: el.href || '',
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                    center_x: Math.round(r.x + r.width / 2),
                    center_y: Math.round(r.y + r.height / 2),
                });
            });
            // Also collect heading/title blocks for context
            document.querySelectorAll('h1,h2,h3,h4,h5,h6,label,legend,caption,th,dt,strong').forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return;
                const t = (el.textContent || '').trim();
                if (!t) return;
                result.push({
                    tag: el.tagName.toLowerCase(),
                    type: 'label',
                    role: 'heading',
                    text: t.slice(0, 200),
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                });
            });
            return result;
        })()"""
        result = await self._transport.send("Runtime.evaluate", {
            "expression": js,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value", [])

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the browser session and clean up."""
        global _module_launched_process

        if self._transport:
            with contextlib.suppress(Exception):
                await self._transport.disconnect()
            self._transport = None
        self._connected = False
        self._session = None
        self._page_ws_url = None

        if self._proc:
            with contextlib.suppress(Exception):
                self._proc.kill()
            self._proc = None
            _module_launched_process = None


# ---------------------------------------------------------------------------
# JS helpers
# ---------------------------------------------------------------------------

def _build_js_find_element(selector: str) -> str:
    """Generate JS to find an element's bounding rect."""
    return f"""(() => {{
    let el = document.querySelector({json.dumps(selector)});
    if (!el) return null;
    let r = el.getBoundingClientRect();
    return {{ x: r.x, y: r.y, width: r.width, height: r.height }};
}})()"""


def _build_js_find_text(text: str, match_type: str = "substring", occurrence: int = 1, fuzzy: bool = False) -> str:
    """Generate JS to find visible text and return bounding box."""
    safe_text = json.dumps(text)
    fuzzy_js = "true" if fuzzy else "false"
    exact_cond = f"t === {safe_text}" if match_type == "exact" else f"t.includes({safe_text})"
    return f"""(() => {{
    let matches = [];
    let walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while (node = walker.nextNode()) {{
        let t = node.textContent.trim();
        if (!t) continue;
        if ({exact_cond}) {{
            let r = node.parentElement.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {{
                matches.push({{
                    element: node.parentElement,
                    text: t.slice(0, 200),
                    x: r.x, y: r.y, width: r.width, height: r.height,
                }});
            }}
        }}
    }}
    if ({fuzzy_js} && matches.length === 0) {{
        let words = {safe_text}.toLowerCase().split(/\\s+/).filter(Boolean);
        walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        while (node = walker.nextNode()) {{
            let t = node.textContent.trim().toLowerCase();
            if (words.every(w => t.includes(w))) {{
                let r = node.parentElement.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {{
                    matches.push({{
                        element: node.parentElement,
                        text: node.textContent.trim().slice(0, 200),
                        x: r.x, y: r.y, width: r.width, height: r.height,
                    }});
                }}
            }}
        }}
    }}
    if (matches.length === 0) return {{ found: false }};
    let idx = Math.min({occurrence} - 1, matches.length - 1);
    let m = matches[idx];
    let targetId = 'encre-' + Date.now() + '-' + idx;
    m.element.setAttribute('data-encre-tid', targetId);
    return {{
        found: true,
        x: m.x, y: m.y, width: m.width, height: m.height,
        center_x: m.x + m.width / 2,
        center_y: m.y + m.height / 2,
        match_count: matches.length,
        match_index: idx,
        text: m.text,
        targetId: targetId,
    }};
}})()"""
