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

"""Browser tool for the Encre tool system.

This module exposes a single :data:`browser_tool` that wraps an
:class:`~encre.computer.browser.EncreBrowserSession` running inside
Encre Agent Desktop's embedded Chromium webview. The browser is
driven through the Chrome DevTools Protocol (CDP) via a WebSocket
relay that the frontend re-pushes every time the webview guest is
(re)created.

Because multiple chat sessions can share the same desktop process,
state is kept per-session: :data:`_sessions` and :data:`_cdp_ws_urls`
are dicts keyed by session ID. The active session ID is set by the
tool executor immediately before each call, so the tool can look up
the caller's browser without the caller needing to pass a session ID
in its own arguments.
"""
import asyncio
import json
import logging
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from encre.tools import runtime as _runtime
from encre.tools.base import build_tool

if TYPE_CHECKING:
    from encre.computer.browser import EncreBrowserSession

logger = logging.getLogger("encre.tools.builtin.browser")

# Per-session browser state - each chat session gets its own isolated browser
_sessions: dict[str, EncreBrowserSession] = _runtime.browser_sessions
_cdp_ws_urls: dict[str, str] = _runtime.browser_cdp_ws_urls
_DEFAULT_KEY = _runtime.BROWSER_DEFAULT_KEY


def set_session_id(sid: str) -> None:
    """Install the current session ID into the tool runtime.

    Called by the tool executor before dispatching an invocation so
    that the module-level session lookup can find the caller's own
    browser session. The runtime slot is process-global state that the
    executor mutates at every dispatch; concurrent invocations of
    different sessions must not interleave through the same slot
    (the executor serialises dispatches on a single event loop, which
    is what makes this scheme safe).
    """
    _runtime.browser_session_id = sid


def set_engine_requester(requester: Any) -> None:
    """Install an engine-install requester on lazily-created browser sessions.

    The requester is what tells the browser session to ask the host
    to install the engine (Chromium binary) it needs. It is passed
    through the runtime so it can be installed on sessions that are
    created *after* the requester is registered (``_get_session``
    forwards it to any newly-constructed session).

    We also push the requester to any sessions that already exist so
    that a session created before this call is not left without a
    working engine-install path.
    """
    _runtime.set_engine_requester(requester)
    for s in _sessions.values():
        if hasattr(s, "set_engine_requester"):
            s.set_engine_requester(requester)


def set_cdp_url(url: str) -> None:
    """Record the CDP WebSocket URL for the current session.

    The frontend calls this whenever the webview guest is (re)created;
    when the URL is empty it is a signal that the browser tab was
    closed and the pending URL should be dropped.

    Before the session ID is known, the URL is stored under
    ``_DEFAULT_KEY`` so it is not lost; the URL is migrated to the
    real session ID the first time :func:`_get_session` is called
    with the real ID set.
    """
    if _runtime.browser_session_id:
        _cdp_ws_urls[_runtime.browser_session_id] = url
    else:
        _cdp_ws_urls[_DEFAULT_KEY] = url


def clear_cdp_url() -> None:
    """Clear the stored CDP WebSocket URL (webview tab closed/destroyed).

    Called by the frontend when the browser tab goes away. The URL is
    dropped so that :func:`_ensure_connected` does not keep retrying
    a dead endpoint while the user reopens the tab.
    """
    _runtime.clear_cdp_url()


def set_search_engine_url(url: str) -> None:
    """Set the search engine URL format for converting search queries.

    The template contains a ``{query}`` placeholder; :func:`_browser_execute`
    substitutes the URL-encoded query string when a ``navigate`` call
    receives a plain text query rather than an ``http(s)://`` URL.
    """
    _runtime.browser_search_engine_url = url


