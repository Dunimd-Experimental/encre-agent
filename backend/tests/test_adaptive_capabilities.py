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

"""Tests for the capability-matrix adaptive routing layer.

Covers ``encre.backends.adaptive`` (pre-call downgrade planning across the
tools / vision / caching / streaming dimensions) and the integration point in
``encre.loop.phases.model`` where a degraded plan must stop tools from reaching
the backend and must strip image references for text-only models.
"""

from types import SimpleNamespace

from encre.backends.adaptive import (
    CapabilityPlan,
    apply_to_messages,
    apply_to_tools,
    compute_plan,
    tool_intent_fallback,
)
from encre.backends.registry import ModelInfo
from encre.loop.phases.model import PhaseModelMixin
from encre.loop.turn_ctx import TurnContext


# ---------------------------------------------------------------------------
# Minimal capability-reporting backend double for the unit tests.
# ---------------------------------------------------------------------------
class _Caps:
    """Backend double exposing only the capability methods under test."""

    def __init__(self, *, tool_calling=True, vision=True, caching=True):
        self._tool_calling = tool_calling
        self._vision = vision
        self._caching = caching

    def supports_tool_calling(self) -> bool:
        return self._tool_calling

    def supports_vision_input(self) -> bool:
        return self._vision

    def supports_prompt_caching(self) -> bool:
        return self._caching


# ---------------------------------------------------------------------------
# SubTask 9.1 -- compute_plan / apply_to_* / tool_intent_fallback
# ---------------------------------------------------------------------------

def test_tools_disabled_strips_schema():
    """supports_tools=False -> plan.use_tools False and no tools payload."""
    info = ModelInfo(name="no-tools", provider="p", supports_tools=False)
    plan = compute_plan(info)
    assert plan.use_tools is False
    assert plan.degraded is True
    assert apply_to_tools([{"type": "function"}], plan) is None


def test_vision_disabled_strips_images_without_mutation():
    """supports_images=False -> image parts replaced by text, input untouched."""
    plan = compute_plan(ModelInfo(name="text-only", provider="p", supports_images=False))
    assert plan.use_vision is False

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/cat.png"},
                    "alt": "a cat",
                },
            ],
        }
    ]
    out = apply_to_messages(messages, plan)

    # New structure, original left untouched.
    assert out is not messages
    assert out[0] is not messages[0]
    assert messages[0]["content"][1]["type"] == "image_url"

    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "look at this"}
    assert parts[1]["type"] == "text"
    assert "图片已省略" in parts[1]["text"]
    assert "a cat" in parts[1]["text"]

    # Anthropic-style image block is handled too.
    anthropic = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
            ],
        }
    ]
    out2 = apply_to_messages(anthropic, plan)
    assert out2[0]["content"][0]["type"] == "text"
    assert "图片已省略" in out2[0]["content"][0]["text"]
    assert anthropic[0]["content"][0]["type"] == "image"

    # Vision-supporting plans pass messages through unchanged.
    keep = compute_plan(ModelInfo(name="vision", provider="p", supports_images=True))
    assert apply_to_messages(messages, keep) is messages


def test_caching_disabled_gates_enable_caching():
    """supports_prompt_caching=False -> use_caching False and the AND gate is off."""
    info = ModelInfo(name="no-cache", provider="p", supports_prompt_caching=False)
    plan = compute_plan(info)
    assert plan.use_caching is False
    assert "caching" in plan.reasons

    config = SimpleNamespace(enable_prompt_caching=True)
    backend = _Caps(caching=True)
    enable_caching = (
        config.enable_prompt_caching
        and backend.supports_prompt_caching()
        and plan.use_caching
    )
    assert enable_caching is False


def test_streaming_disabled():
    """supports_streaming=False -> use_streaming False."""
    plan = compute_plan(ModelInfo(name="batch-only", provider="p", supports_streaming=False))
    assert plan.use_streaming is False
    assert "streaming" in plan.reasons


def test_unknown_model_conservative_default():
    """model_info=None -> conservative defaults with an explicit reason."""
    plan = compute_plan(None)
    assert plan.use_tools is False
    assert plan.use_vision is False
    assert plan.use_caching is False
    assert plan.use_streaming is True
    for dimension in ("tools", "vision", "caching"):
        assert "conservative default" in plan.reasons[dimension]
    assert plan.degraded is True


