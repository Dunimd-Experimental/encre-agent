#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Shared runtime context for tools.

Tool modules -- both the legacy ``encre.tools.builtin`` implementations and
their migrated ``ea_tool_*`` plugin packages -- need access to mutable state
that the host injects at runtime (the active loop, the sandbox workspace, the
cron scheduler, the engine-install requester, the LSP manager).

Historically each module kept its own copy of that state.  Once a tool was
migrated into a plugin package the copy in the plugin module was never wired
by the host, so tools such as ``manage`` failed with "requires a parent
loop reference".

This module is the single source of truth.  The host wires state here (via the
legacy module re-exports it already calls), and every tool module -- old or
new -- reads from here.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

# ---------------------------------------------------------------------------
# Active loop (manage, agent, ...)
# ---------------------------------------------------------------------------

_current_loop: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "encre_current_loop", default=None,
)
_parent_loop: Any = None


def set_parent_loop(loop: Any) -> None:
    """Set the fallback parent loop reference for tool resolution."""
    global _parent_loop
    _parent_loop = loop


def set_active_loop(loop: Any) -> contextvars.Token:
    """Bind *loop* as the active loop for this turn; returns a reset token."""
    return _current_loop.set(loop)


def reset_active_loop(token: contextvars.Token) -> None:
    """Restore the active loop to its previous value using *token*."""
    _current_loop.reset(token)


def _resolve_loop() -> Any:
    """Resolve the loop, preferring the per-turn active loop over the parent."""
    ctx_loop = _current_loop.get()
    if ctx_loop is not None:
        return ctx_loop
    return _parent_loop


def get_loop_cancel_event() -> Any:
    """Return the active loop's cancel event, or ``None`` when unset.

    The event is raised by the host when the user hits Stop / pauses the turn.
    Long-running tools (``bash``) watch it via :func:`_get_loop_cancel_event`
    so they can abort promptly even when the surrounding task is not itself
    cancelled (e.g. the whole turn is torn down instead of the per-tool task).
    """
    loop = _resolve_loop()
    if loop is None:
        return None
    return getattr(loop, "_cancel_event", None)


def _get_loop_cancel_event() -> Any:
    """Alias kept for symmetry with ``_get_progress_emitter``."""
    return get_loop_cancel_event()


# ---------------------------------------------------------------------------
# Bash abort event (tool_stop: kill the shell without cancelling the turn).
# ---------------------------------------------------------------------------

_bash_abort_events: dict[str, asyncio.Event] = {}


def get_session_id() -> str:
    loop = _resolve_loop()
    if loop is None:
        return ""
    session = getattr(loop, "session", None)
    if session is None:
        return ""
    return getattr(session, "id", "") or ""


def get_bash_abort_event(session_id: str) -> asyncio.Event | None:
    return _bash_abort_events.get(session_id)


def set_bash_abort_event(session_id: str, event: asyncio.Event) -> None:
    _bash_abort_events[session_id] = event


def clear_bash_abort_event(session_id: str) -> None:
    _bash_abort_events.pop(session_id, None)


# ---------------------------------------------------------------------------
# Sandbox workspace (bash / terminal)
# ---------------------------------------------------------------------------

_current_workspace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bash_workspace", default=None,
)


def set_workspace(ws: str | None) -> contextvars.Token:
    """Set the sandbox workspace path for the current turn; returns a token."""
    return _current_workspace.set(ws)


def reset_workspace(token: contextvars.Token) -> None:
    """Restore the workspace path to its previous value using *token*."""
    _current_workspace.reset(token)


def _get_workspace() -> str | None:
    """Return the current sandbox workspace path (set by the loop per turn)."""
    return _current_workspace.get()


# ---------------------------------------------------------------------------
# Tool progress streaming (bash / terminal)
# ---------------------------------------------------------------------------

_current_progress_emitter: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "encre_current_progress_emitter", default=None,
)


def set_progress_emitter(emitter: Any) -> contextvars.Token:
    """Bind *emitter* (``str -> None``) as this turn's tool-progress emitter.

    Long-running tools (e.g. ``bash``) read it via :func:`_get_progress_emitter`
    and push incremental output chunks through it while still executing, so the
    wire layer can forward real-time progress instead of waiting for completion.
    Returns a token usable with :func:`reset_progress_emitter`.
    """
    return _current_progress_emitter.set(emitter)


def reset_progress_emitter(token: contextvars.Token) -> None:
    """Restore the previous progress emitter using *token*."""
    _current_progress_emitter.reset(token)


def _get_progress_emitter() -> Any:
    """Return the active tool-progress emitter, or ``None`` when unset."""
    return _current_progress_emitter.get()