def _get_session():
    """Get or create the browser session for the current session ID.

    Sessions are created lazily so the module can be imported without
    instantiating any Chromium objects. The URL may have been stored
    under the default key by :func:`set_cdp_url` before the session ID
    was known; we migrate it here so that the next
    :func:`_ensure_connected` call sees the correct endpoint.
    """
    key = _runtime.browser_session_id if _runtime.browser_session_id else _DEFAULT_KEY
    if key not in _sessions:
        # Deferred import: the browser session class pulls in a large
        # chunk of the Encre harness. Importing it here (rather than
        # at module load) means importing this tool does not force the
        # entire harness into memory until the browser is actually used.
        from encre.computer.browser import EncreBrowserSession
        _sessions[key] = EncreBrowserSession()
        req = _runtime.get_engine_requester()
        if req is not None and hasattr(_sessions[key], "set_engine_requester"):
            _sessions[key].set_engine_requester(req)
    # Migrate URL from default key to real session ID when it becomes available
    if _runtime.browser_session_id and _DEFAULT_KEY in _cdp_ws_urls:
        _cdp_ws_urls[_runtime.browser_session_id] = _cdp_ws_urls.pop(_DEFAULT_KEY)
    return _sessions[key]


def _get_cdp_url() -> str | None:
    """Get the CDP URL for the current session.

    Falls back to the default-key entry when no session-specific URL
    has been recorded yet; this covers the race where the frontend
    pushes the URL before the session ID is registered.
    """
    sid = _runtime.browser_session_id
    if sid and sid in _cdp_ws_urls:
        return _cdp_ws_urls[sid]
    return _cdp_ws_urls.get(_DEFAULT_KEY)


async def _ensure_connected():
    """Ensure the browser session is connected to the CDP relay.

    The CDP URL is re-read on every attempt: the frontend re-pushes
    it whenever the webview guest is (re)created and pushes an empty
    URL when the browser tab is closed, so a closed-then-recreated tab
    converges on the fresh endpoint mid-wait instead of hammering a
    dead port.

    A stale session state (``_connected`` true but transport dead,
    or a URL that has since changed) is detected up front so we can
    reconnect without waiting for the next call to fail.
    """
    session = _get_session()
    cdp_url = _get_cdp_url()
    # If the session thinks it's connected but the transport is dead, reset
    if session._connected:
        if session._transport is None or not session._transport.connected:
            session._connected = False
            logger.info("[browser] transport disconnected, will reconnect")
        elif (cdp_url and session._page_ws_url
              and cdp_url != session._page_ws_url):
            logger.info("[browser] CDP URL changed: %s -> %s, reconnecting",
                        session._page_ws_url, cdp_url)
            await session.close()
            session._connected = False

    if session._connected:
        return

    deadline = time.monotonic() + 30
    while True:
        cdp_url = _get_cdp_url()
        if cdp_url:
            try:
                await session.connect(cdp_url)
                logger.info("[browser] connected to CDP relay at %s", cdp_url)
                return
            except Exception as e:
                session._connected = False
                logger.warning("[browser] connect to %s failed: %s", cdp_url, e)
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.5)
    logger.error("[browser] no working CDP relay within 30s, browser tool will not work")


