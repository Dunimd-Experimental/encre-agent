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

"""Module: builtin/agent.py

Agent implementation for the Encre tool system.
"""

import asyncio
import contextlib
import os
import time
from typing import Any

from encre.logging_config import get_logger
from encre.sub_agent.cache import CacheContext
from encre.sub_agent.lifecycle import SubAgentMode, detect_background_mode
from encre.sub_agent.worktree import WorktreeIsolation
from encre.tools.base import build_tool

logger = get_logger(__name__)

# Max sub-agent tasks that may run concurrently inside a SINGLE ``agent`` tool
# call. The frontend can only tile up to 4 parallel regions cleanly, so we cap
# the backend to match: extra tasks queue and start as running slots free up.
# This limit is per tool call -- independent ``agent`` calls (e.g. a plan run
# and a research run) each get their own budget and never contend.
MAX_PARALLEL_SUB_AGENTS = 4

MAX_SUB_AGENT_DEPTH = 1


def _get_bg_tracker(parent_loop: Any) -> Any:
    """Get or lazily initialise the background sub-agent tracker on parent_loop."""
    if parent_loop is None:
        return None
    tracker = getattr(parent_loop, "_bg_sub_agents", None)
    if tracker is not None:
        return tracker
    from encre.sub_agent.lifecycle import BackgroundSubAgentTracker
    tracker = BackgroundSubAgentTracker()
    parent_loop._bg_sub_agents = tracker
    return tracker

from encre.tools.runtime import (  # noqa: F401
    _current_loop,
    _parent_loop,
    _resolve_loop,
    reset_active_loop,
    set_active_loop,
    set_parent_loop,
)


def _build_agents_list() -> str:
    """Build a formatted string listing available sub-agents for the tool description."""
    loop = _resolve_loop()
    if loop is None:
        return ""
    sub_agents = getattr(loop.config, "sub_agents", [])
    if not sub_agents:
        return ""
    lines = ["Available sub-agents:"]
    for sa in sub_agents:
        policy = getattr(sa, "tool_policy", "all")
        suffix = ""
        if policy == "readonly":
            suffix = " [read-only]"
        elif policy == "no_writes":
            suffix = " [no writes]"
        lines.append(f"  - {sa.name}: {sa.description}{suffix}")
    lines.append(
        "\nYou can run multiple sub-agents in parallel by passing a `tasks` "
        "array -- each entry has the same shape as the single-task fields."
    )
    return "\n".join(lines)


def _resolve_sub_agent_config(agent_name: str) -> Any:
    """Find a sub-agent by name in the active loop's sub_agents list.

    Returns the matching ``SubAgentConfig`` or ``None`` if not found.
    """
    loop = _resolve_loop()
    if loop is None:
        return None
    sub_agents = getattr(loop.config, "sub_agents", [])
    for sa in sub_agents:
        if sa.name == agent_name:
            return sa
    return None


def _enforce_tool_policy(tool_name: str, tool_input: dict[str, Any] | None = None) -> str | None:
    """Return an error string if the current sub-agent's tool policy
    forbids ``tool_name``; return ``None`` when the call is allowed.

    The policy comes from the active loop's ``config.current_tool_policy``
    attribute.  This is set by the parent loop before delegating to a
    sub-agent (see ``loop._run_sub_agent``) and travels with the
    sub-agent's own ``EncreConfig`` so the pre-tool hook can see it
    regardless of which loop is currently active.
    """
    loop = _resolve_loop()
    if loop is None:
        return None
    policy = getattr(loop.config, "current_tool_policy", "all")
    # Hard-fence FIRST: any sub-agent (depth > 0) must NOT spawn further
    # sub-agents or workflows.  This is checked before the policy
    # short-circuit so even an "all"-policy sub-agent cannot call the
    # agent tool.  The depth check is the only layer that reliably stops
    # an LLM from re-trying on a soft error.
    if tool_name in ("agent", "workflow") and getattr(loop, "sub_agent_depth", 0) > 0:
        return (
            "Sub-agents are forbidden from spawning further sub-agents or "
            "workflows. The runtime only allows one level of delegation. "
            "Complete the assigned task with your own tools and return the "
            "result."
        )
    if not isinstance(policy, str) or policy == "all":
        return None
    # Check tool's is_readonly declaration
    tool_obj = loop.tool_registry.get(tool_name) if hasattr(loop, "tool_registry") else None
    if tool_obj is not None:
        args = tool_input or {}
        if tool_obj.is_readonly(args):
            return None
    write_tools = {"file_write", "file_edit", "write_file", "writeFile", "apply_patch"}
    if policy == "readonly" and tool_name in write_tools:
        return f"Tool {tool_name} is forbidden in readonly sub-agent policy."
    if policy == "readonly" and tool_name in ("docker", "deploy", "workflow",
                                              "cron", "task",
                                              "agent"):
        return f"Tool {tool_name} is forbidden in readonly sub-agent policy."
    if policy == "no_writes" and tool_name in write_tools:
        return f"Tool {tool_name} is forbidden in no_writes sub-agent policy."
    if policy == "no_writes" and tool_name in ("docker", "deploy", "workflow", "agent"):
        return f"Tool {tool_name} is forbidden in no_writes sub-agent policy."
    # bash policy is enforced by the safety engine based on the
    # ``dangerous_command_patterns`` list -- we do not duplicate that
    # check here. The safety engine already returns PermissionAsk or
    # PermissionDeny for write-shaped bash commands.
    return None


