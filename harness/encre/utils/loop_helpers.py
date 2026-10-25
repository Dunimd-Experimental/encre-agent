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

"""Pure helper functions extracted from encre.loop.

All functions here are stateless (no reference to EncreLoop or any class).
They operate solely on their parameters.  Extracted to reduce the ~5600-line
loop.py and enable independent testing.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from typing import Any



from encre.logging_config import get_logger
logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────

_WRITE_TOOL_NAMES = {"file_write", "file_edit", "write_file", "writeFile", "apply_patch", "bash", "terminal"}
#: Argument keys that carry a filesystem target on write tools.  Builtin and
#: plugin tools alike; keys are matched in this order.
_WRITE_PATH_ARG_KEYS = (
    "file_path", "path", "file", "target_path", "destination", "output_path",
)
_PROMPT_CACHE_TTL_SECONDS = 30.0
_TASK_STAGES = ("discover", "plan", "execute", "verify", "report")
_STUCK_LOOP_THRESHOLD = 6
_SUMMARY_INTERVAL_TURNS = 3

# Anti-stuck guardrail ladder (port of Hermes tool_guardrails.py).  A tool
# call is fingerprinted by a canonical hash of (name, normalized args) so that
# formatting differences never hide a true repeat.  Escalation is
# warn -> block -> halt: warn injects guidance, block feeds a synthetic error
# result back to the model so it sees the call was refused, and halt hard-stops
# the current turn (opt-in circuit breaker).
_GUARDRAIL_WARN_AFTER = 3
_GUARDRAIL_BLOCK_AFTER = 5
_GUARDRAIL_HALT_AFTER = 8
# Idempotent read-only tools whose *result* being byte-identical on repeat is a
# strong "no progress" signal, even when the args differ.  Mirrors Hermes
# IDEMPOTENT_TOOL_NAMES.
_IDEMPOTENT_TOOL_NAMES = frozenset({
    "file_read", "glob", "grep", "web_search", "web_fetch",
    "lsp", "browser",
})
# How many consecutive identical results on an idempotent tool count as no-progress.
_NO_PROGRESS_AFTER = 3
# Tools whose errors cancel the remaining sequential tools in the same batch:
# a failed shell command often invalidates the context the following calls
# were prepared against, so running them wastes resources on doomed work.
_ERROR_CASCADE_TOOLS = frozenset({"bash", "bash_io", "powershell"})
# Poll interval (seconds) used while a tool is executing to drain incremental
# progress chunks pushed through the runtime progress emitter.
_PROGRESS_POLL_INTERVAL = 0.1


def _canonical_tool_signature(name: str, args_raw: str) -> str:
    """Stable fingerprint for ``(tool_name, args)``.

    Args are whitespace-normalized and canonical-sorted JSON (when parseable)
    then hashed, so two logically-identical calls always collide regardless of
    formatting/whitespace/ordering of JSON keys.

    Args:
        name: The tool name.
        args_raw: The raw arguments string from the model.

    Returns:
        A short hex digest of the canonical form.
    """
    compact = ("".join((args_raw or "").split()))
    try:
        parsed = json.loads(compact) if compact else {}
        if isinstance(parsed, dict):
            compact = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except Exception:
        pass  # keep the whitespace-normalized raw string as the canonical form
    return hashlib.sha256(f"{name}|{compact}".encode()).hexdigest()[:16]


def _result_digest(result: str) -> str:
    """A coarse digest of a tool result for no-progress detection."""
    return hashlib.sha256(("".join((result or "").split()))[:4000].encode("utf-8")).hexdigest()[:16]


def _is_idempotent_tool(name: str) -> bool:
    return name in _IDEMPOTENT_TOOL_NAMES


# Verify-on-stop (port of Hermes verification_stop.py): when a turn edited code
# and is about to end without any verification tool having run, nudge the model
# to verify.  The nudge loop is bounded by _MAX_VERIFY_ON_STOP_NUDGES so it can
# never deadlock the agent; it is a quality reminder, not a hard gate.
_MAX_VERIFY_ON_STOP_NUDGES = 2
# Tools that count as "verification evidence" -- using one clears the pending
# verify-on-stop nudge.
_VERIFY_TOOL_NAMES = frozenset({"test_run", "lint_format", "lsp", "bash", "powershell"})


def build_verify_on_stop_message(files: list[str]) -> str:
    """Synthetic user message asking the model to verify changed files."""
    _files = "\n".join(f"- `{f}`" for f in files[:6])
    _more = f"\n  ... and {len(files) - 6} more" if len(files) > 6 else ""
    return (
        "[VERIFY-ON-STOP] You changed code files this session but did not run any "
        "verification tool on them before stopping. The task is not complete until "
        "the change is verified. Please verify now by running the appropriate "
        "lint/typecheck/test tool on the changed files:\n"
        f"{_files}{_more}\n"
        "Run `test_run`, `lint_format`, or `lsp` (or an equivalent command) and "
        "report the result. Fix any errors you find before finishing."
    )


def _summarize_verify_result(tool: str, result: str, passed: bool) -> str:
    """Extract a short human summary from a verification tool's JSON result.

    test_run returns {"passed":N,"failed":M,"tests":[...]}; lint_format
    returns {"ok":bool,"summary":"...","diagnostics":[...]}; bash returns a
    shell envelope.  This is best-effort: any parse failure falls back to a
    generic pass/fail tag so the ledger never breaks the loop.
    """
    text = (result or "").strip()
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        _d: Any = data
        if tool == "test_run":
            passed_n = int(_d.get("passed", _d.get("passed_count", 0)))
            failed_n = int(_d.get("failed", _d.get("failed_count", 0)))
            skipped = int(_d.get("skipped", 0))
            total = int(_d.get("total", passed_n + failed_n + skipped))
            return f"test_run: {passed_n} passed, {failed_n} failed, {skipped} skipped (of {total})"
        if tool == "lint_format":
            ok = _d.get("ok", passed)
            mode = _d.get("mode", "check")
            summ = _d.get("summary", "")
            diags = _d.get("diagnostics", [])
            n = len(diags) if isinstance(diags, list) else 0
            return f"lint_format({mode}): ok={ok} {summ or (f'{n} diagnostics' if n else '')}".strip()
        if tool in ("bash", "powershell"):
            success = _d.get("success", passed)
            summary = _d.get("summary", "")
            code = _d.get("exit_code")
            tail = f" (exit {code})" if code is not None else ""
            return f"bash: success={success}{tail} {summary}".strip()
    if passed:
        return f"{tool}: passed"
    return f"{tool}: failed"

_MAX_TOOL_CONCURRENCY = max(
    1, int(os.environ.get("ENCRE_MAX_TOOL_USE_CONCURRENCY", "10") or "10")
)

_EVOLUTION_ENABLED = os.environ.get("ENCRE_EVOLUTION", "0") == "1"
_WORKING_SET_ARTIFACT_LIMIT = 8
_WORKING_SET_REFERENCE_LIMIT = 8
_WORKING_SET_TOOL_LIMIT = 12

_PLAN_STATUS_MAP = {
    "pending": "pending",
    "in_progress": "active",
    "completed": "done",
}

# ── Helper functions ────────────────────────────────────────────────


def _spillover_dir(session_id: str) -> str | None:
    try:
        from encre.config import get_data_dir

        d = get_data_dir() / "spillover" / (session_id or "default")
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except Exception:
        return None


def _apply_result_budget(
    result: str,
    tool: Any,
    max_chars: int = 100_000,
    context_ratio: float = 1.0,
    session_id: str = "",
    tool_name: str = "",
) -> str:
    budget = getattr(tool, "max_result_size_chars", max_chars) or max_chars
    if context_ratio < 0.5:
        scaled = int(budget * context_ratio)
        budget = max(500, min(budget, scaled))
    if len(result) <= budget:
        return result
    excess = len(result) - budget
    spillover_path = None
    if session_id:
        import hashlib

        spillover_dir = _spillover_dir(session_id)
        if spillover_dir:
            digest = hashlib.sha1(result.encode("utf-8", errors="replace")).hexdigest()[:16]
            label = (tool_name or "tool").replace("/", "_")[:32]
            fname = f"spillover_{label}_{digest}.txt"
            import os as _os

            fpath = _os.path.join(spillover_dir, fname)
            try:
                from encre.secure_io import write_text as _write_secure_text

                _write_secure_text(fpath, result)
                spillover_path = fpath
            except Exception:
                spillover_path = None
    preview = result[:1000]
    if spillover_path:
        return (
            preview
            + f"\n... (truncated {excess} characters; full output saved to {spillover_path})"
        )
    return result[:budget] + f"\n... (truncated {excess} characters)"


def _extract_file_path(tool_name: str, result: str) -> str | None:
    if tool_name not in _WRITE_TOOL_NAMES:
        return None

    if tool_name == "apply_patch":
        import json as _json

        try:
            json_part = result.split("\n", 1)[1] if "\n" in result else result
            data = _json.loads(json_part)
            files = data.get("files", []) if isinstance(data, dict) else []
            if files:
                fp = files[0].get("new_path") or files[0].get("old_path", "")
                if fp and os.path.isabs(fp) and os.path.exists(fp):
                    return fp
        except (_json.JSONDecodeError, IndexError, KeyError):
            logger.debug("exception suppressed", exc_info=True)
        return None

    for pattern in [
        r"Successfully wrote \d+ characters to (.+)",
        r"Applied \d+ edit\(s\) to (.+?)\.\s*\n",
        r"Wrote .+ to (.+)",
    ]:
        m = re.search(pattern, result, re.IGNORECASE)
        if m:
            path = m.group(1).strip()
            if os.path.isabs(path) and os.path.exists(path):
                return path
    return None


# Language → best-fit verification tool mapping.  Used to turn the passive
# "[VERIFY] read the file" reminder into an actionable quality gate that tells
# the model *which* existing tool to run on the changed file.
_VERIFY_TOOL_BY_EXT = {
    ".py": "lint_format", ".pyi": "lint_format",  # ruff
    ".js": "lint_format", ".jsx": "lint_format",
    ".ts": "lint_format", ".tsx": "lint_format",  # eslint/prettier
    ".rs": "lint_format",                         # cargo fmt + clippy
    ".go": "test_run", ".java": "test_run",
    ".c": "test_run", ".h": "test_run",
    ".cpp": "test_run", ".cc": "test_run", ".hpp": "test_run",
}


def build_verify_instruction(file_path: str, *, has_lsp: bool = False) -> str:
    """Build an actionable, tool-specific verification instruction.

    Replaces the vague "read the file back" reminder with a directive to run
    the concrete verification tool that already exists in the toolchain
    (``lint_format`` / ``test_run``) for the changed file's language, plus
    LSP diagnostics when available.
    """
    ext = pathlib.Path(file_path).suffix.lower()
    tool = _VERIFY_TOOL_BY_EXT.get(ext)
    lines = [
        f"[VERIFY] You changed `{file_path}`. Verification is required before this change is done:",
    ]
    if has_lsp:
        lines.append(f"- Run `lsp` (diagnostics) on `{file_path}` and resolve any reported errors.")
    if tool:
        lines.append(
            f"- Run the `{tool}` tool on `{file_path}` and resolve any errors/warnings it reports "
            f"for the changed file. Do not claim the change is verified unless that check passed."
        )
    lines.append(
        "- If neither LSP nor a lint/test tool applies, read `{fp}` back and confirm the edit "
        "matches the intended change exactly.".replace("{fp}", file_path)
    )
    return "\n".join(lines)



def _extract_diff_text(_tool_name: str, result: str) -> str:
    m = re.search(r"```diff\n(.+?)\n```", result, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""
def _args_summary(args: dict[str, Any]) -> str:
    try:
        return json.dumps(args, ensure_ascii=False)[:600]
    except Exception:
        return str(args)[:600]


def _turn_to_message_index(
    messages: list[dict[str, Any]], target_turn: int,
) -> int | None:
    if target_turn <= 0:
        return 0
    seen = 0
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            seen += 1
            if seen == target_turn:
                return i
    return None


def _permission_reason(tool_name: str) -> str:
    return f"Tool {tool_name} requires permission"


def _extract_apply_patch_paths(result: str) -> list[str]:
    import json as _json

    try:
        json_part = result.split("\n", 1)[1] if "\n" in result else result
        data = _json.loads(json_part)
        files = data.get("files", []) if isinstance(data, dict) else []
        paths = []
        for f in files:
            if f.get("status") != "ok":
                continue
            fp = f.get("new_path") or f.get("old_path", "")
            if fp and os.path.isabs(fp) and os.path.exists(fp):
                paths.append(fp)
        return paths
    except (_json.JSONDecodeError, IndexError, KeyError):
        return []


def _is_write_tool_name(tool_name: str) -> bool:
    """Single verdict for "this tool writes files", shared by every layer.

    Snapshot capture, path-conflict splitting and rollback extraction must
    agree on which tool calls touched the filesystem -- if one of them grew
    its own name list the two sides silently diverged and a rolled-back turn
    restored nothing.  Plugin tools participate through their declared
    write/edit/patch/delete name.
    """
    lowered = (tool_name or "").lower()
    return lowered in _WRITE_TOOL_NAMES or _name_says_write(lowered)


def _name_says_write(lowered_name: str) -> bool:
    """Name heuristic for write semantics (``lowered_name`` already lowered).

    Covers the plugin tool names outside the builtin set (``*_save``,
    ``*_append``) so their files are rollback targets too; read tools
    (``file_read``, ``*_view``) match none of these tokens.
    """
    return any(
        tok in lowered_name for tok in ("write", "edit", "patch", "delete", "save", "append")
    )


def _extract_bash_write_paths(command: str) -> list[str]:
    """Extract file paths that a shell command might write to.

    Heuristic: catches redirections (>, >>), file operations (cp, mv, rm,
    touch, sed -i, tee). False positives filtered downstream by path_guard.
    """
    paths: list[str] = []

    def _add(p: str) -> None:
        p = p.strip().strip('"\'')
        if p and p not in paths and not p.startswith("-") and p not in ("/dev/null", "/dev/stdout", "/dev/stderr", "NUL"):
            paths.append(p)

    for m in re.finditer(r"\d*>\s*([^;\s|&<>]+)", command):
        _add(m.group(1))

    for part in re.split(r"[;|&]+", command):
        part = part.strip()
        if not part:
            continue
        for verb, groups in (
            (r"\bcp\s+(.+?)\s+([^;\s|&]+)", (1, 2)),
            (r"\bmv\s+(.+?)\s+([^;\s|&]+)", (1, 2)),
            (r"\brm\s+(.+)", (1,)),
            (r"\btouch\s+(.+)", (1,)),
            (r"\btee\s+(.+)", (1,)),
            (r"\bsed\s+(?:-\w*i)+\s+(.+)", (1,)),
        ):
            m = re.match(verb, part)
            if m:
                for g in groups:
                    if g <= len(m.groups()):
                        for p in m.group(g).split():
                            _add(p)

    return paths


def extract_write_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Raw file paths a write tool call operates on, in argument order.

    Deliberately un-resolved: callers resolve through
    :func:`encre.tools.path_guard.resolve_write_target` so the on-disk
    resolution rule lives in exactly one place.
    """
    if not isinstance(args, dict) or not _is_write_tool_name(tool_name):
        return []
    if tool_name in ("bash", "terminal"):
        command = args.get("command", "")
        if isinstance(command, str) and command:
            return _extract_bash_write_paths(command)
    paths: list[str] = []
    if tool_name == "apply_patch":
        for fd in (args.get("files") or []):
            if not isinstance(fd, dict):
                continue
            for key in ("old_path", "new_path"):
                p = fd.get(key)
                if isinstance(p, str) and p and p not in paths:
                    paths.append(p)
        return paths
    for key in _WRITE_PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value and value not in paths:
            paths.append(value)
    return paths


