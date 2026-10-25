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

"""P1.5 -- the embedding SDK and the headless JSONL transport.

These tests exercise the **real** runtime: a real
:class:`~encre.session.EncreSession`, a real
:class:`~encre.tools.registry.ToolRegistry`, real collaborator injection, the
real :class:`~encre.loop.runner.EncreLoop` and the real
:func:`~encre.events.project_event` projection.  The only substituted object
is the *model backend*, which is a genuine
:class:`~encre.backends.base.BaseBackend` subclass scripting deterministic
streams (the same technique ``test_long_turn_bench`` uses) -- never a mock
standing in for production code.

Covered invariants:

1. ``EncreSDK`` builds the loop around the session and registry it was given,
   registers the harness builtins, and publishes itself as the parent loop;
2. collaborators the caller injects are the *same objects* the loop uses --
   nothing is rebuilt behind the caller's back;
3. ``EncreSDK.collect`` drains a turn into an ``SDKRunResult`` and
   ``EncreSDK.run`` streams tool calls;
4. ``project_event`` is a total projection over the agent event vocabulary,
   truncates oversized tool results, and ignores unknown events;
5. ``session_snapshot_need`` asks for the renderer history only where the
   frame actually uses it;
6. the WebSocket bridge and the headless transport emit **byte-identical**
   frames for the same event -- the anti-drift guarantee;
7. the headless protocol serves ``run``, rejects a concurrent ``run``,
   reports malformed input as ``error`` frames, and accepts permission /
   plan / engine / cancel / close requests.

The data root is redirected to ``tmp_path`` via ``ENCRE_DATA_DIR`` so the real
user data directory is never touched.
"""

import asyncio
import io
import json
import os
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from encre.backends.base import BaseBackend
from encre.config import EncreConfig
from encre.events import EventSourcedBus
from encre.events.wire import (
    SessionSnapshot,
    project_event,
    session_snapshot_need,
)
from encre.headless import (
    ERR_BAD_JSON,
    ERR_BAD_REQUEST,
    ERR_BUSY,
    ERR_UNKNOWN_TYPE,
    PROTOCOL_VERSION,
    HeadlessSession,
)
from encre.loop import EncreLoop
from encre.memdir.layers import MemoryLayerStore
from encre.sdk import EncreSDK, SDKRunResult
from encre.session import EncreSession
from encre.skills.registry import EncreSkillRegistry
from encre.telemetry import EncreTelemetry
from encre.tools.base import build_tool
from encre.tools.registry import ToolRegistry
from encre.utils.types import (
    Artifact,
    AssistantBoundary,
    BackendError,
    BackendFinish,
    BackendText,
    BackendToolCall,
    CompactFailed,
    CompactNotification,
    CompactStarting,
    EngineInstallProgress,
    EngineInstallRequest,
    Finish,
    PermissionRequest,
    PlanUpdate,
    QuestionRequest,
    Reference,
    SystemMessage,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolProgress,
    ToolResult,
    WorkflowCompletedEvent,
    WorkflowStartedEvent,
    WorkflowTaskEvent,
)

_TOOL_NAME = "sdk_probe"
_FINAL_TEXT = "done probing"


async def _probe(**kwargs: Any) -> str:
    """Deterministic tool body for the SDK tests."""
    return f"probed {kwargs.get('n', 0)}"


class _ScriptedBackend(BaseBackend):
    """A real backend that scripts a deterministic event stream per request.

    ``tool_rounds`` requests emit one tool call each; the request after them
    emits the final text.  Every request also loses a small amount of
    deliberately-retained state so the tests can prove the loop, not the
    backend, owns the session.
    """

    def __init__(self, tool_rounds: int = 0, text: str = _FINAL_TEXT) -> None:
        """Store the script parameters and zero the call and close counters."""
        self.tool_rounds = tool_rounds
        self.text = text
        self.chat_calls = 0
        self.closed = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
        enable_caching: bool = False,
        cache_edits_state: Any = None,
    ):
        """Yield one scripted response: a tool call during warmup, final text after."""
        self.chat_calls += 1
        if self.chat_calls <= self.tool_rounds:
            yield BackendToolCall(
                id=f"call_{self.chat_calls}",
                name=_TOOL_NAME,
                arguments=json.dumps({"n": self.chat_calls}),
            )
            yield BackendFinish(reason="tool_calls", usage={"input_tokens": 10, "output_tokens": 5})
        else:
            yield BackendText(text=self.text)
            yield BackendFinish(reason="stop", usage={"input_tokens": 20, "output_tokens": 9})

    def supports_tool_calling(self) -> bool:
        """Report that the scripted stream can emit tool calls."""
        return True

    def context_window_size(self) -> int:
        """Report a very large context window.

        A huge window keeps compaction inert so every scripted response is
        consumed by the real turn pipeline.
        """
        return 1_000_000

    def supports_prompt_caching(self) -> bool:
        """Report that prompt caching is unavailable for this backend."""
        return False

    async def aclose(self) -> None:
        """Record one close so callers can prove release happened."""
        self.closed += 1