# ---------------------------------------------------------------------------
# Cron scheduler (cron)
# ---------------------------------------------------------------------------

_scheduler: Any = None


def set_scheduler(scheduler: Any) -> None:
    """Set the cron scheduler used by the cron tools."""
    global _scheduler
    _scheduler = scheduler


def get_scheduler() -> Any:
    """Return the cron scheduler, or ``None`` when not started."""
    return _scheduler


# ---------------------------------------------------------------------------
# Engine-install requester (browser / computer)
# ---------------------------------------------------------------------------

_engine_requester: Any = None


def set_engine_requester(requester: Any) -> None:
    """Set the engine-install requester shared by browser-style tools."""
    global _engine_requester
    _engine_requester = requester


def get_engine_requester() -> Any:
    """Return the engine-install requester, or ``None`` when unset."""
    return _engine_requester


# ---------------------------------------------------------------------------
# Pending tool images (screenshot-bearing tools -> tool-result messages)
# ---------------------------------------------------------------------------

#: Images produced by the most recent tool executions, keyed by
#: ``"<session_id>\n<tool_call_id>"``.  Tools that capture screenshots write
#: ``{"media_type": str, "data": str}`` dicts here (base64, no data-URL
#: prefix); the loop's tool phase pops them right after execution and hands
#: them to ``Session.add_tool_result`` so the images can be projected into
#: the model context as image content blocks.  Pop-on-read keeps the slot
#: self-cleaning; the tool_call_id component prevents parallel tool calls
#: within one session from cross-contaminating each other's payloads.
_pending_tool_images: dict[str, list[dict[str, str]]] = {}


def _tool_images_key(session_id: str, tool_call_id: str) -> str:
    return f"{session_id or ''}\n{tool_call_id or ''}"


#: tool_call_id of the tool currently being executed.  Set by the loop's tool
#: phase before dispatch so screenshot-producing tools can address the
#: pending-images slot without the id being part of their schema.
_current_tool_call_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "encre_current_tool_call_id", default=""
)


def set_current_tool_call_id(tool_call_id: str) -> contextvars.Token:
    """Set the tool_call_id for the in-flight tool execution."""
    return _current_tool_call_id.set(tool_call_id or "")


def reset_current_tool_call_id(token: contextvars.Token) -> None:
    """Restore the previous tool_call_id contextvar value."""
    _current_tool_call_id.reset(token)


def get_current_tool_call_id() -> str:
    """Return the tool_call_id of the currently executing tool (may be '')."""
    return _current_tool_call_id.get()


def set_pending_tool_images(
    session_id: str, tool_call_id: str, images: list[dict[str, str]]
) -> None:
    """Store screenshot payloads produced by one tool execution."""
    if not images:
        return
    _pending_tool_images[_tool_images_key(session_id, tool_call_id)] = list(images)


def pop_pending_tool_images(
    session_id: str, tool_call_id: str
) -> list[dict[str, str]]:
    """Take (and clear) the pending images for one tool execution."""
    return _pending_tool_images.pop(_tool_images_key(session_id, tool_call_id), None) or []


# ---------------------------------------------------------------------------
# LSP manager (lsp)
# ---------------------------------------------------------------------------

_lsp_manager: Any = None


def get_lsp_manager() -> Any:
    """Return the process-wide LSP manager, or ``None`` when none is installed.

    The legacy ``EncreLSPManager`` implementation no longer ships with the
    repository, so this simply returns whatever was installed through
    :func:`set_lsp_manager` (``None`` by default).
    """
    return _lsp_manager


def set_lsp_manager(manager: Any) -> None:
    """Override the LSP manager (used for teardown/reset)."""
    global _lsp_manager
    _lsp_manager = manager


# ---------------------------------------------------------------------------
# Browser session state (browser / computer)
# ---------------------------------------------------------------------------

#: Per-chat-session browser sessions, keyed by session id.
browser_sessions: dict[str, Any] = {}
#: CDP websocket URLs per session id.
browser_cdp_ws_urls: dict[str, str] = {}
#: Currently-configured search-engine URL template.
browser_search_engine_url: str | None = None
#: Session id for the in-flight browser call.
browser_session_id: str = ""
#: Fallback key for CDP URLs when no session id is known.
BROWSER_DEFAULT_KEY = "__default__"


def set_browser_session_id(sid: str) -> None:
    """Set the session id used to key the current browser session."""
    global browser_session_id
    browser_session_id = sid


def set_cdp_url(url: str) -> None:
    """Record the CDP websocket URL for the current (or default) session."""
    if browser_session_id:
        browser_cdp_ws_urls[browser_session_id] = url
    else:
        browser_cdp_ws_urls[BROWSER_DEFAULT_KEY] = url