def _extract_write_target_paths(tool_name: str, args: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for p in extract_write_paths(tool_name, args):
        try:
            paths.add(os.path.abspath(p))
        except Exception:
            paths.add(p)
    return paths


def _split_writes_by_path_conflict(
    write_tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path_map: dict[int, set[str]] = {
        i: _extract_write_target_paths(p["name"], p.get("args", {}))
        for i, p in enumerate(write_tools)
    }
    parallel: list[dict[str, Any]] = []
    sequential: list[dict[str, Any]] = []
    for i, p in enumerate(write_tools):
        my_paths = path_map[i]
        if not my_paths:
            sequential.append(p)
            continue
        conflict = any(
            bool(my_paths & path_map[j])
            for j in range(len(write_tools))
            if j != i
        )
        (sequential if conflict else parallel).append(p)
    return parallel, sequential


async def _try_lsp_diagnostics(file_path: str) -> str:
    try:
        from encre.tools.runtime import get_lsp_manager as _get_manager

        mgr = _get_manager()
        if not getattr(mgr, "_workspace", ""):
            return ""
        diags = await mgr.get_diagnostics(file_path)
        if not diags:
            return ""
        sev_name = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}
        lines = [f"\n\n[LSP Diagnostics for {file_path}] ({len(diags)})"]
        for d in diags[:20]:
            sev = sev_name.get(d.severity, "Issue")
            raw_msg = (d.message or "").strip()
            msg = (raw_msg.splitlines()[0][:200] if raw_msg else "")
            loc = ""
            try:
                if d.range and d.range.start is not None:
                    loc = f" line {d.range.start.line}:{d.range.start.character}"
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
            src = f" ({d.source})" if d.source else ""
            lines.append(f"  [{sev}]{loc}{src} {msg}")
        return "\n".join(lines)
    except Exception:
        return ""


def _is_reference_tool(tool_name: str) -> bool:
    name = tool_name.lower()
    return (
        tool_name.startswith("mcp__") or name.startswith("memory_") or name.startswith("web_")
    )


def _memory_file_exists_on_disk(fn: str) -> bool:
    """Check whether a memory file exists in the memory dir.

    Uses the same on-disk name as the memory tools (raw filename, ``.md``
    suffix appended when missing).  References must never point at memory
    files that do not exist, otherwise the summary panel shows phantom
    entries.
    """
    if not fn:
        return False
    if not fn.endswith(".md"):
        fn += ".md"
    from encre.config import get_data_dir

    return os.path.isfile(str(get_data_dir() / "memory" / fn))


def _extract_ref_summary(
    tool_name: str, args: dict[str, Any], result: str,  # noqa: ARG001
) -> str | None:
    """Build the display summary for a reference entry.

    Returns ``None`` when the reference should be omitted entirely: memory
    file operations that targeted a file that does not exist (e.g. the model
    passed a Chinese name while the on-disk file is its sanitized slug) must
    not leave phantom entries in the summary panel.
    """
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) >= 3:
            server = parts[1]
            inner = parts[2]
            sub = args.get("tool_name") or args.get("name") or inner
            return f"MCP {server}: {sub}"
        return f"MCP: {tool_name}"

    name = tool_name.lower()

    if name in ("memory_create", "memory_update"):
        fn = args.get("filename", "") or args.get("name", "")
        if name == "memory_update" and not _memory_file_exists_on_disk(fn):
            return None
        return f"Memory: {fn}" if fn else f"Memory: {tool_name}"

    if name == "memory_read":
        fn = args.get("filename", "") or args.get("name", "")
        if not _memory_file_exists_on_disk(fn):
            return None
        return f"Read memory: {fn}" if fn else "Read memory"

    if name == "memory_delete":
        fn = args.get("filename", "") or args.get("name", "")
        if not _memory_file_exists_on_disk(fn):
            return None
        return f"Deleted memory: {fn}" if fn else "Deleted memory"

    if name == "memory_search":
        q = args.get("query", "")
        return f"Searched memory: {q[:60]}" if q else "Searched memory"

    if name == "memory_profile":
        field = args.get("field", "")
        val = args.get("value", "")
        return f"Profile: {field} = {val[:40]}" if field else "Profile updated"

    if name == "web_search":
        q = args.get("query", "")
        return f"Web search: {q[:80]}" if q else "Web search"

    if name == "web_fetch":
        url = args.get("url", "")
        return f"Fetched: {url[:80]}" if url else "Web fetch"

    return tool_name