def _config(**overrides: Any) -> EncreConfig:
    """A minimal real configuration for an isolated run."""
    base: dict[str, Any] = {
        "model": "sdk-model",
        "backend_type": "openai",
        "permission_mode": "bypass",
        "max_turns": 0,
        "max_tokens": 512,
        "log_level": "ERROR",
        "enable_prompt_caching": False,
        "enable_streaming_tool_execution": False,
        "checkpoint_max_count": 1,
    }
    base.update(overrides)
    return EncreConfig(**base)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Isolate the encrypted data root to ``tmp_path``."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _sdk(
    data_root,
    backend: _ScriptedBackend,
    tool_registry: ToolRegistry | None = None,
    **kwargs: Any,
) -> tuple[EncreSDK, _ScriptedBackend]:
    """Build a real SDK whose backend is the scripted stream."""
    registry = tool_registry if tool_registry is not None else ToolRegistry()
    registry.register(
        build_tool(
            name=_TOOL_NAME,
            description="SDK probe tool that echoes back an integer.",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": [],
            },
            execute=_probe,
        )
    )
    with patch("encre.loop.create_backend", return_value=backend):
        sdk = EncreSDK(EncreSession(_config()), registry, **kwargs)
    return sdk, backend


# ----------------------------------------------------------------------
# EncreSDK -- assembly
# ----------------------------------------------------------------------


def test_sdk_wraps_the_session_and_registry_it_was_given(data_root):
    """The SDK drives the caller's session and registry, and wires builtins."""
    backend = _ScriptedBackend()
    registry = ToolRegistry()
    sdk, _ = _sdk(data_root, backend, registry)

    assert isinstance(sdk.loop, EncreLoop)
    assert sdk.loop.session is sdk.session
    assert sdk.loop.tool_registry is sdk.tool_registry
    # ``from_config`` is the other supported construction path; both must land
    # on a loop whose config is the session's own.
    assert sdk.config is sdk.session.config
    assert sdk.loop.config is sdk.session.config
    assert isinstance(sdk.event_bus, EventSourcedBus)
    # The harness builtins are registered on top of the caller's tools.
    # ``web_fetch`` is not one of them: it ships as the ea-tool-web-fetch
    # plugin, so it only reaches the model through the plugin system.
    assert _TOOL_NAME in sdk.tool_registry.list_tools()
    for builtin in ("file_read", "file_write", "file_edit", "bash",
                    "web_search"):
        assert builtin in sdk.tool_registry.list_tools()
    assert "web_fetch" not in sdk.tool_registry.list_tools()


def test_sdk_publishes_itself_as_the_parent_loop(data_root):
    """Delegating tools resolve the SDK's own loop as their parent."""
    from encre.tools.runtime import _resolve_loop

    sdk, _ = _sdk(data_root, _ScriptedBackend())
    assert _resolve_loop() is sdk.loop


def test_sdk_uses_injected_collaborators_verbatim(data_root):
    """Injected collaborators are the same objects the loop uses."""
    skills = EncreSkillRegistry()
    telemetry = EncreTelemetry(enabled=False)
    registry = ToolRegistry()
    backend = _ScriptedBackend()
    with patch("encre.loop.create_backend", return_value=backend):
        sdk = EncreSDK(
            EncreSession(_config()),
            registry,
            skill_registry=skills,
            telemetry=telemetry,
        )

    assert sdk.loop.skill_registry is skills
    assert sdk.loop.telemetry is telemetry
    assert sdk.tool_registry is registry
    # The layered memory store has no injection point -- the wiring owns it --
    # so the SDK must not fake one: it is real, isolated and bound to the
    # backbone, and reachable from the session too.
    assert isinstance(sdk.loop.memory_layers, MemoryLayerStore)
    assert sdk.loop.memory_layers.root == data_root / "memory"
    assert sdk.session.memory_layers is sdk.loop.memory_layers


