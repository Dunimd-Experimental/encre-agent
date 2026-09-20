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

"""Tools phase: preparation, gating, and intra-turn secondary tools.

Extracted verbatim from the original monolithic ``_run_impl``.  Contains:

* ``_phase_prepare_tools`` -- spec approval gate, tool-call preparation
  (args parsing / registry resolution / semantics tagging), stage inference,
  sequential permission & hook gating (deny / ask / plan-mode interception /
  question tool), pre-executed (streaming) tool result delivery.
* ``_phase_secondary_tools`` -- intra-turn split: merges post-tool content
  (``_extra_*`` buffers streamed after the first tool block) into the
  existing assistant message, then prepares, gates and executes the
  secondary tool calls sequentially.
"""

import asyncio
import builtins
import contextlib
import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from encre.logging_config import get_logger
from encre.loop.phase_api import LoopPhase, PhaseContext, PhaseResult
from encre.loop.turn_ctx import TurnContext
from encre.recovery import ErrorRecoveryEngine, RetryableExecutor
from encre.utils.loop_helpers import (
    _WRITE_TOOL_NAMES,
    _apply_result_budget,
    _args_summary,
    _ensure_plan_items,
    _extract_apply_patch_paths,
    _extract_diff_text,
    _extract_file_path,
    _extract_ref_summary,
    _infer_tool_semantics,
    _is_reference_tool,
    _permission_reason,
    _tool_retry_allowed,
)
from encre.utils.types import (
    Artifact,
    PlanUpdate,
    Reference,
    create_permission_request,
    create_question_request,
    create_system_message,
    create_tool_call_end,
    create_tool_call_start,
    create_tool_progress,
    create_tool_result,
)

logger = get_logger(__name__)