def test_backend_capability_is_stricter_and():
    """Backend methods can only tighten, never loosen, the ModelInfo verdict."""
    info = ModelInfo(
        name="capable",
        provider="p",
        supports_tools=True,
        supports_images=True,
        supports_prompt_caching=True,
    )
    plan = compute_plan(info, backend=_Caps(tool_calling=False, vision=True, caching=True))
    assert plan.use_tools is False
    assert plan.use_vision is True

    plan = compute_plan(info, backend=_Caps(tool_calling=True, vision=False, caching=True))
    assert plan.use_tools is True
    assert plan.use_vision is False

    plan = compute_plan(info, backend=_Caps(tool_calling=True, vision=True, caching=False))
    assert plan.use_caching is False

    # A backend cannot re-enable a capability the model itself lacks.
    weak = ModelInfo(name="weak", provider="p", supports_tools=False)
    plan = compute_plan(weak, backend=_Caps(tool_calling=True, vision=True, caching=True))
    assert plan.use_tools is False


def test_compute_plan_never_raises_on_malformed_input():
    """Malformed / missing inputs degrade gracefully instead of raising."""
    assert compute_plan(None) is not None
    assert compute_plan(None, backend=object(), config=object()) is not None

    # A model_info object with no attributes -> conservative.
    plan = compute_plan(object(), backend=object(), config=object())
    assert plan.use_tools is False
    assert plan.use_vision is False
    assert plan.use_streaming is True

    # Non-boolean attribute values are coerced, never raise.
    weird = SimpleNamespace(
        supports_tools="yes",
        supports_images=None,
        supports_prompt_caching=0,
        supports_streaming="",
    )
    plan = compute_plan(weird)
    assert plan.use_tools is True
    assert plan.use_vision is False
    assert plan.use_caching is False
    assert plan.use_streaming is False

    class _Boom:
        def supports_tool_calling(self):
            raise RuntimeError("boom")

        def supports_vision_input(self):
            raise RuntimeError("boom")

        def supports_prompt_caching(self):
            raise RuntimeError("boom")

    plan = compute_plan(ModelInfo(name="x", provider="p"), backend=_Boom())
    assert plan.use_tools is True


def test_tool_intent_fallback_reuses_payload():
    """The fallback text is built from the real tool payload, never invented."""
    payload = [
        {
            "type": "function",
            "function": {"name": "read_file", "description": "Read a file from disk. Extra detail."},
        },
        {"name": "grep", "description": "Search file contents"},
        {"type": "function", "function": {"name": "", "description": "no name"}},
    ]
    text = tool_intent_fallback(payload)
    assert "工具意图说明" in text
    assert "read_file" in text
    assert "Read a file from disk." in text
    assert "grep" in text
    # Empty payloads produce nothing.
    assert tool_intent_fallback(None) == ""
    assert tool_intent_fallback([]) == ""


def test_capability_plan_defaults_are_permissive():
    """A bare CapabilityPlan keeps every dimension enabled."""
    plan = CapabilityPlan()
    assert (plan.use_tools, plan.use_vision, plan.use_caching, plan.use_streaming) == (
        True,
        True,
        True,
        True,
    )
    assert plan.degraded is False


# ---------------------------------------------------------------------------
# SubTask 9.2 -- integration through loop/phases/model.py
# ---------------------------------------------------------------------------
class _Span:
    def set_attribute(self, *_args, **_kwargs):
        pass

    def end(self):
        pass


class _Tracer:
    def start_span(self, *_args, **_kwargs):
        return _Span()


class _Discovery:
    def __init__(self, payload):
        self.tool_set_name = ""
        self._payload = payload

    def get_active_tools_payload(self, _session_id, fmt="openai"):
        return self._payload


class _EventBus:
    def __init__(self):
        self.kinds = []
        self.published = []

    async def emit(self, _phase, **_payload):
        return None

    def publish(self, kind, *, session_id="", payload=None, refs=None):
        self.kinds.append(kind)
        self.published.append((kind, payload))
        return None


class _Session:
    def __init__(self, messages):
        self.id = "s1"
        self.turn_count = 0
        self.messages = messages
        self.active_branch_id = "main"

    def get_context_messages(self):
        return self.messages


class _SteerQueue:
    def drain_system(self):
        return []

    def drain(self):
        return []


class _CompactEngine:
    def sanitize(self, messages):
        return messages


class _Hooks:
    async def emit_post_model_response(self, *_args, **_kwargs):
        return None