def test_sdk_from_config_builds_its_own_session(data_root):
    """``from_config`` creates a session around the supplied config."""
    backend = _ScriptedBackend()
    with patch("encre.loop.create_backend", return_value=backend):
        sdk = EncreSDK.from_config(_config(), ToolRegistry())

    assert isinstance(sdk.session, EncreSession)
    assert sdk.session.config.model == "sdk-model"
    assert sdk.session.messages == []


def test_sdk_rejects_an_unsupported_tools_argument(data_root):
    """A non-registry, non-iterable ``tools`` is a programming error."""
    with pytest.raises(TypeError, match="ToolRegistry"):
        EncreSDK(EncreSession(_config()), 42)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# EncreSDK -- running
# ----------------------------------------------------------------------


async def test_sdk_collect_returns_the_final_answer(data_root):
    """``collect`` drains a text-only turn into an ``SDKRunResult``."""
    sdk, backend = _sdk(data_root, _ScriptedBackend(text="the answer is 42"))

    result = await sdk.collect("what is the answer?")

    assert isinstance(result, SDKRunResult)
    assert result.text == "the answer is 42"
    assert result.finish_reason == "stop"
    assert result.usage == {"input_tokens": 20, "output_tokens": 9}
    assert result.tool_calls == []
    assert result.error is None
    assert backend.chat_calls == 1


async def test_sdk_run_streams_the_events_of_every_turn(data_root):
    """``run`` streams tool calls and the closing text of a multi-turn run."""
    sdk, backend = _sdk(data_root, _ScriptedBackend(tool_rounds=2))

    starts: list[str] = []
    texts: list[str] = []
    finishes: list[str] = []
    async for event in sdk.run("probe twice"):
        if isinstance(event, ToolCallStart):
            starts.append(event.name)
        elif isinstance(event, TextDelta):
            texts.append(event.text)
        elif isinstance(event, Finish):
            finishes.append(str(event.reason))

    assert starts == [_TOOL_NAME, _TOOL_NAME]
    assert "".join(texts) == _FINAL_TEXT
    assert finishes == ["stop"]
    assert backend.chat_calls == 3

    # The transcript is the loop's, persisted on the caller's session.
    roles = [m.get("role") for m in sdk.session.messages]
    assert roles.count("tool") == 2
    assert roles.count("user") == 1


async def test_sdk_aclose_releases_the_backend_once_per_call(data_root):
    """``aclose`` closes the backend and is safe to call again."""
    sdk, backend = _sdk(data_root, _ScriptedBackend())
    await sdk.aclose()
    await sdk.aclose()
    assert backend.closed == 2


def test_sdk_cancel_reaches_the_loop(data_root):
    """``cancel`` delegates to the loop's cancel path."""
    sdk, _ = _sdk(data_root, _ScriptedBackend())
    assert sdk.loop._cancel_event.is_set() is False
    sdk.cancel()
    assert sdk.loop._cancel_event.is_set() is True


# ----------------------------------------------------------------------
# project_event -- the single source of truth for the wire vocabulary
# ----------------------------------------------------------------------


def _frame(event: Any, **kwargs: Any) -> dict[str, Any]:
    """Project an event and return its flat message, asserting it projected."""
    frame = project_event(event, **kwargs)
    assert frame is not None, f"{type(event).__name__} projected to nothing"
    return frame.to_message()