class PhaseToolsMixin:
    """Tool preparation, gating and secondary intra-turn execution."""

    async def _phase_prepare_tools(self, t: TurnContext) -> AsyncGenerator[Any, None]:
        """Prepare and gate the turn's primary tool calls.

        Args:
            t: The shared turn context.

        Yields:
            Tool start/progress/result/end events, permission and question
            requests, and spec-block system messages.
        """
        # 鈹€鈹€ Prepare tool calls: parse args, resolve tools, categorize 鈹€鈹€
        # NOTE: client-facing events use a stable synthetic id ("call_{turn}_{idx}")
        # so they match the ids already emitted on tool_call_delta events.
        # Internal session/history/telemetry continues to use the real
        # backend id (tc["id"]). Without this split, the UI would create
        # one stub entry from the deltas (call_N) and a second entry from
        # tool_call_start (real id), rendering each tool call twice.

        # 鈹€鈹€ Spec approval gate 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        # In spec mode, block write tools until the spec is approved.
        # Read-only tools (file_read, grep, glob, etc.) are allowed.
        t.spec_approved = True
        if t.slash_command_mode == "spec" and self.spec_engine is not None:
            _current = self.spec_engine.current_spec
            t.spec_approved = _current is not None and (
                hasattr(_current, "status") and getattr(_current.status, "value", None) == "approved"
            )
            if not t.spec_approved:
                # Override the spec mode instruction so the model knows to
                # wait for approval before implementing.
                yield create_system_message(
                    "Specification is pending approval. Write tools are blocked. "
                    "Wait for user to approve the spec before implementing.",
                    kind="spec",
                )
        t.prepared = []
        for idx in sorted(t.tool_call_buffers.keys()):
            tc = t.tool_call_buffers[idx]
            client_id = f"call_{self.session.turn_count}_{idx}"
            yield create_tool_call_start(name=tc["name"], id=client_id)
            t.turn_events += 1

            raw_args = tc["arguments"]
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str) and raw_args.strip():
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                    err_msg = f"Error: Invalid JSON arguments: {raw_args[:200]}"
                    yield create_tool_result(id=client_id, content=err_msg, is_error=True)
                    self.session.add_tool_result(tc["id"], err_msg, is_error=True, client_id=client_id)
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=tc["name"], latency_ms=0, success=False, error_message=err_msg,
                    )
                    yield create_tool_call_end(id=client_id)
                    t.turn_events += 1
                    t.prepared.append({"id": tc["id"], "client_id": client_id,
                                       "name": tc["name"], "args": args,
                                       "tool": None, "skip": True, "error": err_msg})
                    continue
            else:
                args = {}

            tool = self.tool_registry.get(tc["name"])
            if tool is None:
                err_msg = f"Error: Unknown tool: {tc['name']}"
                yield create_tool_result(id=client_id, content=err_msg, is_error=True)
                self.session.add_tool_result(tc["id"], err_msg, is_error=True, client_id=client_id)
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=tc["name"], latency_ms=0, success=False, error_message=err_msg,
                )
                yield create_tool_call_end(id=client_id)
                t.turn_events += 1
                t.prepared.append({"id": tc["id"], "client_id": client_id,
                                   "name": tc["name"], "args": args,
                                   "tool": None, "skip": True, "error": err_msg})
                continue

            # 鈹€鈹€ Spec approval gate: block write tools 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
            # In spec mode, if the spec hasn't been approved yet,
            # block all write-class tools (file_write, file_edit,
            # apply_patch, bash, etc.).  Read-only tools are allowed
            # so the model can still gather context.
            _spec_block = False
            if t.slash_command_mode == "spec" and self.spec_engine is not None and (
                tc["name"] in _WRITE_TOOL_NAMES or tc["name"] == "bash"
            ):
                _current = self.spec_engine.current_spec
                t.spec_approved = _current is not None and (
                    hasattr(_current, "status") and getattr(_current.status, "value", None) == "approved"
                )
                _spec_block = not t.spec_approved
            if _spec_block:
                _spec_msg = (
                    "Blocked: Spec mode is active and the specification has not been "
                    "approved yet. Write tools are disabled. Present the specification "
                    "for user review first."
                )
                yield create_tool_result(id=client_id, content=_spec_msg, is_error=True)
                self.session.add_tool_result(tc["id"], _spec_msg, is_error=True, client_id=client_id)
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=tc["name"], latency_ms=0, success=False, error_message=_spec_msg,
                )
                yield create_tool_call_end(id=client_id)
                t.turn_events += 1
                t.prepared.append({"id": tc["id"], "client_id": client_id,
                                   "name": tc["name"], "args": args,
                                   "tool": tool, "skip": True, "error": _spec_msg})
                continue

            is_safe = tool.is_concurrency_safe(args)
            semantics = _infer_tool_semantics(tc["name"], tool)
            _ts = dict(self._state_mgr.tool_semantics)
            _ts[tc["name"]] = semantics
            self._state_mgr.tool_semantics = _ts
            t.prepared.append({
                "id": tc["id"], "client_id": client_id,
                "name": tc["name"], "args": args,
                "tool": tool, "skip": False, "safe": is_safe,
                "args_summary": _args_summary(args),
                "semantics": semantics,
            })

        # Tag tools that were pre-executed during streaming so the permission
        # and execution phases can skip re-execution.
        if self._streaming_tool_results:
            for _pre_p in t.prepared:
                if _pre_p["client_id"] in self._streaming_tool_results:
                    _pre_p["pre_executed"] = True

        next_stage = self._infer_task_stage(t.prompt, t.prepared)
        self._set_task_stage(next_stage, reason="tool preparation")
        self._refresh_working_set(t.prompt, t.prepared)

        # 鈹€鈹€ Permission & hooks for all tools (sequential -- these may need user input) 鈹€鈹€
        if self._cancelled():
            # Tombstone: the user cancelled mid-turn after the model
            # issued tool_calls but before they were executed.  Synthesize
            # error tool_results so the API does not reject the *next*
            # request with an orphan tool_use / tool_result mismatch.
            for _prep in t.prepared:
                if not _prep.get("skip") and not _prep.get("pre_executed"):
                    self.session.add_tool_result(
                        _prep["id"],
                        "[Cancelled by user]",
                        is_error=True,
                        client_id=_prep.get("client_id", ""),
                    )
            t.do_break = True
            return
        for p in t.prepared:
            if self._cancelled():
                break
            if p.get("skip") or p.get("pre_executed"):
                continue
            if not _tool_retry_allowed(p, self._recent_tool_names):
                retry_msg = (
                    "Blocked repeated high-risk tool retry. "
                    + p.get("semantics", {}).get("safe_fallback", "Gather more context before retrying.")
                )
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="blocked")
                yield create_tool_result(id=p["client_id"], content=retry_msg, is_error=True)
                self.session.add_tool_result(p["id"], retry_msg, is_error=True, client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"], latency_ms=0,
                    success=False, error_message=retry_msg,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                p["skip"] = True
                p["error"] = retry_msg
                continue
            permission = await self.safety.check_tool_permission(p["name"], p["args"])
            if permission.behavior == "deny":
                deny_reason = (
                    getattr(permission, "reason", "")
                    or _permission_reason(p["name"])
                    or "Permission denied by policy."
                )
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="denied")
                yield create_tool_result(id=p["client_id"], content=deny_reason, is_error=True)
                self.session.add_tool_result(p["id"], deny_reason, is_error=True, client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"], latency_ms=0,
                    success=False, error_message=deny_reason,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                p["skip"] = True
                p["error"] = deny_reason
                continue

            if permission.behavior == "ask":
                # Prefer the reason supplied by the Rust permission
                # engine (e.g. "Command matches dangerous pattern:
                # \brm\s+.*-(?:[a-z]*r[a-z]*f|rf)\b").  Fall back
                # to a generic message if the engine didn't supply
                # one.
                permission_reason = (
                    getattr(permission, "reason", "")
                    or _permission_reason(p["name"])
                )
                await self.hook_system.emit_permission_request(
                    p["name"], permission_reason
                )
                yield create_permission_request(
                    tool_name=p["name"],
                    reason=permission_reason,
                )
                permission_granted = await self._plan_mode.request_permission(p["name"])
                await self.hook_system.emit_permission_response(
                    p["name"], permission_granted
                )
                if not permission_granted:
                    err_msg = "Permission denied by user."
                    yield create_tool_result(id=p["client_id"], content=err_msg, is_error=True)
                    self.session.add_tool_result(p["id"], err_msg, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=p["name"], latency_ms=0,
                        success=False, error_message=err_msg,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    p["skip"] = True
                    p["error"] = err_msg
                    continue

            pre_hook = await self.hook_system.emit_pre_tool(p["name"], p["args"])
            if pre_hook and pre_hook.get("block"):
                block_reason = pre_hook.get("block_reason") or f"Blocked by hook: {p['name']}"
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="blocked")
                yield create_tool_result(id=p["client_id"], content=block_reason, is_error=True)
                self.session.add_tool_result(p["id"], block_reason, is_error=True, client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"], latency_ms=0,
                    success=False, error_message=block_reason,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                p["skip"] = True
                p["error"] = block_reason
                continue
            if pre_hook and pre_hook.get("modified_input"):
                p["args"] = pre_hook["modified_input"]

            # 鈹€鈹€ Plan mode interception: propose before executing 鈹€鈹€
            if self.plan_mode_active:
                proposal_emitted = False
                async for _event in self._intercept_plan_mode(
                    p["name"], p["args"], p["id"], p["client_id"],
                ):
                    proposal_emitted = True
                    yield _event
                    t.turn_events += 1
                if proposal_emitted and not self._plan_decision:
                    # User did not approve (rejected or timed out).  Feed a
                    # synthetic error result back to the model so it can
                    # adjust its plan without leaving the tool call hanging.
                    if self._plan_decision_timed_out:
                        plan_err = ("Plan approval timed out with no decision. "
                                    "Proceed carefully, or present a smaller, clearer proposal.")
                    else:
                        plan_err = "Plan rejected by user. Adjust your plan and try a different approach."
                    yield create_tool_result(
                        id=p["client_id"],
                        content=plan_err,
                        is_error=True,
                    )
                    self.session.add_tool_result(p["id"], plan_err, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=p["name"], latency_ms=0,
                        success=False, error_message=plan_err,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    p["skip"] = True
                    p["error"] = plan_err
                    continue

            # 鈹€鈹€ Question tool: block until user answers 鈹€鈹€
            if p["name"] == "question":
                args = p["args"]
                questions_list: list[dict[str, Any]] = []
                questions_raw = args.get("questions")
                if isinstance(questions_raw, str):
                    with contextlib.suppress(builtins.BaseException):
                        questions_raw = json.loads(questions_raw)
                if questions_raw and isinstance(questions_raw, list):
                    for q in questions_raw:
                        if isinstance(q, dict):
                            text = (q.get("question") or "").strip()
                            if text:
                                item: dict[str, Any] = {"question": text}
                                if q.get("details"):
                                    item["details"] = str(q["details"]).strip()
                                if q.get("options") and isinstance(q["options"], list):
                                    item["options"] = [str(o) for o in q["options"]]
                                questions_list.append(item)
                q_text = (args.get("question") or "").strip()
                if q_text:
                    item: dict[str, Any] = {"question": q_text}
                    if args.get("details"):
                        item["details"] = str(args["details"]).strip()
                    if args.get("options") and isinstance(args["options"], list):
                        item["options"] = [str(o) for o in args["options"]]
                    questions_list.append(item)
                yield create_question_request(
                    tool_call_id=p["client_id"], questions=questions_list,
                )
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")
                self._question_event = asyncio.Event()
                self._question_answers = ""
                try:
                    await asyncio.wait_for(self._question_event.wait(), timeout=300.0)
                except TimeoutError:
                    self._question_answers = "Error: Question timed out."
                self._question_event = None
                result = self._question_answers
                yield create_tool_result(id=p["client_id"], content=result)
                self.session.add_tool_result(p["id"], result, client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"], latency_ms=0, success=True,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                p["skip"] = True
                p["result"] = result
                continue

        # 鈹€鈹€ Yield results for pre-executed (streaming) tools 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        for p in list(t.prepared):
            if not p.get("pre_executed"):
                continue
            pr = self._streaming_tool_results[p["client_id"]]
            _pre_result = _apply_result_budget(
                pr["result"], p["tool"],
                session_id=self.session.id or "",
                tool_name=p.get("name", ""),
            )
            yield create_tool_result(
                id=p["client_id"], content=_pre_result, is_error=pr["is_error"],
            )
            self.session.add_tool_result(
                p["id"], _pre_result, is_error=pr["is_error"], client_id=p["client_id"],
            )
            t.turn_events += 1
            self.telemetry.record_tool_call(
                tool_name=p["name"], latency_ms=pr["latency_ms"],
                success=not pr["is_error"],
            )
            if pr["is_error"]:
                self._error_tool_names.add(p["name"])
            else:
                self._error_tool_names.discard(p["name"])
            yield create_tool_call_end(id=p["client_id"])
            t.turn_events += 1
            if not pr["is_error"]:
                _fp = _extract_file_path(p["name"], _pre_result)
                if _fp:
                    _dt = _extract_diff_text(p["name"], _pre_result)
                    _entry = self.session.add_artifact(_fp, p["name"], diff_text=_dt)
                    yield Artifact(artifact=_entry)
                elif _is_reference_tool(p["name"]):
                    _summary = _extract_ref_summary(p["name"], p.get("args", {}), _pre_result)
                    if _summary is not None:
                        _entry = self.session.add_reference(p["name"], _summary)
                        yield Reference(reference=_entry)
                _plan_items = _ensure_plan_items(p["name"], p["args"])
                if _plan_items:
                    yield PlanUpdate(plan_items=_plan_items)
            t.prepared.remove(p)

    async def _phase_secondary_tools(self, t: TurnContext) -> AsyncGenerator[Any, None]:
        """Merge intra-turn extra content and run secondary tool calls.

        Args:
            t: The shared turn context.

        Yields:
            Tool start/progress/result/end events for the secondary
            (post-first-tool) tool calls.
        """
        # 鈹€鈹€ Intra-turn split: merge post-tool content into existing assistant message 鈹€鈹€
        # When the model produces thinking/text -> tool_calls -> more thinking/text
        # within the same backend.chat() call, the post-tool content is
        # buffered in _extra_* variables. Merge it into the existing assistant
        # message so the session doesn't get split into two messages for a
        # single model response.
        if t.in_extra and (t.extra_text or t.extra_thinking or t.extra_buffers):
            for i in range(len(self.session.messages) - 1, -1, -1):
                if self.session.messages[i].get("role") == "assistant":
                    msg = self.session.messages[i]
                    if t.extra_text:
                        existing = msg.get("content") or ""
                        extra = "".join(t.extra_text)
                        msg["content"] = (existing + "\n\n" + extra) if existing else extra
                    if t.extra_buffers:
                        extra_tc = []
                        for idx in sorted(t.extra_buffers.keys()):
                            tc = t.extra_buffers[idx]
                            extra_client_id = f"call_{self.session.turn_count}_extra_{idx}"
                            extra_tc_entry = {
                                "id": tc["id"] or extra_client_id,
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            }
                            if extra_tc_entry["id"] != extra_client_id:
                                extra_tc_entry["_client_id"] = extra_client_id
                            extra_tc.append(extra_tc_entry)
                        existing_tc = msg.get("tool_calls", [])
                        msg["tool_calls"] = existing_tc + extra_tc
                    if t.extra_thinking:
                        existing_r = msg.get("reasoning_content", "") or ""
                        extra_r = "".join(t.extra_thinking)
                        msg["reasoning_content"] = existing_r + extra_r
                    # Preserve segment ordering for intra-turn extra content
                    extra_segs = []
                    if t.extra_thinking:
                        extra_segs.append({"kind": "thinking", "text": "".join(t.extra_thinking)})
                    if t.extra_text:
                        extra_segs.append({"kind": "text", "text": "".join(t.extra_text)})
                    for etc in (extra_tc if t.extra_buffers else []):
                        extra_segs.append({"kind": "tool", "tool_id": etc["id"]})
                    if extra_segs:
                        existing_segs = msg.get("segments", [])
                        msg["segments"] = existing_segs + extra_segs
                    self.session.mark_messages_dirty()
                    break

            # Prepare secondary tool calls
            extra_prepared: list[dict[str, Any]] = []
            for idx in sorted(t.extra_buffers.keys()):
                tc = t.extra_buffers[idx]
                client_id = f"call_{self.session.turn_count}_extra_{idx}"
                yield create_tool_call_start(name=tc["name"], id=client_id)
                t.turn_events += 1

                raw_args = tc["arguments"]
                if isinstance(raw_args, dict):
                    args = raw_args
                elif isinstance(raw_args, str) and raw_args.strip():
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                        err_msg = f"Error: Invalid JSON arguments: {raw_args[:200]}"
                        yield create_tool_result(id=client_id, content=err_msg, is_error=True)
                        self.session.add_tool_result(tc["id"], err_msg, is_error=True, client_id=client_id)
                        t.turn_events += 1
                        yield create_tool_call_end(id=client_id)
                        t.turn_events += 1
                        continue
                else:
                    args = {}

                tool = self.tool_registry.get(tc["name"])
                if tool is None:
                    err_msg = f"Error: Unknown tool: {tc['name']}"
                    yield create_tool_result(id=client_id, content=err_msg, is_error=True)
                    self.session.add_tool_result(tc["id"], err_msg, is_error=True, client_id=client_id)
                    t.turn_events += 1
                    yield create_tool_call_end(id=client_id)
                    t.turn_events += 1
                    continue

                # 鈹€鈹€ Spec approval gate for secondary tools 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
                _spec_block = False
                if t.slash_command_mode == "spec" and self.spec_engine is not None and (
                    tc["name"] in _WRITE_TOOL_NAMES or tc["name"] == "bash"
                ):
                    _current = self.spec_engine.current_spec
                    t.spec_approved = _current is not None and (
                        hasattr(_current, "status") and getattr(_current.status, "value", None) == "approved"
                    )
                    _spec_block = not t.spec_approved
                if _spec_block:
                    _spec_msg = (
                        "Blocked: Spec mode is active and the specification has not been "
                        "approved yet. Write tools are disabled. Present the specification "
                        "for user review first."
                    )
                    yield create_tool_result(id=client_id, content=_spec_msg, is_error=True)
                    self.session.add_tool_result(tc["id"], _spec_msg, is_error=True, client_id=client_id)
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=tc["name"], latency_ms=0, success=False, error_message=_spec_msg,
                    )
                    yield create_tool_call_end(id=client_id)
                    t.turn_events += 1
                    continue

                is_safe = tool.is_concurrency_safe(args)
                semantics = _infer_tool_semantics(tc["name"], tool)
                _ts = dict(self._state_mgr.tool_semantics)
                _ts[tc["name"]] = semantics
                self._state_mgr.tool_semantics = _ts
                extra_prepared.append({
                    "id": tc["id"], "client_id": client_id,
                    "name": tc["name"], "args": args,
                    "tool": tool, "skip": False, "safe": is_safe,
                    "args_summary": _args_summary(args),
                    "semantics": semantics,
                })

            # Permission & hooks for secondary tools
            if not self._cancelled():
                for p in extra_prepared:
                    if self._cancelled():
                        break
                    if not _tool_retry_allowed(p, self._recent_tool_names):
                        retry_msg = (
                            "Blocked repeated high-risk tool retry. "
                            + p.get("semantics", {}).get("safe_fallback", "Gather more context before retrying.")
                        )
                        yield create_tool_result(id=p["client_id"], content=retry_msg, is_error=True)
                        self.session.add_tool_result(p["id"], retry_msg, is_error=True, client_id=p["client_id"])
                        t.turn_events += 1
                        self.telemetry.record_tool_call(
                            tool_name=p["name"], latency_ms=0,
                            success=False, error_message=retry_msg,
                        )
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        p["skip"] = True
                        p["error"] = retry_msg
                        continue
                    permission = await self.safety.check_tool_permission(p["name"], p["args"])
                    if permission.behavior == "deny":
                        deny_reason = (
                            getattr(permission, "reason", "")
                            or _permission_reason(p["name"])
                            or "Permission denied by policy."
                        )
                        yield create_tool_result(id=p["client_id"], content=deny_reason, is_error=True)
                        self.session.add_tool_result(p["id"], deny_reason, is_error=True, client_id=p["client_id"])
                        t.turn_events += 1
                        self.telemetry.record_tool_call(
                            tool_name=p["name"], latency_ms=0,
                            success=False, error_message=deny_reason,
                        )
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        p["skip"] = True
                        p["error"] = deny_reason
                        continue

                    if permission.behavior == "ask":
                        permission_reason = (
                            getattr(permission, "reason", "")
                            or _permission_reason(p["name"])
                        )
                        await self.hook_system.emit_permission_request(
                            p["name"], permission_reason
                        )
                        yield create_permission_request(
                            tool_name=p["name"],
                            reason=permission_reason,
                        )
                        permission_granted = await self._plan_mode.request_permission(p["name"])
                        await self.hook_system.emit_permission_response(
                            p["name"], permission_granted
                        )
                        if not permission_granted:
                            err_msg = "Permission denied by user."
                            yield create_tool_result(id=p["client_id"], content=err_msg, is_error=True)
                            self.session.add_tool_result(p["id"], err_msg, is_error=True, client_id=p["client_id"])
                            t.turn_events += 1
                            self.telemetry.record_tool_call(
                                tool_name=p["name"], latency_ms=0,
                                success=False, error_message=err_msg,
                            )
                            yield create_tool_call_end(id=p["client_id"])
                            t.turn_events += 1
                            p["skip"] = True
                            p["error"] = err_msg
                            continue

                    pre_hook = await self.hook_system.emit_pre_tool(p["name"], p["args"])
                    if pre_hook and pre_hook.get("block"):
                        block_reason = pre_hook.get("block_reason") or f"Blocked by hook: {p['name']}"
                        yield create_tool_result(id=p["client_id"], content=block_reason, is_error=True)
                        self.session.add_tool_result(p["id"], block_reason, is_error=True, client_id=p["client_id"])
                        t.turn_events += 1
                        self.telemetry.record_tool_call(
                            tool_name=p["name"], latency_ms=0,
                            success=False, error_message=block_reason,
                        )
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        p["skip"] = True
                        p["error"] = block_reason
                        continue
                    if pre_hook and pre_hook.get("modified_input"):
                        p["args"] = pre_hook["modified_input"]

                    # 鈹€鈹€ Plan mode interception for secondary tools 鈹€鈹€
                    if self.plan_mode_active:
                        sec_proposal_emitted = False
                        async for _event in self._intercept_plan_mode(
                            p["name"], p["args"], p["id"], p["client_id"],
                        ):
                            sec_proposal_emitted = True
                            yield _event
                            t.turn_events += 1
                        if sec_proposal_emitted and not self._plan_decision:
                            if self._plan_decision_timed_out:
                                plan_err = ("Plan approval timed out with no decision. "
                                            "Proceed carefully, or present a smaller, clearer proposal.")
                            else:
                                plan_err = "Plan rejected by user. Adjust your plan and try a different approach."
                            yield create_tool_result(
                                id=p["client_id"],
                                content=plan_err,
                                is_error=True,
                            )
                            self.session.add_tool_result(p["id"], plan_err, is_error=True, client_id=p["client_id"])
                            t.turn_events += 1
                            self.telemetry.record_tool_call(
                                tool_name=p["name"], latency_ms=0,
                                success=False, error_message=plan_err,
                            )
                            yield create_tool_call_end(id=p["client_id"])
                            t.turn_events += 1
                            p["skip"] = True
                            p["error"] = plan_err
                            continue

            # Execute secondary tools sequentially
            for p in extra_prepared:
                if p.get("skip"):
                    continue
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")
                tool_start = time.time()
                tool_error = False
                try:
                    executor = RetryableExecutor(self.recovery_engine)
                    state = await executor.execute(
                        tool_name=p["name"],
                        tool_args=p["args"],
                        execute_fn=lambda args, p=p: p["tool"].execute(**args),
                    )
                    if state.succeeded:
                        result = state.final_result
                        result = self.safety.validate_tool_output(p["name"], result)
                        if state.recovery_history:
                            correction = ErrorRecoveryEngine.infer_correction(state)
                            self.learner.record_correction(
                                tool_name=p["name"],
                                error_context=p["args_summary"],
                                correction=correction,
                            )
                    else:
                        result = state.final_result
                        tool_error = True
                    extra = await self.hook_system.emit_post_tool(p["name"], p["args"], result)
                    if extra:
                        result = result + "\n" + extra
                    await self._collect_tool_skill(p["name"])
                    await self._collect_doc_skills(p["args"])
                except Exception as exc:
                    result = f"Tool execution crashed: {type(exc).__name__}: {exc}"
                    tool_error = True
                result = _apply_result_budget(
                    result, p["tool"],
                    session_id=self.session.id or "",
                    tool_name=p.get("name", ""),
                )
                yield create_tool_result(id=p["client_id"], content=result, is_error=tool_error)
                self.session.add_tool_result(p["id"], result, is_error=tool_error, client_id=p["client_id"])
                t.turn_events += 1
                tool_latency = (time.time() - tool_start) * 1000
                self.telemetry.record_tool_call(
                    tool_name=p["name"], latency_ms=tool_latency,
                    success=not tool_error, error_message=result if tool_error else "",
                )
                if tool_error:
                    self._error_tool_names.add(p["name"])
                    self.learner.record_error(
                        tool_name=p["name"], error_type="execution_error",
                        context=p["args_summary"], correction="",
                    )
                    if self.feedback is not None:
                        self.feedback.record_correction(
                            tool_name=p["name"], error_type="execution_error",
                            error_context=p["args_summary"],
                            user_correction=result[:400],
                        )
                else:
                    self._error_tool_names.discard(p["name"])
                    self.learner.record_success(
                        tool_name=p["name"], intent=t.prompt[:300], params=p["args"],
                        outcome=result[:500], latency_ms=tool_latency,
                    )
                self.optimizer.record_outcome(
                    tool_name=p["name"], params=p["args"],
                    success=not tool_error, latency_ms=tool_latency,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                if not tool_error:
                    fp = _extract_file_path(p["name"], result)
                    if fp:
                        if p["name"] == "apply_patch":
                            for ap_path in _extract_apply_patch_paths(result):
                                entry = self.session.add_artifact(ap_path, p["name"], diff_text="")
                                yield Artifact(artifact=entry)
                        else:
                            diff_text = _extract_diff_text(p["name"], result)
                            entry = self.session.add_artifact(fp, p["name"], diff_text=diff_text)
                            yield Artifact(artifact=entry)
                    plan_items = _ensure_plan_items(p["name"], p["args"])
                    if plan_items:
                        yield PlanUpdate(plan_items=plan_items)
                        self.session.plan_items = plan_items


class PrepareToolsPhase(LoopPhase):
    """Turn-scoped phase: prepare + gate the primary tool calls.

    Wraps :meth:`PhaseToolsMixin._phase_prepare_tools` -- the exact code the
    runner used to call fourth inside every turn iteration.  This is the
    phase that owns the historical ``t.do_break`` checkpoint after tool
    gating (plan-mode approval flow): when it completes with that flag set,
    the pipeline skips the remaining phases and leaves the turn loop --
    expressed here as :attr:`PhaseAction.END_RUN`.
    """

    name = "prepare_tools"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        t = ctx.turn
        await self._stream_through(ctx, self._loop._phase_prepare_tools(t))
        if t.do_break:
            t.do_break = False
            return PhaseResult.end_run("tool gating ended the turn")
        return PhaseResult.continue_()


class SecondaryToolsPhase(LoopPhase):
    """Turn-scoped phase: secondary tool calls requested by executed tools.

    Wraps :meth:`PhaseToolsMixin._phase_secondary_tools` -- the exact code
    the runner used to call sixth inside every turn iteration.
    """

    name = "secondary_tools"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        await self._stream_through(ctx, self._loop._phase_secondary_tools(ctx.turn))
        return PhaseResult.continue_()
