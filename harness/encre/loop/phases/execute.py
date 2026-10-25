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

"""Execute phase: run the prepared (and already gated) tool calls.

Extracted verbatim from the original monolithic ``_run_impl``.  Contains the
safe/unsafe split with path-aware write parallelism, pre-write file
snapshots, the cancel-aware parallel execution of concurrency-safe tools
(bounded by the profile's ``parallel_fan_out`` semaphore), and the sequential
execution of unsafe tools with live sub-agent / workflow progress streaming.
"""

import asyncio
import contextlib
import json
import os
import shutil
import threading
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger
from encre.loop.phase_api import LoopPhase, PhaseContext, PhaseResult
from encre.loop.turn_ctx import TurnContext
from encre.recovery import ErrorRecoveryEngine, RetryableExecutor, compute_args_hash
from encre.tools.executor import _is_untrusted_source, _wrap_untrusted_result
from encre.tools.fault import annotate_tool_fault
from encre.tools.runtime import reset_progress_emitter, set_progress_emitter
from encre.tracing import trace_tool_call
from encre.utils.loop_helpers import (
    _ERROR_CASCADE_TOOLS,
    _PROGRESS_POLL_INTERVAL,
    _WRITE_TOOL_NAMES,
    _apply_result_budget,
    _ensure_plan_items,
    _extract_apply_patch_paths,
    _extract_diff_text,
    _extract_file_path,
    _extract_ref_summary,
    _is_idempotent_tool,
    _is_reference_tool,
    _is_write_tool_name,
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


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO8601 string."""
    return datetime.now(UTC).isoformat()


def _undo_backup_name(seq: int, path: str) -> str:
    """Backup file name ``<seq>_<basename>`` inside the session undo dir."""
    base = os.path.basename(path.replace("\\", "/")) or "unnamed"
    return f"{seq:06d}_{base}"


class UndoStack:
    """Explicit LIFO undo stack for write-tool side effects.

    Every successful write-tool execution pushes one record describing how
    to revert the change:

    * ``created``  -- the tool created a new file; undo deletes it.
    * ``modified`` -- the tool overwrote an existing file; undo restores the
      backed-up prior content.
    * ``deleted``  -- the tool removed an existing file; undo recreates it
      from the backup.

    Record schema (one JSONL line in ``<session_dir>/undo/stack.jsonl``):
    ``{ts, tool, path, action, backup_path, content_available, undo_seq}``.
    The stack is loaded from that file on startup so undo keeps working after
    a process restart.  Prior content is backed up to
    ``<session_dir>/undo/<seq>_<basename>``; content larger than
    :attr:`BACKUP_SIZE_LIMIT` is NOT backed up and the record carries
    ``content_available: false`` so undo can report it honestly.  A pending
    capture made *before* the tool runs is turned into a record by
    :meth:`commit` only when the tool succeeded (:meth:`discard` on failure).
    """

    #: Cap for backed-up prior content (oversized content: unavailable marker).
    BACKUP_SIZE_LIMIT = 1024 * 1024

    def __init__(self, session_dir: str | os.PathLike[str]) -> None:
        self._dir = Path(session_dir) / "undo"
        self._stack_path = self._dir / "stack.jsonl"
        self._log_path = self._dir / "undo.jsonl"
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._pending: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self._load()

    # -- Persistence ---------------------------------------------------------

    def _load(self) -> None:
        """Load persisted records and re-sync the backup sequence counter."""
        try:
            with self._stack_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict) and rec.get("path"):
                        self._records.append(rec)
        except FileNotFoundError:
            logger.debug("exception suppressed", exc_info=True)
        except OSError:
            logger.debug("[undo] failed to load undo stack", exc_info=True)
        # The counter continues past both the loaded records and any backup
        # file left on disk (e.g. orphaned by a discarded pending capture).
        seqs = [int(rec.get("undo_seq", 0) or 0) for rec in self._records]
        try:
            for child in self._dir.iterdir():
                head = child.name.split("_", 1)[0]
                if head.isdigit():
                    seqs.append(int(head))
        except OSError:
            logger.debug("exception suppressed", exc_info=True)
        self._counter = max(seqs, default=0)

    def _persist_append(self, record: dict[str, Any]) -> None:
        """Append one record line to ``stack.jsonl`` (best effort)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._stack_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.debug("[undo] failed to persist stack record", exc_info=True)

    def _persist_rewrite(self) -> None:
        """Rewrite ``stack.jsonl`` from memory after a pop (best effort)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._stack_path.with_name(self._stack_path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for rec in self._records:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            os.replace(tmp, self._stack_path)
        except OSError:
            logger.debug("[undo] failed to rewrite stack file", exc_info=True)

    def log_operation(self, entry: dict[str, Any]) -> None:
        """Append one audit line to ``undo.jsonl`` (redo trail, best effort)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.debug("[undo] failed to append undo log", exc_info=True)

    # -- Capture / commit / discard -------------------------------------------

    def capture(self, tool: str, path: str) -> dict[str, Any] | None:
        """Snapshot the pre-write state of ``path`` before a write tool runs.

        Returns a pending token to hand to :meth:`commit` (tool succeeded) or
        :meth:`discard` (tool failed), or ``None`` for an empty path.  A
        second capture for the same path supersedes the previous pending one.
        """
        if not path:
            return None
        with self._lock:
            old = self._pending.pop(path, None)
            if old is not None:
                self._remove_backup(old)
            pending: dict[str, Any] = {
                "tool": tool,
                "path": path,
                "existed": os.path.isfile(path),
                "backup_path": None,
                "content_available": False,
                "undo_seq": 0,
            }
            if pending["existed"]:
                try:
                    if os.path.getsize(path) <= self.BACKUP_SIZE_LIMIT:
                        self._dir.mkdir(parents=True, exist_ok=True)
                        seq = self._counter + 1
                        bpath = self._dir / _undo_backup_name(seq, path)
                        shutil.copyfile(path, bpath)
                        pending["backup_path"] = str(bpath)
                        pending["content_available"] = True
                        pending["undo_seq"] = seq
                        self._counter = seq
                except OSError:
                    logger.debug("[undo] failed to back up %s", path, exc_info=True)
            self._pending[path] = pending
            return pending

    def commit(self, pending: dict[str, Any] | None) -> dict[str, Any] | None:
        """Turn a pending capture into an undo record (tool succeeded).

        The action is derived by comparing the captured pre-state with the
        file system now: ``created`` (absent before, present now),
        ``deleted`` (present before, absent now) or ``modified`` (present
        both times).  Returns the pushed record or ``None`` when there is
        nothing to undo.
        """
        if pending is None:
            return None
        with self._lock:
            path = pending.get("path", "")
            if path and self._pending.get(path) is pending:
                self._pending.pop(path, None)
            existed = bool(pending.get("existed"))
            now_exists = os.path.isfile(path)
            if not existed and not now_exists:
                return None
            action = "created" if not existed else ("deleted" if not now_exists else "modified")
            keep_backup = action in ("modified", "deleted")
            record: dict[str, Any] = {
                "ts": _utc_now_iso(),
                "tool": pending.get("tool", ""),
                "path": path,
                "action": action,
                "backup_path": pending.get("backup_path") if keep_backup else None,
                "content_available": bool(pending.get("content_available")) if keep_backup else False,
                "undo_seq": pending.get("undo_seq") or (self._counter + 1),
            }
            if not keep_backup and record["undo_seq"] > self._counter:
                self._counter = int(record["undo_seq"])
            self._records.append(record)
            self._persist_append(record)
            return record

    def discard(self, pending: dict[str, Any] | None) -> None:
        """Drop a pending capture (tool failed); its backup is removed."""
        if pending is None:
            return
        with self._lock:
            path = pending.get("path", "")
            if path and self._pending.get(path) is pending:
                self._pending.pop(path, None)
        self._remove_backup(pending)

    @staticmethod
    def _remove_backup(pending: dict[str, Any]) -> None:
        """Delete the backup file of a pending capture, if any."""
        bpath = pending.get("backup_path")
        if bpath:
            with contextlib.suppress(OSError):
                os.remove(bpath)

    # -- Pop / restore --------------------------------------------------------

    def pop(self) -> dict[str, Any] | None:
        """Pop the most recent undo record (LIFO), or ``None`` when empty."""
        with self._lock:
            if not self._records:
                return None
            record = self._records.pop()
            self._persist_rewrite()
            return record

    def push_back(self, record: dict[str, Any]) -> None:
        """Re-append a popped record (retryable undo failure)."""
        with self._lock:
            self._records.append(record)
            self._persist_append(record)

    def restore(self, record: dict[str, Any]) -> bool:
        """Revert the filesystem change described by ``record``.

        ``created`` -> delete the file; ``modified``/``deleted`` -> copy the
        backed-up prior content back.  Returns ``False`` when the change
        cannot be reverted (unavailable content, missing backup, I/O error).
        """
        action = record.get("action")
        path = str(record.get("path", ""))
        try:
            if action == "created":
                if os.path.isfile(path):
                    os.remove(path)
                return True
            if action in ("modified", "deleted"):
                if not record.get("content_available"):
                    return False
                bpath = record.get("backup_path")
                if not bpath or not os.path.isfile(bpath):
                    return False
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.copyfile(bpath, path)
                return True
        except OSError:
            logger.debug("[undo] restore failed for %s", path, exc_info=True)
        return False


class PhaseExecuteMixin:
    """Primary tool execution (safe-parallel and unsafe-sequential)."""

    def _write_tool_paths(self, name: str, args: dict[str, Any]) -> list[str]:
        """On-disk paths a write tool is about to touch (from its arguments).

        The raw argument is resolved through the same chain the file tools
        use, so snapshot capture, the undo stack and the rollback commit's
        file states all key on the file that actually gets written -- a
        relative or virtual (``/workspace/``) argument used to key on the
        server's working directory instead, and the rollback then restored
        nothing.

        ``file_write``/``write_file``/``writeFile``/``file_edit`` carry a
        ``file_path`` kwarg; ``apply_patch`` touches every ``old_path`` /
        ``new_path`` pair of its file descriptors.
        """
        from encre.tools.path_guard import resolve_write_target
        from encre.utils.loop_helpers import extract_write_paths

        paths: list[str] = []
        for raw in extract_write_paths(name, args):
            resolved = resolve_write_target(raw, self.session)
            if resolved and resolved not in paths:
                paths.append(resolved)
        return paths

    def _undo_commit_for_tool(
        self,
        name: str,
        args: dict[str, Any],
        pending: dict[str, dict[str, Any]],
    ) -> None:
        """Push undo records for the paths a *successful* write tool touched.

        Each committed record is also recorded in the loop event stream as a
        ``tool_executed`` event carrying the ``undo_seq`` so the event log can
        correlate tool runs with undo entries.
        """
        stack = self.session.ensure_undo_stack()
        for fp in self._write_tool_paths(name, args):
            pend = pending.pop(fp, None)
            if pend is None:
                continue
            record = stack.commit(pend)
            if record is not None:
                # First committed (side-effecting) write of the turn: arm the
                # post-turn write checkpoint in the runner.
                self._writes_this_turn = True
                self._loop_sm.record(
                    "tool_executed",
                    {
                        "tool": name,
                        "path": fp,
                        "action": record.get("action", ""),
                        "undo_seq": record.get("undo_seq", 0),
                    },
                )

    def _undo_discard_for_tool(
        self,
        name: str,
        args: dict[str, Any],
        pending: dict[str, dict[str, Any]],
    ) -> None:
        """Drop pending undo captures after a failed/cancelled write tool."""
        stack = self.session.ensure_undo_stack()
        for fp in self._write_tool_paths(name, args):
            pend = pending.pop(fp, None)
            if pend is not None:
                stack.discard(pend)

    def _idem_key_for(self, name: str, args: dict[str, Any]) -> str:
        """Content-addressed idempotency key for a guarded tool call.

        Only side-effecting write tools are journaled; read-only and
        idempotent-by-nature tools always return an empty key.  The key
        deliberately excludes the turn -- after a crash the turn counter
        moves, so the key must stay stable across restarts.
        """
        if self._idempotency is None or name not in _WRITE_TOOL_NAMES or _is_idempotent_tool(name):
            return ""
        return f"{name}|{compute_args_hash(args)}"

    async def _phase_execute_tools(self, t: TurnContext) -> AsyncGenerator[Any]:
        """Split prepared tools into safe/unsafe groups and execute them.

        Args:
            t: The shared turn context.

        Yields:
            Tool progress/result/end events, artifacts, references and
            plan updates produced by the executed tools.
        """
        # Turn identity for the idempotency journal (audit field only; the
        # guard key above is turn-free by design).
        _turn_key = (
            f"{getattr(self.session, 'id', '') or ''}|{getattr(self.session, 'active_branch_id', '') or ''}|{self.session.turn_count}"
        )
        # ── Split into safe (concurrent) and unsafe (sequential) groups ──
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
        _unsafe_writes = [p for p in unsafe_tools if _is_write_tool_name(p["name"])]
        _unsafe_other = [p for p in unsafe_tools if not _is_write_tool_name(p["name"])]
        _parallel_writes, _sequential_writes = _split_writes_by_path_conflict(_unsafe_writes)
        if _parallel_writes:
            safe_tools = safe_tools + _parallel_writes
            unsafe_tools = _sequential_writes + _unsafe_other

        # ── Capture file snapshots before any tool writes to disk ──
        # Two mechanisms coexist here:
        #   * ``capture_file_snapshot`` feeds checkpoint/rollback restore.
        #   * the explicit LIFO undo stack backs per-write undo: capture the
        #     pre-state BEFORE the tool runs, then commit (push the undo
        #     record) only after the tool succeeds, or discard on failure.
        _undo_stack = self.session.ensure_undo_stack()
        _undo_pending: dict[str, dict[str, Any]] = {}
        for p in safe_tools + unsafe_tools:
            name = p["name"]
            if not _is_write_tool_name(name):
                continue
            for fp in self._write_tool_paths(name, p["args"]):
                self.session.capture_file_snapshot(fp)
                pend = _undo_stack.capture(name, fp)
                if pend is not None:
                    _undo_pending[fp] = pend
        # ── Execute safe tools in parallel ──
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
                    # Load this tool's usage guide *before* the call so the
                    # guidance is in context for the very next step --
                    # including interpreting a failure and retrying.  The
                    # post-call collect below stays as a safety net.
                    await self._collect_tool_skill(p["name"])
                    executor = RetryableExecutor(self.recovery_engine)
                    state = await executor.execute(
                        tool_name=p["name"],
                        tool_args=p["args"],
                        execute_fn=lambda a, p=p: p["tool"].execute(**a),
                        idempotency=self._idempotency,
                        idem_key=self._idem_key_for(p["name"], p["args"]),
                        turn_key=_turn_key,
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
            # of launching every concurrency-safe tool at once.  The active
            # model profile clamps it further (weak models degrade to
            # strictly sequential tool use); the stricter value wins.
            _tool_sem = asyncio.Semaphore(self._model_profile().fan_out_for(self._profile.parallel_fan_out))

            async def _execute_safe_bounded(p: dict[str, Any]) -> dict[str, Any]:
                async with _tool_sem:
                    return await _execute_safe(p)

            # Materialise child tasks so per-tool completion can still be
            # inspected after the gather is cancelled.
            safe_tasks = [asyncio.ensure_future(_execute_safe_bounded(p)) for p in safe_tools]
            # Cancel-aware gather: if the user hits Stop, cancel all
            # in-flight safe tool tasks immediately.
            cancel_watcher = asyncio.create_task(self._cancel_event.wait())
            gather_task = asyncio.ensure_future(asyncio.gather(*safe_tasks, return_exceptions=True))
            done, _pending = await asyncio.wait(
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
                # Distinguish tools that already finished from tools that
                # were still pending: completed results are kept verbatim,
                # only the pending ones are reported as cancelled.
                completed = []
                for task in safe_tasks:
                    if task.cancelled() or not task.done():
                        completed.append(None)
                    else:
                        exc = task.exception()
                        completed.append(exc if exc is not None else task.result())
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
                    # The write never committed a known outcome -> drop the
                    # pending undo captures ("failed tools push nothing").
                    self._undo_discard_for_tool(p["name"], p["args"], _undo_pending)
                    if isinstance(item, BaseException):
                        err_msg = f"Tool execution crashed: {type(item).__name__}: {item}"
                    else:
                        err_msg = "[Cancelled by user]"
                    yield create_tool_result(id=p["client_id"], content=err_msg, is_error=True)
                    self.session.add_tool_result(p["id"], err_msg, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self._error_tool_names.add(p["name"])
                    self.telemetry.record_tool_call(
                        tool_name=p["name"],
                        latency_ms=0.0,
                        success=False,
                        error_message=err_msg,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    continue
                p = item
                p["result"] = _apply_result_budget(
                    p["result"],
                    p["tool"],
                    session_id=self.session.id or "",
                    tool_name=p.get("name", ""),
                )
                # Prompt-injection defense: wrap untrusted-source results
                # (web, MCP family, plugins, ...) in delimiters on the live path.
                if not p["is_error"] and _is_untrusted_source(p["name"]):
                    p["result"] = _wrap_untrusted_result(p["name"], p["result"])
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
                self.session.add_tool_result(
                    p["id"],
                    p["result"],
                    is_error=p["is_error"],
                    sub_agent_messages=p.get("sub_agent_messages"),
                    client_id=p["client_id"],
                )
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"],
                    latency_ms=p["latency_ms"],
                    success=not p["is_error"],
                    error_message=p["result"] if p["is_error"] else "",
                )
                if p["is_error"]:
                    # Failed write tool: drop its pending undo captures.
                    self._undo_discard_for_tool(p["name"], p["args"], _undo_pending)
                    self._error_tool_names.add(p["name"])
                    self.learner.record_error(
                        tool_name=p["name"],
                        error_type="execution_error",
                        context=p["args_summary"],
                        correction="",
                    )
                    # NOTE: no feedback.record_correction here -- a tool
                    # execution error is NOT a user correction.  Recording
                    # the raw error text as a "user correction" polluted
                    # the feedback learner with noise (it then injected
                    # tool error strings into later prompts as if the user
                    # had corrected the agent).  Execution errors go to the
                    # evolution learner's error channel above instead.
                else:
                    # Successful write tool: push its undo records (LIFO).
                    self._undo_commit_for_tool(p["name"], p["args"], _undo_pending)
                    self._error_tool_names.discard(p["name"])
                    self.learner.record_success(
                        tool_name=p["name"],
                        intent=t.prompt[:300],
                        params=p["args"],
                        outcome=p["result"][:500],
                        latency_ms=p["latency_ms"],
                    )
                    if p.get("recovery_history"):
                        correction = ErrorRecoveryEngine.infer_correction_from_history(p["recovery_history"], p["name"])
                        self.learner.record_correction(
                            tool_name=p["name"],
                            error_context=p["args_summary"],
                            correction=correction,
                        )
                self.optimizer.record_outcome(
                    tool_name=p["name"],
                    params=p["args"],
                    success=not p["is_error"],
                    latency_ms=p["latency_ms"],
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                if not p["is_error"]:
                    fp = _extract_file_path(p["name"], p["result"])
                    if fp:
                        if p["name"] == "apply_patch":
                            for ap_path in _extract_apply_patch_paths(p["result"]):
                                entry = self.session.add_artifact(ap_path, p["name"], diff_text="")
                                if entry:
                                    yield Artifact(artifact=entry)
                        else:
                            diff_text = _extract_diff_text(p["name"], p["result"])
                            entry = self.session.add_artifact(fp, p["name"], diff_text=diff_text)
                            if entry:
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
                        for sub_ref in p.get("sub_agent_references") or []:
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

        # ── Execute unsafe tools sequentially ─────────
        _cascade_reason: str | None = None
        for p in unsafe_tools:
            if self._cancelled():
                break
            if _cascade_reason is not None:
                # Sibling error cascade (converged from the retired
                # ToolPipeline): a failed bash-family command invalidates the
                # context of the remaining calls; tombstone them so every
                # tool_use still receives a tool_result.
                _cascade_err = f"Tool execution was cancelled: {_cascade_reason}"
                self.session.add_tool_result(
                    p["id"],
                    _cascade_err,
                    is_error=True,
                    client_id=p.get("client_id", ""),
                )
                yield create_tool_result(id=p["client_id"], content=_cascade_err, is_error=True)
                t.turn_events += 1
                self._error_tool_names.add(p["name"])
                self.telemetry.record_tool_call(
                    tool_name=p["name"],
                    latency_ms=0.0,
                    success=False,
                    error_message=_cascade_err,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                continue
            tool_start = time.time()
            yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")

            tool_error = False
            sub_agent_messages = None
            sub_agent_session_id = None
            sub_agent_references: list[dict[str, Any]] = []
            try:
                # Load this tool's usage guide before the call (see the safe
                # path in ``_execute_safe`` for the rationale).
                await self._collect_tool_skill(p["name"])
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
                            {get_task, _agent_cancel},
                            return_when=asyncio.FIRST_COMPLETED,
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
                            # Structured handoff consumption via the single
                            # shared consumer (mirrors the tracked executor path).
                            from encre.loop_sub_agent import apply_sub_agent_handoff

                            # ``self`` is the live EncreLoop here: this mixin
                            # method runs bound to the loop, not the adapter.
                            result = apply_sub_agent_handoff(result, result_obj.get("handoff"), sub_agent_session_id, self)
                        else:
                            result = str(result_obj)
                        result = self.safety.validate_tool_output(p["name"], result)
                    # On cancellation ``result`` is already the tombstone
                    # "[Cancelled by user]" set above; ``result_obj`` is a
                    # sentinel (never awaited) and must stay untouched.
                    else:
                        result_obj = None
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
                            {get_task, _wf_cancel},
                            return_when=asyncio.FIRST_COMPLETED,
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
                    # The tool-progress emitter is wired before the task is
                    # created so the contextvar propagates into it: tools such
                    # as persistent terminal sessions push incremental output
                    # chunks that are drained below and forwarded as progress
                    # events (converged from the retired ToolPipeline).
                    _progress_chunks: list[str] = []

                    def _on_tool_progress(chunk: str, _chunks: list[str] = _progress_chunks) -> None:
                        _chunks.append(chunk)

                    _emitter_token = set_progress_emitter(_on_tool_progress)
                    try:
                        exec_task = asyncio.create_task(
                            executor.execute(
                                tool_name=p["name"],
                                tool_args=p["args"],
                                execute_fn=lambda args, p=p: p["tool"].execute(**args),
                                idempotency=self._idempotency,
                                idem_key=self._idem_key_for(p["name"], p["args"]),
                                turn_key=_turn_key,
                            )
                        )
                        cancel_task = asyncio.create_task(self._cancel_event.wait())
                        self._tool_stop_event.clear()
                        stop_task = asyncio.create_task(self._tool_stop_event.wait())
                        done = set()
                        while True:
                            done, _pending = await asyncio.wait(
                                {exec_task, cancel_task, stop_task},
                                timeout=_PROGRESS_POLL_INTERVAL,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if _progress_chunks:
                                yield create_tool_progress(
                                    id=p["client_id"],
                                    tool_name=p["name"],
                                    status="running",
                                    content="".join(_progress_chunks),
                                )
                                _progress_chunks.clear()
                            if done:
                                break
                    finally:
                        reset_progress_emitter(_emitter_token)
                    cancel_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_task
                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                    if stop_task in done and exec_task not in done:
                        # Per-tool stop (not a whole-turn cancel): abort the
                        # tool and surface a normal error result so the turn
                        # continues.
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        result = "Tool stopped by user."
                        tool_error = True
                        self._undo_discard_for_tool(p["name"], p["args"], _undo_pending)
                        self.session.add_tool_result(
                            p["id"],
                            result,
                            is_error=True,
                            client_id=p.get("client_id", ""),
                        )
                        yield create_tool_result(id=p["client_id"], content=result, is_error=True)
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        continue
                    if exec_task in done:
                        state = exec_task.result()
                    else:
                        # Cancelled: abort the tool execution
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        result = "[Cancelled by user]"
                        tool_error = True
                        self._undo_discard_for_tool(p["name"], p["args"], _undo_pending)
                        self.session.add_tool_result(
                            p["id"],
                            result,
                            is_error=True,
                            client_id=p.get("client_id", ""),
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
                result,
                p["tool"],
                session_id=self.session.id or "",
                tool_name=p.get("name", ""),
            )
            # Plugin faults grow a model-facing tail: the full error stays
            # verbatim, plus the identities and the manage() recovery path.
            if tool_error:
                result = annotate_tool_fault(result, tool_name=p.get("name", ""), is_error=True)
            # Prompt-injection defense: wrap untrusted-source results
            # (web, MCP family, plugins, ...) in delimiters on the live path.
            if not tool_error and _is_untrusted_source(p["name"]):
                result = _wrap_untrusted_result(p["name"], result)
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
                tool_name=p["name"],
                latency_ms=tool_latency,
                success=not tool_error,
                error_message=result if tool_error else "",
            )
            if tool_error:
                # Failed write tool: drop its pending undo captures.
                self._undo_discard_for_tool(p["name"], p["args"], _undo_pending)
                self._error_tool_names.add(p["name"])
                self.learner.record_error(
                    tool_name=p["name"],
                    error_type="execution_error",
                    context=p["args_summary"],
                    correction="",
                )
                if self.feedback is not None:
                    self.feedback.record_correction(
                        tool_name=p["name"],
                        error_type="execution_error",
                        error_context=p["args_summary"],
                        user_correction=result[:400],
                    )
            else:
                # Successful write tool: push its undo records (LIFO).
                self._undo_commit_for_tool(p["name"], p["args"], _undo_pending)
                self._error_tool_names.discard(p["name"])
                self.learner.record_success(
                    tool_name=p["name"],
                    intent=t.prompt[:300],
                    params=p["args"],
                    outcome=result[:500],
                    latency_ms=tool_latency,
                )
            self.optimizer.record_outcome(
                tool_name=p["name"],
                params=p["args"],
                success=not tool_error,
                latency_ms=tool_latency,
            )
            yield create_tool_call_end(id=p["client_id"])
            t.turn_events += 1
            if tool_error and p["name"] in _ERROR_CASCADE_TOOLS:
                # Trigger the sibling cascade for the remaining unsafe tools.
                _cascade_reason = f"sibling error: {p['name']} failed"
            if not tool_error:
                fp = _extract_file_path(p["name"], result)
                if fp:
                    if p["name"] == "apply_patch":
                        for ap_path in _extract_apply_patch_paths(result):
                            entry = self.session.add_artifact(ap_path, p["name"], diff_text="")
                            if entry:
                                yield Artifact(artifact=entry)
                    else:
                        diff_text = _extract_diff_text(p["name"], result)
                        entry = self.session.add_artifact(fp, p["name"], diff_text=diff_text)
                        if entry:
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