def test_project_event_covers_the_whole_vocabulary():
    """Every event type the transports know maps to its documented frame."""
    cases: list[tuple[Any, str]] = [
        (TextDelta(text="hi"), "text_delta"),
        (ThinkingDelta(text="hmm"), "thinking_delta"),
        (ToolCallStart(name="t", id="1"), "tool_call_start"),
        (ToolCallDelta(id="1", key="k", value="v"), "tool_call_delta"),
        (ToolCallEnd(id="1"), "tool_call_end"),
        (ToolProgress(id="1", tool_name="t", status="running"), "tool_progress"),
        (ToolResult(id="1", content="ok", is_error=False), "tool_result"),
        (PermissionRequest(tool_name="t", reason="needs it"), "permission_request"),
        (QuestionRequest(tool_call_id="1", questions=[{"question": "q"}]), "question_request"),
        (Artifact(artifact={"path": "a"}), "artifacts_update"),
        (Reference(reference={"url": "u"}), "references_update"),
        (BackendError(error="boom"), "error"),
        (PlanUpdate(plan_items=[{"step": 1}]), "plan_update"),
        (AssistantBoundary(), "assistant_boundary"),
        (CompactStarting(), "compact_starting"),
        (CompactFailed(reason="noop"), "compact_failed"),
        (CompactNotification(old_count=4, new_count=1, old_tokens=90, new_tokens=10), "compact"),
        (SystemMessage(content="note", kind="warning"), "system_message"),
        (WorkflowStartedEvent(workflow_id="w", goal="g", total_tasks=2), "workflow_started"),
        (WorkflowTaskEvent(workflow_id="w", task_id="t", task_name="n", status="started"),
         "workflow_task"),
        (WorkflowCompletedEvent(workflow_id="w", goal="g", success=True), "workflow_completed"),
        (EngineInstallRequest(request_id="r", engine="chromium-cdp", title="T", body="B"),
         "engine_install_request"),
        (EngineInstallProgress(request_id="r", pct=10.0, message="m"), "engine_install_progress"),
        (Finish(reason="stop"), "finish"),
    ]
    for event, expected in cases:
        assert _frame(event, session_id="s")["type"] == expected, type(event).__name__


def test_project_event_stamps_the_session_id_even_when_absent():
    """``session_id`` is always present, preserving ``None`` as ``None``."""
    assert _frame(TextDelta(text="x"))["session_id"] is None
    assert _frame(TextDelta(text="x"), session_id="s")["session_id"] == "s"


def test_project_event_drops_empty_deltas_and_unknown_events():
    """Empty deltas and unknown types have no frame; transports stay alive."""
    assert project_event(TextDelta(text=""), session_id="s") is None
    assert project_event(ThinkingDelta(text=""), session_id="s") is None
    assert project_event(object(), session_id="s") is None
    assert project_event("not an event", session_id="s") is None


def test_project_event_truncates_an_oversized_tool_result():
    """A multi-megabyte tool result is bounded on the wire."""
    from encre.events.wire import TOOL_RESULT_TRUNCATION_SUFFIX, TOOL_RESULT_WIRE_LIMIT

    huge = "x" * (TOOL_RESULT_WIRE_LIMIT + 5_000)
    payload = _frame(ToolResult(id="1", content=huge, is_error=False), session_id="s")
    assert payload["content"].endswith(TOOL_RESULT_TRUNCATION_SUFFIX)
    assert len(payload["content"]) == TOOL_RESULT_WIRE_LIMIT + len(
        TOOL_RESULT_TRUNCATION_SUFFIX
    )

    # A result exactly at the limit is passed through untouched.
    exact = "y" * TOOL_RESULT_WIRE_LIMIT
    assert _frame(ToolResult(id="1", content=exact, is_error=False))["content"] == exact


def test_project_event_routes_the_structured_system_messages():
    """Spec / plan-review payloads never leak as visible system bubbles."""
    spec = _frame(SystemMessage(content="__spec_data__:{\"title\": \"T\"}"))
    assert spec["type"] == "spec_update"
    assert spec["spec"] == {"title": "T"}

    review = _frame(SystemMessage(content="__plan_review__:{\"ok\": true}"))
    assert review["type"] == "plan_review"
    assert review["review"] == {"ok": True}

    # Malformed JSON sends nothing rather than dumping the raw payload.
    assert project_event(SystemMessage(content="__spec_data__:{nope")) is None


def test_project_event_carries_the_snapshot_only_into_snapshot_frames():
    """Session state appears on ``compact``/``finish`` and nowhere else."""
    snapshot = SessionSnapshot(
        renderer_messages=[{"id": "m1", "role": "user"}],
        tail_message={"id": "m2", "role": "assistant"},
        plan_items=[{"step": 1}],
        artifacts=[{"path": "a"}],
        references=[{"url": "u"}],
    )

    compact = _frame(
        CompactNotification(old_count=2, new_count=1, old_tokens=9, new_tokens=2),
        session_id="s", snapshot=snapshot,
    )
    assert compact["messages"] == snapshot.renderer_messages
    assert compact["plan_items"] == snapshot.plan_items

    finish = _frame(
        Finish(reason="stop", compacted=True), session_id="s", snapshot=snapshot,
    )
    assert finish["assistant_message_id"] == "m2"
    assert finish["messages"] == snapshot.renderer_messages

    # Without a compaction the conversation is not resent -- clients already
    # hold it, and rendering it every turn would be O(n) per event.
    unchanged = _frame(Finish(reason="stop", compacted=False), session_id="s")
    assert unchanged["messages"] is None
    assert unchanged["assistant_message_id"] is None


