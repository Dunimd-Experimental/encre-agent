#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

from __future__ import annotations

"""Tests for encre.computer.browser -- EncreBrowserSession and BrowserState."""

import time

# ===========================================================================
# BrowserState dataclass
# ===========================================================================

class TestBrowserState:
    """Engineered to validate the BrowserState dataclass contract and defaults.

    This test class exercises BrowserState across 4 scenarios to ensure that
    the dataclass initializes with empty string defaults for all fields,
    correctly stores provided values, and is recognized as a dataclass by
    the standard library. The design ensures that BrowserState can be
    instantiated with zero arguments and used as a neutral container for
    browser page state before any navigation occurs.
    """

    def test_verify_default_creation_produces_empty_strings(self):
        """Validate that BrowserState() initializes all fields to empty strings.

        The test constructs a BrowserState with no arguments and asserts that
        url, title, html, and text are all empty strings because the dataclass
        must provide safe zero-value defaults for all fields.
        """
        from encre.computer.browser import BrowserState
        state = BrowserState()
        assert state.url == ""
        assert state.title == ""
        assert state.html == ""
        assert state.text == ""

    def test_verify_construction_with_explicit_values(self):
        """Validate that BrowserState stores all provided field values correctly.

        The test constructs a BrowserState with explicit url, title, html, and
        text values and asserts each field matches because the dataclass must
        preserve all constructor arguments without mutation or transformation.
        """
        from encre.computer.browser import BrowserState
        state = BrowserState(
            url="https://example.com",
            title="Example Domain",
            html="<html><body>Example</body></html>",
            text="Example",
        )
        assert state.url == "https://example.com"
        assert state.title == "Example Domain"
        assert state.html == "<html><body>Example</body></html>"
        assert state.text == "Example"

    def test_verify_browserstate_is_dataclass(self):
        """Validate that BrowserState is recognized as a dataclass by dataclasses.is_dataclass.

        The test asserts is_dataclass(BrowserState) is True because the class
        relies on dataclass-generated __init__, __repr__, and comparison
        methods for correct object behavior.
        """
        from dataclasses import is_dataclass
        from encre.computer.browser import BrowserState
        assert is_dataclass(BrowserState)

    def test_verify_all_fields_have_empty_string_defaults(self):
        """Validate that every field on BrowserState defaults to an empty string.

        The test iterates over the four known field names and asserts each
        returns "" on a default-constructed instance because all fields must
        have uniform empty-string defaults for safe unpacking.
        """
        from encre.computer.browser import BrowserState
        state = BrowserState()
        for field_name in ["url", "title", "html", "text"]:
            assert getattr(state, field_name) == ""


# ===========================================================================
# EncreBrowserSession construction
# ===========================================================================

class TestEncreBrowserSessionConstruction:
    """Engineered to validate EncreBrowserSession construction parameters and initial internal state.

    This test class exercises session construction across 5 scenarios to ensure
    that default and custom constructor arguments are correctly stored, and that
    internal private attributes (_pw, _browser, _context, _page) are initialized
    to None before any browser is launched. The design follows a lazy-initialization
    pattern where the Playwright handle is created on first navigate() call.
    """

    def test_verify_default_construction_applies_builtin_defaults(self):
        """Validate that EncreBrowserSession() applies correct default constructor values.

        The test constructs a session with no arguments and asserts headless is
        True, viewport is 1280x800, and timeout is 30000ms because these are
        the production-safe defaults for automated browser automation.
        """
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session is not None
        assert session.headless is True
        assert session.viewport_width == 1280
        assert session.viewport_height == 800
        assert session.timeout == 30000

    def test_verify_custom_construction_stores_all_parameters(self):
        """Validate that EncreBrowserSession stores all custom constructor arguments.

        The test constructs a session with non-default headless, viewport, and
        timeout values and asserts each is stored correctly because the
        constructor must not silently override or discard user-provided config.
        """
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession(
            headless=False,
            viewport_width=1920,
            viewport_height=1080,
            timeout=60000,
        )
        assert session.headless is False
        assert session.viewport_width == 1920
        assert session.viewport_height == 1080
        assert session.timeout == 60000

    def test_verify_initial_internal_browser_handles_are_none(self):
        """Validate that all internal CDP handles start as None/disconnected before launch.

        The test asserts _transport, _session, and _page_ws_url are None and
        _connected is False because the session uses lazy initialization —
        these handles are created on first navigate() call and must not be
        pre-allocated.
        """
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session._transport is None
        assert session._session is None
        assert session._page_ws_url is None
        assert session._connected is False

    def test_verify_initial_browser_state_is_empty(self):
        """Validate that the internal BrowserState starts with all empty fields.

        The test asserts session._state has empty url, title, html, and text
        because a freshly constructed session has not navigated anywhere and
        its state snapshot must reflect a neutral uninitialized condition.
        """
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session._state.url == ""
        assert session._state.title == ""
        assert session._state.html == ""
        assert session._state.text == ""

    def test_verify_last_used_timestamp_is_set_on_construction(self):
        """Validate that _last_used is set to the current time during construction.

        The test records wall-clock time before and after construction and asserts
        _last_used falls within that window because the idle detection mechanism
        depends on an accurate timestamp to determine session freshness.
        """
        from encre.computer.browser import EncreBrowserSession
        before = time.time()
        session = EncreBrowserSession()
        after = time.time()
        assert before <= session._last_used <= after


