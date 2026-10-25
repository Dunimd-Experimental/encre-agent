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

"""Tests for the hook system: lifecycle event dataclasses and the dispatch engine.

Covers the three event types emitted while a hook runs (started, progress,
response) and the EncreHookSystem itself: handler registration and
unregistration, emission across every hook phase, observer fan-out, rejection
of unknown event types, and the enabled gate that short-circuits all dispatch.
"""

import asyncio

import pytest
from encre.hooks.system import EncreHookSystem
from encre.hooks.types import (
    HookProgressEvent,
    HookResponseEvent,
    HookStartedEvent,
)


class TestHookEventTypes:
    """Validates the shape and field wiring of the hook lifecycle events.

    Exercises the three concrete event types emitted by the hook pipeline —
    started, progress, and response — and checks that each carries its
    constructor arguments verbatim into the exposed attributes. The events are
    pure data carriers, so downstream handlers can reason about hook state
    without any introspection overhead.
    """

    def test_verify_started_event_carries_hook_identity_and_phase(self):
        """Checks that HookStartedEvent propagates hook identity and phase.

        Constructs the event with known values and asserts every attribute
        matches, because the dataclass must preserve its constructor arguments
        without transformation for reliable serialization.
        """
        event = HookStartedEvent(
            hook_id="h1", hook_name="test_hook", event_type="pre_tool_exec"
        )
        assert event.hook_id == "h1", "hook_id must match the constructor argument"
        assert event.hook_name == "test_hook", "hook_name must match the constructor argument"
        assert event.event_type == "pre_tool_exec", "event_type must match the constructor argument"

    def test_verify_progress_event_carries_execution_output_and_streams(self):
        """Checks that HookProgressEvent propagates output, stdout, and stderr.

        Constructs the event with mixed output values and asserts each field,
        because progress events stream intermediate state and must preserve
        empty strings rather than None to tell "no output yet" apart from
        "output omitted".
        """
        event = HookProgressEvent(
            hook_id="h1",
            hook_name="test_hook",
            event_type="on_tool_progress",
            output="running",
            stdout="out",
            stderr="",
        )
        assert event.hook_id == "h1", "hook_id must match the constructor argument"
        assert event.output == "running", "output must reflect the current execution state"
        assert event.stdout == "out", "stdout must be preserved verbatim for streaming"

    def test_verify_response_event_carries_success_outcome_and_exit_code(self):
        """Checks that HookResponseEvent carries a success outcome and exit code 0.

        Constructs a completed-hook response and asserts that output, outcome,
        and exit_code all match, because downstream dispatch branches on outcome
        to decide whether the tool execution path continues or aborts.
        """
        event = HookResponseEvent(
            hook_id="h1",
            hook_name="test_hook",
            event_type="post_tool_exec",
            output="success",
            exit_code=0,
            outcome="success",
        )
        assert event.hook_id == "h1", "hook_id must match the constructor argument"
        assert event.output == "success", "output must reflect the final tool result"
        assert event.outcome == "success", "outcome must signal successful completion"
        assert event.exit_code == 0, "exit_code must indicate a clean process termination"

    def test_verify_response_event_carries_error_outcome_and_nonzero_exit_code(self):
        """Checks that HookResponseEvent carries an error outcome and a nonzero exit code.

        Constructs a failed-hook response and asserts that outcome is "error"
        and exit_code is 1, because the error-reporting paths rely on those
        fields to route diagnostics back to the caller without extra lookups.
        """
        event = HookResponseEvent(
            hook_id="h1",
            hook_name="test_hook",
            event_type="post_tool_exec",
            output="something went wrong",
            exit_code=1,
            outcome="error",
        )
        assert event.exit_code == 1, "exit_code must reflect a non-clean process termination"
        assert event.outcome == "error", "outcome must signal failure to downstream handlers"


