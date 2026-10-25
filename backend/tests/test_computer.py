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

"""Tests for the encre.computer.browser module.

Covers the BrowserState dataclass defaults, EncreBrowserSession constructor
parameters and initial internal state, the state-only methods that are safe to
call before a browser is launched (get_state, is_idle, save_cookies, close),
and the public API surface exported by encre.computer.
"""

import time

# ===========================================================================
# BrowserState dataclass
# ===========================================================================

class TestBrowserState:
    """Tests for the BrowserState dataclass contract and its empty-string defaults."""

    def test_verify_default_creation_produces_empty_strings(self):
        """BrowserState() initializes every field to an empty string."""
        from encre.computer.browser import BrowserState
        state = BrowserState()
        assert state.url == ""
        assert state.title == ""
        assert state.html == ""
        assert state.text == ""

    def test_verify_construction_with_explicit_values(self):
        """BrowserState stores each provided field value without mutation."""
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
        """BrowserState is recognized as a dataclass by dataclasses.is_dataclass."""
        from dataclasses import is_dataclass
        from encre.computer.browser import BrowserState
        assert is_dataclass(BrowserState)

    def test_verify_all_fields_have_empty_string_defaults(self):
        """Every known field returns "" on a default-constructed BrowserState."""
        from encre.computer.browser import BrowserState
        state = BrowserState()
        for field_name in ["url", "title", "html", "text"]:
            assert getattr(state, field_name) == ""


# ===========================================================================
# EncreBrowserSession construction
# ===========================================================================

class TestEncreBrowserSessionConstruction:
    """Tests for EncreBrowserSession constructor defaults and initial internal state."""

    def test_verify_default_construction_applies_builtin_defaults(self):
        """EncreBrowserSession() applies the production-safe automation defaults."""
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session is not None
        assert session.headless is True
        assert session.viewport_width == 1280
        assert session.viewport_height == 800
        assert session.timeout == 30000

    def test_verify_custom_construction_stores_all_parameters(self):
        """EncreBrowserSession stores every custom constructor argument as given."""
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
        """All internal CDP handles start unconnected before any browser is launched."""
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session._transport is None
        assert session._session is None
        assert session._page_ws_url is None
        assert session._connected is False

    def test_verify_initial_browser_state_is_empty(self):
        """The internal BrowserState starts with every field empty."""
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session._state.url == ""
        assert session._state.title == ""
        assert session._state.html == ""
        assert session._state.text == ""

    def test_verify_last_used_timestamp_is_set_on_construction(self):
        """_last_used is stamped with the current time during construction."""
        from encre.computer.browser import EncreBrowserSession
        before = time.time()
        session = EncreBrowserSession()
        after = time.time()
        assert before <= session._last_used <= after


# ===========================================================================
# EncreBrowserSession state methods (no browser needed)
# ===========================================================================

class TestEncreBrowserSessionState:
    """Tests for state-only methods that work on a session that never launched a browser."""

    def test_verify_get_state_before_navigate_returns_browserstate(self):
        """get_state() returns a BrowserState snapshot before any navigation."""
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            """Assert that get_state() returns a BrowserState on a fresh session."""
            session = EncreBrowserSession()
            state = await session.get_state()
            assert isinstance(state, object)
            from encre.computer.browser import BrowserState
            assert isinstance(state, BrowserState)

        import asyncio
        asyncio.run(_test())

    def test_verify_fresh_session_is_not_idle_within_tolerance(self):
        """A just-created session is not idle under a generous idle threshold."""
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session.is_idle(max_idle_seconds=600) is False

    def test_verify_zero_threshold_immediately_idles_fresh_session(self):
        """is_idle(max_idle_seconds=0) treats a fresh session as idle immediately."""
        from encre.computer.browser import EncreBrowserSession
        session = EncreBrowserSession()
        assert session.is_idle(max_idle_seconds=0) is True

    def test_verify_save_cookies_before_launch_returns_empty_list(self):
        """save_cookies() returns an empty list before any browser context exists."""
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            """Assert that save_cookies() returns [] on a pre-launch session."""
            session = EncreBrowserSession()
            cookies = await session.save_cookies()
            assert cookies == []

        import asyncio
        asyncio.run(_test())

    def test_verify_close_before_launch_is_safe(self):
        """close() on a pre-launch session is a no-op that leaves handles as None."""
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            """Assert that close() does not raise and does not allocate handles."""
            session = EncreBrowserSession()
            await session.close()
            assert session._transport is None
            assert session._session is None
            assert session._page_ws_url is None

        import asyncio
        asyncio.run(_test())

    def test_verify_close_is_idempotent(self):
        """Calling close() twice does not raise an exception."""
        from encre.computer.browser import EncreBrowserSession

        async def _test():
            """Assert that a second close() call is a safe no-op."""
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
    """Tests that the public API surface of encre.computer.browser is complete."""

    def test_verify_public_exports_are_resolvable(self):
        """BrowserState and EncreBrowserSession are importable from encre.computer."""
        from encre.computer import BrowserState, EncreBrowserSession
        assert EncreBrowserSession is not None
        assert BrowserState is not None

    def test_verify_all_expected_async_methods_exist_on_session(self):
        """Every documented browser-interaction method exists on a session instance."""
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