async def _browser_execute(**kwargs: Any) -> str:
    """Execute a browser action against the caller's session.

    Dispatches a large set of CDP-driven actions by name. All errors
    are returned as string messages prefixed with ``Error:`` rather
    than raised, because the tool executor surfaces strings directly
    to the LLM.

    Args:
        **kwargs: Tool arguments. ``_session_id`` is a special key
            injected by the tool executor that carries the caller's
            session identifier; it is popped before the dispatch so
            the action dispatcher does not see it.

    Returns:
        A human-readable string describing the action's outcome, or
        an ``Error:``-prefixed string on failure.
    """
    # Extract session_id from kwargs (injected by the tool executor)
    _runtime.browser_session_id = kwargs.pop("_session_id", "")
    action = kwargs.get("action", "")
    session = _get_session()
    await _ensure_connected()

    if not session._connected or session._transport is None or not session._transport.connected:
        return "Error: browser not connected to webview. Try opening the browser tab in the sidebar first."

    if action == "navigate":
        url = kwargs.get("url", "")
        if not url:
            return "Error: url parameter required for navigate action"
        # Convert search queries to the configured search engine URL
        if not re.match(r'^https?://', url):
            engine_url = _runtime.browser_search_engine_url or "https://www.google.com/search?q={query}"
            url = engine_url.replace('{query}', urllib.parse.quote(url))
        state = await session.navigate(url)
        return f"Navigated to {state.url}\nTitle: {state.title}"

    elif action == "click":
        selector = kwargs.get("selector", "")
        if not selector:
            return "Error: selector parameter required for click action"
        ok = await session.click(selector)
        return f"Clicked {selector}" if ok else f"Error: failed to click {selector}"

    elif action == "click_at":
        x = kwargs.get("x")
        y = kwargs.get("y")
        if x is None or y is None:
            return "Error: x and y coordinates required for click_at"
        ok = await session.click_at(int(x), int(y))
        return f"Clicked at ({x}, {y})" if ok else f"Error: failed to click at ({x}, {y})"

    elif action == "double_click_at":
        x = kwargs.get("x")
        y = kwargs.get("y")
        if x is None or y is None:
            return "Error: x and y coordinates required for double_click_at"
        ok = await session.double_click_at(int(x), int(y))
        return f"Double-clicked at ({x}, {y})" if ok else "Error: failed"

    elif action == "right_click_at":
        x = kwargs.get("x")
        y = kwargs.get("y")
        if x is None or y is None:
            return "Error: x and y coordinates required for right_click_at"
        ok = await session.right_click_at(int(x), int(y))
        return f"Right-clicked at ({x}, {y})" if ok else "Error: failed"

    elif action == "agent_control":
        request = kwargs.get("request", "")
        if request not in ("user_takeover", "agent_control"):
            return ("Error: request must be 'user_takeover' (hand the page back to the user "
                    "immediately, e.g. login, CAPTCHA, payment) or 'agent_control' (ask the "
                    "user to approve handing control back to you)")
        result = await session.agent_control_request(request)
        if result.get("granted"):
            if request == "user_takeover":
                return ("Handed control back to the user (effective immediately, no approval "
                        "needed). Do NOT operate the page now — tell them what to do in text "
                        "if needed; to take control back later, call agent_control with "
                        "request='agent_control' and wait for the user to approve.")
            return "Granted: the user handed control back to you. Continue operating."
        reason = result.get("reason", "declined")
        return (f"Denied ({reason}). Do not retry the request immediately — continue with "
                "other work or explain the situation to the user in text.")

    elif action == "type":
        selector = kwargs.get("selector", "")
        text = kwargs.get("text", "")
        if not selector:
            return "Error: selector parameter required for type action"
        ok = await session.type_text(selector, text)
        return f"Typed into {selector}" if ok else f"Error: failed to type into {selector}"

    elif action == "type_at":
        x = kwargs.get("x")
        y = kwargs.get("y")
        text = kwargs.get("text", "")
        if x is None or y is None:
            return "Error: x and y coordinates required for type_at"
        if not text:
            return "Error: text parameter required for type_at"
        ok = await session.type_at(int(x), int(y), text)
        return f"Typed at ({x}, {y})" if ok else f"Error: failed to type at ({x}, {y})"

    elif action == "screenshot":
        full_page = kwargs.get("full_page", False)
        selector = kwargs.get("selector")
        return await session.screenshot(full_page=full_page, selector=selector)

    elif action == "screenshot_viewport":
        info = await session.screenshot_viewport()
        return json.dumps(info, ensure_ascii=False)

    elif action == "get_html":
        return await session.get_html()

    elif action == "get_text":
        selector = kwargs.get("selector")
        return await session.get_text(selector=selector)

    elif action == "execute_js":
        code = kwargs.get("code", "")
        if not code:
            return "Error: code parameter required for execute_js action"
        result = await session.execute_js(code)
        return str(result)

    elif action == "wait_for_selector":
        selector = kwargs.get("selector", "")
        if not selector:
            return "Error: selector parameter required for wait_for_selector action"
        timeout = kwargs.get("timeout")
        ok = await session.wait_for_selector(selector, timeout=timeout)
        return (
            f"Element found: {selector}"
            if ok
            else f"Timeout: element not found: {selector}"
        )

    elif action == "scroll_to":
        x = kwargs.get("x", 0)
        y = kwargs.get("y", 0)
        await session.scroll_to(x=x, y=y)
        return f"Scrolled to ({x}, {y})"

    elif action == "fill_form":
        fields = kwargs.get("fields", {})
        if not fields:
            return "Error: fields parameter required for fill_form action"
        ok = await session.fill_form(fields)
        return "Form filled successfully" if ok else "Error: failed to fill form"

    elif action == "press_key":
        key = kwargs.get("key", "")
        if not key:
            return "Error: key parameter required for press_key action"
        await session.press_key(key)
        return f"Pressed key: {key}"

    elif action == "hotkey":
        keys = kwargs.get("keys", [])
        if not keys:
            return "Error: keys array required for hotkey"
        ok = await session.hotkey([str(k) for k in keys])
        return f"Pressed hotkey: {'+'.join(keys)}" if ok else "Error: hotkey failed"

    elif action == "hover":
        selector = kwargs.get("selector", "")
        if not selector:
            return "Error: selector parameter required for hover action"
        ok = await session.hover(selector)
        return f"Hovered {selector}" if ok else f"Error: failed to hover {selector}"

    elif action == "hover_at":
        x = kwargs.get("x")
        y = kwargs.get("y")
        if x is None or y is None:
            return "Error: x and y coordinates required for hover_at"
        ok = await session.hover_at(int(x), int(y))
        return f"Hovered at ({x}, {y})" if ok else "Error: failed"

    elif action == "drag":
        x1 = kwargs.get("x")
        y1 = kwargs.get("y")
        x2 = kwargs.get("x2")
        y2 = kwargs.get("y2")
        if x1 is None or y1 is None or x2 is None or y2 is None:
            return "Error: x, y (start) and x2, y2 (end) required for drag"
        ok = await session.drag(int(x1), int(y1), int(x2), int(y2))
        return f"Dragged from ({x1},{y1}) to ({x2},{y2})" if ok else "Error: drag failed"

    elif action == "get_viewport":
        vp = await session.get_viewport()
        return json.dumps({
            "width": vp.width,
            "height": vp.height,
            "scroll_x": vp.scroll_x,
            "scroll_y": vp.scroll_y,
            "device_pixel_ratio": vp.device_pixel_ratio,
        })

    elif action == "get_state":
        state = await session.get_state()
        return f"URL: {state.url}\nTitle: {state.title}"

    elif action == "save_cookies":
        cookies = await session.save_cookies()
        return json.dumps(cookies)

    elif action == "load_cookies":
        cookies = kwargs.get("cookies", [])
        if not cookies:
            return "Error: cookies parameter required for load_cookies action"
        await session.load_cookies(cookies)
        return f"Loaded {len(cookies)} cookies"

    elif action == "close_session":
        await session.close()
        global _session
        _session = None
        return "Browser session closed"

    elif action == "a11y_snapshot":
        interesting_only = bool(kwargs.get("interesting_only", True))
        root_selector = kwargs.get("root_selector") or None
        snap = await session.a11y_snapshot(
            interesting_only=interesting_only,
            root_selector=root_selector,
        )
        return json.dumps(snap, ensure_ascii=False)

    elif action == "click_by_role":
        role = kwargs.get("role", "")
        name = kwargs.get("name", "")
        if not role or not name:
            return "Error: 'role' and 'name' are required for click_by_role"
        exact = bool(kwargs.get("exact", False))
        ok = await session.click_by_role(role, name, exact=exact)
        return (
            f"Clicked role={role} name={name!r}"
            if ok else f"Error: failed to click role={role} name={name!r}"
        )

    elif action == "get_by_text_count":
        text = kwargs.get("name") or kwargs.get("text") or ""
        if not text:
            return "Error: 'name' (text to match) required for get_by_text_count"
        exact = bool(kwargs.get("exact", False))
        count = await session.get_by_text_count(text, exact=exact)
        return json.dumps({"text": text, "count": count, "exact": exact})

    elif action == "get_page_structure":
        elements = await session.get_page_structure()
        return json.dumps({
            "url": session._state.url,
            "viewport_width": session.viewport_width,
            "viewport_height": session.viewport_height,
            "elements_count": len(elements),
            "elements": elements,
        }, ensure_ascii=False)

    elif action == "select_option":
        selector = kwargs.get("selector", "")
        value = kwargs.get("value", "")
        by = str(kwargs.get("by", "value"))
        if not selector:
            return "Error: selector parameter required for select_option"
        ok = await session.select_option(selector, value, by=by)
        return f"Selected option {value!r} in {selector}" if ok else \
            f"Error: select_option failed for {selector}"

    elif action == "get_attribute":
        selector = kwargs.get("selector", "")
        name = kwargs.get("name") or kwargs.get("attr", "")
        if not selector or not name:
            return "Error: selector and name required for get_attribute"
        value = await session.get_attribute(selector, str(name))
        return json.dumps({"selector": selector, "name": name, "value": value},
                          ensure_ascii=False)

    elif action == "get_property":
        selector = kwargs.get("selector", "")
        name = kwargs.get("name") or kwargs.get("property", "")
        if not selector or not name:
            return "Error: selector and name required for get_property"
        value = await session.get_property(selector, str(name))
        return json.dumps({"selector": selector, "name": name, "value": value},
                          ensure_ascii=False)

    elif action == "get_all_text":
        max_chars = int(kwargs.get("max_chars", 200000))
        return await session.get_all_text(max_chars=max_chars)

    elif action == "list_tabs":
        tabs = await session.list_tabs()
        return json.dumps(tabs, ensure_ascii=False)

    elif action == "switch_tab":
        index = kwargs.get("index")
        if index is None:
            return "Error: index required for switch_tab"
        ok = await session.switch_tab(int(index))
        return f"Switched to tab {index}" if ok else f"Error: tab {index} not found"

    elif action == "new_tab":
        url = kwargs.get("url") or None
        info = await session.new_tab(url=url)
        return json.dumps(info, ensure_ascii=False)

    elif action == "close_tab":
        index = kwargs.get("index")
        ok = await session.close_tab(int(index) if index is not None else None)
        return "Closed tab" if ok else "Error: failed to close tab"

    elif action == "set_dialog_handler":
        accept = bool(kwargs.get("accept", True))
        prompt_text = str(kwargs.get("prompt_text", ""))
        await session.set_dialog_handler(accept=accept, prompt_text=prompt_text)
        return f"Dialog handler installed (accept={accept})"

    elif action == "set_file_chooser_handler":
        paths = kwargs.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            return "Error: paths required for set_file_chooser_handler"
        await session.set_file_chooser_handler([str(p) for p in paths])
        return f"File chooser handler installed with {len(paths)} file(s)"

    elif action == "click_text":
        text = kwargs.get("text", "")
        if not text:
            return "Error: text parameter required for click_text"
        fuzzy = bool(kwargs.get("fuzzy", False))
        exact = bool(kwargs.get("exact", False))
        occurrence = int(kwargs.get("occurrence", 1))
        ok = await session.click_text(
            str(text), fuzzy=fuzzy, exact=exact, occurrence=occurrence,
        )
        return f"Clicked text {text!r}" if ok else f"Error: text {text!r} not found"

    elif action == "find_text":
        text = kwargs.get("text", "")
        if not text:
            return "Error: text parameter required for find_text"
        fuzzy = bool(kwargs.get("fuzzy", False))
        exact = bool(kwargs.get("exact", False))
        occurrence = int(kwargs.get("occurrence", 1))
        result = await session.find_text(
            str(text), fuzzy=fuzzy, exact=exact, occurrence=occurrence,
        )
        return json.dumps(result, ensure_ascii=False)

    elif action == "wait":
        ms = int(kwargs.get("ms", 0))
        await session.wait(ms)
        return json.dumps({"action": "wait", "ms": ms})

    elif action == "execute_cdp":
        method = kwargs.get("method", "")
        if not method:
            return "Error: 'method' parameter required for execute_cdp (e.g. 'Network.enable')"
        params = kwargs.get("params", {})
        if isinstance(params, str):
            # The LLM sometimes serialises ``params`` as a JSON string
            # even though the schema says it should be an object; we
            # tolerate both so a slight schema slip does not break a
            # call that would otherwise succeed.
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                params = {}
        result = await session._transport.send(method, params)
        return json.dumps(result, ensure_ascii=False, default=str)

    else:
        return f"Error: unknown action '{action}'"


