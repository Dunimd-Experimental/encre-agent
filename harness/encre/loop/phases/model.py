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

"""Model phase: prepare the backend request and stream one model slot.

Extracted verbatim from the original monolithic ``_run_impl``.  Contains the
per-turn tool-payload refresh, the ``pre_model_request`` phase-bus emission,
evolution/advisor/standing-orders message merging, the pre-API stability
checks (interrupt, mid-conversation system injection, steer injection, message
repair, checkpoint hard-gate, token-pressure compact, thinking prefill,
cache-edits registration), the streaming ``backend.chat()`` loop with inline
thinking-tag extraction, and the unified error recovery (reactive compact,
model fallback, retry, consume, release).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

from encre.backends.adaptive import (
    apply_to_messages,
    apply_to_tools,
    compute_plan,
    expand_tool_images,
    relocate_tool_images,
    tool_intent_fallback,
)
from encre.backends.base import format_backend_error
from encre.backends.model_profiles import ModelProfile, resolve_model_profile
from encre.backends.registry import ModelInfo, resolve_model_info
from encre.compact.loop_state.transition import TurnTransition
from encre.logging_config import get_logger
from encre.loop.phase_api import (
    LoopPhase,
    PhaseContext,
    PhaseResult,
)
from encre.loop.turn_ctx import TurnContext, TurnExit
from encre.loop_error import RecoveryAction, RecoveryDecision
from encre.loop_stability import (
    PRE_API_COMPACT_THRESHOLD_RATIO,
    append_to_last_user_message,
    build_checkpoint_message,
    build_steer_injection,
    build_thinking_prefill,
    build_tombstone_messages,
    check_interrupt,
    check_token_pressure,
    checkpoint_gate_relaxed,
    checkpoint_threshold_for,
    count_consecutive_tool_steps,
    recent_user_texts,
    repair_messages,
)
from encre.session import compact_notice_anchor
from encre.tracing import trace_llm_call
from encre.utils.loop_helpers import _EVOLUTION_ENABLED
from encre.utils.tokens import count_message_tokens, estimate_request_overhead
from encre.utils.types import (
    BackendError,
    BackendFinish,
    BackendText,
    BackendThinking,
    BackendToolCall,
    BackendToolCallDelta,
    CompactFailed,
    CompactNotification,
    CompactStarting,
    ModelSwitch,
    create_assistant_boundary,
    create_finish,
    create_text_delta,
    create_thinking_delta,
    create_tool_call_delta,
    RetryNotice,
)

logger = get_logger(__name__)

# Patterns for community-standard thinking / CoT tags
_THINK_OPEN = re.compile(r"<(think|thought|thinking|reasoning|analysis)>|\[internal\]")
_THINK_CLOSE = re.compile(r"</(think|thought|thinking|reasoning|analysis)>|\[/internal\]")


def _make_llm_gateway(loop: Any) -> Any:
    """Build the plugin ``llm_call`` gateway bound to a loop's live backend.

    Returns an async ``(messages, *, max_tokens=None, system=None) -> str``
    that streams the loop's *current* backend and collects assistant text.
    Captures the loop rather than the backend instance so fallback swaps
    are honoured automatically.  The ``_owner_loop`` marker lets the model
    phase rebind idempotently when a different loop takes over the hosts.
    """
    from encre.utils.types import BackendFinish, BackendText

    async def gateway(
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> str:
        backend = getattr(loop, "backend", None)
        if backend is None:
            raise RuntimeError("no active backend")
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": str(system)}, *msgs]
        parts: list[str] = []
        async for event in backend.chat(messages=msgs, max_tokens=max_tokens, stream=False):
            if isinstance(event, BackendText):
                parts.append(event.text)
            elif isinstance(event, BackendFinish):
                break
        return "".join(parts)

    gateway._owner_loop = loop
    return gateway


def _append_to_system_prompt(messages: list[dict[str, Any]], suffix: str) -> None:
    """把 ``suffix`` 追加到消息列表首条 system 消息的末尾。

    追加而非前置, 保证 system prompt 的既有前缀完全不变, 从而不破坏 prompt
    caching 的缓存前缀语义。若列表中没有 system 消息, 则插入一条新的 system
    消息作为首元素。
    """
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            existing = message.get("content")
            if not isinstance(existing, str):
                existing = "" if existing is None else str(existing)
            messages[index] = {**message, "content": existing + "\n\n" + suffix}
            return
    messages.insert(0, {"role": "system", "content": suffix})


class PhaseModelMixin:
    """Backend request preparation and streaming for one turn."""

    def _record_thinking_block(self, t: TurnContext, text: str) -> None:
        """Append thinking text to the open thinking block.

        A block opens on the first non-empty thinking event after the
        previous block was closed by its signature (or at turn start), so
        blocks stay aligned with the signatures the server emitted.
        """
        if not text:
            return
        if not t.thinking_blocks or t.thinking_blocks[-1].get("signature"):
            t.thinking_blocks.append({"text": "", "signature": ""})
        t.thinking_blocks[-1]["text"] += text

    def _discard_failed_attempt(self, t: TurnContext) -> None:
        """Drop the streaming state left behind by a failed backend attempt.

        Every recovery action that re-issues ``backend.chat()`` -- the
        unknown-error retry, the network/server backoff retry and the model
        fallback -- starts a brand new attempt, but the turn context still
        holds the buffers the failed attempt filled.  Left in place they get
        concatenated with the retry's output: duplicated text in the
        assistant message and, worse, tool calls the failed model only half
        emitted would be materialised and actually executed.

        ``turn_events`` is deliberately *not* reset: it counts the events
        this turn really emitted (diagnostic, turn-scoped), not per-attempt
        output.
        """
        t.text_parts = []
        t.thinking_parts = []
        t.extra_text = []
        t.extra_thinking = []
        t.tool_call_buffers.clear()
        t.extra_buffers.clear()
        t.tool_seen = False
        t.in_extra = False
        t.think_buf = ""
        t.in_think = False
        # Block-level thinking capture restarts on every attempt: a fresh
        # response re-emits its own blocks and signatures.
        t.thinking_blocks = []
        t._sig_buf = ""
        t.slot_finish_reason = "stop"
        t.backend_usage = None
        # Also drop the partial content kept for the cancel-persistence path
        # (see LoopSupportMixin): if the user cancels after a retry, only the
        # live attempt's tail belongs in the session.
        self._pending_stream_text = []
        self._pending_stream_thinking = []

    async def _inject_plugin_context_messages(self, t: TurnContext) -> None:
        """Merge plugin context-provider messages into ``t.backend_messages``.

        Plugin providers are COMPLEMENTARY to the built-in context
        assembly: the built-in pipeline produces the base request (session
        history + prompt) and every registered provider contributes extra
        fresh message dicts that are merged on top before this model call.
        Neither side replaces the other.

        Providers may live on the agent's host or the shared registry host
        depending on which activation path ran; collect from both and
        de-duplicate by identity.  Every call is guarded and timeout-bounded
        on the host side -- a failing provider degrades to no injection.

        Args:
            t: The shared turn context (backend_messages updated in place).

        Returns:
            None.
        """
        try:
            from encre.kernel.services import shared_plugin_registry_raw as _get_shared_plugin_registry_raw

            _plugin_registry = _get_shared_plugin_registry_raw()
        except Exception:
            return
        _plugin_hosts = []
        _agent_host = getattr(_plugin_registry, "agent_host", None)
        if _agent_host is not None:
            _plugin_hosts.append(_agent_host)
        _shared_host = getattr(_plugin_registry, "host", None)
        if _shared_host is not None and _shared_host not in _plugin_hosts:
            _plugin_hosts.append(_shared_host)
        if not _plugin_hosts:
            return
        _extra_msgs: list[dict] = []
        _seen_ids: set[int] = set()
        for _ph in _plugin_hosts:
            try:
                _msgs = await _ph.collect_context_messages(
                    {
                        "prompt": t.prompt,
                        "session_id": self.session.id or "",
                        "turn": getattr(self.session, "turn_count", 0),
                    }
                )
            except Exception:
                logger.debug("[model] plugin context provider failed", exc_info=True)
                continue

            for _m in _msgs:
                if id(_m) not in _seen_ids:
                    _seen_ids.add(id(_m))
                    _extra_msgs.append(_m)
        if _extra_msgs:
            t.backend_messages = [*t.backend_messages, *_extra_msgs]

    async def _phase_model(self, t: TurnContext) -> AsyncGenerator[Any, None]:
        """Call the model backend and stream its events.

        Refreshes the active tool payload, emits ``pre_model_request`` through
        the phase bus, merges evolution guidance / advisor advice / standing
        orders into the backend messages, runs the pre-API stability checks,
        then drives ``backend.chat()`` with the fallback/retry recovery loop.

        Args:
            t: The shared turn context.

        Yields:
            Streaming ``AgentEvent`` items; may raise :class:`TurnExit` to
            terminate the run (mirrors the original ``return`` statements).
        """
        # ── 能力矩阵自适应: 调用前按 ModelInfo + backend 能力算出降级方案 ──
        # ModelInfo 以 backend 暴露的模型名为准 (registry 查询, 未注册时退到
        # provider 级默认值); 连模型名都拿不到时传 None, 走保守默认。
        plan = compute_plan(
            self._resolve_capability_model_info(),
            backend=self.backend,
            config=self.config,
        )
        if plan.degraded:
            self._publish_capability_degraded(plan)

        # tools 始终取一次活跃清单: 即便原生工具调用被降级, 也需要用它的
        # 名称/描述生成纯文本工具意图指令。注册即激活: 清单恒等于注册表
        # 全集, 无需再按模式切换 toolset。弱模型按 profile 只收窄可见性
        # (执行侧仍全量可调用), 与 prompt 里烘焙的 tool_catalog 同源;
        # use_tools 已降级时不过滤 -- 文本兜底需要完整清单。
        _raw_tools = self.discovery.get_active_tools_payload(
            self.session.id,
            fmt="openai",
            profile=self._model_profile() if plan.use_tools else None,
        )
        tools = apply_to_tools(_raw_tools, plan)
        t.tools = tools

        # Reset the streaming buffer for this turn so the cancel-persistence
        # path only ever sees the current turn's partial content.
        self._pending_stream_text = []
        self._pending_stream_thinking = []

        _t_pm = time.time()
        pre_model = await self.event_bus.emit(
            "pre_model_request",
            messages=self.session.messages,
            tools=tools,
            # The ledger must not reference the live conversation: it is
            # append-only, kept in an in-memory window and flushed to disk,
            # so a live reference would (a) pin one full transcript snapshot
            # per turn -- O(n^2) memory over an n-turn run -- and (b) write
            # the entire conversation into the ledger file every turn.  Hook
            # handlers still receive the real objects; only the recorded
            # event carries this bounded summary.
            ledger_payload={
                "message_count": len(self.session.messages),
                "tool_count": len(tools or ()),
            },
        )
        if isinstance(pre_model, dict) and pre_model.get("block"):
            # A handler vetoed the pre_model phase: the bus-level block only
            # invalidates this turn's pre_model rewrite -- the request still
            # proceeds with the built-in request engineering (never a silent
            # stall), and the veto stays recorded on the ledger.
            logger.warning(
                "[run] pre_model_request blocked by handler (%s) -- ignoring phase rewrite turn=%s",
                pre_model.get("block_reason", "unspecified"),
                self.session.turn_count,
            )
            pre_model = None
        t.pre_model = pre_model
        logger.info("[run] emit_pre_model_request done turn=%s (%.2fs)", self.session.turn_count, time.time() - _t_pm)

        t.backend_messages = list(t.context_msgs)
        t.backend_tools = tools
        # Plugin context engineering (S6), per-turn: providers contribute
        # fresh message dicts before EVERY model call.  Runs BEFORE the
        # request-engineering block below so a plugin that replaces the
        # whole message list stays authoritative; provider output is
        # guarded, timeout-bounded and collected from both the agent host
        # and the shared registry host.
        await self._inject_plugin_context_messages(t)
        if pre_model and pre_model.get("modified_input"):
            # Plugins hold full request-engineering authority here: they
            # may replace the message list, reshape the tool payload, and
            # override backend call parameters (temperature, max_tokens,
            # ...).  Unknown keys are ignored so a stale plugin cannot
            # corrupt the request.
            mi = pre_model["modified_input"]
            if isinstance(mi, dict):
                if isinstance(mi.get("messages"), list):
                    t.backend_messages = mi["messages"]
                if isinstance(mi.get("tools"), list):
                    t.backend_tools = mi["tools"]
                if isinstance(mi.get("model_params"), dict):
                    # Only the safe parameter surface is honored: unknown
                    # keys are dropped so a plugin cannot crash backends
                    # with unsupported kwargs.
                    t.model_params_override = {
                        k: mi["model_params"][k] for k in ("temperature", "max_tokens", "tool_choice") if k in mi["model_params"]
                    }

        # tools 被降级关闭时, 把工具清单压缩成纯文本指令追加到 system prompt
        # 末尾 (前缀保持稳定), 让模型改用文本表达工具意图。同时更新
        # t.system_prompt, 使后续 request-overhead 估算反映这段新增文本。
        if not plan.use_tools:
            _tool_intent = tool_intent_fallback(_raw_tools)
            if _tool_intent:
                _append_to_system_prompt(t.backend_messages, _tool_intent)
                if t.system_prompt:
                    t.system_prompt = t.system_prompt + "\n\n" + _tool_intent

        # 截图投影：把会话历史中 tool 消息的 ``images`` 侧键展开为图片内容
        # 块，再统一搬进紧随其后的合成 user 消息（OpenAI 系原生接受
        # image_url 块，Google 的 _convert_messages 会转 inline_data，
        # Anthropic 由 backend 内的归一化处理）。顺序必须在下面的 vision
        # 降级之前：无 vision 的模型由 apply_to_messages 把剩余图片块
        # 替换为文本描述。
        t.backend_messages = expand_tool_images(t.backend_messages)
        t.backend_messages = relocate_tool_images(t.backend_messages)

        # 无 vision 的模型不再收到图片引用: 剥离并替换为文本描述。
        if not plan.use_vision:
            t.backend_messages = apply_to_messages(t.backend_messages, plan)

        # Inject evolution guidance and feedback into backend messages only
        # (not into self.session.messages) so they don't appear as user input in the UI,
        # don't cause tool duplication on subsequent turns, and -- critically --
        # don't end the agent prematurely.  Guidance is merged into the LAST user
        # message rather than appended as a NEW user turn, because a separate turn
        # tricks the model into responding to the guidance as if it were a fresh
        # instruction, often producing a text-only summary that hits the `return`
        # at the text-only-exit points below, terminating the entire agent loop.
        if _EVOLUTION_ENABLED and self.session.turn_count > 0:
            guidance_parts: list[str] = []
            learner_hint = self.learner.get_guidance("__any__", t.prompt[:300])
            if not learner_hint:
                learner_hint = ""  # no guidance yet
            reflex_hint = self.reflex.get_improvement_context()
            meta_hint = self.meta.get_self_awareness_context()
            for hint in [learner_hint, reflex_hint, meta_hint]:
                if hint:
                    guidance_parts.append(hint)

            if guidance_parts:
                guidance_msg = "\n\n".join(guidance_parts)
                append_to_last_user_message(t.backend_messages, f"[SYSTEM GUIDANCE]\n{guidance_msg}")

            if self.feedback is not None:
                fb = self.feedback.get_relevant_feedback("__any__", t.prompt[:300])
                if fb:
                    append_to_last_user_message(t.backend_messages, f"[PAST CORRECTIONS]\n{fb}")

        advisor_note = ""
        if _EVOLUTION_ENABLED:
            advisor_seed: list[dict[str, Any]] = []
            ws_tools = (self._state_mgr.working_set or {}).get("tools") or []
            for item in ws_tools:
                advisor_seed.append(
                    {
                        "name": item.get("name", ""),
                        "semantics": {
                            "semantic_type": item.get("semantic_type", ""),
                            "cost_level": item.get("cost_level", ""),
                        },
                    }
                )
            advisor_note = await self._maybe_run_advisor_sub_agent(t.prompt, advisor_seed)
        if advisor_note:
            # Shared helper (loop_stability.append_to_last_user_message) folds
            # the advisor sub-agent's guidance into the context without
            # mutating the original session message.
            append_to_last_user_message(t.backend_messages, f"[SUB-AGENT ADVICE]\n{advisor_note}")

        # 0. Standing-orders reminder: re-arm the binding pre-action
        #    constraints on the FIRST model call after a fresh user turn.
        #    Merged into the last user message (not a new turn) so it does
        #    not trigger a text-only exit or a spurious "fresh instruction"
        #    response.  Main agent only; sub-agents get their own briefing.
        if not t.standing_orders_injected and t.standing_orders_text:
            append_to_last_user_message(t.backend_messages, t.standing_orders_text)
            t.standing_orders_injected = True

        # ── Pre-API stability checks ────────────────────────────────
        # 1. Interrupt check: abort if the user cancelled
        if check_interrupt(self):
            logger.info("[run] interrupt detected before API call turn=%d", self.session.turn_count)
            yield create_finish("cancelled")
            raise TurnExit()

        # 1. Mid-conversation system message injection: drain any queued
        #    system directives (stage transition, stuck recovery, dynamic
        #    policy) and append them as discrete ``role: system`` entries
        #    right after the base system prompt -- without rewriting the
        #    prefix, so the cached base prompt stays stable.
        _system_msgs = self._steer_queue.drain_system()
        t.injected_system_entries = []
        if _system_msgs:
            # Insert after the leading system message(s) but before the
            # user/assistant history.  ``backend_messages`` typically
            # starts with one system entry; we splice ours in after it.
            _insert_at = 0
            for _i, _m in enumerate(t.backend_messages):
                if _m.get("role") == "system":
                    _insert_at = _i + 1
                else:
                    break
            for _offset, _sm in enumerate(_system_msgs):
                _entry = {"role": "system", "content": _sm}
                t.backend_messages.insert(_insert_at + _offset, _entry)
                t.injected_system_entries.append(_entry)
            logger.info(
                "[run] injected %d mid-conversation system message(s) turn=%d",
                len(_system_msgs),
                self.session.turn_count,
            )

        # 2. Steer injection: drain any queued /steer instructions
        t.steer_msgs = self._steer_queue.drain()
        t.steer_text = build_steer_injection(t.steer_msgs)
        if t.steer_text:
            t.backend_messages.append({"role": "user", "content": t.steer_text})
            logger.info("[run] injected %d steer instruction(s) turn=%d", len(t.steer_msgs), self.session.turn_count)

        # 3. Message repair: fix role alternation, surrogates, whitespace
        t.backend_messages = repair_messages(t.backend_messages)
        # 3b. Re-pair tool_call groups after repair so an assistant message
        # with tool_calls is always followed by matching tool results.
        t.backend_messages = self.compact_engine.sanitize(t.backend_messages)

        # 3c. Checkpoint hard-gate -- the ONE authority on pausing a long
        # autonomous chain.  When the agent has run more consecutive tool-call
        # steps than its register's budget without a fresh user message, a
        # checkpoint message is injected that hands control back.  There is no
        # prompt-level copy of this rule any more: the model learns about
        # pausing exactly when it happens, so "finish the job" is never
        # contradicted by a standing "stop at five" instruction.
        #
        # Main agent only (``skip_enrichment`` marks delegated/headless runs,
        # which are authorised by construction).  ``chat`` and ``answer`` turns
        # carry no budget at all.  An explicit hands-off authorisation in the
        # current or a recent user message disables the gate -- scanned across
        # recent user turns, because the user should not have to re-authorise
        # every single message.
        _pause_budget = checkpoint_threshold_for(t.register)
        if (
            not t.skip_enrichment
            and _pause_budget > 0
            and not t.checkpoint_injected_this_run
            and not checkpoint_gate_relaxed(t.prompt, recent_user_texts(t.backend_messages))
        ):
            _tool_steps = count_consecutive_tool_steps(t.backend_messages)
            if _tool_steps >= _pause_budget:
                t.checkpoint_text = build_checkpoint_message(_tool_steps)
                if t.checkpoint_text:
                    t.backend_messages.append({"role": "user", "content": t.checkpoint_text})
                    t.checkpoint_injected_this_run = True
                    logger.info(
                        "[run] checkpoint hard-gate fired after %d tool steps (register=%s) turn=%d",
                        _tool_steps,
                        t.register,
                        self.session.turn_count,
                    )

        # 4. Pre-API token pressure check
        t.context_window = self.backend.context_window_size()
        # Compute the output slot budget once per turn (used both for the
        # pre-API pressure check and for the chat call below), instead of
        # probing an in-scope local via the fragile `dir()` hack.
        t.slot_budget = self._resolve_slot_budget()
        # Fixed per-request overhead (system prompt + tool schemas) that the
        # message-only estimate misses.  Without it the pressure check
        # under-reports by tens of thousands of tokens on memory-heavy
        # agents and compaction fires too late -- the model then degrades
        # into long repetitive reasoning on an overfull context.
        t.request_overhead_tokens = estimate_request_overhead(
            t.system_prompt,
            t.backend_tools,
        )
        _pressure = check_token_pressure(
            t.backend_messages,
            t.context_window,
            t.slot_budget,
            extra_tokens=t.request_overhead_tokens,
        )
        if _pressure > PRE_API_COMPACT_THRESHOLD_RATIO:
            logger.warning(
                "[run] token pressure %.1f%% before API call turn=%d (overhead=%dk tokens) -- compacting",
                _pressure * 100,
                self.session.turn_count,
                t.request_overhead_tokens // 1000,
            )
            _notice_id = ""
            try:
                # This synchronous pass is authoritative: bump the epoch so
                # any in-flight background compaction discards its result
                # instead of clobbering this one.
                self._compact_epoch += 1
                _context_msgs = self.session.get_context_messages()
                _pre_tokens = count_message_tokens(_context_msgs)
                # Notice identity + anchor shared by the live indicator and
                # the persisted notice (same line, two states).
                _notice_id, _anchor_id = compact_notice_anchor(_context_msgs)
                # Signal the frontend: output is paused while the history
                # is being compressed (shows the "compressing" indicator).
                yield CompactStarting(notice_id=_notice_id, anchor_id=_anchor_id)
                _compacted = await self.compact_engine.compact(
                    copy.deepcopy(_context_msgs),
                    backend=self.backend,
                    turn_count=self.session.turn_count,
                    system_prompt=t.system_prompt or "",
                    enable_caching=self.config.enable_prompt_caching,
                    session_id=self.session.id or "",
                    force=True,
                )
                if _compacted is not None:
                    # View overlay, not branch replacement: the persisted
                    # history stays append-only and intact; only the model's
                    # context shrinks.
                    _upto = max(
                        (m.get("seq_in_branch", -1) for m in _context_msgs),
                        default=-1,
                    )
                    self.session.apply_compacted_view(
                        self.session.active_branch_id,
                        _compacted,
                        _upto,
                    )
                    self._compacted_this_turn = True
                    # Persist the compaction as a UI notice BEFORE the
                    # notification, so the refreshed message list the client
                    # loads on receipt already carries the divider.
                    self.session.record_ui_notice(
                        "compact",
                        {
                            "old_tokens": _pre_tokens,
                            "new_tokens": count_message_tokens(_compacted),
                            "old_count": len(_context_msgs),
                            "new_count": len(_compacted),
                        },
                        notice_id=_notice_id,
                        anchor_id=_anchor_id,
                    )
                    yield CompactNotification(
                        old_count=len(_context_msgs),
                        new_count=len(_compacted),
                        old_tokens=_pre_tokens,
                        new_tokens=count_message_tokens(_compacted),
                        notice_id=_notice_id,
                        anchor_id=_anchor_id,
                    )
                    t.backend_messages = self.session.get_context_messages()
                    # Re-inject system messages that were lost during session refresh
                    if t.injected_system_entries:
                        _reinsert_at = 0
                        for _i, _m in enumerate(t.backend_messages):
                            if _m.get("role") == "system":
                                _reinsert_at = _i + 1
                            else:
                                break
                        for _offset, _entry in enumerate(t.injected_system_entries):
                            t.backend_messages.insert(_reinsert_at + _offset, dict(_entry))
                    # Re-inject steer messages that were lost during session refresh
                    if t.steer_text:
                        t.backend_messages.append({"role": "user", "content": t.steer_text})
                    # Re-inject the standing-orders reminder lost during session refresh
                    if t.standing_orders_injected and t.standing_orders_text:
                        append_to_last_user_message(t.backend_messages, t.standing_orders_text)
                    # Re-inject the checkpoint gate message lost during session refresh
                    if t.checkpoint_injected_this_run and t.checkpoint_text:
                        t.backend_messages.append({"role": "user", "content": t.checkpoint_text})
                    logger.info("[run] pre-API compact succeeded turn=%d", self.session.turn_count)
                    # Re-run pairing repairs: get_context_messages() splices
                    # the compaction view with the persisted tail, and
                    # messages written while the compact LLM call was in
                    # flight can leave the tail starting mid tool-group
                    # (orphan tool results).  DeepSeek/OpenAI reject those
                    # with HTTP 400 ("role 'tool' must follow tool_calls").
                    t.backend_messages = repair_messages(t.backend_messages)
                    t.backend_messages = self.compact_engine.sanitize(t.backend_messages)
                else:
                    # Compaction was a no-op: dismiss the "compressing"
                    # indicator so it cannot get stuck on screen.
                    yield CompactFailed(reason="no compaction applied", notice_id=_notice_id)
            except Exception as _pc_err:
                logger.warning("[run] pre-API compact failed: %s", _pc_err)
                yield CompactFailed(reason=str(_pc_err), notice_id=_notice_id)

        # 5. Thinking prefill (if enabled)
        t.thinking_prefill = build_thinking_prefill(
            t.prompt,
            enabled=self._thinking_prefill_enabled,
        )

        # Plugin llm_call gateway: bind a lazy gateway to this loop's live
        # backend on both plugin hosts (idempotent; rebinds when a
        # different loop takes over so backend swaps are honoured).
        try:
            from encre.kernel.services import shared_plugin_registry_raw as _get_shared_plugin_registry_raw

            _llm_reg = _get_shared_plugin_registry_raw()
            for _llm_host in (
                getattr(_llm_reg, "agent_host", None) if _llm_reg else None,
                getattr(_llm_reg, "host", None) if _llm_reg else None,
            ):
                if _llm_host is None:
                    continue
                _existing_gw = getattr(_llm_host, "llm_gateway", None)
                if _existing_gw is None or getattr(_existing_gw, "_owner_loop", None) is not self:
                    _llm_host.llm_gateway = _make_llm_gateway(self)
        except Exception:
            logger.debug("[model] llm gateway injection skipped", exc_info=True)

        t.response_text = ""
        t.backend_usage = None
        _t_chat = time.time()

        # Cached microcompact: register tool_results and queue deletions
        # for the Anthropic cache_edits API layer.  Only runs when the
        # backend is Anthropic (other backends ignore the block) and the
        # capability plan allows prompt caching this turn.
        _cache_edits_state = self._cache_edits_state if plan.use_caching else None
        if _cache_edits_state is None and self.config.enable_prompt_caching and plan.use_caching:
            try:
                from encre.cache_edits import create_state

                self._cache_edits_state = create_state()
                _cache_edits_state = self._cache_edits_state
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        if _cache_edits_state is not None:
            try:
                from encre.cache_edits import (
                    create_cache_edits_block,
                    get_tool_results_to_delete,
                    register_tool_results,
                )

                register_tool_results(_cache_edits_state, t.backend_messages)
                to_delete = get_tool_results_to_delete(_cache_edits_state)
                if to_delete:
                    create_cache_edits_block(_cache_edits_state, to_delete)
            except Exception:
                logger.debug("[cache_edits] loop registration failed", exc_info=True)

        logger.info(
            "[run] calling backend.chat() turn=%s msgs=%s tools=%s", self.session.turn_count, len(t.backend_messages), bool(t.backend_tools)
        )
        _chat_first_event = True
        t.llm_span = trace_llm_call(
            self._tracer,
            self.config.model,
            str(t.backend_messages[0])[:200] if t.backend_messages else "",
        )
        t.llm_span.set_attribute("llm.turn", self.session.turn_count)
        self._error_orch.reset_for_new_turn()
        # Slot reservation: start with a small output budget and
        # escalate to the full budget only when the model hits the
        # limit ("max_tokens" or "length" finish reason).  This
        # encourages concise responses (~70% fit in 4K) while
        # allowing long outputs on demand.
        t.slot_finish_reason = "stop"
        # Fallback loop: retry with fallback model on rate-limit/overload
        _attempt_fallback = True
        t.error_consumed = False
        while _attempt_fallback:
            _attempt_fallback = False
            try:
                # Wrap the chat generator with a 120s timeout on the first event,
                # so a hanging API call (wrong key, no network, etc.) surfaces an
                # error rather than freezing the UI indefinitely.
                # Enable caching only when the user requested it AND the backend
                # reports it AND the capability plan allows it this turn.
                _enable_caching = self.config.enable_prompt_caching and self.backend.supports_prompt_caching() and plan.use_caching
                _mpo = t.model_params_override or {}
                _chat_gen = self.backend.chat(
                    messages=t.backend_messages,
                    tools=t.backend_tools,
                    max_tokens=int(_mpo.get("max_tokens", t.slot_budget)),
                    temperature=float(_mpo.get("temperature", 0.0)),
                    tool_choice=str(_mpo.get("tool_choice", "auto")),
                    stream=plan.use_streaming,
                    enable_caching=_enable_caching,
                    cache_edits_state=_cache_edits_state,
                )
                async for event in self._chat_with_retry_notices(_chat_gen, timeout=120.0):
                    # A retry announcement is not model output: it must not mark
                    # the stream as started, and it has to reach the client
                    # unchanged so the turn reads as "still working" instead of
                    # stalled.
                    if isinstance(event, RetryNotice):
                        yield event
                        continue
                    if _chat_first_event:
                        logger.info("[run] backend.chat() first event after %.1fs turn=%s", time.time() - _t_chat, self.session.turn_count)
                        _chat_first_event = False
                    if isinstance(event, BackendText):
                        text = event.text
                        # Inline community thinking-tag extraction.
                        # Splits text into text_delta (outside tags) and
                        # thinking_delta (inside tags), handling cross-event
                        # tags via t.think_buf / t.in_think state.

                        if t.in_think:
                            cm = _THINK_CLOSE.search(text)
                            if cm:
                                t.think_buf += text[: cm.start()]
                                if t.think_buf:
                                    if t.tool_seen:
                                        t.extra_thinking.append(t.think_buf)
                                    else:
                                        t.thinking_parts.append(t.think_buf)
                                        self._pending_stream_thinking.append(t.think_buf)
                                    yield create_thinking_delta(t.think_buf)
                                t.think_buf = ""
                                t.in_think = False
                                text = text[cm.end() :]
                            else:
                                t.think_buf += text
                                text = ""
                        while text:
                            om = _THINK_OPEN.search(text)
                            if om:
                                before = text[: om.start()]
                                if before:
                                    if t.tool_seen:
                                        if not t.in_extra:
                                            t.in_extra = True
                                            yield create_assistant_boundary()
                                        t.extra_text.append(before)
                                    else:
                                        t.text_parts.append(before)
                                        self._pending_stream_text.append(before)
                                    yield create_text_delta(before)
                                t.in_think = True
                                t.think_buf = ""
                                text = text[om.end() :]
                                cm = _THINK_CLOSE.search(text)
                                if cm:
                                    think = text[: cm.start()]
                                    if think:
                                        if t.tool_seen:
                                            t.extra_thinking.append(think)
                                        else:
                                            t.thinking_parts.append(think)
                                            self._pending_stream_thinking.append(think)
                                        yield create_thinking_delta(think)
                                    t.in_think = False
                                    text = text[cm.end() :]
                                else:
                                    t.think_buf = text
                                    text = ""
                            else:
                                if text:
                                    if t.tool_seen:
                                        if not t.in_extra:
                                            t.in_extra = True
                                            yield create_assistant_boundary()
                                        t.extra_text.append(text)
                                    else:
                                        t.text_parts.append(text)
                                        self._pending_stream_text.append(text)
                                    yield create_text_delta(text)
                                text = ""
                        t.turn_events += 1

                    elif isinstance(event, BackendThinking):
                        self._record_thinking_block(t, event.text)
                        if event.signature_delta:
                            # Anthropic closes the block with a signature --
                            # it must be replayed verbatim with the block
                            # text or the next request 400s.
                            t._sig_buf += event.signature_delta
                            if t.thinking_blocks:
                                t.thinking_blocks[-1]["signature"] = t._sig_buf
                        if t.tool_seen:
                            if not t.in_extra:
                                t.in_extra = True
                                yield create_assistant_boundary()
                            t.extra_thinking.append(event.text)
                            yield create_thinking_delta(event.text)
                        else:
                            t.thinking_parts.append(event.text)
                            self._pending_stream_thinking.append(event.text)
                            yield create_thinking_delta(event.text)
                        t.turn_events += 1

                    elif isinstance(event, BackendToolCallDelta):
                        t.tool_seen = True
                        if t.in_extra:
                            idx = event.index
                            # If this tool index was already being accumulated
                            # in tool_call_buffers before _in_extra, keep
                            # appending there instead of creating a duplicate
                            # in _extra_buffers.
                            if idx in t.tool_call_buffers:
                                buf = t.tool_call_buffers[idx]
                                if event.key == "name":
                                    buf["name"] += event.value
                                elif event.key == "arguments":
                                    buf["arguments"] += event.value
                            else:
                                if idx not in t.extra_buffers:
                                    t.extra_buffers[idx] = {"id": "", "name": "", "arguments": ""}
                                buf = t.extra_buffers[idx]
                                if event.key == "name":
                                    buf["name"] += event.value
                                elif event.key == "arguments":
                                    buf["arguments"] += event.value
                        else:
                            idx = event.index
                            if idx not in t.tool_call_buffers:
                                t.tool_call_buffers[idx] = {"id": "", "name": "", "arguments": ""}
                            buf = t.tool_call_buffers[idx]
                            if event.key == "name":
                                buf["name"] += event.value
                            elif event.key == "arguments":
                                buf["arguments"] += event.value
                            yield create_tool_call_delta(
                                id=f"call_{t.tool_id_token}_{idx}",
                                key=event.key,
                                value=event.value,
                            )
                        t.turn_events += 1

                    elif isinstance(event, BackendToolCall):
                        t.tool_seen = True
                        if t.in_extra:
                            # Check if this tool already exists in tool_call_buffers
                            # (accumulated from deltas before _in_extra) and update
                            # in-place to avoid duplicates.
                            found = False
                            for _existing_idx, buf in t.tool_call_buffers.items():
                                if buf["id"] == event.id or (not buf["id"] and buf["name"] == event.name):
                                    buf["id"] = event.id or buf["id"]
                                    buf["name"] = event.name
                                    buf["arguments"] = event.arguments
                                    found = True
                                    break
                            if not found:
                                for _existing_idx, buf in t.extra_buffers.items():
                                    if buf["id"] == event.id or (not buf["id"] and buf["name"] == event.name):
                                        buf["id"] = event.id or buf["id"]
                                        buf["name"] = event.name
                                        buf["arguments"] = event.arguments
                                        found = True
                                        break
                            if not found:
                                idx = len(t.extra_buffers)
                                t.extra_buffers[idx] = {
                                    "id": event.id,
                                    "name": event.name,
                                    "arguments": event.arguments,
                                }
                        else:
                            # Update existing buffer entry (from deltas) if present;
                            # otherwise create a new one.
                            found = False
                            _streaming_call_idx = -1
                            for _existing_idx, buf in t.tool_call_buffers.items():
                                if buf["id"] == event.id or (not buf["id"] and buf["name"] == event.name):
                                    buf["id"] = event.id or buf["id"]
                                    buf["name"] = event.name
                                    buf["arguments"] = event.arguments
                                    found = True
                                    _streaming_call_idx = _existing_idx
                                    break
                            if not found:
                                _streaming_call_idx = len(t.tool_call_buffers)
                                t.tool_call_buffers[_streaming_call_idx] = {
                                    "id": event.id,
                                    "name": event.name,
                                    "arguments": event.arguments,
                                }
                            # Streaming tool execution: pre-execute allowed tools in
                            # background while the model continues generating output.
                            # Exclude tools with side effects on the tool/runtime state
                            # (manage discovers/unlocks tools and installs new ones,
                            # agent/swarm/workflow spawn sub-agents) -- running
                            # those during streaming corrupts state because the current
                            # turn's tool schema was already sent.  They run in the normal
                            # post-streaming phase instead.
                            if (
                                self.config.enable_streaming_tool_execution
                                and not self.plan_mode_active
                                and event.name
                                not in (
                                    "question",
                                    "agent",
                                    "workflow",
                                    "manage",
                                )
                            ):
                                _sc_client = f"call_{t.tool_id_token}_{_streaming_call_idx}"
                                self._streaming_tool_clients.add(_sc_client)
                                if _sc_client not in self._streaming_tool_results:
                                    _stream_task = asyncio.create_task(
                                        self._pre_execute_in_background(
                                            _sc_client,
                                            event.name,
                                            event.arguments,
                                        )
                                    )
                                    self._streaming_tool_tasks.add(_stream_task)

                    elif isinstance(event, BackendFinish):
                        # Capture token usage from the backend
                        if event.usage:
                            t.backend_usage = event.usage
                            t.last_backend_usage = event.usage
                        t.slot_finish_reason = event.reason

                    elif isinstance(event, BackendError):
                        await self.hook_system.emit_error(Exception(event.error), "backend_error")
                        await self.hook_system.emit_backend_error(event.error, self.config.backend_type)
                        # Raise so the generic except handler below catches this
                        # and continues the session instead of killing it.
                        raise RuntimeError(event.error)

            except Exception as exc:
                from encre.errors import AgentError

                # build_tombstone_messages is imported at module top
                # level from encre.loop_stability (the module that
                # actually defines it).  Do NOT re-import it from
                # recovery_loop -- that module does not define it and
                # the import would raise ImportError whenever this
                # fallback path runs.
                from encre.loop_stability import classify_error as _legacy_classify
                from encre.recovery_loop import (
                    is_context_overflow,
                    is_rate_limit_or_overload,
                )

                # Build structured error from the exception
                _agent_err = AgentError.from_exception(exc, finish_reason=None)
                _error_kind = _legacy_classify(exc)
                _is_ctx_overflow = is_context_overflow(exc)
                _is_rate_limit = is_rate_limit_or_overload(exc)

                decision = self._error_orch.handle_backend_exception(
                    exc,
                    error_code=_agent_err.code.value,
                    error_category=_agent_err.category.value,
                    is_context_overflow=_is_ctx_overflow,
                    is_rate_limit=_is_rate_limit,
                    config=self.config,
                    compact_engine=self.compact_engine,
                    session=self.session,
                    backend=self.backend,
                    system_prompt=t.system_prompt or "",
                    tool_call_buffers=t.tool_call_buffers,
                    turn_count=self.session.turn_count,
                )

                # Plugin recovery engineering: an ``on_error`` handler may
                # override the built-in decision via
                # ``{"modified_input": {"recovery_action": "<member>"}}``.
                # Unknown member names are ignored (fail-safe) so a stale
                # plugin cannot derail recovery.
                _plugin_err = await self.hook_system.emit_error(exc, "backend_chat_exception")
                if isinstance(_plugin_err, dict):
                    _act_name = str((_plugin_err.get("modified_input") or {}).get("recovery_action", "")).strip().upper()
                    _override = getattr(RecoveryAction, _act_name, None) if _act_name else None
                    if _override is not None:
                        decision = RecoveryDecision(
                            action=_override,
                            error_code=_agent_err.code.value,
                            error_category=_agent_err.category.value,
                            detail=f"plugin recovery override: {_act_name}",
                        )
                        logger.info("[recovery] plugin override -> %s turn=%s", _act_name, self.session.turn_count)

                if decision.action == RecoveryAction.COMPACT_CONTINUE:
                    # Reactive compact: compress session and retry
                    _notice_id = ""
                    try:
                        # Compact retry budget: a denied spend aborts the
                        # compaction attempt (mirrors a compaction failure)
                        # instead of looping.
                        if not self._error_orch.budget.try_spend("compact"):
                            raise RuntimeError("compact retry budget exhausted")
                        # Authoritative pass: bump the epoch so any in-flight
                        # background compaction discards its result.
                        self._compact_epoch += 1
                        context_msgs = self.session.get_context_messages()
                        est = count_message_tokens(context_msgs)
                        # Notice identity + anchor shared by the live
                        # indicator and the persisted notice.
                        _notice_id, _anchor_id = compact_notice_anchor(context_msgs)
                        # Signal the frontend: the failed (overflowed) attempt
                        # stops here and compression takes over visibly.
                        yield CompactStarting(notice_id=_notice_id, anchor_id=_anchor_id)
                        compacted = await self.compact_engine.compact(
                            copy.deepcopy(context_msgs),
                            backend=self.backend,
                            turn_count=self.session.turn_count,
                            system_prompt=t.system_prompt or "",
                            enable_caching=self.config.enable_prompt_caching,
                            session_id=self.session.id or "",
                            force=True,
                        )
                        if compacted is not None:
                            # View overlay, not branch replacement: the
                            # persisted history stays append-only and intact.
                            _upto = max(
                                (m.get("seq_in_branch", -1) for m in context_msgs),
                                default=-1,
                            )
                            self.session.apply_compacted_view(
                                self.session.active_branch_id,
                                compacted,
                                _upto,
                            )
                            self._compacted_this_turn = True
                            self._update_user_requirements(compacted)
                            # Persist the compaction as a UI notice BEFORE
                            # the notification so the client's refreshed
                            # message list already carries the divider.
                            self.session.record_ui_notice(
                                "compact",
                                {
                                    "old_tokens": est,
                                    "new_tokens": count_message_tokens(compacted),
                                    "old_count": len(context_msgs),
                                    "new_count": len(compacted),
                                },
                                notice_id=_notice_id,
                                anchor_id=_anchor_id,
                            )
                            yield CompactNotification(
                                old_count=len(context_msgs),
                                new_count=len(compacted),
                                old_tokens=est,
                                new_tokens=count_message_tokens(compacted),
                                notice_id=_notice_id,
                                anchor_id=_anchor_id,
                            )
                            logger.info("[reactive] compact succeeded turn=%d, continuing without error", self.session.turn_count)
                            self._has_attempted_reactive_compact = True
                            t.llm_span.set_attribute("llm.reactive_compact", "succeeded")
                            t.llm_span.end()
                            if self._state is not None:
                                self._state.transitions.record(
                                    TurnTransition.REACTIVE_COMPACT,
                                    turn=self.session.turn_count,
                                    detail="context overflow",
                                )
                            if self.memory_system is not None:
                                try:
                                    self.memory_system.refresh()
                                except Exception:
                                    logger.warning("[reactive] memory refresh failed", exc_info=True)
                            continue
                        else:
                            # Compaction was a no-op: dismiss the indicator.
                            yield CompactFailed(reason="no compaction applied", notice_id=_notice_id)
                    except Exception as _ce:
                        logger.warning("[reactive] compact failed turn=%d: %s", self.session.turn_count, _ce)
                        yield CompactFailed(reason=str(_ce), notice_id=_notice_id)

                if decision.action == RecoveryAction.FALLBACK_CONTINUE:
                    # Model fallback: the pool's own list order (sequential
                    # mode) or a shuffled pick (random mode), restricted to
                    # the target model ids when one was configured (gateway
                    # adapter / automation job) and skipping rate-limited
                    # models that are still cooling down.
                    original_model = self.config.model
                    if original_model:
                        self._fallback_tried.add(original_model)
                    # Rate-limited models cool down before the pool may
                    # touch them again: -1 = unlimited (out until the cycle
                    # is reset), 0 = no cooldown, N = N milliseconds.
                    _cooldown_ms = int(getattr(self.config, "model_pool_cooldown_ms", -1))
                    if decision.error_category == "rate_limit" and original_model:
                        if _cooldown_ms < 0:
                            self._pool_cooldowns[original_model] = float("inf")
                            logger.info("[fallback] cooldown %s unlimited (rate limit)", original_model)
                        elif _cooldown_ms > 0:
                            self._pool_cooldowns[original_model] = time.time() + _cooldown_ms / 1000.0
                            logger.info("[fallback] cooldown %s for %dms (rate limit)", original_model, _cooldown_ms)
                    candidates = self.config.resolve_model_candidates(
                        self._fallback_tried, cooldowns=self._pool_cooldowns,
                    )
                    if not candidates:
                        # Every pool model is tried-and-cooling.  -1 (default)
                        # keeps retrying (5s rounds, bounded by the per-turn
                        # budget), 0 retries immediately, N > 0 waits N ms.
                        # Each round clears the cycle state and starts from
                        # the top (the model that just failed stays excluded).
                        _recycle_raw = int(getattr(self.config, "model_pool_recycle_wait_ms", -1))
                        _recycle_ms = 5000 if _recycle_raw < 0 else _recycle_raw
                        if _recycle_ms > 0:
                            logger.info("[fallback] pool exhausted -- recycle wait %dms turn=%d", _recycle_ms, self.session.turn_count)
                            yield RetryNotice(
                                scope="server",
                                attempt=1,
                                delay_ms=_recycle_ms,
                                message="pool recycle wait",
                            )
                            import asyncio as _aio_r
                            await _aio_r.sleep(_recycle_ms / 1000.0)
                        else:
                            logger.info("[fallback] pool exhausted -- immediate recycle turn=%d", self.session.turn_count)
                        self._fallback_tried.clear()
                        self._pool_cooldowns.clear()
                        if original_model:
                            self._fallback_tried.add(original_model)
                        candidates = self.config.resolve_model_candidates(
                            self._fallback_tried, cooldowns=self._pool_cooldowns,
                        )
                    if not candidates:
                        logger.warning("[fallback] no more fallback models available after %s", original_model)
                        decision = RecoveryDecision(
                            RecoveryAction.RELEASE,
                            error_code=_agent_err.code.value,
                            error_category=_agent_err.category.value,
                            detail="no fallback models",
                        )
                        # fall through to RELEASE handling below
                        t.llm_span.set_attribute("llm.error", str(exc))
                        t.llm_span.end()
                        await self.hook_system.emit_error(exc, "backend_chat_exception")
                        await self.hook_system.emit_backend_error(str(exc), type(self.backend).__name__ if self.backend else "unknown")
                        err_msg = format_backend_error(exc)
                        yield create_finish(
                            "error", error=err_msg, error_code=_agent_err.code.value, error_category=_agent_err.category.value
                        )
                        raise TurnExit() from None
                    _next = candidates[0]
                    fallback_model = _next.model_id
                    fallback_backend_type = _next.backend_type or self.config.backend_type
                    logger.info("[fallback] switching from %s to %s due to: %s", original_model, fallback_model, exc)
                    self._active_fallback_model = fallback_model
                    self._active_fallback_backend_type = fallback_backend_type
                    t.llm_span.set_attribute("llm.fallback", f"{original_model}->{fallback_model}")
                    t.llm_span.end()
                    # Record the switch like a tool call: a persisted UI
                    # notice plus a live event, so the desktop renders the
                    # persistent "模型已从 A 更改为 B" line for automatic
                    # pool cycling exactly as it does for a manual switch.
                    _switch_id = f"ms-{int(time.time() * 1000)}-{fallback_model}"
                    _notice = None
                    with contextlib.suppress(Exception):
                        _notice = self.session.record_ui_notice(
                            "model_switch",
                            {
                                "from_model": original_model,
                                "to_model": fallback_model,
                                "reason": str(exc),
                                "source": "pool",
                            },
                            notice_id=_switch_id,
                        )
                    # A burst of switches coalesces into the FIRST notice:
                    # the returned dict carries the chain-start from_model
                    # and the final usable model, so the event (and the UI
                    # line) shows the net change under the original id.
                    _n = _notice if isinstance(_notice, dict) else {}
                    yield ModelSwitch(
                        from_model=str(_n.get("from_model", original_model)),
                        to_model=str(_n.get("to_model", fallback_model)),
                        reason=str(_n.get("reason", str(exc))),
                        source=str(_n.get("source", "pool")),
                        notice_id=str(_n.get("id", _switch_id)),
                        anchor_id=str(_n.get("anchor_id", "")),
                    )
                    if t.tool_call_buffers:
                        _tombstones = build_tombstone_messages(
                            t.tool_call_buffers,
                            f"model fallback: {exc}",
                            reasoning_content="".join(t.thinking_parts),
                        )
                        for _ts in _tombstones:
                            self.session.add_message(
                                _ts["role"],
                                _ts.get("content", ""),
                                tool_call_id=_ts.get("tool_call_id"),
                                name=_ts.get("name"),
                                is_error=_ts.get("is_error", False),
                                tool_calls=_ts.get("tool_calls"),
                                reasoning_content=_ts.get("reasoning_content"),
                            )
                        t.tool_call_buffers.clear()
                    from encre.backend import create_backend

                    fallback_backend = create_backend(
                        fallback_backend_type,
                        model=fallback_model,
                        base_url=_next.base_url or self.config.base_url,
                        api_key=_next.api_key or self.config.api_key,
                        thinking_config=self._thinking_config,
                        # The fallback model may have a different context
                        # window (e.g. 1M primary -> 256K fallback); the
                        # compaction thresholds must follow immediately.
                        context_window=getattr(_next, "context_window", 0) or 0,
                    )
                    self.backend = fallback_backend
                    # Update active_model_index so the frontend selector reflects the switch
                    _fallback_idx = next(
                        (i for i, _m in enumerate(self.config.models) if _m.model_id == fallback_model),
                        self.config.active_model_index,
                    )
                    self.config.active_model_index = _fallback_idx
                    # Refresh the flat config fields (self.model /
                    # backend_type / api_key / context_window kwargs) onto
                    # the fallback model.  Without this ``self.config.model``
                    # keeps reporting the ORIGINAL model, so the NEXT failure
                    # in this turn excludes the wrong id from
                    # ``_fallback_tried`` and ``candidates[0]`` re-picks the
                    # model that just failed -- the pool cycles in place on
                    # one dead model instead of advancing.
                    self.config.apply_active_model()
                    # The tombstones above already recorded the failed model's
                    # half-emitted tool calls in the session; everything else it
                    # streamed is discarded so the fallback answer is not
                    # prefixed with the dead model's partial text.
                    self._discard_failed_attempt(t)
                    _attempt_fallback = True
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.MODEL_FALLBACK,
                            turn=self.session.turn_count,
                            detail=f"{original_model} -> {fallback_model}",
                        )
                    # Announce the switch: the turn visibly restarts on another
                    # model, and without this the user only sees the output
                    # begin again from nothing.
                    yield RetryNotice(
                        scope="model",
                        attempt=1,
                        delay_ms=0,
                        message=f"{original_model} -> {fallback_model}",
                    )
                    continue

                if decision.action == RecoveryAction.RETRY:
                    import asyncio as _aio

                    _retry_kind = "server" if "server" in decision.detail else "network"
                    _retry_count_attr = "_server_retry_count" if _retry_kind == "server" else "_network_retry_count"
                    # Announce the wait before taking it -- that wait is exactly
                    # the "why is nothing happening?" window the user should not
                    # be left guessing about.
                    yield RetryNotice(
                        scope=_retry_kind,
                        attempt=int(getattr(self._error_orch, _retry_count_attr, 0)) + 1,
                        delay_ms=int(decision.delay * 1000),
                        message=str(decision.detail or ""),
                    )
                    await _aio.sleep(decision.delay)
                    logger.info("[run] %s error -- retried after %.1fs delay turn=%d", _retry_kind, decision.delay, self.session.turn_count)
                    # Fresh attempt: drop what the failed one streamed so the
                    # retry's output is not concatenated onto it.
                    self._discard_failed_attempt(t)
                    _attempt_fallback = True
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.NETWORK_RETRY,
                            turn=self.session.turn_count,
                            detail=f"{decision.delay}s delay retry",
                        )
                    continue

                if decision.action == RecoveryAction.CONTINUE:
                    # Unknown / uncategorised failure with one attempt still in
                    # reserve (UNKNOWN_ERROR_RETRY_LIMIT).  Retry it for real by
                    # re-entering the fallback loop.  The previous code fell out
                    # of the loop instead (``_attempt_fallback`` was left False),
                    # so the documented "one silent retry" never happened: the
                    # turn then finished with a bare ``stop`` frame -- no output,
                    # no error, nothing for the user or the caller to act on.
                    if t.text_parts or t.thinking_parts or t.tool_call_buffers:
                        # Part of a response already reached the client.  Re-issuing
                        # the call would superimpose a second copy on the same
                        # assistant message, so surface the failure instead.
                        decision = RecoveryDecision(
                            RecoveryAction.RELEASE,
                            error_code=_agent_err.code.value,
                            error_category=_agent_err.category.value,
                            detail="unknown error after partial output",
                        )
                    else:
                        self._discard_failed_attempt(t)
                        _attempt_fallback = True
                        if self._state is not None:
                            self._state.transitions.record(
                                TurnTransition.UNKNOWN_ERROR,
                                turn=self.session.turn_count,
                                detail=decision.detail,
                            )
                        logger.warning(
                            "[run] unknown error -- retrying (%s) turn=%d error=%s",
                            decision.detail,
                            self.session.turn_count,
                            format_backend_error(exc),
                        )
                        continue

                if decision.action == RecoveryAction.CONSUMED:
                    # The orchestrator deliberately withheld the error instead
                    # of surfacing it (per-turn recovery budget exhausted).
                    # Flag the turn so the post-stream phase does not mistake
                    # the resulting empty output for a lazy model and inject a
                    # "please respond" nudge on top of a dead backend.
                    t.error_consumed = True
                    if self._state is not None:
                        self._state.transitions.record(
                            TurnTransition.ERROR_CONSUMED,
                            turn=self.session.turn_count,
                            detail=decision.detail,
                        )
                    logger.warning(
                        "[run] error withheld -- ending turn quietly turn=%d error=%s", self.session.turn_count, format_backend_error(exc)
                    )
                    continue

                # RecoveryAction.RELEASE — surface to user
                t.llm_span.set_attribute("llm.error", str(exc))
                t.llm_span.end()
                await self.hook_system.emit_error(exc, "backend_chat_exception")
                await self.hook_system.emit_backend_error(str(exc), type(self.backend).__name__ if self.backend else "unknown")
                err_msg = format_backend_error(exc)
                yield create_finish("error", error=err_msg, error_code=_agent_err.code.value, error_category=_agent_err.category.value)
                _last_ui = -1
                for _j in range(len(self.session.messages) - 1, -1, -1):
                    if self.session.messages[_j].get("role") == "user":
                        _last_ui = _j
                        break
                _cur_asst = None
                for _j in range(_last_ui + 1, len(self.session.messages)):
                    if self.session.messages[_j].get("role") == "assistant":
                        _cur_asst = self.session.messages[_j]
                        break
                if _cur_asst is not None:
                    _cur_asst["errorMessage"] = err_msg
                    _cur_asst["errorCode"] = _agent_err.code.value
                    _c = _cur_asst.get("content", "")
                    if isinstance(_c, str):
                        _cur_asst["content"] = _c + f"\n\n[Backend API Error]\n{err_msg}"
                else:
                    self.session.add_message(
                        "assistant",
                        f"[Backend API Error]\n{err_msg}",
                        errorMessage=err_msg,
                        errorCode=_agent_err.code.value,
                        segments=[{"kind": "text", "text": f"[Backend API Error]\n{err_msg}"}],
                    )
                if self._state is not None:
                    self._state.transitions.record(
                        TurnTransition.ERROR,
                        turn=self.session.turn_count,
                        detail=err_msg[:200],
                    )
                logger.info("[run] stored backend error on assistant msg turn=%s, exiting", self.session.turn_count)
                raise TurnExit() from None
            else:
                logger.info(
                    "[run] backend.chat() completed in %.1fs turn=%s events=%s",
                    time.time() - _t_chat,
                    self.session.turn_count,
                    t.turn_events,
                )
                # Record token usage on the LLM span when the backend provided it
                if t.backend_usage:
                    t.llm_span.set_attribute("llm.token_count.prompt", t.backend_usage.get("input_tokens", 0))
                    t.llm_span.set_attribute("llm.token_count.completion", t.backend_usage.get("output_tokens", 0))
                t.llm_span.end()

        # Post-model hook
        t.response_text = "".join(t.text_parts)
        await self.hook_system.emit_post_model_response(t.response_text, len(t.tool_call_buffers))

    def _resolve_capability_model_info(self) -> ModelInfo | None:
        """解析当前 backend 对应的 :class:`ModelInfo`。

        优先按 backend 暴露的模型名查询 registry (未注册时退回 provider 级
        默认值); 聚合型 backend (router / failover)不暴露 ``model``, 此时回退
        到 ``config.model``。当两者都拿不到模型名、或解析过程出现任何异常时返回
        ``None``, 由 :func:`compute_plan` 走保守默认。
        """
        backend = getattr(self, "backend", None)
        model_name = getattr(backend, "model", "") or getattr(self.config, "model", "") or ""
        if not model_name:
            return None
        provider: str | None = None
        provider_fn = getattr(backend, "provider_name", None)
        if callable(provider_fn):
            try:
                provider = str(provider_fn() or "") or None
            except Exception:
                provider = None
        try:
            return resolve_model_info(str(model_name), provider)
        except Exception:
            return None

    def _model_profile(self) -> ModelProfile:
        """当前模型的 :class:`ModelProfile`（带缓存，模型切换时自动失效）。

        以 backend 暴露模型名 + 用户显式 ``capabilities["tier"]`` 为缓存键；
        二者任一变化即重新解析，因此无需在热切换路径上手动清理。解析本身
        绝不抛异常（见 :mod:`encre.backends.model_profiles`）。
        """
        model_info = self._resolve_capability_model_info()
        name = str(getattr(model_info, "name", "") or "") or str(
            getattr(self.backend, "model", "") or getattr(self.config, "model", "") or ""
        )
        capabilities: dict[str, Any] = {}
        try:
            capabilities = dict(getattr(self.config.get_active_model(), "capabilities", {}) or {})
        except Exception:
            capabilities = {}
        cache_key = (name.lower(), str(capabilities.get("tier", "") or ""))
        cached = getattr(self, "_model_profile_memo", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        profile = resolve_model_profile(
            model_info,
            model_id=name,
            backend_type=str(getattr(self.config, "backend_type", "") or ""),
            capabilities=capabilities,
        )
        self._model_profile_memo = (cache_key, profile)
        return profile

    def _try_tier_escalation(self, reason: str) -> bool:
        """卡死的弱模型兜底: 把会话升级到用户池里的更高档模型继续跑。

        仅当当前模型解析为 light 档时由调用方触发。候选 = 用户在
        ``config.models`` 里自己配置的、解析后 tier 非 light 的其他模型
        （strong 优先，其次 standard）-- 绝不发明模型，只使用已配置的。
        每个会话最多升级一次（防乒乓）。镜像 API-error fallback 的切换
        机制（backend + active_model_index），并广播 tier_escalation 事件
        供前端提示"已为你切换更强的模型"。

        Returns:
            True 表示已切换、本轮应继续；False 表示无可升级候选。
        """
        if getattr(self, "_tier_escalated", False):
            return False
        profile = self._model_profile()
        if not profile.is_light:
            return False
        from encre.backends.model_profiles import ModelTier, resolve_model_profile

        current = str(getattr(self.backend, "model", "") or self.config.model or "")
        best: tuple[int, Any] | None = None
        for m in list(getattr(self.config, "models", []) or []):
            mid = str(getattr(m, "model_id", "") or "")
            if not mid or mid == current:
                continue
            try:
                p = resolve_model_profile(
                    None,
                    model_id=mid,
                    capabilities=getattr(m, "capabilities", None) or {},
                )
            except Exception:
                continue
            if p.tier is ModelTier.LIGHT:
                continue
            rank = 0 if p.tier is ModelTier.STRONG else 1
            if best is None or rank < best[0]:
                best = (rank, m)
        if best is None:
            logger.warning("[tier] escalation requested but no non-light model in pool (%s)", reason)
            return False
        _next = best[1]
        try:
            from encre.backend import create_backend

            fallback_backend = create_backend(
                getattr(_next, "backend_type", "") or self.config.backend_type,
                model=_next.model_id,
                base_url=_next.base_url or self.config.base_url,
                api_key=_next.api_key or self.config.api_key,
                thinking_config=self._thinking_config,
                context_window=getattr(_next, "context_window", 0) or 0,
            )
        except Exception:
            logger.warning("[tier] escalation backend creation failed", exc_info=True)
            return False

        self.backend = fallback_backend
        self.config.active_model_index = next(
            (i for i, _m in enumerate(self.config.models) if _m.model_id == _next.model_id),
            self.config.active_model_index,
        )
        self._tier_escalated = True
        # Give the stronger model a clean detection window so the old
        # model's loop history cannot instantly re-halt the turn.
        with contextlib.suppress(Exception):
            self._recent_tool_names.clear()
            self._guardrail_call_counts.clear()
        logger.warning("[tier] escalated light model %s -> %s (%s)", current, _next.model_id, reason)
        try:
            self.event_bus.publish(
                "tier_escalation",
                session_id=self.session.id or "",
                payload={
                    "from_model": current,
                    "to_model": _next.model_id,
                    "reason": reason,
                    "turn": self.session.turn_count,
                },
            )
        except Exception:
            logger.debug("[tier] tier_escalation publish failed", exc_info=True)
        if self._state is not None:
            self._state.transitions.record(
                TurnTransition.TIER_ESCALATION,
                turn=self.session.turn_count,
                detail=f"{current} -> {_next.model_id}: {reason}",
            )
        return True

    def _publish_capability_degraded(self, plan: Any) -> None:
        """把降级决策发布到全局事件主干, 供审计/排障。

        ``self.event_bus`` 不可用时静默降级, 绝不因观测失败中断模型调用。
        """
        try:
            self.event_bus.publish(
                "capability_degraded",
                session_id=self.session.id or "",
                payload={
                    "model": getattr(self.backend, "model", "") or "",
                    "plan": {
                        "use_tools": plan.use_tools,
                        "use_vision": plan.use_vision,
                        "use_caching": plan.use_caching,
                        "use_streaming": plan.use_streaming,
                    },
                    "reasons": dict(plan.reasons),
                },
            )
        except Exception:
            logger.debug("[capability] capability_degraded publish failed", exc_info=True)


class ModelPhase(LoopPhase):
    """Turn-scoped phase: backend request preparation and model streaming.

    Wraps :meth:`PhaseModelMixin._phase_model` -- the exact code the runner
    used to call second inside every turn iteration.  A :class:`TurnExit`
    raised deep inside the streaming loop propagates unchanged; the pipeline
    driver translates it into the historical immediate run exit.
    """

    name = "model"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        await self._stream_through(ctx, self._loop._phase_model(ctx.turn))
        return PhaseResult.continue_()