async def _run_one_sub_agent(
    parent_loop: Any,
    prompt: str,
    agent_name: str,
    progress_callback: Any = None,
    mode: str = "sync",
    isolated: bool = False,
    cache_context: CacheContext | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run a single sub-agent. Used both by the legacy single-task path
    and by the parallel ``tasks`` path.

    When ``agent_name`` is non-empty, it is resolved against the parent's
    ``config.sub_agents`` list to obtain the ``tool_policy`` and
    ``system_prompt``.  This ensures Explore runs readonly, Plan runs
    no-writes, and general-purpose runs unrestricted.
    """
    logger.info("[agent] agent_name=%s | prompt_len=%d | mode=%s | isolated=%s",
                agent_name, len(prompt), mode, isolated)
    logger.info("[agent] prompt=%.300s", prompt)
    # Resolve agent_name -> tool_policy + system_prompt
    tool_policy = "all"
    system_prompt = ""
    if agent_name and parent_loop is not None:
        sub_agents = getattr(parent_loop.config, "sub_agents", [])
        for sa in sub_agents:
            if sa.name == agent_name:
                tool_policy = getattr(sa, "tool_policy", "all")
                system_prompt = getattr(sa, "system_prompt", "")
                break
    if parent_loop is not None and parent_loop.sub_agent_depth >= MAX_SUB_AGENT_DEPTH:
        return {"content": "Error: Maximum sub-agent recursion depth reached", "messages": []}
    if parent_loop is None:
        from encre.config import EncreConfig
        from encre.loop import EncreLoop
        from encre.session import EncreSession
        config = EncreConfig()
        session = EncreSession(config)
        parent_loop = EncreLoop(config, session)

    # Build cache context from parent if not provided
    if cache_context is None and getattr(parent_loop, "_sys_prompt_cache", None):
        cache_context = CacheContext.from_parent_context(
            parent_loop._sys_prompt_cache,
            parent_session_id=parent_loop.session.id or "",
        )

    # Worktree isolation
    worktree: WorktreeIsolation | None = None
    if isolated:
        ws = getattr(parent_loop, "_workspace_root", "") or os.getcwd()
        worktree = WorktreeIsolation(workspace_root=ws)
        await worktree.__aenter__()

    try:
        # Capture the latest streamed transcript so a timeout can return the
        # partial work instead of an empty placeholder.
        captured: list[dict[str, Any]] = []

        async def _capture_cb(messages: list[dict[str, Any]]) -> None:
            """Capture the sub-agent's latest transcript and forward it upstream.

            Maintains ``captured`` as a shallow copy of the most recent
            snapshot so a timeout can return the partial work instead of an
            empty placeholder. The captured list is intentionally cleared
            before extending so a peer callback can never interleave stale
            messages into the current snapshot.
            """
            if isinstance(messages, list):
                captured.clear()
                captured.extend(messages)
            if progress_callback is not None:
                await progress_callback(messages)

        run_coro = parent_loop._run_sub_agent(
            prompt=prompt,
            system_prompt=system_prompt,
            max_turns=0,
            tool_policy=tool_policy,
            progress_callback=_capture_cb,
            cache_context=cache_context,
        )
        if timeout_seconds and timeout_seconds > 0:
            sub_result = await asyncio.wait_for(run_coro, timeout=timeout_seconds)
        else:
            sub_result = await run_coro
        # Persist planner/architect sub-agent output as a binding architecture
        # contract artifact so the parent stays anchored to the agreed design.
        if isinstance(sub_result, dict):
            content = str(sub_result.get("content", "") or "")
            if agent_name and content.strip():
                try:
                    from encre.contract import is_contract_role, save_architecture_contract
                    if is_contract_role(agent_name):
                        ws = getattr(parent_loop.config, "workspace", "") or ""
                        _saved = save_architecture_contract(
                            ws, agent_name, content, parent_task=prompt,
                        )
                        if _saved:
                            logger.info("[agent] saved %s contract artifact: %s", agent_name, _saved)
                except Exception:
                    logger.warning("[agent] failed to persist %s contract artifact", agent_name, exc_info=True)
            return sub_result
        return {"content": str(sub_result), "messages": []}
    except asyncio.TimeoutError:
        # Wall-clock budget exhausted. Stop the sub-agent and return the
        # partial work it already streamed, mirroring the interrupt path.
        text_parts: list[str] = []
        for m in captured:
            if isinstance(m, dict):
                c = m.get("content", "")
                if c and isinstance(c, str):
                    text_parts.append(c)
        content = "\n".join(text_parts) or f"(timed out after {int(timeout_seconds)}s)"
        logger.info("[agent] sub-agent timed out after %ss; returning partial result", timeout_seconds)
        return {"content": content, "messages": list(captured), "timed_out": True}
    except asyncio.CancelledError:
        # The parent run was stopped: keep whatever the sub-agent already
        # streamed instead of propagating a bare cancellation into the parent.
        raise
    except BaseException as exc:
        # A failure AFTER the sub-agent streamed real work (e.g. contract
        # persistence or worktree teardown) must not discard that work: the
        # UI already showed the transcript, so returning the exception alone
        # makes the parent report a crash for a run that visibly succeeded.
        text_parts: list[str] = []
        for m in captured:
            if isinstance(m, dict):
                c = m.get("content", "")
                if c and isinstance(c, str):
                    text_parts.append(c)
        content = "\n".join(text_parts)
        logger.warning("[agent] sub-agent raised %s: %s", type(exc).__name__, exc, exc_info=True)
        if not content:
            # Nothing usable was produced -- report the failure honestly.
            return {"content": f"Sub-agent failed: {type(exc).__name__}: {exc}", "messages": []}
        # Partial success: lead with the real output, note the failure after it.
        return {
            "content": f"{content}\n\n[sub-agent raised {type(exc).__name__} after producing this output: {exc}]",
            "messages": list(captured),
            "timed_out": False,
        }
    finally:
        if worktree is not None:
            # Teardown must never raise: an exception here would replace the
            # return value above and turn a completed sub-agent run into a
            # parent-visible crash. Log and keep going.
            try:
                if isolated:
                    changed = worktree.sync_back()
                    if changed:
                        logger.info("[agent] worktree sync: %d files changed back", len(changed))
            except Exception:
                logger.warning("[agent] worktree sync_back failed", exc_info=True)
            try:
                await worktree.__aexit__()
            except Exception:
                logger.warning("[agent] worktree __aexit__ failed", exc_info=True)
            try:
                worktree.cleanup()
            except Exception:
                logger.warning("[agent] worktree cleanup failed", exc_info=True)


async def _agent_execute(**kwargs: Any) -> Any:
    """Spawn one or more sub-agents.

    Two calling shapes are supported:

    1. **Single task (legacy / Claude-Code-style)**: pass ``prompt``
       and optionally ``agent_name``. A single sub-agent runs and its
       result is returned as ``{"content": ..., "messages": [...]}``.

    2. **Parallel tasks (Claude Code ``tasks`` parameter)**: pass
       ``tasks`` as a list of ``{"prompt": ..., "agent_name": ...}``
       objects. Each task runs concurrently via ``asyncio.gather`` and
       the results are returned as a list, in the same order. The
       parent sees a single tool result that aggregates the parallel
       outcomes -- matching Claude Code's "fire off three Explore agents
       in parallel" UX.

    **Mode parameter** controls the sub-agent lifecycle:

    - ``"sync"`` (default): the parent awaits the sub-agent inline.
    - ``"async"``: fire-and-forget; returns immediately with a session
      ID.  The sub-agent continues in the background.
    - ``"background"``: auto-detects long-running tasks and switches to
      async mode; otherwise runs sync.
    - ``"isolated"``: runs the sub-agent in a temporary worktree so it
      cannot affect the parent's working directory.
    """
    progress_callback = kwargs.get("progress_callback")
    parent_loop = _resolve_loop()
    mode = kwargs.get("mode", "sync")
    isolated = kwargs.get("isolated", False)

    tasks = kwargs.get("tasks")
    if tasks:
        if not isinstance(tasks, list):
            return {"content": "Error: 'tasks' must be an array of {prompt, agent_name} objects.", "messages": []}

        def _task_label(idx: int) -> str:
            """Human-readable name for the parallel task at ``idx``."""
            t = tasks[idx] if idx < len(tasks) and isinstance(tasks[idx], dict) else {}
            return (t.get("agent_name", "") or "default")

        def _divider(idx: int, status: str = "running") -> dict[str, Any]:
            """Build a task-boundary marker message for the parallel UI.

            The frontend groups every message that follows (up to the next
            divider) under this task and renders a dedicated section header.
            This is display-only metadata carried inside ``sub_agent_messages``
            -- the parent model never sees it (it reads the aggregated
            ``content`` instead).
            """
            return {
                "role": "assistant",
                "content": _task_label(idx),
                "mode": "task_divider",
                "task_index": idx,
                "task_name": _task_label(idx),
                "task_status": status,
                "tool_calls": [],
                "segments": [],
                "created_at": time.time(),
            }

        # Track the latest streamed snapshot per task so the parent's single
        # progress callback can render a combined, live transcript of every
        # parallel sub-agent.  Each sub-agent streams into its own slot; we
        # rebuild the merged view on each update so nothing clobbers a peer.
        latest: dict[int, list[dict[str, Any]]] = {}

        async def _emit_combined() -> None:
            """Emit the merged transcript of all parallel sub-agents.

            Iterates every task slot and emits one divider-plus-transcript
            block per task so the frontend can render a stable card grid.
            Failures of the progress callback are swallowed because progress
            reporting is strictly best-effort -- a broken UI sink must never
            fail the actual sub-agent work.
            """
            if progress_callback is None:
                return
            combined: list[dict[str, Any]] = []
            for i in range(len(tasks)):
                ms = latest.get(i)
                has_started = ms is not None
                # Always include a divider so the frontend can create one agent
                # card per task immediately, before any task produces output.
                status = "running" if has_started and ms else "pending"
                combined.append(_divider(i, status))
                if has_started:
                    combined.extend(ms)
            with contextlib.suppress(Exception):
                await progress_callback(combined)

        # Pre-initialise every task slot with an empty list and emit the
        # dividers immediately so the frontend creates one agent card per task.
        # Tasks only start streaming once they acquire the semaphore; the
        # initial emit gives the user a visual placeholder for every planned
        # sub-agent before any real work begins.
        for i in range(len(tasks)):
            latest[i] = []
        await _emit_combined()

        # Cap concurrency so at most MAX_PARALLEL_SUB_AGENTS tasks run at
        # once; the rest queue until a slot frees. We deliberately avoid
        # `asyncio.Semaphore`/`asyncio.Condition`: on Python 3.11+ they bind
        # their internal loop lazily via get_event_loop(), which under
        # pytest-asyncio / embedded loops can point at a stale loop and
        # silently disable the cap. asyncio is single-threaded and
        # cooperative, so a synchronous check-and-decrement of a shared int
        # is race-free; `asyncio.sleep(0)` (which always uses the *running*
        # loop) yields control so a finished task can free a slot.
        _slots: list[int] = [MAX_PARALLEL_SUB_AGENTS]

        @contextlib.asynccontextmanager
        async def _acquire_slot():
            """Async semaphore replacement that stays bound to the running loop.

            We deliberately avoid ``asyncio.Semaphore`` / ``asyncio.Condition``:
            on Python 3.11+ they bind their internal loop lazily via
            ``get_event_loop()``, which under pytest-asyncio / embedded loops
            can point at a stale loop and silently disable the cap. ``asyncio``
            is single-threaded and cooperative, so a synchronous check-and-
            decrement of a shared int is race-free; ``asyncio.sleep(0)`` (which
            always uses the *running* loop) yields control so a finished task
            can free a slot.
            """
            nonlocal _slots
            while _slots[0] <= 0:
                await asyncio.sleep(0)
            _slots[0] -= 1
            try:
                yield
            finally:
                _slots[0] += 1

        # Build coroutines for each task
        async def _runner(idx: int, t: dict[str, Any]) -> dict[str, Any]:
            """Run one parallel sub-agent task."""
            if not isinstance(t, dict):
                return {"content": "Error: each task must be a dict", "messages": []}

            async def _cb(messages: list[dict[str, Any]], _idx: int = idx) -> None:
                """Stream this sub-agent's snapshot into its slot.

                Forward this sub-agent's live snapshot into its slot and
                re-emit the merged transcript so the UI creates and keeps
                the card populated during the parallel run. The default
                ``_idx`` binds the loop variable at definition time, which
                prevents late-binding capture across the gather.
                """
                latest[_idx] = messages
                await _emit_combined()

            # Queue here when all slots are busy -- the sub-agent only starts
            # (and streams) once it holds a free concurrency slot.
            async with _acquire_slot():
                return await _run_one_sub_agent(
                    parent_loop,
                    t.get("prompt", ""),
                    t.get("agent_name", ""),
                    progress_callback=_cb,
                    timeout_seconds=t.get("timeout_seconds"),
                )
        coros = [_runner(i, t) for i, t in enumerate(tasks)]
        try:
            results = await asyncio.gather(*coros, return_exceptions=True)
        except asyncio.CancelledError:
            # Parent run was interrupted (user stopped).  The gather is
            # cancelled before every task finishes, so build partial
            # results from whatever each task streamed into ``latest``
            # via the progress callback.  This preserves the already-
            # delivered content instead of silently dropping it.
            results = []
            for i in range(len(tasks)):
                msgs = latest.get(i) or []
                text_parts: list[str] = []
                for m in msgs:
                    if isinstance(m, dict):
                        c = m.get("content", "")
                        if c and isinstance(c, str):
                            text_parts.append(c)
                content = "\n".join(text_parts) or "(interrupted)"
                results.append({"content": content, "messages": list(msgs) if msgs else []})
        # Normalize exceptions and derive a per-task status for the UI.
        out: list[dict[str, Any]] = []
        statuses: list[str] = []
        for idx, r in enumerate(results):
            if isinstance(r, BaseException):
                out.append({"content": f"Sub-agent {idx} failed: {type(r).__name__}: {r}", "messages": []})
                statuses.append("error")
            else:
                out.append(r)
                if isinstance(r, dict) and r.get("timed_out"):
                    statuses.append("timeout")
                else:
                    c = str(r.get("content", "")) if isinstance(r, dict) else str(r)
                    statuses.append("error" if c.startswith("Error:") else "done")
        # Format the aggregated result so the parent can read each
        # sub-agent's outcome cleanly, and aggregate the real transcripts so
        # the tool card renders what actually happened instead of an empty
        # "succeeded" placeholder.
        lines = [f"Parallel sub-agent results ({len(out)} tasks):"]
        agg_messages: list[dict[str, Any]] = []
        for idx, r in enumerate(out):
            content = r.get("content", "") if isinstance(r, dict) else str(r)
            name = _task_label(idx)
            lines.append(f"\n--- Task {idx + 1} ({name}) ---\n{content}")
            agg_messages.append(_divider(idx, statuses[idx]))
            if isinstance(r, dict):
                msgs = r.get("messages") or []
                if isinstance(msgs, list):
                    agg_messages.extend(msgs)
        return {"content": "\n".join(lines), "messages": agg_messages, "sub_results": out}

    # Resolve mode: "background" auto-detects if this task should be async
    resolved_mode = mode
    if resolved_mode == "isolated":
        # "isolated" is a worktree-isolation request, not a lifecycle:
        # map it onto the `isolated` flag and run synchronously.
        isolated = True
        resolved_mode = "sync"
    if resolved_mode == "background":
        prompt_text = kwargs.get("prompt", "")
        agent_name = kwargs.get("agent_name", "")
        if detect_background_mode(prompt_text, agent_name):
            resolved_mode = "async"
            logger.info("[agent] auto-switched to async mode for background task agent=%s", agent_name)
        else:
            resolved_mode = "sync"

    if resolved_mode == "async":
        agent_name = kwargs.get("agent_name", "")
        prompt_text = kwargs.get("prompt", "")
        import uuid
        session_id = str(uuid.uuid4())
        cache_ctx = None
        if parent_loop is not None and getattr(parent_loop, "_sys_prompt_cache", None):
            cache_ctx = CacheContext.from_parent_context(
                parent_loop._sys_prompt_cache,
                parent_session_id=parent_loop.session.id or "",
            )

        async def _bg_run():
            try:
                result = await _run_one_sub_agent(
                    parent_loop, prompt_text, agent_name,
                    mode="sync", isolated=isolated, cache_context=cache_ctx,
                    timeout_seconds=kwargs.get("timeout_seconds"),
                )
                _bg_tracker = _get_bg_tracker(parent_loop)
                if _bg_tracker is not None:
                    _bg_tracker.complete(session_id, result=result)
            except Exception as exc:
                _bg_tracker = _get_bg_tracker(parent_loop)
                if _bg_tracker is not None:
                    _bg_tracker.complete(session_id, error=str(exc))

        task = asyncio.create_task(_bg_run())
        _bg_tracker = _get_bg_tracker(parent_loop)
        if _bg_tracker is not None:
            info = _bg_tracker.register(session_id, agent_name, prompt_text)
            _bg_tracker.attach_task(session_id, task)
        return {
            "content": f"[Background sub-agent launched]\nSession ID: {session_id}\nAgent: {agent_name}\nStatus: running\n\nUse agent status tools or check the sub-agents panel for results.",
            "session_id": session_id,
            "messages": [],
            "mode": "async",
        }

    # Capture the latest streamed transcript so a user interrupt can return
    # the partial work instead of an empty placeholder. The wrapper forwards
    # every snapshot to the real progress callback unchanged.
    last_messages: list[dict[str, Any]] = []

    async def _capturing_cb(messages: list[dict[str, Any]]) -> None:
        """Capture the latest streamed snapshot and forward it to the real sink.

        The wrapper forwards every snapshot to the real progress callback
        unchanged while also stashing a shallow copy of the latest list so a
        user interrupt can return the partial work instead of an empty
        placeholder.
        """
        if isinstance(messages, list):
            last_messages.clear()
            last_messages.extend(messages)
        if progress_callback is not None:
            await progress_callback(messages)

    try:
        return await _run_one_sub_agent(
            parent_loop,
            kwargs.get("prompt", ""),
            kwargs.get("agent_name", ""),
            progress_callback=_capturing_cb,
            mode=resolved_mode,
            isolated=isolated,
            timeout_seconds=kwargs.get("timeout_seconds"),
        )
    except asyncio.CancelledError:
        # User interrupted the single sub-agent. Return whatever the
        # progress callback last delivered so the frontend and the parent
        # model preserve the partial work instead of collapsing to an
        # empty "stopped" placeholder.
        text_parts: list[str] = []
        for m in last_messages:
            if isinstance(m, dict):
                c = m.get("content", "")
                if c and isinstance(c, str):
                    text_parts.append(c)
        content = "\n".join(text_parts) or "(interrupted)"
        return {"content": content, "messages": list(last_messages)}


def _agent_to_openai_format(self) -> dict[str, Any]:
    """Convert to OpenAI tool format, appending available sub-agents to the description."""
    agents_block = _build_agents_list()
    description = self.description
    if agents_block:
        description += f"\n\n{agents_block}"
    return {
        "type": "function",
        "function": {
            "name": self.name,
            "description": description,
            "parameters": self.input_schema,
        },
    }


def _agent_to_anthropic_format(self) -> dict[str, Any]:
    """Convert to Anthropic tool format, appending available sub-agents to the description."""
    agents_block = _build_agents_list()
    description = self.description
    if agents_block:
        description += f"\n\n{agents_block}"
    return {
        "name": self.name,
        "description": description,
        "input_schema": self.input_schema,
    }


agent_tool = build_tool(
    name="agent",
    description=(
        "Spawn one or more sub-agents to work on independent sub-tasks in "
        "parallel; each sub-agent runs as a full autonomous session with its "
        "own tool budget and returns an aggregated result.\n\n"
        "WHEN to use: a goal splits into independent workstreams (e.g. research "
        "X while coding Y); you want to fan out exploration across multiple "
        "angles; a long sub-task would otherwise block the main thread.\n"
        "WHEN NOT to use: for a single linear task, do it inline; for a "
        "sequenced pipeline with dependencies use the workflow tool; for "
        "role-specialised multi-agent review use the workflow tool in "
        "'team' mode.\n"
        "TIPS: pass a `tasks` array to run sub-agents concurrently (capped at "
        "4 in flight); each prompt is the COMPLETE instruction -- be specific "
        "about inputs, expected output, and constraints; pick a built-in "
        "agent_name (Explore=readonly, Plan=no-writes) to scope capabilities.\n"
        "PITFALLS: sub-agents cannot spawn further sub-agents (single level of "
        "delegation only); concurrency is per-call, not global.\n"
        "IMPORTANT: All prompts passed to sub-agents MUST be written in "
        "English -- sub-agents think, reason, and respond in English for "
        "reliable state matching and output parsing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Complete task instruction for a single sub-agent. "
                               "MUST be written in English -- the sub-agent's "
                               "thinking, output, and tool calls are all in English "
                               "for reliable parsing. Include all context, inputs, "
                               "and the expected output format. Required for "
                               "single-task mode; ignored when `tasks` is provided.",
            },
            "agent_name": {
                "type": "string",
                "description": "Optional built-in sub-agent name that scopes "
                               "capabilities. Built-ins: Explore (read-only), "
                               "Plan (no writes), general-purpose (full access), "
                               "coder, researcher, critic, architect, planner. "
                               "Omit for an unrestricted general-purpose sub-agent.",
            },
            "mode": {
                "type": "string",
                "enum": ["sync", "async", "background", "isolated"],
                "description": "Execution lifecycle (optional, default 'sync'). "
                               "'sync' waits inline for the result; 'async' "
                               "fire-and-forgets and returns a session ID; "
                               "'background' auto-detects long-running tasks and "
                               "switches to async; 'isolated' runs in a temp "
                               "git worktree so the parent's files are untouched.",
            },
            "isolated": {
                "type": "boolean",
                "description": "When true, run the sub-agent in an isolated "
                               "temporary git worktree so it cannot affect the "
                               "parent's working directory. Changed files are "
                               "synced back on completion (optional, default false).",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Wall-clock budget in seconds for this "
                               "sub-agent. YOU decide this based on task "
                               "complexity: quick lookups 60-300, coding or "
                               "research tasks 600-1800. On timeout the "
                               "sub-agent is stopped and its partial output "
                               "is returned. Omit or 0 for no limit "
                               "(optional).",
            },
            "tasks": {
                "type": "array",
                "description": "Run multiple sub-agents concurrently (max 4 in "
                               "flight; extras queue). Each entry has the same "
                               "`prompt` (required, MUST be in English) and "
                               "optional `agent_name` as the single-task form. "
                               "The parent receives one aggregated result with "
                               "each sub-agent's outcome in submission order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Complete task instruction in English -- the sole input a sub-agent receives.",
                        },
                        "agent_name": {"type": "string", "description": "Optional built-in sub-agent name (see agent_name above)."},
                        "timeout_seconds": {
                            "type": "number",
                            "description": "Optional per-task wall-clock budget in seconds; you decide per task. On timeout the task's partial output is returned. Omit for no limit.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
    },
    execute=_agent_execute,
    intents=["general", "coding", "system"],
    category="delegation",
    triggers=["sub-agent", "spawn agent", "delegate", "parallel task", "subagent"],
    semantic_type="orchestrate",
    cost_level="high",
    retryability="manual",
    safe_fallback="If delegation is not clearly splitting independent work, continue in the main thread and summarize the next concrete step.",
    to_openai_format=_agent_to_openai_format,
    to_anthropic_format=_agent_to_anthropic_format,
)