def test_session_snapshot_need_is_exact():
    """Only snapshot frames ask for a snapshot, and only ``compact`` /
    compacted ``finish`` ask for the renderer history."""
    compacted = session_snapshot_need(Finish(reason="stop", compacted=True))
    assert (compacted.required, compacted.renderer_history) == (True, True)

    plain = session_snapshot_need(Finish(reason="stop", compacted=False))
    assert (plain.required, plain.renderer_history) == (True, False)

    compact = session_snapshot_need(
        CompactNotification(old_count=1, new_count=1, old_tokens=1, new_tokens=1),
    )
    assert (compact.required, compact.renderer_history) == (True, True)

    for event in (CompactStarting(), CompactFailed(reason="noop"),
                  TextDelta(text="x"), ToolResult(id="1", content="c", is_error=False),
                  PlanUpdate(plan_items=[]), PermissionRequest(tool_name="t", reason="r")):
        need = session_snapshot_need(event)
        assert (need.required, need.renderer_history) == (False, False), type(event).__name__


# ----------------------------------------------------------------------
# Anti-drift: both transports emit the same frame
# ----------------------------------------------------------------------


class _RecordingManager:
    """The slice of ``SessionManager`` the dispatch mixin calls."""

    def __init__(self) -> None:
        """Start with empty recordings of state writes and scheduled saves."""
        self.states: list[tuple[str | None, Any]] = []
        self.saved: list[Any] = []

    def set_session_state(self, sid: str | None, state: Any) -> None:
        """Record one state write keyed by session id for later inspection."""
        self.states.append((sid, state))

    def _schedule_save(self, info: Any) -> None:
        """Record one deferred save request for later inspection."""
        self.saved.append(info)


class _Info:
    """The slice of a session-info object the dispatch mixin reads."""

    def __init__(self, sid: str, session: EncreSession, loop: EncreLoop) -> None:
        """Attach the given session and loop to a mock agent under ``sid``."""
        self.session_id = sid
        self.agent = MagicMock()
        self.agent.session = session
        self.agent.loop = loop


def _ws_handler() -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """A bare handler whose ``_send`` records frames instead of writing."""
    from encre.transport.ws import EncreWSHandler

    handler = EncreWSHandler.__new__(EncreWSHandler)
    sent: list[tuple[str, dict[str, Any]]] = []

    async def _send(ws, msg_type, **kwargs):
        """Append the frame type and its fields to the recorded output."""
        sent.append((msg_type, kwargs))

    handler._send = _send  # type: ignore[assignment]
    handler._manager = _RecordingManager()  # type: ignore[assignment]
    return handler, sent