# ===========================================================================
# EncreBrowserSession state methods (no browser needed)
# ===========================================================================

class TestEncreBrowserSessionState:
    """Engineered to validate state-only methods that operate without a running Playwright browser.

    This test class exercises 6 scenarios covering get_state, is_idle,
    save_cookies, and close on a session that has never launched a browser.
    The design ensures these methods are safe to call in the pre-launch
    state without raising, supporting idempotent close and correct idle
    detection based on the last_used timestamp.
    """

    def test_verify_get_state_before_navigate_returns_browserstate(self):
        """Validate that get_state() returns a BrowserState instance before any navigation.

        The test runs get_state on a fresh session and asserts the result is
        an instance of BrowserState because the method must always return the
        current state snapshot regardless of whether a browser page exists.
        """
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            session = EncreBrowserSession()
            state = await session.get_state()
            assert isinstance(state, object)
            from encre.computer.browser import BrowserState
            assert isinstance(state, BrowserState)

        import asyncio
        asyncio.run(_test())

    def test_verify_fresh_session_is_not_idle_within_tolerance(self):
        """Validate that a freshly created session is not considered idle with a generous threshold.

        The test asserts is_idle(max_idle_seconds=600) returns False because a
        session just constructed has _last_used equal to now and must not
        trigger idle eviction before any actual idle period elapses.
        """
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session.is_idle(max_idle_seconds=600) is False

    def test_verify_zero_threshold_immediately_idles_fresh_session(self):
        """Validate that is_idle(max_idle_seconds=0) returns True immediately after construction.

        The test asserts is_idle(0) is True because a zero-second threshold
        means any non-negative elapsed time qualifies as idle, which is the
        expected behavior for the boundary condition of the idle check.
        """
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session.is_idle(max_idle_seconds=0) is True

    def test_verify_save_cookies_before_launch_returns_empty_list(self):
        """Validate that save_cookies() returns an empty list before the browser is launched.

        The test runs save_cookies on a fresh session and asserts the result
        is [] because no cookies exist until a browser context is created and
        a page is navigated.
        """
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            session = EncreBrowserSession()
            cookies = await session.save_cookies()
            assert cookies == []

        import asyncio
        asyncio.run(_test())

    def test_verify_close_before_launch_is_safe(self):
        """Validate that close() on a pre-launch session does not raise and leaves handles as None.

        The test calls close() on a fresh session and asserts _transport,
        _session, and _page_ws_url remain None because close must be a no-op
        when no browser has been launched, preventing AttributeError on
        teardown.
        """
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            session = EncreBrowserSession()
            await session.close()
            assert session._transport is None
            assert session._session is None
            assert session._page_ws_url is None

        import asyncio
        asyncio.run(_test())

    def test_verify_close_is_idempotent(self):
        """Validate that calling close() multiple times does not raise an exception.

        The test calls close() twice in succession and asserts no exception
        is raised because close must be idempotent — a second call should
        be a safe no-op after the first has already cleaned up handles.
        """
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            session = EncreBrowserSession()
            await session.close()
            await session.close()
            # Should not raise on the second call.

        import asyncio
        asyncio.run(_test())


# ===========================================================================
# EncreBrowserSession public API exports
# ===========================================================================

class TestBrowserPublicAPI:
    """Engineered to validate that the public API surface of encre.computer.browser is complete.

    This test class exercises 2 scenarios to ensure that BrowserState and
    EncreBrowserSession are exported from the package root, and that all
    expected async interaction methods are present on the session instance.
    The design requires a stable public contract so that callers can rely
    on method availability without introspecting implementation details.
    """

    def test_verify_public_exports_are_resolvable(self):
        """Validate that BrowserState and EncreBrowserSession are exported from encre.computer.

        The test imports both names from the package root and asserts neither
        is None because public API consumers depend on these symbols being
        discoverable through the documented import path.
        """
        from encre.computer import BrowserState, EncreBrowserSession
        assert EncreBrowserSession is not None
        assert BrowserState is not None

    def test_verify_all_expected_async_methods_exist_on_session(self):
        """Validate that every documented async method is present on the session instance.

        The test constructs a session and asserts hasattr for each of the 13
        expected methods (navigate, click, type_text, screenshot, get_html,
        get_text, execute_js, get_state, wait_for_selector, scroll_to,
        fill_form, press_key, save_cookies, load_cookies, close, is_idle)
        because the public API contract requires all methods to exist.
        """
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert hasattr(session, "navigate")
        assert hasattr(session, "click")
        assert hasattr(session, "type_text")
        assert hasattr(session, "screenshot")
        assert hasattr(session, "get_html")
        assert hasattr(session, "get_text")
        assert hasattr(session, "execute_js")
        assert hasattr(session, "get_state")
        assert hasattr(session, "wait_for_selector")
        assert hasattr(session, "scroll_to")
        assert hasattr(session, "fill_form")
        assert hasattr(session, "press_key")
        assert hasattr(session, "save_cookies")
        assert hasattr(session, "load_cookies")
        assert hasattr(session, "close")
        assert hasattr(session, "is_idle")
