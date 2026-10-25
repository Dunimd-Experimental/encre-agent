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

"""Retry contract of the model phase: what happens after a backend failure.

The model phase owns backend-error recovery. Its ladder reserves exactly one
attempt for an *unknown* failure (``UNKNOWN_ERROR_RETRY_LIMIT``) and classifies
the rest (compact / fallback / network retry / server retry / auth). This
module pins behaviour that used to be documented but never implemented: the
reserved attempt has to be **spent on a real retry**, and a failure that
survives it has to be **surfaced**.

Covered invariants:

1. an unknown failure re-issues ``backend.chat()`` and the recovered answer is
   what the caller sees;
2. an unknown failure that survives the reserved attempt reaches the caller as
   a terminal ``error`` -- never as a silent ``stop`` with no output;
3. an unknown failure *after* part of a response was already streamed is
   surfaced instead of retried (a retry would superimpose a second copy on the
   same assistant message);
4. a transient (network-classified) failure still retries, and the aborted
   attempt's partial output is **discarded** -- it must not be concatenated
   into the assistant message, and half-emitted tool calls must never be
   materialised;
5. once the per-turn recovery budget is spent the error is withheld: the turn
   ends quietly rather than injecting an empty-response nudge on top of a dead
   backend.

Every test drives the real ``EncreLoop`` through ``EncreSDK``; the only
substituted object is the model backend, a genuine ``BaseBackend`` subclass
scripting deterministic failures and streams.
"""

from typing import Any

import pytest

from encre.loop_error import (
    MAX_TOTAL_RECOVERIES_PER_TURN,
    UNKNOWN_ERROR_RETRY_LIMIT,
    ErrorOrchestrator,
    RecoveryAction,
)
from encre.utils.types import BackendError, BackendFinish, BackendText, Finish

from backend.tests.test_sdk_headless import (
    _ScriptedBackend,
    _sdk,
    data_root,  # noqa: F401 -- re-exported fixture (module-scoped in the sibling)
)

# Messages chosen so the error classifier lands where the test needs it:
# "provider is down" matches no known pattern (=> unknown), while a connection
# error matches the network patterns.
_UNKNOWN_FAILURE = "provider is down"
_NETWORK_FAILURE = "connection reset by peer"


class _FailingBackend(_ScriptedBackend):
    """Fails the first ``failures`` attempts, then answers normally.

    ``partial`` is streamed before the failure so a test can prove what the
    loop does with an already-delivered fragment of a response.
    """

    def __init__(
        self,
        exc: BaseException,
        failures: int = 1,
        text: str = "recovered",
        partial: str = "",
    ) -> None:
        """Store the scripted exception, failure count, recovery text, and prefix."""
        super().__init__(text=text)
        self.exc = exc
        self.failures = failures
        self.partial = partial
        self.attempts: list[str] = []

    async def chat(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        """Raise for the first ``failures`` calls (optionally after a partial), then answer."""
        self.chat_calls += 1
        self.attempts.append(type(self.exc).__name__)
        if self.chat_calls <= self.failures:
            if self.partial:
                yield BackendText(text=self.partial)
            raise self.exc
        yield BackendText(text=self.text)
        yield BackendFinish(reason="stop", usage={"input_tokens": 3, "output_tokens": 4})


def _assistants(sdk) -> list[dict[str, Any]]:
    """Return the assistant messages the loop committed to the caller's session."""
    return [m for m in sdk.session.messages if m.get("role") == "assistant"]


# ----------------------------------------------------------------------
# The reserved attempt is spent on a real retry
# ----------------------------------------------------------------------


async def test_unknown_error_uses_the_reserved_retry_and_recovers():
    """An unknown failure is retried once and the recovered answer reaches the caller."""
    backend = _FailingBackend(RuntimeError(_UNKNOWN_FAILURE), failures=1)
    sdk, _ = _sdk(data_root, backend)

    result = await sdk.collect("hello")

    assert backend.chat_calls == 2, "the reserved attempt must be spent on a retry"
    assert result.finish_reason == "stop"
    assert result.text == "recovered"
    assert result.error is None
    assert [m["content"] for m in _assistants(sdk)] == ["recovered"]
    await sdk.aclose()


async def test_unknown_error_is_surfaced_after_the_retry_is_spent():
    """A failure that survives the reserved attempt surfaces as a terminal error."""
    backend = _FailingBackend(RuntimeError(_UNKNOWN_FAILURE), failures=99)
    sdk, _ = _sdk(data_root, backend)

    result = await sdk.collect("hello")

    assert backend.chat_calls == 1 + UNKNOWN_ERROR_RETRY_LIMIT
    assert result.finish_reason == "error"
    assert _UNKNOWN_FAILURE in (result.error or "")
    # The failure is persisted on the assistant message too, so reloading the
    # session still explains why the turn has no answer.
    assert _UNKNOWN_FAILURE in str(_assistants(sdk)[-1].get("errorMessage", ""))
    await sdk.aclose()


async def test_unknown_error_after_partial_output_is_surfaced_not_retried():
    """A half-delivered response is surfaced instead of being re-issued."""
    backend = _FailingBackend(
        RuntimeError(_UNKNOWN_FAILURE), failures=99, partial="half an answer"
    )
    sdk, _ = _sdk(data_root, backend)

    result = await sdk.collect("hello")

    assert backend.chat_calls == 1, "re-issuing would duplicate the streamed half"
    assert result.finish_reason == "error"
    assert _UNKNOWN_FAILURE in (result.error or "")
    await sdk.aclose()


# ----------------------------------------------------------------------
# Transient failures retry with a clean attempt state
# ----------------------------------------------------------------------


async def test_network_retry_discards_the_aborted_attempts_output():
    """The retry's answer is not prefixed with the aborted attempt's fragment."""
    backend = _FailingBackend(
        ConnectionError(_NETWORK_FAILURE),
        failures=1,
        text="clean answer",
        partial="garbage prefix",
    )
    sdk, _ = _sdk(data_root, backend)

    result = await sdk.collect("hello")

    assert backend.chat_calls == 2
    assert result.finish_reason == "stop"
    # The aborted attempt's text was streamed before the failure, so the
    # transport saw it; what must not happen is the *session* carrying it,
    # merged into the answer.
    assert result.text.endswith("clean answer")
    assert [m["content"] for m in _assistants(sdk)] == ["clean answer"]
    await sdk.aclose()


async def test_unknown_error_after_a_partial_tool_call_is_surfaced():
    """Half-emitted tool calls are never materialised or re-issued."""
    from encre.utils.types import BackendToolCallDelta

    class _TruncatedToolCallBackend(_FailingBackend):
        """Fails mid tool-call delta, then answers normally on the next call."""

        async def chat(self, *args: Any, **kwargs: Any):
            """Stream two truncated tool-call deltas before raising."""
            self.chat_calls += 1
            if self.chat_calls == 1:
                yield BackendToolCallDelta(index=0, key="name", value="sdk_probe")
                yield BackendToolCallDelta(index=0, key="arguments", value='{"n":')
                raise RuntimeError(_UNKNOWN_FAILURE)
            yield BackendText(text=self.text)
            yield BackendFinish(reason="stop", usage={"input_tokens": 3, "output_tokens": 4})

    backend = _TruncatedToolCallBackend(RuntimeError(_UNKNOWN_FAILURE), text="late")
    sdk, _ = _sdk(data_root, backend)

    result = await sdk.collect("hello")

    assert backend.chat_calls == 1, "a truncated tool call must not be re-issued"
    assert result.finish_reason == "error"
    assert result.tool_calls == [], "the half tool call must never be executed"
    await sdk.aclose()


# ----------------------------------------------------------------------
# Spending the turn's recovery budget withholds the error
# ----------------------------------------------------------------------


async def test_the_turn_ends_quietly_once_the_recovery_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
):
    """A spent recovery budget withholds the error and ends the turn quietly."""
    monkeypatch.setattr("encre.loop_error.MAX_TOTAL_RECOVERIES_PER_TURN", 1)
    backend = _FailingBackend(RuntimeError(_UNKNOWN_FAILURE), failures=99)
    sdk, _ = _sdk(data_root, backend)

    events = [event async for event in sdk.run("hello")]

    assert backend.chat_calls == 2, "one attempt, then the budget is spent"
    assert [e.reason for e in events if isinstance(e, Finish)] == ["stop"]
    assert [e for e in events if isinstance(e, BackendError)] == []
    # Withheld means the turn really ends: no assistant message is fabricated
    # and no empty-response nudge is queued on top of a dead backend.
    assert _assistants(sdk) == []
    await sdk.aclose()


