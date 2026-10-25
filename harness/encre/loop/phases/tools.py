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
import contextlib
import fnmatch
import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from encre.logging_config import get_logger
from encre.loop.phase_api import LoopPhase, PhaseContext, PhaseResult
from encre.loop.state import LoopState as LoopRunState
from encre.loop.turn_ctx import TurnContext
from encre.recovery import ErrorRecoveryEngine, RetryableExecutor
from encre.tools.executor import (
    _is_untrusted_source,
    _wrap_untrusted_result,
    validate_tool_args,
)
from encre.tools.fault import annotate_tool_fault
from encre.tools.runtime import (
    pop_pending_tool_images,
    reset_current_tool_call_id,
    reset_progress_emitter,
    set_current_tool_call_id,
    set_progress_emitter,
)
from encre.utils.loop_helpers import (
    _ERROR_CASCADE_TOOLS,
    _PROGRESS_POLL_INTERVAL,
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
    _is_write_tool_name,
    _permission_reason,
    _tool_retry_allowed,
)
from encre.utils.types import (
    Artifact,
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
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

# --- Permission rule engine helpers ----------------------------------------
#
# The structured ``permissions.rules`` configuration is evaluated BEFORE
# the mode-aware engine / classifier check.  Decision sources recorded
# for audit: ``rule_exact`` | ``rule_wildcard`` | ``default_effect`` |
# ``classifier`` | ``fallback_ask``.

#: Argument keys consulted (in order) when extracting a tool call's
#: primary string argument for permission patterns.  Mirrors the
#: selection the safety engine uses for its own checks (bash ->
#: ``command``, file tools -> ``path``/``file_path``).
_PERMISSION_PRIMARY_ARG_KEYS: tuple[str, ...] = (
    "command",
    "path",
    "file_path",
    "url",
    "query",
    "code",
    "pattern",
    "text",
    "name",
)


def _normalize_rule_effect(effect: str) -> str:
    """Map a configured rule effect onto ``allow`` / ``ask`` / ``deny``."""
    return effect if effect in ("allow", "ask", "deny") else "ask"


def _has_pattern_wildcard(text: str) -> bool:
    """Whether *text* contains fnmatch wildcard characters."""
    return any(ch in text for ch in "*?[")


def _tool_primary_arg(args: dict[str, Any]) -> str:
    """Return the tool call's primary string argument for rule patterns."""
    for key in _PERMISSION_PRIMARY_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    for value in args.values():
        if isinstance(value, str) and value:
            return value
    return ""


def _rule_pattern_matches(pattern: str, tool_name: str, primary: str) -> bool:
    """Match a rule pattern against a tool invocation (fnmatch semantics).

    The canonical match target is ``"<tool>(<primary_arg>)"``, so
    ``Bash(git *)`` matches a Bash call whose command starts with
    ``"git "``.  A pattern without parentheses additionally matches the
    primary argument alone, so ``git *`` works too.
    """
    if fnmatch.fnmatchcase(f"{tool_name}({primary})", pattern):
        return True
    return "(" not in pattern and bool(primary) and fnmatch.fnmatchcase(primary, pattern)


class PhaseToolsMixin:
    """Tool preparation, gating and secondary intra-turn execution."""

    def _evaluate_permission_rules(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[str, str, int, str] | None:
        """Evaluate the structured permission rules for one tool call.

        Evaluation order: fully-literal rules first (``rule_exact``),
        then wildcard rules in configuration order (``rule_wildcard``),
        then the configured ``default_effect`` (``allow`` / ``deny``
        decide; ``ask`` defers to the engine/classifier path).

        Returns:
            ``(effect, source, rule_index, matched_rule)`` or ``None``
            when no rule decided and the call must fall through to the
            engine/classifier check.  *matched_rule* is a human-readable
            description of the winning rule (empty when none matched).
        """
        rules, default_effect = self.config.get_permission_rules()
        if not rules and default_effect not in ("allow", "deny"):
            return None
        primary = _tool_primary_arg(args)
        # 1) Exact rules: fully literal tool + pattern beat wildcards.
        for idx, rule in enumerate(rules):
            if _has_pattern_wildcard(rule.tool) or _has_pattern_wildcard(rule.pattern):
                continue
            if rule.tool != tool_name:
                continue
            target = f"{tool_name}({primary})"
            if rule.pattern == target or (bool(primary) and rule.pattern == primary):
                desc = f"rules[{idx}] {rule.tool}:{rule.pattern}"
                return _normalize_rule_effect(rule.effect), "rule_exact", idx, desc
        # 2) Wildcard rules: first match in configuration order wins.
        for idx, rule in enumerate(rules):
            if not fnmatch.fnmatchcase(tool_name, rule.tool or "*"):
                continue
            if _rule_pattern_matches(rule.pattern or "*", tool_name, primary):
                desc = f"rules[{idx}] {rule.tool}:{rule.pattern}"
                return _normalize_rule_effect(rule.effect), "rule_wildcard", idx, desc
        # 3) Default effect: allow/deny decide here, ask defers.
        if default_effect in ("allow", "deny"):
            return default_effect, "default_effect", -1, ""
        return None

    async def _phase_prepare_tools(self, t: TurnContext) -> AsyncGenerator[Any, None]:
        """Prepare and gate the turn's primary tool calls.

        Args:
            t: The shared turn context.

        Yields:
            Tool start/progress/result/end events, permission and question
            requests, and spec-block system messages.
        """
        # ── Prepare tool calls: parse args, resolve tools, categorize ──
        # NOTE: client-facing events use a per-run synthetic id
        # ("call_{t.tool_id_token}_{idx}") so they match the ids already
        # emitted on tool_call_delta events.  The token is opaque and unique
        # per run (see TurnContext), so ids never repeat across turns the way
        # the old session.turn_count base did once it was reset by a reload or
        # recomputed by a message edit.  Internal session/history/telemetry
        # continues to use the real backend id (tc["id"]). Without this split,
        # the UI would create one stub entry from the deltas (call_x) and a
        # second entry from tool_call_start (real id), rendering each tool call
        # twice.

        # --- Spec approval gate -------------------------------------------------
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
                    "Specification is pending approval. Write tools are blocked. Wait for user to approve the spec before implementing.",
                    kind="spec",
                )
        t.prepared = []
        for idx in sorted(t.tool_call_buffers.keys()):
            tc = t.tool_call_buffers[idx]
            client_id = f"call_{t.tool_id_token}_{idx}"
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
                        tool_name=tc["name"],
                        latency_ms=0,
                        success=False,
                        error_message=err_msg,
                    )
                    yield create_tool_call_end(id=client_id)
                    t.turn_events += 1
                    t.prepared.append(
                        {
                            "id": tc["id"],
                            "client_id": client_id,
                            "name": tc["name"],
                            "args": args,
                            "tool": None,
                            "skip": True,
                            "error": err_msg,
                        }
                    )
                    continue
            else:
                args = {}

            tool = self.tool_registry.get(tc["name"])
            if tool is None:
                err_msg = f"Error: Unknown tool: {tc['name']}"
                if self._model_profile().is_light:
                    # Weak models keep re-emitting the invented call; spell
                    # out the single allowed next action in one short line.
                    err_msg += (
                        ". STOP calling this tool -- it does not exist. Choose "
                        "one tool from the Available Tools list, or answer in text."
                    )
                yield create_tool_result(id=client_id, content=err_msg, is_error=True)
                self.session.add_tool_result(tc["id"], err_msg, is_error=True, client_id=client_id)
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=tc["name"],
                    latency_ms=0,
                    success=False,
                    error_message=err_msg,
                )
                yield create_tool_call_end(id=client_id)
                t.turn_events += 1
                t.prepared.append(
                    {"id": tc["id"], "client_id": client_id, "name": tc["name"], "args": args, "tool": None, "skip": True, "error": err_msg}
                )
                continue

            # --- Argument schema validation (pre-execution) -----------------
            # Validate the model-emitted arguments against the registry's
            # JSON Schema before gating/execution.  Failures return a
            # structured error immediately -- no LLM repair, no retry.
            _schema_err = validate_tool_args(getattr(tool, "input_schema", None), args)
            if _schema_err:
                err_msg = f"Error: invalid arguments for tool '{tc['name']}': {_schema_err}"
                if self._model_profile().is_light:
                    err_msg += (
                        f". Fix the arguments for `{tc['name']}` and call it again "
                        "with ONLY the fields shown in its schema."
                    )
                yield create_tool_result(id=client_id, content=err_msg, is_error=True)
                self.session.add_tool_result(tc["id"], err_msg, is_error=True, client_id=client_id)
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=tc["name"],
                    latency_ms=0,
                    success=False,
                    error_message=err_msg,
                )
                yield create_tool_call_end(id=client_id)
                t.turn_events += 1
                t.prepared.append(
                    {"id": tc["id"], "client_id": client_id, "name": tc["name"], "args": args, "tool": tool, "skip": True, "error": err_msg}
                )
                continue

            # ── Spec approval gate: block write tools ───────────
            # In spec mode, if the spec hasn't been approved yet,
            # block all write-class tools (file_write, file_edit,
            # apply_patch, bash, etc.).  Read-only tools are allowed
            # so the model can still gather context.
            _spec_block = False
            if (
                t.slash_command_mode == "spec"
                and self.spec_engine is not None
                and (tc["name"] in _WRITE_TOOL_NAMES or tc["name"] == "bash")
            ):
                # Markdown artifacts are the spec phase's deliverable: writing
                # them stays allowed pre-approval (submit_review presents them).
                _md_artifact = tc["name"] in ("file_write", "write_file", "writeFile", "file_edit") and str(
                    args.get("file_path", "") or ""
                ).lower().endswith(".md")
                if not _md_artifact:
                    # An approved standing spec goal also opens the gate: the
                    # spec engine's status is in-memory and is lost on reload,
                    # so without it a resumed objective would block itself.
                    t.spec_approved = self._plan_mode.spec_write_allowed(self.spec_engine)
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
                    tool_name=tc["name"],
                    latency_ms=0,
                    success=False,
                    error_message=_spec_msg,
                )
                yield create_tool_call_end(id=client_id)
                t.turn_events += 1
                t.prepared.append(
                    {
                        "id": tc["id"],
                        "client_id": client_id,
                        "name": tc["name"],
                        "args": args,
                        "tool": tool,
                        "skip": True,
                        "error": _spec_msg,
                    }
                )
                continue

            _safety = getattr(tool, "is_concurrency_safe", None)
            # Third-party plugin tools may not implement the full protocol;
            # default to NOT concurrency-safe (serial execution) so a
            # missing method can never produce a data race.
            is_safe = _safety(args) if callable(_safety) else False
            semantics = _infer_tool_semantics(tc["name"], tool)
            _ts = dict(self._state_mgr.tool_semantics)
            _ts[tc["name"]] = semantics
            self._state_mgr.tool_semantics = _ts
            t.prepared.append(
                {
                    "id": tc["id"],
                    "client_id": client_id,
                    "name": tc["name"],
                    "args": args,
                    "tool": tool,
                    "skip": False,
                    "safe": is_safe,
                    "args_summary": _args_summary(args),
                    "semantics": semantics,
                }
            )

        # A tool pre-executed during streaming is consumed below and never
        # reaches the execute phase, so it would otherwise never receive a
        # "running" progress frame -- the client's strip sat on "pending" for
        # the whole (possibly long) command and only the terminal stop control
        # exposed it.  Announce "running" here, before the drain below blocks
        # for the entire execution, so the control appears as soon as the tool
        # starts instead of only after a session reload forces the status.
        for _pre_p in t.prepared:
            _pre_cid = _pre_p.get("client_id", "")
            if (
                _pre_cid
                and _pre_cid in self._streaming_tool_clients
                and _pre_cid not in self._streaming_tool_results
            ):
                yield create_tool_progress(id=_pre_cid, tool_name=_pre_p["name"], status="running")

        # Tag tools that were pre-executed during streaming so the permission
        # and execution phases can skip re-execution.  Drain the background
        # pre-execution tasks first: without this a still-running task and
        # the sequential path below could both execute the same tool.
        if self._streaming_tool_tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*list(self._streaming_tool_tasks), return_exceptions=True)
            self._streaming_tool_tasks.clear()
        if self._streaming_tool_results:
            for _pre_p in t.prepared:
                if _pre_p["client_id"] in self._streaming_tool_results:
                    _pre_p["pre_executed"] = True

        next_stage = self._infer_task_stage(t.prompt, t.prepared)
        self._set_task_stage(next_stage, reason="tool preparation")
        self._refresh_working_set(t.prompt, t.prepared)

        # Permission & hooks for all tools (sequential -- may need user input)
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
                retry_msg = "Blocked repeated high-risk tool retry. " + p.get("semantics", {}).get(
                    "safe_fallback", "Gather more context before retrying."
                )
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="blocked")
                yield create_tool_result(id=p["client_id"], content=retry_msg, is_error=True)
                self.session.add_tool_result(p["id"], retry_msg, is_error=True, client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"],
                    latency_ms=0,
                    success=False,
                    error_message=retry_msg,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                p["skip"] = True
                p["error"] = retry_msg
                continue
            # Rule engine first: exact rules -> wildcard rules ->
            # default_effect -> engine/classifier -> fallback ask.
            _rule_hit = self._evaluate_permission_rules(p["name"], p["args"])
            _perm_rule_idx = -1
            perm_matched = ""
            if _rule_hit is not None:
                _rule_effect, perm_source, _perm_rule_idx, perm_matched = _rule_hit
                if _rule_effect == "allow":
                    permission = PermissionAllow(reason="Allowed by permission rule.")
                elif _rule_effect == "deny":
                    permission = PermissionDeny(reason=f"Denied by permission rule {perm_matched}.")
                else:
                    permission = PermissionAsk(reason=f"Confirmation required by permission rule {perm_matched}.")
            else:
                try:
                    permission = await self.safety.check_tool_permission(p["name"], p["args"])
                    perm_source = "classifier"
                except Exception as _perm_exc:
                    # Degrade to ask: a crashed permission check must never
                    # translate into silent approval.
                    logger.error("Permission check failed for tool %s: %s", p["name"], _perm_exc)
                    permission = PermissionAsk(reason="Permission check unavailable; confirm to proceed.")
                    perm_source = "fallback_ask"
            # Destructive escalation (converged from the retired ToolPipeline):
            # in guarded modes, a destructive tool the engine auto-allowed
            # still prompts the user.  The Rust engine only knows about bash
            # patterns, not about tools like file_write, deploy, git_tool...
            _tool_obj = p.get("tool")
            if (
                permission.behavior == "allow"
                and _tool_obj is not None
                and callable(getattr(_tool_obj, "is_destructive", None))
                and _tool_obj.is_destructive(p["args"])
                and str(getattr(self.config, "permission_mode", "default") or "default") not in ("bypass", "auto", "plan", "spec")
            ):
                permission = PermissionAsk(reason=f"Destructive operation: {p['name']}. Confirm to proceed.")
            # Decision-source audit: record which layer decided and the
            # final effect (destructive escalation may have turned an
            # allow into an ask) -- telemetry + loop event stream.
            self.telemetry.record_permission_decision(
                p["name"],
                effect=str(permission.behavior),
                source=perm_source,
                matched_rule=perm_matched,
            )
            self._loop_sm.record(
                "permission_decision",
                {
                    "tool": p["name"],
                    "source": perm_source,
                    "effect": str(permission.behavior),
                    "rule_index": _perm_rule_idx,
                },
            )
            if permission.behavior == "deny":
                deny_reason = getattr(permission, "reason", "") or _permission_reason(p["name"]) or "Permission denied by policy."
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="denied")
                yield create_tool_result(id=p["client_id"], content=deny_reason, is_error=True)
                self.session.add_tool_result(p["id"], deny_reason, is_error=True, client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"],
                    latency_ms=0,
                    success=False,
                    error_message=deny_reason,
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
                permission_reason = getattr(permission, "reason", "") or _permission_reason(p["name"])
                await self.hook_system.emit_permission_request(p["name"], permission_reason)
                yield create_permission_request(
                    tool_name=p["name"],
                    reason=permission_reason,
                )
                permission_granted = await self._request_permission(p["name"])
                await self.hook_system.emit_permission_response(p["name"], permission_granted)
                if not permission_granted:
                    err_msg = "Permission denied by user."
                    yield create_tool_result(id=p["client_id"], content=err_msg, is_error=True)
                    self.session.add_tool_result(p["id"], err_msg, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=p["name"],
                        latency_ms=0,
                        success=False,
                        error_message=err_msg,
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
                    tool_name=p["name"],
                    latency_ms=0,
                    success=False,
                    error_message=block_reason,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                p["skip"] = True
                p["error"] = block_reason
                continue
            if pre_hook and pre_hook.get("modified_input"):
                # Partial-override semantics: merge the handler's keys on
                # top of the original args -- a handler that only tweaks
                # one field must not erase the rest of the tool input.
                p["args"] = {**p["args"], **pre_hook["modified_input"]}

            # Bash gate: a dedicated veto/rewrite point for shell commands.
            # Plugins may block a raw command outright or rewrite it before
            # execution -- deeper than generic pre_tool_exec because the
            # guarded surface is the command STRING, not the arg dict.
            if p["name"] == "bash" and not p.get("skip"):
                _bash_cmd = p["args"].get("command", "") if isinstance(p["args"], dict) else ""
                _pre_bash = await self.hook_system.emit_pre_bash(_bash_cmd)
                if _pre_bash and _pre_bash.get("block"):
                    _bash_reason = _pre_bash.get("block_reason") or "Blocked by hook: bash"
                    yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="blocked")
                    yield create_tool_result(id=p["client_id"], content=_bash_reason, is_error=True)
                    self.session.add_tool_result(p["id"], _bash_reason, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=p["name"],
                        latency_ms=0,
                        success=False,
                        error_message=_bash_reason,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    p["skip"] = True
                    p["error"] = _bash_reason
                    continue
                _bash_mi = (_pre_bash or {}).get("modified_input") or {}
                if isinstance(p["args"], dict) and isinstance(_bash_mi.get("command"), str) and _bash_mi["command"]:
                    p["args"]["command"] = _bash_mi["command"]

            # ── Plan mode interception: propose before executing ──
            if self.plan_mode_active:
                proposal_emitted = False
                async for _event in self._intercept_plan_mode(
                    p["name"],
                    p["args"],
                    p["id"],
                    p["client_id"],
                ):
                    proposal_emitted = True
                    yield _event
                    t.turn_events += 1
                if proposal_emitted and not self._plan_decision:
                    # User did not approve (rejected or timed out).  Feed a
                    # synthetic error result back to the model so it can
                    # adjust its plan without leaving the tool call hanging.
                    if self._plan_decision_timed_out:
                        plan_err = "Plan approval timed out with no decision. Proceed carefully, or present a smaller, clearer proposal."
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
                        tool_name=p["name"],
                        latency_ms=0,
                        success=False,
                        error_message=plan_err,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    p["skip"] = True
                    p["error"] = plan_err
                    continue

            # ── submit_review: block until the user decides on a review ──
            if p["name"] == "submit_review":
                mode_now = t.slash_command_mode
                review = (
                    self._build_review_from_args(p["args"], mode_now, p["client_id"])
                    if mode_now in ("plan", "spec")
                    else None
                )
                if review is None:
                    logger.warning(
                        "[review] submit_review produced no card: mode=%s args=%r",
                        mode_now, p["args"],
                    )
                    result = (
                        "Error: submit_review requires plan or spec mode and at "
                        "least one existing artifact file. Write the files "
                        "first, then submit with their real paths."
                    )
                    yield create_tool_result(id=p["client_id"], content=result, is_error=True)
                    self.session.add_tool_result(p["id"], result, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=p["name"], latency_ms=0, success=False, error_message=result,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    p["skip"] = True
                    p["error"] = result
                    continue
                # Projected to the `plan_review` WS frame by events.wire --
                # the desktop renders the review card from it.
                logger.info(
                    "[review] card shown: review=%s call=%s mode=%s files=%d",
                    review.review_id, p["client_id"], mode_now, len(review.files),
                )
                yield create_system_message(
                    f"__plan_review__:{json.dumps(review.__dict__, ensure_ascii=False)}",
                    kind="plan",
                )
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")
                self._review_event = asyncio.Event()
                self._review_decision = None
                self._transition_loop(LoopRunState.WAITING_QUESTION, {"tool": "submit_review"})
                try:
                    await asyncio.wait_for(self._review_event.wait(), timeout=1800.0)
                except TimeoutError:
                    self._review_decision = (
                        False, "The review request timed out; the user has not decided yet.",
                    )
                    # Nothing waits on the card any more -- retire it, or it
                    # keeps offering a decision that goes nowhere.
                    _dead = self._cancel_pending_review()
                    if _dead is not None:
                        yield create_system_message(
                            f"__plan_review__:{json.dumps(_dead, ensure_ascii=False)}",
                            kind="plan",
                        )
                finally:
                    self._transition_loop(LoopRunState.RUNNING, {"tool": "submit_review"})
                self._review_event = None
                approved, feedback = self._review_decision or (False, "")
                logger.info(
                    "[review] resolved: review=%s approved=%s feedback=%r",
                    review.review_id, approved, feedback[:80],
                )
                if approved:
                    result = (
                        "Review approved by the user. Proceed with "
                        "implementation exactly as specified in the reviewed files."
                    )
                else:
                    # The card's 取消 carries no free text: the model is told
                    # the plan was rejected and must go ask the user what has
                    # to change, so the follow-up lands in the transcript as
                    # a question instead of a silent guess-and-resubmit.
                    result = (
                        "Review rejected by the user."
                        + (f" Feedback: {feedback}" if feedback else "")
                        + " Do NOT revise the artifacts on your own: ask the "
                        "user what must change so the plan matches their "
                        "requirements (the question tool), then update the "
                        "files and call submit_review again."
                    )
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

            # ── Question tool: block until user answers ──
            if p["name"] == "question":
                args = p["args"]
                questions_list: list[dict[str, Any]] = []
                questions_raw = args.get("questions")
                if isinstance(questions_raw, str):
                    # Narrow suppress: a malformed JSON string is recoverable
                    # (the raw text is kept), but KeyboardInterrupt and other
                    # BaseExceptions must propagate.
                    with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
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
                    tool_call_id=p["client_id"],
                    questions=questions_list,
                )
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")
                self._question_event = asyncio.Event()
                self._question_answers = ""
                # Loop runtime state: the question tool blocks the turn on
                # user input -- track that window explicitly.
                self._transition_loop(LoopRunState.WAITING_QUESTION, {"tool": "question"})
                try:
                    await asyncio.wait_for(self._question_event.wait(), timeout=300.0)
                except TimeoutError:
                    self._question_answers = "Error: Question timed out."
                finally:
                    self._transition_loop(LoopRunState.RUNNING, {"tool": "question"})
                self._question_event = None
                _qa = self._question_answers
                # Batched ask: the client returned one answer per question (in
                # ask order).  Flatten to one Q-block per question — the
                # question text on the Q-line and the indented answer beneath —
                # so both the model and the collapsed UI strip can align each
                # answer with the question it belongs to.
                if isinstance(_qa, list):
                    result = "\n".join(
                        f"Q{i + 1}: {q.get('question', '')}\n    {a}"
                        for i, (q, a) in enumerate(zip(questions_list, _qa, strict=False))
                    )
                elif _qa.startswith("Error"):
                    # Timeout / failure text stays as-is; wrapping an error into
                    # a Q-block would imply the user answered.
                    result = _qa
                elif questions_list:
                    # Single ask: same Q-block shape as batched asks so the
                    # collapsed strip and the expandable body stay uniform.
                    result = f"Q1: {questions_list[0].get('question', '')}\n    {_qa}"
                else:
                    result = _qa
                yield create_tool_result(id=p["client_id"], content=result)
                self.session.add_tool_result(p["id"], result, client_id=p["client_id"])
                t.turn_events += 1
                self.telemetry.record_tool_call(
                    tool_name=p["name"],
                    latency_ms=0,
                    success=True,
                )
                yield create_tool_call_end(id=p["client_id"])
                t.turn_events += 1
                p["skip"] = True
                p["result"] = result
                continue

        # ── Yield results for pre-executed (streaming) tools ─────────
        for p in list(t.prepared):
            if not p.get("pre_executed"):
                continue
            pr = self._streaming_tool_results[p["client_id"]]
            _pre_result = _apply_result_budget(
                pr["result"],
                p["tool"],
                session_id=self.session.id or "",
                tool_name=p.get("name", ""),
            )
            # Prompt-injection defense: wrap untrusted-source results
            # (web, MCP family, plugins, ...) in delimiters on the live path.
            if not pr["is_error"] and _is_untrusted_source(p["name"]):
                _pre_result = _wrap_untrusted_result(p["name"], _pre_result)
            yield create_tool_result(
                id=p["client_id"],
                content=_pre_result,
                is_error=pr["is_error"],
            )
            self.session.add_tool_result(
                p["id"],
                _pre_result,
                is_error=pr["is_error"],
                client_id=p["client_id"],
            )
            t.turn_events += 1
            self.telemetry.record_tool_call(
                tool_name=p["name"],
                latency_ms=pr["latency_ms"],
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
                    if _entry:
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
        # ── Intra-turn split: merge post-tool content into existing assistant message ──
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
                            extra_client_id = f"call_{t.tool_id_token}_extra_{idx}"
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
                    for etc in extra_tc if t.extra_buffers else []:
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
                client_id = f"call_{t.tool_id_token}_extra_{idx}"
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

                _schema_err = validate_tool_args(getattr(tool, "input_schema", None), args)
                if _schema_err:
                    err_msg = f"Error: invalid arguments for tool '{tc['name']}': {_schema_err}"
                    yield create_tool_result(id=client_id, content=err_msg, is_error=True)
                    self.session.add_tool_result(tc["id"], err_msg, is_error=True, client_id=client_id)
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=tc["name"],
                        latency_ms=0,
                        success=False,
                        error_message=err_msg,
                    )
                    yield create_tool_call_end(id=client_id)
                    t.turn_events += 1
                    continue

                # ── Spec approval gate for secondary tools ──────────
                _spec_block = False
                if (
                    t.slash_command_mode == "spec"
                    and self.spec_engine is not None
                    and (tc["name"] in _WRITE_TOOL_NAMES or tc["name"] == "bash")
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
                        tool_name=tc["name"],
                        latency_ms=0,
                        success=False,
                        error_message=_spec_msg,
                    )
                    yield create_tool_call_end(id=client_id)
                    t.turn_events += 1
                    continue

                _safety = getattr(tool, "is_concurrency_safe", None)
                is_safe = _safety(args) if callable(_safety) else False
                semantics = _infer_tool_semantics(tc["name"], tool)
                _ts = dict(self._state_mgr.tool_semantics)
                _ts[tc["name"]] = semantics
                self._state_mgr.tool_semantics = _ts
                extra_prepared.append(
                    {
                        "id": tc["id"],
                        "client_id": client_id,
                        "name": tc["name"],
                        "args": args,
                        "tool": tool,
                        "skip": False,
                        "safe": is_safe,
                        "args_summary": _args_summary(args),
                        "semantics": semantics,
                    }
                )

            # Permission & hooks for secondary tools
            if not self._cancelled():
                for p in extra_prepared:
                    if self._cancelled():
                        break
                    if not _tool_retry_allowed(p, self._recent_tool_names):
                        retry_msg = "Blocked repeated high-risk tool retry. " + p.get("semantics", {}).get(
                            "safe_fallback", "Gather more context before retrying."
                        )
                        yield create_tool_result(id=p["client_id"], content=retry_msg, is_error=True)
                        self.session.add_tool_result(p["id"], retry_msg, is_error=True, client_id=p["client_id"])
                        t.turn_events += 1
                        self.telemetry.record_tool_call(
                            tool_name=p["name"],
                            latency_ms=0,
                            success=False,
                            error_message=retry_msg,
                        )
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        p["skip"] = True
                        p["error"] = retry_msg
                        continue
                    # Rule engine first: exact rules -> wildcard rules ->
                    # default_effect -> engine/classifier -> fallback ask.
                    _rule_hit = self._evaluate_permission_rules(p["name"], p["args"])
                    _perm_rule_idx = -1
                    perm_matched = ""
                    if _rule_hit is not None:
                        _rule_effect, perm_source, _perm_rule_idx, perm_matched = _rule_hit
                        if _rule_effect == "allow":
                            permission = PermissionAllow(reason="Allowed by permission rule.")
                        elif _rule_effect == "deny":
                            permission = PermissionDeny(reason=f"Denied by permission rule {perm_matched}.")
                        else:
                            permission = PermissionAsk(reason=f"Confirmation required by permission rule {perm_matched}.")
                    else:
                        try:
                            permission = await self.safety.check_tool_permission(p["name"], p["args"])
                            perm_source = "classifier"
                        except Exception as _perm_exc:
                            # Degrade to ask: a crashed permission check must
                            # never translate into silent approval.
                            logger.error("Permission check failed for tool %s: %s", p["name"], _perm_exc)
                            permission = PermissionAsk(reason="Permission check unavailable; confirm to proceed.")
                            perm_source = "fallback_ask"
                    # Destructive escalation (converged from the retired
                    # ToolPipeline): in guarded modes, a destructive tool the
                    # engine auto-allowed still prompts the user.
                    _tool_obj = p.get("tool")
                    if (
                        permission.behavior == "allow"
                        and _tool_obj is not None
                        and callable(getattr(_tool_obj, "is_destructive", None))
                        and _tool_obj.is_destructive(p["args"])
                        and str(getattr(self.config, "permission_mode", "default") or "default") not in ("bypass", "auto", "plan", "spec")
                    ):
                        permission = PermissionAsk(reason=f"Destructive operation: {p['name']}. Confirm to proceed.")
                    # Decision-source audit: record which layer decided and
                    # the final effect (destructive escalation may have
                    # turned an allow into an ask).
                    self.telemetry.record_permission_decision(
                        p["name"],
                        effect=str(permission.behavior),
                        source=perm_source,
                        matched_rule=perm_matched,
                    )
                    self._loop_sm.record(
                        "permission_decision",
                        {
                            "tool": p["name"],
                            "source": perm_source,
                            "effect": str(permission.behavior),
                            "rule_index": _perm_rule_idx,
                        },
                    )
                    if permission.behavior == "deny":
                        deny_reason = getattr(permission, "reason", "") or _permission_reason(p["name"]) or "Permission denied by policy."
                        yield create_tool_result(id=p["client_id"], content=deny_reason, is_error=True)
                        self.session.add_tool_result(p["id"], deny_reason, is_error=True, client_id=p["client_id"])
                        t.turn_events += 1
                        self.telemetry.record_tool_call(
                            tool_name=p["name"],
                            latency_ms=0,
                            success=False,
                            error_message=deny_reason,
                        )
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        p["skip"] = True
                        p["error"] = deny_reason
                        continue

                    if permission.behavior == "ask":
                        permission_reason = getattr(permission, "reason", "") or _permission_reason(p["name"])
                        await self.hook_system.emit_permission_request(p["name"], permission_reason)
                        yield create_permission_request(
                            tool_name=p["name"],
                            reason=permission_reason,
                        )
                        permission_granted = await self._request_permission(p["name"])
                        await self.hook_system.emit_permission_response(p["name"], permission_granted)
                        if not permission_granted:
                            err_msg = "Permission denied by user."
                            yield create_tool_result(id=p["client_id"], content=err_msg, is_error=True)
                            self.session.add_tool_result(p["id"], err_msg, is_error=True, client_id=p["client_id"])
                            t.turn_events += 1
                            self.telemetry.record_tool_call(
                                tool_name=p["name"],
                                latency_ms=0,
                                success=False,
                                error_message=err_msg,
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
                            tool_name=p["name"],
                            latency_ms=0,
                            success=False,
                            error_message=block_reason,
                        )
                        yield create_tool_call_end(id=p["client_id"])
                        t.turn_events += 1
                        p["skip"] = True
                        p["error"] = block_reason
                        continue
                    if pre_hook and pre_hook.get("modified_input"):
                        # Partial-override semantics: merge on top of the
                        # original args (see the primary path above).
                        p["args"] = {**p["args"], **pre_hook["modified_input"]}

                    # Bash gate for secondary tools (same contract as the
                    # primary path above).
                    if p["name"] == "bash" and not p.get("skip"):
                        _bash_cmd = p["args"].get("command", "") if isinstance(p["args"], dict) else ""
                        _pre_bash = await self.hook_system.emit_pre_bash(_bash_cmd)
                        if _pre_bash and _pre_bash.get("block"):
                            _bash_reason = _pre_bash.get("block_reason") or "Blocked by hook: bash"
                            yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="blocked")
                            yield create_tool_result(id=p["client_id"], content=_bash_reason, is_error=True)
                            self.session.add_tool_result(p["id"], _bash_reason, is_error=True, client_id=p["client_id"])
                            t.turn_events += 1
                            self.telemetry.record_tool_call(
                                tool_name=p["name"],
                                latency_ms=0,
                                success=False,
                                error_message=_bash_reason,
                            )
                            yield create_tool_call_end(id=p["client_id"])
                            t.turn_events += 1
                            p["skip"] = True
                            p["error"] = _bash_reason
                            continue
                        _bash_mi = (_pre_bash or {}).get("modified_input") or {}
                        if isinstance(p["args"], dict) and isinstance(_bash_mi.get("command"), str) and _bash_mi["command"]:
                            p["args"]["command"] = _bash_mi["command"]

                    # ── Plan mode interception for secondary tools ──
                    if self.plan_mode_active:
                        sec_proposal_emitted = False
                        async for _event in self._intercept_plan_mode(
                            p["name"],
                            p["args"],
                            p["id"],
                            p["client_id"],
                        ):
                            sec_proposal_emitted = True
                            yield _event
                            t.turn_events += 1
                        if sec_proposal_emitted and not self._plan_decision:
                            if self._plan_decision_timed_out:
                                plan_err = (
                                    "Plan approval timed out with no decision. Proceed carefully, or present a smaller, clearer proposal."
                                )
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
                                tool_name=p["name"],
                                latency_ms=0,
                                success=False,
                                error_message=plan_err,
                            )
                            yield create_tool_call_end(id=p["client_id"])
                            t.turn_events += 1
                            p["skip"] = True
                            p["error"] = plan_err
                            continue

            # Execute secondary tools sequentially
            #
            # These calls bypass the execute phase's pre-write snapshot pass,
            # so capture here: without it a gated write (the common case for
            # permission-ask tools) left no snapshot and the rollback silently
            # kept the modified file.  The undo stack is paired the same way as
            # there -- commit on success, discard on error or stop.
            _undo_stack = self.session.ensure_undo_stack()
            _undo_pending: dict[str, dict[str, Any]] = {}
            for p in extra_prepared:
                if p.get("skip") or not _is_write_tool_name(p["name"]):
                    continue
                for fp in self._write_tool_paths(p["name"], p["args"]):
                    self.session.capture_file_snapshot(fp)
                    pend = _undo_stack.capture(p["name"], fp)
                    if pend is not None:
                        _undo_pending[fp] = pend
            _cascade_reason: str | None = None
            for p in extra_prepared:
                if p.get("skip"):
                    continue
                if _cascade_reason is not None:
                    # Sibling error cascade (converged from the retired
                    # ToolPipeline): a failed bash-family command invalidates
                    # the context of the remaining calls; tombstone them so
                    # every tool_use still receives a tool_result.
                    _cascade_err = f"Tool execution was cancelled: {_cascade_reason}"
                    yield create_tool_result(id=p["client_id"], content=_cascade_err, is_error=True)
                    self.session.add_tool_result(p["id"], _cascade_err, is_error=True, client_id=p["client_id"])
                    t.turn_events += 1
                    self.telemetry.record_tool_call(
                        tool_name=p["name"],
                        latency_ms=0.0,
                        success=False,
                        error_message=_cascade_err,
                    )
                    yield create_tool_call_end(id=p["client_id"])
                    t.turn_events += 1
                    self._undo_discard_for_tool(p["name"], p["args"], _undo_pending)
                    continue
                yield create_tool_progress(id=p["client_id"], tool_name=p["name"], status="running")
                tool_start = time.time()
                tool_error = False
                try:
                    # Load this tool's usage guide before the call (see the
                    # safe path in ``_execute_safe`` for the rationale).
                    await self._collect_tool_skill(p["name"])
                    executor = RetryableExecutor(self.recovery_engine)
                    # Wire the tool-progress emitter before creating the task
                    # so the contextvar propagates into it: long-running tools
                    # (persistent terminal sessions) push incremental output
                    # chunks that are drained below and forwarded as progress
                    # events (converged from the retired ToolPipeline).
                    _progress_chunks: list[str] = []

                    def _on_tool_progress(chunk: str, _chunks: list[str] = _progress_chunks) -> None:
                        _chunks.append(chunk)

                    _emitter_token = set_progress_emitter(_on_tool_progress)
                    _tcid_token = set_current_tool_call_id(p["id"])
                    try:
                        exec_task = asyncio.create_task(
                            executor.execute(
                                tool_name=p["name"],
                                tool_args=p["args"],
                                execute_fn=lambda args, p=p: p["tool"].execute(**args),
                            )
                        )
                        self._tool_stop_event.clear()
                        stop_task = asyncio.create_task(self._tool_stop_event.wait())
                        done = set()
                        while True:
                            done, _pending = await asyncio.wait(
                                {exec_task, stop_task},
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
                        reset_current_tool_call_id(_tcid_token)
                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                    stopped = exec_task not in done
                    # Screenshot payloads written by the tool into the runtime
                    # bypass slot (see encre.tools.runtime).  Popped BEFORE the
                    # str pipeline so safety/hooks/budget/wrapping only ever see
                    # the text part; the images ride along to the session as a
                    # side key and are projected into the model context at
                    # request-build time (backends/adaptive.py).
                    pending_images = pop_pending_tool_images(self.session.id or "", p["id"])
                    if stopped:
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        result = "Tool stopped by user."
                        tool_error = True
                    else:
                        state = exec_task.result()
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
                if pending_images and "attached" not in result[-200:]:
                    result = result + "\n[screenshot attached]"
                yield create_tool_result(id=p["client_id"], content=result, is_error=tool_error)
                self.session.add_tool_result(
                    p["id"],
                    result,
                    is_error=tool_error,
                    client_id=p["client_id"],
                    images=pending_images or None,
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
                if tool_error:
                    self._undo_discard_for_tool(p["name"], p["args"], _undo_pending)
                else:
                    self._undo_commit_for_tool(p["name"], p["args"], _undo_pending)
                if tool_error and p["name"] in _ERROR_CASCADE_TOOLS:
                    # Trigger the sibling cascade for the remaining tools.
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
        # Tool-execution boundary: every tool of this model round has run;
        # deliver any queued steering messages before the next model call.
        self._loop._inject_drained_messages(ctx.turn)
        return PhaseResult.continue_()