def _ensure_plan_items(tool_name: str, args: dict[str, Any]) -> list[dict[str, Any]] | None:
    if tool_name != "todo":
        return None
    todos = args.get("todos")
    if not todos or not isinstance(todos, list):
        return None
    items: list[dict[str, Any]] = []
    for i, todo in enumerate(todos):
        content = todo.get("content", "")
        if not content:
            continue
        status = _PLAN_STATUS_MAP.get(todo.get("status", "pending"), "pending")
        # Prefer the stable id the todo tool assigns (survives reordering);
        # fall back to positional ids for raw/legacy calls.
        item: dict[str, Any] = {
            "id": str(todo.get("id", "")).strip() or f"plan-{i}",
            "text": content,
            "status": status,
        }
        if todo.get("priority"):
            item["priority"] = todo.get("priority")
        items.append(item)
    return items if items else None


def _infer_tool_semantics(tool_name: str, tool: Any) -> dict[str, str]:
    semantic_type = str(getattr(tool, "semantic_type", "") or "").strip().lower()
    cost_level = str(getattr(tool, "cost_level", "") or "").strip().lower()
    retryability = str(getattr(tool, "retryability", "") or "").strip().lower()
    safe_fallback = str(getattr(tool, "safe_fallback", "") or "").strip()
    lowered = tool_name.lower()

    declared_type = bool(semantic_type) and semantic_type != "general"

    if not semantic_type or semantic_type == "general":
        if _is_write_tool_name(lowered):
            semantic_type = "write"
        elif "read" in lowered or "cat" in lowered or "view" in lowered:
            semantic_type = "read"
        elif "search" in lowered or "grep" in lowered or "glob" in lowered or "find" in lowered:
            semantic_type = "search"
        elif lowered in {"bash", "shell", "execute", "run"}:
            semantic_type = "exec"
        elif lowered.startswith("web_") or lowered in {"browser", "rest_client"}:
            semantic_type = "network"
        elif lowered in {"agent", "workflow"}:
            semantic_type = "orchestrate"
        else:
            semantic_type = "general"

    # When semantic_type was derived from the tool name (rather than declared
    # on the tool), the cost/retryability defaults ("medium"/"auto") are just
    # unset placeholders -- derive them from the inferred type too.  Otherwise
    # a generic default of "medium" would mask name-based inference and a
    # search tool (e.g. "grep") would be treated as medium-cost.
    if semantic_type in {"search", "read"}:
        inferred_cost = "low"
        inferred_retry = "auto"
    elif semantic_type in {"write", "exec", "network", "orchestrate"}:
        inferred_cost = "high"
        inferred_retry = "guarded" if semantic_type != "network" else "auto"
    else:
        inferred_cost = "medium"
        inferred_retry = "manual"

    if not cost_level or (cost_level == "medium" and not declared_type):
        cost_level = inferred_cost

    if not retryability or (retryability == "auto" and not declared_type):
        retryability = inferred_retry

    if not safe_fallback:
        fallback_map = {
            "write": "Read the target file again, narrow the scope, and propose a smaller change.",
            "exec": "Inspect the environment and command preconditions before retrying.",
            "search": "Refine the query or switch to a more specific file/path filter.",
            "network": "Use local context first and only retry if an external lookup is necessary.",
            "orchestrate": "Summarize the sub-task and continue in the main thread if delegation is unnecessary.",
        }
        safe_fallback = fallback_map.get(semantic_type, "Gather more context before retrying.")

    return {
        "semantic_type": semantic_type,
        "cost_level": cost_level,
        "retryability": retryability,
        "safe_fallback": safe_fallback,
    }