async def test_both_transports_emit_identical_frames(data_root):
    """The WebSocket bridge and the headless transport cannot drift apart.

    Every event is projected once by each transport, against the same live
    session, and the resulting frames must be byte-identical.  This is what
    makes :mod:`encre.events.wire` the single source of truth rather than a
    duplicated mapping waiting to fall out of step.
    """
    backend = _ScriptedBackend()
    with patch("encre.loop.create_backend", return_value=backend):
        sdk = EncreSDK(EncreSession(_config()), ToolRegistry())
    # A real transcript so the snapshot-bearing frames have something to carry.
    sdk.session.add_message("user", "hello")
    sdk.session.add_message("assistant", "hi there")

    handler, sent = _ws_handler()
    info = _Info(sdk.session.id, sdk.session, sdk.loop)
    headless_out = io.StringIO()
    headless = HeadlessSession.__new__(HeadlessSession)
    headless._stdout = headless_out
    headless.sdk = sdk

    events: list[Any] = [
        TextDelta(text="chunk"),
        ThinkingDelta(text="reasoning"),
        ToolCallStart(name=_TOOL_NAME, id="c1"),
        ToolCallDelta(id="c1", key="n", value="1"),
        ToolCallEnd(id="c1"),
        ToolProgress(id="c1", tool_name=_TOOL_NAME, status="running"),
        ToolResult(id="c1", content="probed 1", is_error=False),
        PermissionRequest(tool_name=_TOOL_NAME, reason="gate"),
        QuestionRequest(tool_call_id="c1", questions=[{"question": "q?"}]),
        Artifact(artifact={"path": "a.txt"}),
        Reference(reference={"url": "https://example.invalid"}),
        BackendError(error="boom", code="e1", category="network", retryable=True),
        PlanUpdate(plan_items=[{"step": 1}]),
        AssistantBoundary(),
        CompactStarting(),
        CompactFailed(reason="noop"),
        CompactNotification(old_count=3, new_count=1, old_tokens=50, new_tokens=10),
        SystemMessage(content="plain note", kind="info"),
        SystemMessage(content="__spec_data__:{\"title\": \"T\"}"),
        SystemMessage(content="__plan_review__:{\"ok\": true}"),
        WorkflowStartedEvent(workflow_id="w", goal="g", total_tasks=1, task_ids=["t"]),
        WorkflowTaskEvent(workflow_id="w", task_id="t", task_name="n", status="running"),
        WorkflowCompletedEvent(workflow_id="w", goal="g", success=True),
        EngineInstallRequest(request_id="r1", engine="chromium-cdp", title="T", body="B"),
        EngineInstallProgress(request_id="r1", pct=42.0, message="m"),
        Finish(reason="stop", usage={"input_tokens": 1, "output_tokens": 2}),
        Finish(reason="stop", compacted=True),
    ]

    for event in events:
        sent.clear()
        headless_out.seek(0)
        headless_out.truncate(0)
        await handler._dispatch_event(ws=object(), _info=info, event=event)
        headless._emit(event)

        # Frames that mirror server state (``agent_state``, ``context_usage``)
        # are appended after the projected frame, so only the first sent frame
        # is the projection under comparison.
        assert sent, f"{type(event).__name__}: nothing was sent"
        ws_name, ws_payload = sent[0]
        line = headless_out.getvalue().strip()
        assert line, f"{type(event).__name__}: headless emitted nothing"
        headless_frame = json.loads(line)

        assert headless_frame.get("type") == ws_name, type(event).__name__
        assert headless_frame == {"type": ws_name, **ws_payload}, type(event).__name__


# ----------------------------------------------------------------------
# Headless JSONL protocol
# ----------------------------------------------------------------------


def _headless(data_root, backend: _ScriptedBackend, **kwargs: Any):
    """Build a headless session writing into a captured buffer."""
    out = io.StringIO()
    with patch("encre.loop.create_backend", return_value=backend):
        session = HeadlessSession(
            _config(), stdin=io.StringIO(""), stdout=out,
            register_builtins=False, **kwargs,
        )
    return session, out


def _lines(out: io.StringIO) -> list[dict[str, Any]]:
    """Parse the buffered response frames."""
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


async def test_headless_ready_then_run_then_finish(data_root):
    """A ``run`` streams its frames and terminates with ``finish``."""
    session, out = _headless(data_root, _ScriptedBackend(text="headless answer"))
    session._loop = asyncio.get_running_loop()
    session._emit_ready()

    assert await session.handle_line(json.dumps({"type": "run", "prompt": "hi"})) is False
    await session.wait_turn()

    frames = _lines(out)
    assert frames[0]["type"] == "ready"
    assert frames[0]["protocol"] == PROTOCOL_VERSION
    assert frames[0]["session_id"] == session.session_id
    assert "model" in frames[0] and "tools" in frames[0]

    assert frames[1]["type"] == "text_delta"
    assert frames[1]["text"] == "headless answer"
    assert frames[2]["type"] == "finish"
    assert frames[2]["reason"] == "stop"
    assert frames[2]["session_id"] == session.session_id
    await session._shutdown()


async def test_headless_rejects_a_concurrent_run(data_root):
    """One conversation means one turn at a time."""
    session, out = _headless(data_root, _ScriptedBackend())
    session._loop = asyncio.get_running_loop()

    assert await session.handle_line(json.dumps({"type": "run", "prompt": "one"})) is False
    # The first turn is still in flight, so the second is refused outright.
    await session.handle_line(json.dumps({"type": "run", "prompt": "two"}))
    busy = [f for f in _lines(out) if f.get("code") == ERR_BUSY]
    assert len(busy) == 1

    await session.wait_turn()
    # Once it finishes the same session accepts the next turn.
    await session.handle_line(json.dumps({"type": "run", "prompt": "three"}))
    await session.wait_turn()
    reasons = [f["reason"] for f in _lines(out) if f["type"] == "finish"]
    assert reasons == ["stop", "stop"]
    await session._shutdown()