# ----------------------------------------------------------------------
# The orchestrator ladder itself
# ----------------------------------------------------------------------


def test_orchestrator_reserves_one_unknown_retry_then_releases():
    """The orchestrator retries an unknown failure once, then releases it."""
    orchestrator = ErrorOrchestrator()

    first = orchestrator.handle_backend_exception(
        RuntimeError(_UNKNOWN_FAILURE), error_code="unknown", error_category="unknown"
    )
    assert first.action is RecoveryAction.CONTINUE
    assert first.detail == f"unknown retry 1/{UNKNOWN_ERROR_RETRY_LIMIT}"

    second = orchestrator.handle_backend_exception(
        RuntimeError(_UNKNOWN_FAILURE), error_code="unknown", error_category="unknown"
    )
    assert second.action is RecoveryAction.RELEASE


def test_orchestrator_withholds_once_the_turn_budget_is_spent():
    """The budget check runs first and yields the withhold action."""
    orchestrator = ErrorOrchestrator()
    orchestrator._recoveries_this_turn = MAX_TOTAL_RECOVERIES_PER_TURN

    decision = orchestrator.handle_backend_exception(
        RuntimeError(_UNKNOWN_FAILURE), error_code="unknown", error_category="unknown"
    )

    assert decision.action is RecoveryAction.CONSUMED
    assert decision.detail == "recovery budget exhausted"


def test_orchestrator_keeps_auth_errors_terminal():
    """An auth failure is released immediately -- never retried, never withheld."""
    orchestrator = ErrorOrchestrator()

    decision = orchestrator.handle_backend_exception(
        RuntimeError("invalid api key"),
        error_code="auth_invalid_key",
        error_category="auth",
    )

    assert decision.action is RecoveryAction.RELEASE
    assert decision.detail == "auth error"


async def test_a_clean_turn_is_committed_once_and_not_marked_interrupted():
    """No retry happened, so nothing may be written twice or flagged as cut off.

    Regression: the streamed tail stayed in ``_pending_stream_text`` after the
    turn committed it, and the cancel-finalisation pass -- which also runs on
    turns that finish normally -- appended that second copy onto the message it
    had just written and stamped ``interrupted: true`` on a healthy turn.
    """
    sdk, _ = _sdk(data_root, _ScriptedBackend(text="a clean answer"))

    result = await sdk.collect("hello")

    assistants = _assistants(sdk)
    assert result.text == "a clean answer"
    assert [m["content"] for m in assistants] == ["a clean answer"]
    assert not any(m.get("interrupted") for m in assistants)
    await sdk.aclose()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