browser_tool = build_tool(
    name="browser",
    description=(
        "Control the embedded Chromium browser inside Encre Agent Desktop to browse the "
        "web, interact with pages, and extract information.\n\n"
        "WHEN to use: tasks that need real browser interaction -- logins, clicking "
        "buttons, filling forms, scraping JS-rendered SPAs, or taking screenshots for "
        "vision-based reasoning.\n"
        "WHEN NOT to use: for plain keyword search use web_search (returns content "
        "inline, no browser needed); for a single static URL use web_fetch; for raw "
        "JSON API calls use rest_client.\n\n"
        "WORKFLOW:\n"
        "1. `navigate(url)` loads a page -- a plain query (e.g. \"today's weather\") is "
        "routed to the user's configured search engine.\n"
        "2. `get_page_structure()` maps the page: every interactive element with its "
        "tag, role, visible text, and bounding box (center_x/center_y are ready-made "
        "targets for coordinate actions).\n"
        "3. Prefer text/role matching (most robust): `click_text(\"Sign in\")`, "
        "`click_by_role(\"button\", \"Submit\")`; use `find_text` / `get_by_text_count` "
        "to locate or disambiguate visible text.\n"
        "4. Fill fields: `type(selector, text)` with your own CSS selector, "
        "`fill_form({selector: value, ...})`, `select_option(selector, value, by)`.\n"
        "5. Or act on coordinates: `screenshot()` to see the page, then "
        "`click_at(x, y)` / `type_at(x, y, text)` / `hover_at(x, y)` / "
        "`drag(x, y, x2, y2)`.\n"
        "6. Extract: `get_all_text()`, `get_text(selector)`, `get_html()`, "
        "`get_attribute(selector, name)`, `get_property(selector, name)`, or "
        "`execute_js(code)`.\n"
        "7. Tabs: `list_tabs()` / `new_tab(url)` / `switch_tab(index)` / "
        "`close_tab(index)`.\n\n"
        "TIPS:\n"
        "- Call `get_page_structure()` after every navigation. Text/role matching "
        "survives page changes; coordinates do not.\n"
        "- Gate on loading with `wait_for_selector(selector, timeout)`; pause with "
        "`wait(ms)`.\n"
        "- JS dialogs (alert/confirm/prompt) and file pickers must be handled "
        "explicitly: install `set_dialog_handler(accept, prompt_text)` or "
        "`set_file_chooser_handler(paths)` BEFORE triggering them.\n"
        "- Keyboard: `press_key(key)` / `hotkey(keys)`. Layout: `get_viewport()` / "
        "`screenshot_viewport()`.\n"
        "- `a11y_snapshot()` exposes the accessibility tree; `execute_cdp(method, "
        "params)` gives raw Chrome DevTools Protocol access (e.g. Network.enable, "
        "DOM.getDocument).\n"
        "- Pointer moves and wheel scrolling are rendered as natural human gestures "
        "(curved trajectories, eased speed) -- do not add artificial hesitation.\n\n"
        "CONTROL HANDOFF:\n"
        "You never need the user's passwords or credentials -- never ask for them in "
        "chat. When a step needs the user's own hands (login form, CAPTCHA, 2FA, "
        "payment), call `agent_control(request='user_takeover')`: control returns to "
        "the user INSTANTLY (no approval needed), and every other browser action "
        "fails while they drive -- guide them in text instead. To resume, call "
        "`agent_control(request='agent_control')`: an approval prompt appears on the "
        "page pill and ONLY the user's consent takes effect (60s silence = denied). "
        "A denial means continue differently -- never retry the request immediately.\n\n"
        "PITFALLS: if a `click` or `type` fails, the element may be off-screen -- "
        "`scroll_to(x, y)` first. The browser lives inside the Encre Agent Desktop "
        "window (no external browser opens); if actions fail with a connection "
        "error, ask the user to open the browser tab."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "navigate", "click", "click_at", "double_click_at",
                    "right_click_at", "type", "type_at", "screenshot",
                    "screenshot_viewport", "get_html", "get_text",
                    "get_all_text", "execute_js", "wait_for_selector",
                    "wait", "scroll_to", "fill_form", "press_key",
                    "hotkey", "hover", "hover_at", "drag", "get_viewport",
                    "get_state", "save_cookies", "load_cookies",
                    "close_session", "a11y_snapshot", "click_by_role",
                    "click_text", "find_text", "get_by_text_count",
                    "get_page_structure", "select_option", "get_attribute",
                    "get_property", "list_tabs", "switch_tab", "new_tab",
                    "close_tab", "set_dialog_handler",
                    "set_file_chooser_handler", "execute_cdp",
                    "agent_control",
                ],
                "description": "Browser action to perform. See tool description for workflow guidance on which action to use when",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to, or a plain search query "
                "(e.g. 'today's news') which will be sent to the configured search engine",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for DOM-based actions (click, type, hover, etc.). "
                "Get valid selectors from get_page_structure() output",
            },
            "request": {
                "type": "string",
                "enum": ["user_takeover", "agent_control"],
                "description": "agent_control: 'user_takeover' hands the page back to the user "
                "immediately (login, CAPTCHA, payment); 'agent_control' asks the user to approve "
                "handing control back to you",
            },
            "x": {
                "type": "integer",
                "description": "X coordinate in viewport pixels "
                "(for click_at, type_at, hover_at, drag start)",
            },
            "y": {
                "type": "integer",
                "description": "Y coordinate in viewport pixels "
                "(for click_at, type_at, hover_at, drag start)",
            },
            "x2": {
                "type": "integer",
                "description": "Target X coordinate (for drag end)",
            },
            "y2": {
                "type": "integer",
                "description": "Target Y coordinate (for drag end)",
            },
            "text": {
                "type": "string",
                "description": "Text to match or type: for click_text/find_text the visible text to find; "
                "for type/type_at the text to input",
            },
            "full_page": {
                "type": "boolean",
                "description": "screenshot: when true, capture the entire scrollable page "
                "instead of just the visible viewport (default false).",
            },
            "code": {
                "type": "string",
                "description": "execute_js: JavaScript expression or statement block to evaluate in "
                "the page context. Must return a JSON-serializable value to be readable.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds (for wait_for_selector)",
            },
            "fields": {
                "type": "object",
                "description": "Dict of selector:value pairs (for fill_form)",
            },
            "key": {
                "type": "string",
                "description": "Key name to press (for press_key)",
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key combination (e.g. [\"Control\", \"c\"] for copy)",
            },
            "cookies": {
                "type": "array",
                "description": "List of cookie dicts (for load_cookies)",
            },
            "interesting_only": {
                "type": "boolean",
                "description": "a11y_snapshot: prune uninteresting nodes (default true)",
            },
            "role": {
                "type": "string",
                "description": "ARIA role for click_by_role (button, link, textbox, ...)",
            },
            "name": {
                "type": "string",
                "description": "Accessible name for click_by_role / get_by_text_count",
            },
            "exact": {
                "type": "boolean",
                "description": "Use exact name matching (click_by_role / get_by_text_count)",
            },
            "root_selector": {
                "type": "string",
                "description": "Optional CSS root for a11y_snapshot to limit the tree",
            },
            "value": {
                "type": "string",
                "description": "select_option: option value (or label/index -- see by)",
            },
            "by": {
                "type": "string",
                "enum": ["value", "label", "index"],
                "description": "select_option: how value is matched (default value)",
            },
            "index": {
                "type": "integer",
                "description": "switch_tab / close_tab: 0-based tab index",
            },
            "ms": {
                "type": "integer",
                "description": "wait: milliseconds to sleep before returning",
            },
            "method": {
                "type": "string",
                "description": "CDP method name for execute_cdp "
                "(e.g. 'Network.enable', 'Console.enable', 'DOM.getDocument', 'Network.getCookies')",
            },
            "params": {
                "type": "object",
                "description": "CDP method parameters as JSON object for execute_cdp",
            },
            "accept": {
                "type": "boolean",
                "description": "set_dialog_handler: True=accept, False=dismiss (default True)",
            },
            "prompt_text": {
                "type": "string",
                "description": "set_dialog_handler: text to feed prompt() dialogs",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "set_file_chooser_handler: list of file paths to inject",
            },
            "fuzzy": {
                "type": "boolean",
                "description": (
                    "click_text / find_text: when true, match the query as a "
                    "sequence of whitespace-separated tokens appearing in order"
                ),
            },
            "occurrence": {
                "type": "integer",
                "description": "click_text / find_text: 1-based match index",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    "get_all_text: truncate the response to this many "
                    "chars (default 200000)"
                ),
            },
        },
        "required": ["action"],
    },
    execute=_browser_execute,
    intents=["coding", "system"],
    category="web",
    semantic_type="network",
    is_destructive=lambda args: args.get("action", "") in ("click", "type", "fill_form", "navigate", "execute_js", "press_key", "drag"),
)