async def test_headless_reports_malformed_requests_as_error_frames(data_root):
    """Malformed input is reported, never fatal."""
    session, out = _headless(data_root, _ScriptedBackend())
    session._loop = asyncio.get_running_loop()

    await session.handle_line("{not json")
    await session.handle_line("[1, 2, 3]")
    await session.handle_line(json.dumps({"prompt": "no type"}))
    await session.handle_line(json.dumps({"type": "mystery"}))
    await session.handle_line(json.dumps({"type": "run", "prompt": ""}))
    await session.handle_line(json.dumps({"type": "run"}))
    await session.handle_line(json.dumps({"type": "permission", "decision": "yes"}))
    await session.handle_line(json.dumps({"type": "question"}))
    await session.handle_line(json.dumps({"type": "plan", "action": "maybe"}))
    await session.handle_line(json.dumps({"type": "engine_install", "request_id": 1}))

    codes = [f["code"] for f in _lines(out) if f["type"] == "error"]
    assert codes == [
        ERR_BAD_JSON,
        ERR_BAD_REQUEST,
        ERR_BAD_REQUEST,
        ERR_UNKNOWN_TYPE,
        ERR_BAD_REQUEST,
        ERR_BAD_REQUEST,
        ERR_BAD_REQUEST,
        ERR_BAD_REQUEST,
        ERR_BAD_REQUEST,
        ERR_BAD_REQUEST,
    ]
    # Nothing ran, so the transcript stayed empty and the session is reusable.
    assert session.sdk.session.messages == []
    await session._shutdown()


async def test_headless_resolves_permission_question_plan_and_cancel(data_root):
    """Every interactive request is acknowledged and reaches the real loop."""
    from unittest.mock import patch as _patch

    session, out = _headless(data_root, _ScriptedBackend())
    session._loop = asyncio.get_running_loop()

    with _patch.object(
        session.sdk.loop, "resolve_permission",
        wraps=session.sdk.loop.resolve_permission,
    ) as permission, _patch.object(
        session.sdk.loop, "resolve_question",
        wraps=session.sdk.loop.resolve_question,
    ) as question, _patch.object(
        session.sdk.loop, "approve_plan",
        wraps=session.sdk.loop.approve_plan,
    ) as approve:
        await session.handle_line(
            json.dumps({"type": "permission", "decision": True, "remember": True})
        )
        await session.handle_line(json.dumps({"type": "question", "answers": "42"}))
        await session.handle_line(
            json.dumps({"type": "plan", "action": "approve", "proposal_id": "p1"})
        )

    permission.assert_called_once_with(True, True)
    question.assert_called_once_with("42")
    approve.assert_called_once_with("p1")

    frames = _lines(out)
    assert [f["type"] for f in frames] == [
        "permission_resolved", "question_resolved", "plan_resolved",
    ]
    assert frames[0]["decision"] is True
    assert frames[2]["action"] == "approve"

    # ``cancel`` arms the loop's cancel path and acknowledges.
    assert session.sdk.loop._cancel_event.is_set() is False
    session._loop = asyncio.get_running_loop()
    await session.handle_line(json.dumps({"type": "cancel"}))
    assert session.sdk.loop._cancel_event.is_set() is True
    assert _lines(out)[-1]["type"] == "cancel_acknowledged"
    await session._shutdown()


async def test_headless_close_returns_true_and_shuts_down(data_root):
    """``close`` is the clean stop signal."""
    session, _ = _headless(data_root, _ScriptedBackend())
    session._loop = asyncio.get_running_loop()
    assert await session.handle_line(json.dumps({"type": "close"})) is True
    await session._shutdown()


async def test_headless_unknown_engine_request_is_reported_not_fatal(data_root):
    """An engine response for an unknown id is answered, not crashed on."""
    session, out = _headless(data_root, _ScriptedBackend())
    session._loop = asyncio.get_running_loop()

    await session.handle_line(json.dumps({
        "type": "engine_install", "request_id": "ghost", "choice": "install",
    }))
    frame = _lines(out)[0]
    assert frame["type"] == "engine_install_resolved"
    assert frame["resolved"] is False
    await session._shutdown()


