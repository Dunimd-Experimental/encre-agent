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

"""Execute phase: run the prepared (and already gated) tool calls.

Extracted verbatim from the original monolithic ``_run_impl``.  Contains the
safe/unsafe split with path-aware write parallelism, pre-write file
snapshots, the cancel-aware parallel execution of concurrency-safe tools
(bounded by the profile's ``parallel_fan_out`` semaphore), and the sequential
execution of unsafe tools with live sub-agent / workflow progress streaming.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator
from typing import Any

from encre.logging_config import get_logger
from encre.loop.phase_api import LoopPhase, PhaseContext, PhaseResult
from encre.loop.turn_ctx import TurnContext
from encre.recovery import ErrorRecoveryEngine, RetryableExecutor
from encre.tracing import trace_tool_call
from encre.utils.loop_helpers import (
    _WRITE_TOOL_NAMES,
    _apply_result_budget,
    _ensure_plan_items,
    _extract_apply_patch_paths,
    _extract_diff_text,
    _extract_file_path,
    _extract_ref_summary,
    _is_reference_tool,
    _split_writes_by_path_conflict,
    _try_lsp_diagnostics,
    build_verify_instruction,
)
from encre.utils.types import (
    Artifact,
    PlanUpdate,
    Reference,
    WorkflowCompletedEvent,
    WorkflowStartedEvent,
    WorkflowTaskEvent,
    create_tool_call_end,
    create_tool_progress,
    create_tool_result,
)

logger = get_logger(__name__)


class PhaseExecuteMixin:
    """Primary tool execution (safe-parallel and unsafe-sequential)."""

    async def _phase_execute_tools(self, t: TurnContext) -> AsyncGenerator[Any, None]:
        """Split prepared tools into safe/unsafe groups and execute them.

        Args:
            t: The shared turn context.

        Yields:
            Tool progress/result/end events, artifacts, references and
            plan updates produced by the executed tools.
        """
        # 鈹€鈹€ Split into safe (concurrent) and unsafe (sequential) groups 鈹€鈹€
        prepared = t.prepared
        safe_tools = [p for p in prepared if not p.get("skip") and p.get("safe")]
        unsafe_tools = [p for p in prepared if not p.get("skip") and not p.get("safe")]

        # Path-aware parallelism: write tools that touch *different*
        # files are safe to run concurrently (mirrors Hermes'
        # ``_paths_overlap``).  Tools whose paths can't be statically
        # determined (bash, a write tool missing its path arg, ...) and
        # tools whose paths overlap with another write tool stay
        # sequential.  ``apply_patch`` participates too (all of its
        # multi-file paths are checked for overlap).
        _unsafe_writes = [p for p in unsafe_tools if p["name"] in _WRITE_TOOL_NAMES]
        _unsafe_other = [p for p in unsafe_tools if p["name"] not in _WRITE_TOOL_NAMES]
        _parallel_writes, _sequential_writes = _split_writes_by_path_conflict(_unsafe_writes)
        if _parallel_writes:
            safe_tools = safe_tools + _parallel_writes
            unsafe_tools = _sequential_writes + _unsafe_other

        # 鈹€鈹€ Capture file snapshots before any tool writes to disk 鈹€鈹€
        for p in safe_tools + unsafe_tools:
            name = p["name"]
            args = p["args"]
            if name in _WRITE_TOOL_NAMES:
                # file_write, write_file, writeFile: file_path kwarg
                fp = args.get("file_path", "")
                # file_edit: file_path kwarg
                if not fp and name in ("file_edit",):
                    fp = args.get("file_path", "")
                # apply_patch: files list (capture both old and new paths)
                if name == "apply_patch":
                    for fd in args.get("files", []):
                        if isinstance(fd, dict):
                            old_p = fd.get("old_path") or ""
                            new_p = fd.get("new_path") or ""
                            if old_p:
                                self.session.capture_file_snapshot(old_p)
                            if new_p and new_p != old_p:
                                self.session.capture_file_snapshot(new_p)
                    continue
                if fp:
                    self.session.capture_file_snapshot(fp)
        # 鈹€鈹€ Execute safe tools in parallel 鈹€鈹€
        if safe_tools:
            # Emit progress for all safe tools upfront
            for p in safe_tools:
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")

            async def _execute_safe(p: dict[str, Any]) -> dict[str, Any]:
                """Execute a single safe (auto-allowed) tool with retries.

                Wraps the tool in a :class:`~encre.recovery.RetryableExecutor`,
                runs tracing/telemetry around it, validates the output through
                the safety engine, and returns a normalised result dict carrying
                the content, error flag, latency and references.

                Args:
                    p: A prepared-tool dict with ``name``, ``args`` and ``tool``.

                Returns:
                    A dict with ``client_id``, ``name``, ``result``,
                    ``is_error``, ``latency_ms``, ``references`` and any
                    sub-agent ``messages``.
                """
                tool_start = time.time()
                tool_error = False
                _span = trace_tool_call(self._tracer, p["name"], p["args"])
                try:
                    executor = RetryableExecutor(self.recovery_engine)
                    state = await executor.execute(
                        tool_name=p["name"],
                        tool_args=p["args"],
                        execute_fn=lambda a, p=p: p["tool"].execute(**a),
                    )
                    if state.succeeded:
                        result = state.final_result
                        sub_agent_messages = None
                        sub_agent_references: list[dict[str, Any]] = []
                        if isinstance(result, dict):
                            sub_agent_messages = result.get("messages")
                            sub_agent_references = result.get("references", [])
                            result = str(result.get("content", ""))
                        result = self.safety.validate_tool_output(p["name"], result)
                    else:
                        result = state.final_result
                        sub_agent_messages = None
                        sub_agent_references = []
                        if isinstance(result, dict):
                            sub_agent_messages = result.get("messages")
                            result = str(result.get("content", ""))
                        tool_error = True
                    extra = await self.hook_system.emit_post_tool(p["name"], p["args"], result)
                    if extra:
                        result = result + "\n" + extra
                    await self._collect_tool_skill(p["name"])
                    await self._collect_doc_skills(p["args"])
                    _span.set_attribute("tool.success", not tool_error)
                    _span.set_attribute("tool.latency_ms", (time.time() - tool_start) * 1000)
                except Exception as _exc:
                    _span.record_exception(_exc)
                    raise
                finally:
                    _span.end()
                p["result"] = result
                p["sub_agent_messages"] = sub_agent_messages
                p["sub_agent_references"] = sub_agent_references
                p["is_error"] = tool_error
                p["recovery_history"] = list(state.recovery_history)
                p["latency_ms"] = (time.time() - tool_start) * 1000
                return p

            # Cap parallel fan-out to match Claude Code (default 10) instead
            # of launching every concurrency-safe tool at once.
            _tool_sem = asyncio.Semaphore(self._profile.parallel_fan_out)

            async def _execute_safe_bounded(p: dict[str, Any]) -> dict[str, Any]:
                async with _tool_sem:
                    return await _execute_safe(p)

            safe_tasks = [_execute_safe_bounded(p) for p in safe_tools]
            # Cancel-aware gather: if the user hits Stop, cancel all
            # in-flight safe tool tasks immediately.
            cancel_watcher = asyncio.create_task(self._cancel_event.wait())
            gather_task = asyncio.ensure_future(asyncio.gather(*safe_tasks, return_exceptions=True))
            done, pending = await asyncio.wait(
                {gather_task, cancel_watcher},
                return_when=asyncio.FIRST_COMPLETED,
            )
            cancel_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_watcher

            if cancel_watcher in done and self._cancelled():
                gather_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gather_task
                completed = [None] * len(safe_tools)
            else:
                try:
                    completed = gather_task.result()
                except BaseException:
                    completed = [None] * len(safe_tools)
            for idx, item in enumerate(completed):
                p = safe_tools[idx]
                if item is None or isinstance(item, BaseException):
                    # Cancelled (None) or crashed (BaseException): emit
                    # a tombstone result so the UI tool tag closes
                    # properly and the session history stays consistent.
                    if isinstance(item, BaseException):
                        err_msg = f"Tool execution crashed: {type(item).__name__}: {item}"
                    else:
                        err_msg = "[Cancelled by user]"
                    yield create_tool_result(id=p["client_id"], content=err_msg, is_error=True)
                    self.session.add_tool_result(p["id"], err_msg, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self._error_tool_names.add(p["name"])
                    self.telemetry.record_tool_call(
                        tool_name=p["name"], latency_ms=0.0,
                        success=False, error_message=err_msg,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    continue
                p = item
                p["result"] = _apply_result_budget(
                    p["result"], p["tool"],
                    session_id=self.session.id or "",
                    tool_name=p.get("name", ""),
                )
                # Auto-verify: append LSP diagnostics (or an actionable
                # VERIFY instruction when LSP is unavailable) for write
                # tools.  The instruction tells the model which existing
                # lint/test tool to run on the changed file, so the gate
                # is enforceable rather than a passive reminder.
                if not p["is_error"] and p["name"] in _WRITE_TOOL_NAMES:
                    fp = _extract_file_path(p["name"], p["result"])
                    if fp:
                        _lsp_text = await _try_lsp_diagnostics(fp)
                        if _lsp_text:
                            p["result"] += _lsp_text
                        else:
                            p["result"] += "\n\n" + build_verify_instruction(fp)
                        # Track changed code in the verification ledger.
                        self._verif_ledger.mark_edited(fp)
                yield create_tool_result(
                    id=p["client_id"],
                    content=p["result"],
                    is_error=p["is_error"],
                    sub_agent_messages=p.get("sub_agent_messages"),
                )
                self.session.add_tool_result(p["id"], p["result"], is_error=p["is_error"], sub_agent_messages=p.get("sub_agent_messages"), client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"], latency_ms=p["latency_ms"],
                    success=not p["is_error"],
                    error_message=p["result"] if p["is_error"] else "",
                )
                if p["is_error"]:
                    self._error_tool_names.add(p["name"])
                    self.learner.record_error(
                        tool_name=p["name"], error_type="execution_error",
                        context=p["args_summary"], correction="",
                    )
                    # NOTE: no feedback.record_correction here -- a tool
                    # execution error is NOT a user correction.  Recording
                    # the raw error text as a "user correction" polluted
                    # the feedback learner with noise (it then injected
                    # tool error strings into later prompts as if the user
                    # had corrected the agent).  Execution errors go to the
                    # evolution learner's error channel above instead.
                else:
                    self._error_tool_names.discard(p["name"])
                    self.learner.record_success(
                        tool_name=p["name"], intent=t.prompt[:300], params=p["args"],
                        outcome=p["result"][:500], latency_ms=p["latency_ms"],
                    )
                    if p.get("recovery_history"):
                        correction = ErrorRecoveryEngine.infer_correction_from_history(p["recovery_history"], p["name"])
                        self.learner.record_correction(
                            tool_name=p["name"],
                            error_context=p["args_summary"],
                            correction=correction,
                        )
                self.optimizer.record_outcome(
                    tool_name=p["name"], params=p["args"],
                    success=not p["is_error"], latency_ms=p["latency_ms"],
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                if not p["is_error"]:
                    fp = _extract_file_path(p["name"], p["result"])
                    if fp:
                        if p["name"] == "apply_patch":
                            for ap_path in _extract_apply_patch_paths(p["result"]):
                                entry = self.session.add_artifact(ap_path, p["name"], diff_text="")
                                yield Artifact(artifact=entry)
                        else:
                            diff_text = _extract_diff_text(p["name"], p["result"])
                            entry = self.session.add_artifact(fp, p["name"], diff_text=diff_text)
                            yield Artifact(artifact=entry)
                    else:
                        # Non-file tool -> record as reference
                        if _is_reference_tool(p["name"]):
                            summary = _extract_ref_summary(p["name"], p.get("args", {}), p["result"])
                            if summary is not None:
                                ref_icon = ""
                                entry = self.session.add_reference(p["name"], summary, icon=ref_icon)
                                yield Reference(reference=entry)
                        # Forward references from sub-agents (agent / workflow tools)
                        for sub_ref in (p.get("sub_agent_references") or []):
                            if isinstance(sub_ref, dict) and _is_reference_tool(sub_ref.get("tool", "")):
                                ref_entry = self.session.add_reference(
                                    sub_ref.get("tool", ""),
                                    sub_ref.get("summary", ""),
                                    icon=sub_ref.get("icon", ""),
                                )
                                yield Reference(reference=ref_entry)
                    plan_items = _ensure_plan_items(p["name"], p["args"])
                    if plan_items:
                        yield PlanUpdate(plan_items=plan_items)
                        self.session.plan_items = plan_items

        # 鈹€鈹€ Execute unsafe tools sequentially 鈹€鈹€
        for p in unsafe_tools:
            if self._cancelled():
                break
            tool_start = time.time()
            yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")

            tool_error = False
            sub_agent_messages = None
            sub_agent_session_id = None
            sub_agent_references: list[dict[str, Any]] = []
            try:
                if p["name"] == "agent":
                    progress_queue: asyncio.Queue[list[dict[str, Any]] | None] = asyncio.Queue()

                    async def _sub_agent_progress(messages: list[dict[str, Any]], progress_queue=progress_queue) -> None:
                        nonlocal sub_agent_messages
                        sub_agent_messages = messages
                        await progress_queue.put(messages)

                    agent_args = dict(p["args"])
                    agent_args["progress_callback"] = _sub_agent_progress

                    async def _run_agent_tool(p=p, agent_args=agent_args, progress_queue=progress_queue) -> Any:
                        try:
                            return await p["tool"].execute(**agent_args)
                        finally:
                            await progress_queue.put(None)

                    agent_task = asyncio.create_task(_run_agent_tool())
                    _agent_cancel = asyncio.create_task(self._cancel_event.wait())
                    while True:
                        get_task = asyncio.create_task(progress_queue.get())
                        done, _ = await asyncio.wait(
                            {get_task, _agent_cancel}, return_when=asyncio.FIRST_COMPLETED,
                        )
                        if _agent_cancel in done:
                            agent_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await agent_task
                            get_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await get_task
                            _agent_cancel.cancel()
                            result = "[Cancelled by user]"
                            tool_error = True
                            self.session.add_tool_result(p["id"], result, is_error=True, client_id=p.get("client_id", ""))
                            yield create_tool_result(id=p["client_id"], content=result, is_error=True)
                            yield create_tool_call_end(id=p["client_id"])
                            t.turn_events += 1
                            break
                        if get_task in done:
                            live_messages = get_task.result()
                        else:
                            get_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await get_task
                            continue
                        _agent_cancel.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await _agent_cancel
                        _agent_cancel = asyncio.create_task(self._cancel_event.wait())
                        if live_messages is None:
                            break
                        yield create_tool_progress(
                            id=p["client_id"],
                            tool_name=p["name"],
                            status="running",
                            sub_agent_messages=live_messages,
                        )
                    else:
                        _agent_cancel.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await _agent_cancel
                    if not tool_error:
                        result_obj = await agent_task
                    if isinstance(result_obj, dict):
                        sub_agent_messages = result_obj.get("messages")
                        sub_agent_session_id = result_obj.get("session_id")
                        sub_agent_references = result_obj.get("references", [])
                        if sub_agent_messages:
                            yield create_tool_progress(
                                id=p["client_id"],
                                tool_name=p["name"],
                                status="running",
                                sub_agent_messages=sub_agent_messages,
                            )
                        result = str(result_obj.get("content", ""))
                    else:
                        result = str(result_obj)
                    result = self.safety.validate_tool_output(p["name"], result)
                elif p["name"] == "workflow":
                    progress_queue: asyncio.Queue[list[dict[str, Any]] | None] = asyncio.Queue()

                    async def _wf_progress(messages: list[dict[str, Any]], progress_queue=progress_queue) -> None:
                        await progress_queue.put(messages)

                    wf_args = dict(p["args"])
                    wf_args["progress_callback"] = _wf_progress

                    async def _run_wf_tool(p=p, wf_args=wf_args, progress_queue=progress_queue) -> Any:
                        try:
                            return await p["tool"].execute(**wf_args)
                        finally:
                            await progress_queue.put(None)

                    wf_task = asyncio.create_task(_run_wf_tool())
                    _wf_cancel = asyncio.create_task(self._cancel_event.wait())
                    while True:
                        get_task = asyncio.create_task(progress_queue.get())
                        done, _ = await asyncio.wait(
                            {get_task, _wf_cancel}, return_when=asyncio.FIRST_COMPLETED,
                        )
                        if _wf_cancel in done:
                            wf_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await wf_task
                            get_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await get_task
                            _wf_cancel.cancel()
                            result = "[Cancelled by user]"
                            tool_error = True
                            self.session.add_tool_result(p["id"], result, is_error=True, client_id=p.get("client_id", ""))
                            yield create_tool_result(id=p["client_id"], content=result, is_error=True)
                            yield create_tool_call_end(id=p["client_id"])
                            t.turn_events += 1
                            break
                        if get_task in done:
                            live_messages = get_task.result()
                        else:
                            get_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await get_task
                            continue
                        _wf_cancel.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await _wf_cancel
                        _wf_cancel = asyncio.create_task(self._cancel_event.wait())
                        if live_messages is None:
                            break
                        for msg in live_messages:
                            if isinstance(msg, dict) and msg.get("role") == "workflow":
                                wf_type = msg.get("type", "")
                                if wf_type == "workflow_started":
                                    yield WorkflowStartedEvent(
                                        workflow_id=msg.get("workflow_id", ""),
                                        goal=msg.get("goal", ""),
                                        total_tasks=msg.get("total_tasks", 0),
                                        task_ids=msg.get("task_ids", []),
                                    )
                                elif wf_type == "workflow_task":
                                    yield WorkflowTaskEvent(
                                        workflow_id=msg.get("workflow_id", ""),
                                        task_id=msg.get("task_id", ""),
                                        task_name=msg.get("task_name", ""),
                                        status=msg.get("status", "running"),
                                    )
                                elif wf_type == "workflow_completed":
                                    yield WorkflowCompletedEvent(
                                        workflow_id=msg.get("workflow_id", ""),
                                        goal=msg.get("goal", ""),
                                        success=msg.get("success", True),
                                        completed_count=msg.get("completed_count", 0),
                                        failed_count=msg.get("failed_count", 0),
                                        skipped_count=msg.get("skipped_count", 0),
                                        total_duration=msg.get("total_duration", 0.0),
                                    )
                            else:
                                sub_agent_messages = [live_messages] if not isinstance(live_messages, list) else live_messages
                                yield create_tool_progress(
                                    id=p["client_id"],
                                    tool_name=p["name"],
                                    status="running",
                                    sub_agent_messages=sub_agent_messages,
                                )
                    else:
                        _wf_cancel.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await _wf_cancel
                    if not tool_error:
                        result_obj = await wf_task
                        sub_agent_messages = None
                        if isinstance(result_obj, dict):
                            sub_agent_messages = result_obj.get("messages")
                            result = str(result_obj.get("content", ""))
                        else:
                            result = str(result_obj)
                        result = self.safety.validate_tool_output(p["name"], result)
                else:
                    executor = RetryableExecutor(self.recovery_engine)
                    # Wrap execution in a cancel-aware wait so the user's
                    # Stop button takes effect immediately, even if the
                    # tool is mid-execution (e.g. long-running bash command).
                    exec_task = asyncio.create_task(
                        executor.execute(
                            tool_name=p["name"],
                            tool_args=p["args"],
                            execute_fn=lambda args, p=p: p["tool"].execute(**args),
                        )
                    )
                    cancel_task = asyncio.create_task(self._cancel_event.wait())
                    done, pending = await asyncio.wait(
                        {exec_task, cancel_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    cancel_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_task
                    if exec_task in done:
                        state = exec_task.result()
                    else:
                        # Cancelled: abort the tool execution
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        result = "[Cancelled by user]"
                        tool_error = True
                        self.session.add_tool_result(
                            p["id"], result, is_error=True, client_id=p.get("client_id", ""),
                        )
                        yield create_tool_result(id=p["client_id"], content=result, is_error=True)
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        continue
                    if state.succeeded:
                        result = state.final_result
                        if isinstance(result, dict):
                            sub_agent_messages = result.get("messages")
                            result = str(result.get("content", ""))
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
                        if isinstance(result, dict):
                            sub_agent_messages = result.get("messages")
                            result = str(result.get("content", ""))
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
            yield create_tool_result(
                id=p["client_id"],
                content=result,
                is_error=tool_error,
                sub_agent_messages=sub_agent_messages,
                sub_agent_session_id=sub_agent_session_id,
            )
            self.session.add_tool_result(
                p["id"],
                result,
                is_error=tool_error,
                sub_agent_messages=sub_agent_messages,
                sub_agent_session_id=sub_agent_session_id,
                client_id=p["client_id"],
            )
            t.turn_events += 1

            tool_latency = (time.time() - tool_start) * 1000
            self.telemetry.record_tool_call(
                tool_name=p["name"], latency_ms=tool_latency,
                success=not tool_error,
                error_message=result if tool_error else "",
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
                else:
                    # Non-file tool -> record as reference
                    if _is_reference_tool(p["name"]):
                        summary = _extract_ref_summary(p["name"], p.get("args", {}), result)
                        if summary is not None:
                            entry = self.session.add_reference(p["name"], summary)
                            yield Reference(reference=entry)
                # Forward references from sub-agents (agent tool)
                for sub_ref in sub_agent_references:
                    if isinstance(sub_ref, dict) and _is_reference_tool(sub_ref.get("tool", "")):
                        ref_entry = self.session.add_reference(
                            sub_ref.get("tool", ""),
                            sub_ref.get("summary", ""),
                            icon=sub_ref.get("icon", ""),
                        )
                        yield Reference(reference=ref_entry)
                plan_items = _ensure_plan_items(p["name"], p["args"])
                if plan_items:
                    yield PlanUpdate(plan_items=plan_items)


class ExecuteToolsPhase(LoopPhase):
    """Turn-scoped phase: execute the prepared tool calls.

    Wraps :meth:`PhaseExecuteMixin._phase_execute_tools` -- the exact code
    the runner used to call fifth inside every turn iteration.
    """

    name = "execute_tools"

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        await self._stream_through(ctx, self._loop._phase_execute_tools(ctx.turn))
        return PhaseResult.continue_()