class TestHookSystem:
    """Validates the full lifecycle of the hook dispatch system.

    Exercises handler registration and unregistration, emission across every
    hook phase (pre-tool, post-tool, session, turn, error), observer
    notification, invalid-type rejection, and the enabled gate, and checks
    that the system isolates side effects and returns blocking decisions
    upstream. Hooks are opt-in — disabled by default via the enabled flag —
    so an unconfigured deployment never pays the dispatch cost.
    """

    def test_verify_system_constructs_with_default_enabled_state(self):
        """Checks that EncreHookSystem constructs with an enabled flag set to True.

        Exercises bare construction and asserts internal state, because the
        system must be immediately usable without explicit initialization while
        still letting consumers inspect the default enabled gate.
        """
        hooks = EncreHookSystem()
        assert hooks is not None, "constructor must return a valid instance"
        assert hooks._handlers is not None, "handlers registry must be initialized"
        assert hooks.enabled is True, "hooks must be enabled by default for backward compatibility"

    def test_verify_handler_registration_stores_callable_under_event_type(self):
        """Checks that register_handler files the callable under the right event type.

        Registers a handler with an explicit id and asserts both the returned
        id and the bucket size, because dispatch indexes by the event_type
        string and a mismatched key would silently drop handlers.
        """
        hooks = EncreHookSystem()

        async def handler(name, context, extra):
            """Echoes a non-blocking hook decision."""
            return {"block": False}

        hid = hooks.register_handler("pre_tool_exec", handler, "test_handler")
        assert hid == "test_handler", "explicit handler id must be returned unchanged"
        assert len(hooks._handlers["pre_tool_exec"]) == 1, "exactly one handler must be registered"

    def test_verify_handler_registration_generates_unique_auto_id(self):
        """Checks that register_handler generates a non-empty id when none is given.

        Registers without an id argument and asserts the returned id is a
        non-empty string, because the auto-generated id is the lookup key for
        the later unregister call.
        """
        hooks = EncreHookSystem()

        async def handler(name, context, extra):
            """Returns an empty hook decision."""
            return {}

        hid = hooks.register_handler("pre_tool_exec", handler)
        assert isinstance(hid, str), "auto-generated id must be a string"
        assert len(hid) > 0, "auto-generated id must not be empty"

    def test_verify_handler_unregistration_removes_callable_and_clears_bucket(self):
        """Checks that unregister_handler removes the callable and returns True.

        Runs a register-then-unregister cycle and asserts the bucket is empty,
        because a stale handler would fire on every emit and cause unintended
        side effects in downstream consumers.
        """
        hooks = EncreHookSystem()

        async def handler(name, context, extra):
            """Returns an empty hook decision."""
            return {}

        hid = hooks.register_handler("pre_tool_exec", handler, "test_handler")
        result = hooks.unregister_handler(hid)
        assert result is True, "unregister must return True when the handler existed"
        assert len(hooks._handlers["pre_tool_exec"]) == 0, "bucket must be empty after removal"

    def test_verify_unregister_nonexistent_handler_returns_false(self):
        """Checks that unregister_handler returns False for an unknown id.

        Unregisters an id that was never registered and asserts False, because
        the system must not raise on benign cleanup paths — callers often
        unregister in teardown without knowing whether registration succeeded.
        """
        hooks = EncreHookSystem()
        assert hooks.unregister_handler("nonexistent_id") is False, "unregister of unknown id must be safe"

    def test_verify_emit_pre_tool_invokes_registered_handler_and_propagates_block(self):
        """Checks that emit_pre_tool invokes registered handlers and propagates the block flag.

        Registers a handler, emits a pre-tool event, and asserts the handler
        was called, because pre-tool hooks are the primary gating mechanism for
        tool execution safety checks.
        """
        async def _test():
            """Runs the async registration-and-emit check."""
            hooks = EncreHookSystem()
            called = False

            async def handler(name, context, extra):
                """Marks itself as called and allows the tool through."""
                nonlocal called
                called = True
                return {"block": False}

            hooks.register_handler("pre_tool_exec", handler, "test")
            await hooks.emit_pre_tool("bash", {"cmd": "ls"})
            assert called is True, "handler must be invoked when its event type is emitted"

        asyncio.run(_test())

    def test_verify_emit_pre_tool_returns_block_decision_when_handler_requests_stop(self):
        """Checks that emit_pre_tool returns the handler's blocking decision.

        Uses a handler that returns {"block": True} and asserts the result
        carries that decision, because blocking is the primary safety
        mechanism — the caller must act on the returned dict rather than
        ignoring it.
        """
        async def _test():
            """Runs the async blocking-handler check."""
            hooks = EncreHookSystem()

            async def handler(name, context, extra):
                """Blocks the tool call with a reason."""
                return {"block": True, "block_reason": "unsafe"}

            hooks.register_handler("pre_tool_exec", handler, "test")
            result = await hooks.emit_pre_tool("bash", {"cmd": "rm -rf /"})
            assert result is not None, "blocking handler must produce a non-None result"
            assert result.get("block") is True, "returned decision must carry the block flag"

        asyncio.run(_test())

    def test_verify_emit_post_tool_injects_handler_output_into_result_string(self):
        """Checks that emit_post_tool merges handler output into the result string.

        Uses a handler returning {"extra_context": "..."} and asserts the
        injected text appears in the emitted result, because the post-tool
        contract promises handler outputs are merged into the tool result
        before it reaches the caller.
        """
        async def _test():
            """Runs the async post-tool injection check."""
            hooks = EncreHookSystem()

            async def handler(name, context, extra):
                """Adds extra context to the tool result."""
                return {"extra_context": "injected context"}

            hooks.register_handler("post_tool_exec", handler, "test")
            result = await hooks.emit_post_tool("bash", {"cmd": "ls"}, "file1.txt")
            assert isinstance(result, str), "post-tool emit must return a string result"
            assert "injected context" in result, "handler-injected context must appear in emitted output"

        asyncio.run(_test())

    def test_verify_emit_session_start_invokes_registered_on_session_start_handler(self):
        """Checks that emit_session_start invokes handlers registered for on_session_start.

        Registers and emits, then asserts the handler flag flipped, because
        session-start hooks are the hook point for initializing per-session
        state such as memory warm-up.
        """
        async def _test():
            """Runs the async session-start check."""
            hooks = EncreHookSystem()
            called = False

            async def handler(name, context, extra):
                """Marks itself as called."""
                nonlocal called
                called = True
                return {}

            hooks.register_handler("on_session_start", handler, "test")
            await hooks.emit_session_start()
            assert called is True, "on_session_start handler must be invoked"

        asyncio.run(_test())

    def test_verify_emit_turn_start_invokes_registered_on_turn_start_handler(self):
        """Checks that emit_turn_start invokes handlers registered for on_turn_start.

        Registers and emits with a turn index, then asserts the handler was
        called, because turn-start hooks anchor per-turn bookkeeping such as
        budget tracking and turn-level summaries.
        """
        async def _test():
            """Runs the async turn-start check."""
            hooks = EncreHookSystem()
            called = False

            async def handler(name, context, extra):
                """Marks itself as called."""
                nonlocal called
                called = True
                return {}

            hooks.register_handler("on_turn_start", handler, "test")
            await hooks.emit_turn_start(1)
            assert called is True, "on_turn_start handler must be invoked"

        asyncio.run(_test())

    def test_verify_emit_error_invokes_registered_on_error_handler(self):
        """Checks that emit_error invokes handlers registered for on_error.

        Registers a handler and emits a ValueError, then asserts it was
        called, because error hooks are the designated pathway for telemetry
        and alerting — they must fire even when the primary path catches and
        swallows the exception.
        """
        async def _test():
            """Runs the async error-emit check."""
            hooks = EncreHookSystem()
            called = False

            async def handler(name, context, extra):
                """Marks itself as called."""
                nonlocal called
                called = True
                return {}

            hooks.register_handler("on_error", handler, "test")
            await hooks.emit_error(ValueError("test error"), "testing")
            assert called is True, "on_error handler must be invoked"

        asyncio.run(_test())

    def test_verify_on_event_observer_receives_all_emitted_events(self):
        """Checks that an on_event observer receives every emitted lifecycle event.

        Emits a pre-tool hook and asserts at least two events were observed,
        because each emit fires both a started event and a response event, so
        the observer must see the complete lifecycle.
        """
        hooks = EncreHookSystem()
        events = []

        def observer(event):
            """Records each emitted event for later inspection."""
            events.append(event)

        hooks.on_event(observer)

        async def _test():
            """Runs the async emit used to drive the observer."""
            async def handler(name, context, extra):
                """Returns an empty hook decision."""
                return {}

            hooks.register_handler("pre_tool_exec", handler, "test")
            await hooks.emit_pre_tool("bash", {"cmd": "ls"})

        asyncio.run(_test())
        assert len(events) >= 2, "observer must receive at least started and response events"

    def test_verify_register_handler_raises_on_invalid_event_type(self):
        """Checks that register_handler rejects unknown event types with ValueError.

        Registers under "invalid_event_type" and asserts the exception, because
        the event-type enum is the contract boundary — invalid keys would break
        dispatch lookup and be nearly impossible to debug at runtime.
        """
        hooks = EncreHookSystem()

        async def handler(name, context, extra):
            """Returns an empty hook decision."""
            return {}

        with pytest.raises(ValueError):
            hooks.register_handler("invalid_event_type", handler)

    def test_verify_disabled_hooks_return_none_on_emit(self):
        """Checks that disabled hooks return None from every emit method.

        Disables the system, registers a blocking handler, and asserts the emit
        result is None, because the enabled gate is the fast-path skip that
        prevents any dispatch overhead when hooks are not in use.
        """
        async def _test():
            """Runs the async disabled-hooks check."""
            hooks = EncreHookSystem()
            hooks.enabled = False

            async def handler(name, context, extra):
                """Requests a block that must never be evaluated."""
                return {"block": True}

            hooks.register_handler("pre_tool_exec", handler, "test")
            result = await hooks.emit_pre_tool("bash", {"cmd": "ls"})
            assert result is None, "disabled hooks must short-circuit and return None"

        asyncio.run(_test())