async def test_headless_serve_reads_stdin_and_exits_cleanly(data_root):
    """``serve`` drives the whole protocol off a real stdin pipe.

    The writer thread plays a client: it sends a ``run``, waits for the
    ``finish`` frame to come back, then asks for ``close``.  This is the
    end-to-end proof that the reader thread and the streaming writer coexist.
    """
    backend = _ScriptedBackend(text="over the pipe")
    out = io.StringIO()
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    write_stream = os.fdopen(write_fd, "w", encoding="utf-8", buffering=1)

    with patch("encre.loop.create_backend", return_value=backend):
        session = HeadlessSession(
            _config(), stdin=read_stream, stdout=out, register_builtins=False,
        )

    def _client() -> None:
        """Write a run request, wait for the finish frame, then request close."""
        write_stream.write(json.dumps({"type": "run", "prompt": "ping"}) + "\n")
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if '"finish"' in out.getvalue():
                break
            time.sleep(0.01)
        write_stream.write(json.dumps({"type": "close"}) + "\n")
        write_stream.close()

    client = threading.Thread(target=_client, daemon=True)
    client.start()
    try:
        assert await asyncio.wait_for(session.serve(), timeout=30.0) == 0
    finally:
        client.join(timeout=5.0)
        read_stream.close()

    frames = _lines(out)
    assert frames[0]["type"] == "ready"
    types = [f["type"] for f in frames]
    assert "text_delta" in types
    assert types[-1] == "finish"
    assert [f["text"] for f in frames if f["type"] == "text_delta"] == ["over the pipe"]


async def test_headless_reports_a_non_recoverable_backend_failure(data_root):
    """A non-recoverable backend failure ends the turn as ``reason: "error"``.

    The loop owns backend-error recovery.  A failure it classifies as
    non-recoverable (auth, here) is released to the client, so the terminal
    ``finish`` frame carries the message and the classification the client
    needs to render it.
    """

    class _RejectedBackend(_ScriptedBackend):
        """Backend whose every request fails with an authentication error."""

        async def chat(self, *args: Any, **kwargs: Any):
            """Count the call and raise the auth failure the loop must classify."""
            self.chat_calls += 1
            raise RuntimeError("invalid api key: provider is down")
            yield  # pragma: no cover - makes this an async generator

    session, out = _headless(data_root, _RejectedBackend())
    session._loop = asyncio.get_running_loop()
    await session.handle_line(json.dumps({"type": "run", "prompt": "boom"}))
    await session.wait_turn()

    frames = _lines(out)
    finishes = [f for f in frames if f["type"] == "finish"]
    assert len(finishes) == 1, "the turn must terminate exactly once"
    finish = finishes[-1]
    assert finish["reason"] == "error"
    assert "provider is down" in finish["error"]
    assert finish["error_code"] == "auth_invalid_key"
    assert finish["error_category"] == "auth"
    await session._shutdown()


async def test_headless_reports_an_exhausted_retry_as_an_error_finish(data_root):
    """An unrecognised failure is retried once, then reported to the client.

    The model phase reserves exactly one attempt for an error it cannot
    classify.  When that retry fails too, the error is released -- so a
    headless client always receives a terminal ``finish`` frame carrying the
    reason, never a "successful" turn that produced nothing and never a hang.
    """

    class _DeadProviderBackend(_ScriptedBackend):
        """Backend whose every request fails with an unclassified outage."""

        async def chat(self, *args: Any, **kwargs: Any):
            """Count the call and raise a generic provider outage."""
            self.chat_calls += 1
            raise RuntimeError("provider is down")
            yield  # pragma: no cover - makes this an async generator

    backend = _DeadProviderBackend()
    session, out = _headless(data_root, backend)
    session._loop = asyncio.get_running_loop()
    await session.handle_line(json.dumps({"type": "run", "prompt": "boom"}))
    await session.wait_turn()

    frames = _lines(out)
    assert backend.chat_calls == 2, "the reserved retry must be spent first"
    assert not [f for f in frames if f["type"] == "text_delta"]
    finishes = [f for f in frames if f["type"] == "finish"]
    assert len(finishes) == 1, "the turn must terminate exactly once"
    assert finishes[-1]["reason"] == "error"
    assert "provider is down" in finishes[-1]["error"]
    await session._shutdown()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
