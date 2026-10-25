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
    """Backend double exposing only the capability-query methods under test.

    Each keyword flag toggles one dimension independently, so the routing layer
    can be exercised against any tools / vision / caching combination.
    """

    def __init__(self, *, tool_calling=True, vision=True, caching=True):
        """Remember the requested capability flags for the methods below."""
        self._tool_calling = tool_calling
        self._vision = vision
        self._caching = caching

    def supports_tool_calling(self) -> bool:
        """Report whether the backend can execute function/tool calls."""
        return self._tool_calling

    def supports_vision_input(self) -> bool:
        """Report whether the backend accepts image-bearing message parts."""
        return self._vision

    def supports_prompt_caching(self) -> bool:
        """Report whether the backend honours prompt-cache breakpoints."""
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

    # The caller-side gate combines user config, backend support and the plan.
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
        """Backend double whose capability probes raise, to test resilience."""

        def supports_tool_calling(self):
            """Raise to simulate a backend that fails its tool-support probe."""
            raise RuntimeError("boom")

        def supports_vision_input(self):
            """Raise to simulate a backend that fails its vision-support probe."""
            raise RuntimeError("boom")

        def supports_prompt_caching(self):
            """Raise to simulate a backend that fails its cache-support probe."""
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
    """No-op tracing span standing in for the real OpenTelemetry span."""

    def set_attribute(self, *_args, **_kwargs):
        """Discard attribute writes; the tests never inspect span attributes."""
        pass

    def end(self):
        """Discard the span-end signal."""
        pass


class _Tracer:
    """No-op tracer that hands back a throwaway span on every request."""

    def start_span(self, *_args, **_kwargs):
        """Return a no-op span regardless of the requested span name."""
        return _Span()


class _Discovery:
    """Tool-discovery double returning one fixed tools payload."""

    def __init__(self, payload):
        """Keep the payload that every lookup should return."""
        self.tool_set_name = ""
        self._payload = payload

    def get_active_tools_payload(self, _session_id, fmt="openai", profile=None):
        """Return the stored payload, narrowed by the model profile if given.

        Mirrors the real discovery semantics: visibility filtering happens
        on tool *names* before materialisation, and an empty match falls
        back to the unrestricted list.
        """
        if profile is None:
            return self._payload
        names = [
            t.get("function", {}).get("name", "") for t in self._payload
        ]
        kept = set(profile.filter_tool_names([n for n in names if n]))
        filtered = [
            t for t in self._payload
            if t.get("function", {}).get("name", "") in kept
        ]
        return filtered or self._payload


class _EventBus:
    """Event bus double that records the kinds it would publish."""

    def __init__(self):
        """Start with empty publish and emit logs."""
        self.kinds = []
        self.published = []

    async def emit(self, _phase, **_payload):
        """Accept phase events without forwarding them anywhere."""
        return None

    def publish(self, kind, *, session_id="", payload=None, refs=None):
        """Record the event kind and payload so tests can assert on them."""
        self.kinds.append(kind)
        self.published.append((kind, payload))
        return None


class _Session:
    """Session double holding the conversation to be sent to the model."""

    def __init__(self, messages):
        """Store the messages and the minimal session metadata the phase reads."""
        self.id = "s1"
        self.turn_count = 0
        self.messages = messages
        self.active_branch_id = "main"

    def get_context_messages(self):
        """Return the stored conversation as the model context."""
        return self.messages


class _SteerQueue:
    """Steering queue double whose drains always come up empty."""

    def drain_system(self):
        """Return no pending system-level steering instructions."""
        return []

    def drain(self):
        """Return no pending user steering messages."""
        return []


class _CompactEngine:
    """Compaction double that passes messages through verbatim."""

    def sanitize(self, messages):
        """Return the messages unchanged so the phase sees real content."""
        return messages


class _Hooks:
    """Hook system double that swallows the post-model-response hook."""

    async def emit_post_model_response(self, *_args, **_kwargs):
        """Accept the hook invocation without running any subscriber."""
        return None



class _ErrorOrch:
    """Error orchestrator double with a no-op turn reset."""

    def reset_for_new_turn(self):
        """Clear nothing; the phase only needs the method to exist."""
        pass


class _StateMgr:
    """State manager double exposing the class-level working set."""


class _Config:
    """Settings double with the values the model phase reads."""

    model = "deepseek-v4-flash"
    enable_prompt_caching = True
    enable_streaming_tool_execution = False


class _StubBackend:
    """Minimal controllable backend double used only by the integration test."""

    def __init__(self, model, *, tool_calling=True):
        """Remember the model name, tool support flag and capture slot."""
        self.model = model
        self._tool_calling = tool_calling
        self.captured: dict = {}

    def supports_tool_calling(self) -> bool:
        """Report the configured tool-calling capability."""
        return self._tool_calling

    def supports_vision_input(self) -> bool:
        """Report vision support; always true here."""
        return True

    def supports_prompt_caching(self) -> bool:
        """Report prompt-cache support; always true here."""
        return True

    def context_window_size(self) -> int:
        """Return a large window so no compaction is triggered."""
        return 128000

    async def chat(self, **kwargs):
        """Capture the chat kwargs, then yield nothing.

        The trailing unreachable ``yield`` marks the function as an async
        generator so the phase's ``async for`` loop terminates immediately.
        """
        self.captured = kwargs
        return
        yield  # pragma: no cover - marks this as an async generator


class _Harness(PhaseModelMixin):
    """Drives the real ``_phase_model`` with lightweight collaborators."""

    def __init__(self, backend, messages):
        """Wire every dependency the model phase touches onto one object."""
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
        """Return a fixed slot budget so budget math stays deterministic."""
        return 1024

    async def _chat_with_timeout(self, gen, timeout=120.0):
        """Forward each event from the backend generator without a real timeout."""
        async for event in gen:
            yield event


def _make_messages():
    """Build a two-message conversation containing one image reference."""
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
    """Construct a turn context and consume the phase generator to completion."""
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
    """Return the first message whose role matches the given string."""
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

    # Tool intent is summarised into the system prompt as a text fallback.
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