class _ErrorOrch:
    def reset_for_new_turn(self):
        pass


class _StateMgr:
    working_set: dict = {}


class _Config:
    model = "deepseek-v4-flash"
    enable_prompt_caching = True
    enable_streaming_tool_execution = False


class _StubBackend:
    """Minimal controllable backend double used only by the integration test."""

    def __init__(self, model, *, tool_calling=True):
        self.model = model
        self._tool_calling = tool_calling
        self.captured: dict = {}

    def supports_tool_calling(self) -> bool:
        return self._tool_calling

    def supports_vision_input(self) -> bool:
        return True

    def supports_prompt_caching(self) -> bool:
        return True

    def context_window_size(self) -> int:
        return 128000

    async def chat(self, **kwargs):
        self.captured = kwargs
        return
        yield  # pragma: no cover - marks this as an async generator


class _Harness(PhaseModelMixin):
    """Drives the real ``_phase_model`` with lightweight collaborators."""

    def __init__(self, backend, messages):
        self.backend = backend
        self.discovery = _Discovery(
            [
                {
                    "type": "function",
                    "function": {"name": "read_file", "description": "Read a file from disk."},
                }
            ]
        )
        self._tool_set_name = "default"
        self._pending_stream_text = []
        self._pending_stream_thinking = []
        self.event_bus = _EventBus()
        self.session = _Session(messages)
        self.config = _Config()
        self._steer_queue = _SteerQueue()
        self.compact_engine = _CompactEngine()
        self._thinking_prefill_enabled = False
        self._cache_edits_state = None
        self._tracer = _Tracer()
        self._error_orch = _ErrorOrch()
        self.hook_system = _Hooks()
        self._state = None
        self._state_mgr = _StateMgr()
        self.plan_mode_active = False
        self._streaming_tool_results: dict = {}
        self._cancelled = lambda: False

    def _resolve_slot_budget(self) -> int:
        return 1024

    async def _chat_with_timeout(self, gen, timeout=120.0):
        async for event in gen:
            yield event


def _make_messages():
    return [
        {"role": "system", "content": "SYS"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            ],
        },
    ]


async def _run_phase(harness):
    messages = harness.session.messages
    turn = TurnContext(
        prompt="hello",
        system_prompt="SYS",
        custom_instructions="",
        slash_command_mode="normal",
        slash_commands=None,
    )
    turn.context_msgs = list(messages)
    turn.system_prompt = "SYS"
    async for _ in harness._phase_model(turn):
        pass


def _find_role(messages, role):
    return next(m for m in messages if m.get("role") == role)


async def test_phase_model_does_not_send_tools_when_degraded():
    """A backend without tool calling must never receive a tools schema."""
    messages = _make_messages()
    backend = _StubBackend("deepseek-v4-flash", tool_calling=False)
    harness = _Harness(backend, messages)

    await _run_phase(harness)

    assert backend.captured["tools"] is None
    assert backend.captured["stream"] is True
    # The downgrade is recorded on the event backbone.
    assert "capability_degraded" in harness.event_bus.kinds

    system_msg = _find_role(backend.captured["messages"], "system")
    assert system_msg["content"].startswith("SYS")
    assert "工具意图说明" in system_msg["content"]
    assert "read_file" in system_msg["content"]

    # Text-only model (deepseek) must not receive image references.
    user_msg = _find_role(backend.captured["messages"], "user")
    assert all(part.get("type") != "image_url" for part in user_msg["content"])
    assert any("图片已省略" in part.get("text", "") for part in user_msg["content"])
    # The original session message is untouched.
    assert messages[1]["content"][1]["type"] == "image_url"


async def test_phase_model_sends_tools_when_supported():
    """A fully capable model keeps tools and image references intact."""
    messages = _make_messages()
    backend = _StubBackend("gpt-4.1", tool_calling=True)
    harness = _Harness(backend, messages)

    await _run_phase(harness)

    assert backend.captured["tools"] is not None
    assert backend.captured["tools"][0]["function"]["name"] == "read_file"
    assert "capability_degraded" not in harness.event_bus.kinds

    user_msg = _find_role(backend.captured["messages"], "user")
    assert any(part.get("type") == "image_url" for part in user_msg["content"])
    system_msg = _find_role(backend.captured["messages"], "system")
    assert "工具意图说明" not in system_msg["content"]