def clear_cdp_url() -> None:
    """Drop the stored CDP websocket URL for the current (or default) session.

    Called when the frontend webview is closed or destroyed: the relay behind
    the old URL is gone, so keeping the entry would only make the browser tool
    retry a dead port instead of waiting for the fresh endpoint.
    """
    browser_cdp_ws_urls.pop(browser_session_id, None)
    browser_cdp_ws_urls.pop(BROWSER_DEFAULT_KEY, None)


def set_search_engine_url(url: str) -> None:
    """Set the search-engine URL template used to rewrite search queries."""
    global browser_search_engine_url
    browser_search_engine_url = url


def configure_browser_engine_requester(requester: Any) -> None:
    """Install an engine-install requester on existing and future sessions."""
    set_engine_requester(requester)
    for session in browser_sessions.values():
        if hasattr(session, "set_engine_requester"):
            session.set_engine_requester(requester)


def get_browser_session() -> Any:
    """Get or create the browser session for the current session id."""
    key = browser_session_id or BROWSER_DEFAULT_KEY
    if key not in browser_sessions:
        from encre.computer.browser import EncreBrowserSession

        browser_sessions[key] = EncreBrowserSession()
        req = get_engine_requester()
        if req is not None and hasattr(browser_sessions[key], "set_engine_requester"):
            browser_sessions[key].set_engine_requester(req)
    # Migrate the URL from the default key to the real session id once known.
    if browser_session_id and BROWSER_DEFAULT_KEY in browser_cdp_ws_urls:
        browser_cdp_ws_urls[browser_session_id] = browser_cdp_ws_urls.pop(BROWSER_DEFAULT_KEY)
    return browser_sessions[key]


def get_cdp_url() -> str | None:
    """Return the CDP URL for the current session, falling back to default."""
    sid = browser_session_id
    if sid and sid in browser_cdp_ws_urls:
        return browser_cdp_ws_urls[sid]
    return browser_cdp_ws_urls.get(BROWSER_DEFAULT_KEY)


# ---------------------------------------------------------------------------
# Sub-agent policy (agent)
# ---------------------------------------------------------------------------

#: Maximum delegation depth: a sub-agent may not spawn further sub-agents.
MAX_SUB_AGENT_DEPTH = 1


def _enforce_tool_policy(tool_name: str, tool_input: dict[str, Any] | None = None) -> str | None:
    """Return an error string if the active sub-agent's policy forbids the tool.

    The policy is read from the active loop's ``config.current_tool_policy``,
    which the parent loop sets before delegating to a sub-agent.  Returns
    ``None`` when the call is allowed.
    """
    loop = _resolve_loop()
    if loop is None:
        return None
    policy = getattr(loop.config, "current_tool_policy", "all")
    # Hard-fence first: a sub-agent (depth > 0) must not spawn more sub-agents
    # or workflows, regardless of policy.
    if tool_name in ("agent", "workflow") and getattr(loop, "sub_agent_depth", 0) > 0:
        return (
            "Sub-agents are forbidden from spawning further sub-agents or "
            "workflows. The runtime only allows one level of delegation. "
            "Complete the assigned task with your own tools and return the "
            "result."
        )
    if not isinstance(policy, str) or policy == "all":
        return None
    tool_obj = loop.tool_registry.get(tool_name) if hasattr(loop, "tool_registry") else None
    if tool_obj is not None:
        args = tool_input or {}
        if tool_obj.is_readonly(args):
            return None
    write_tools = {"file_write", "file_edit", "write_file", "writeFile", "apply_patch"}
    if policy == "readonly" and tool_name in write_tools:
        return f"Tool {tool_name} is forbidden in readonly sub-agent policy."
    if policy == "readonly" and tool_name in (
        "docker", "deploy", "workflow", "cron", "task", "agent",
    ):
        return f"Tool {tool_name} is forbidden in readonly sub-agent policy."
    if policy == "no_writes" and tool_name in write_tools:
        return f"Tool {tool_name} is forbidden in no_writes sub-agent policy."
    if policy == "no_writes" and tool_name in ("docker", "deploy", "workflow", "agent"):
        return f"Tool {tool_name} is forbidden in no_writes sub-agent policy."
    return None


# ---------------------------------------------------------------------------
# Search manager (web_search)
# ---------------------------------------------------------------------------

_search_manager: Any = None


def get_search_manager() -> Any:
    """Return the process-wide web-search manager, lazily creating it."""
    global _search_manager
    if _search_manager is None:
        from encre.search.manager import EncreSearchManager

        _search_manager = EncreSearchManager()
    return _search_manager


def set_search_manager(manager: Any) -> None:
    """Override the web-search manager (used for teardown/reset)."""
    global _search_manager
    _search_manager = manager