def _tool_retry_allowed(p: dict[str, Any], repeated_signatures: list[tuple[str, ...]]) -> bool:
    semantics = p.get("semantics", {}) or {}
    retryability = str(semantics.get("retryability", "auto"))
    if retryability == "auto":
        return True
    if not repeated_signatures:
        return True
    sig = f"{p.get('name', '')}:{p.get('args_summary', '')[:80]}"
    # Search the whole recent window rather than only the immediately preceding
    # turn.  Otherwise an alternating pattern (A,B,A,B,...) or a tool repeated
    # after other work slips past the guarded retry guard and can loop forever.
    recent = repeated_signatures[-8:]
    if retryability == "manual":
        # Manual retry is permitted unless the exact call already happened in
        # the very recent past, where re-invoking is almost always a stall.
        return not any(sig == item for turn in recent for item in turn)
    if retryability == "guarded":
        # Guarded (write/exec/orchestrate): block once the identical call has
        # been made recently, even if other tools ran in between.
        return not any(sig == item for turn in recent for item in turn)
    return True


def _turn_to_message_index(
    messages: list[dict[str, Any]], target_turn: int,
) -> int | None:
    if target_turn <= 0:
        return 0
    seen = 0
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            seen += 1
            if seen == target_turn:
                return i
    return None
