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

"""Unified ``computer`` tool -- one schema for browser, desktop & VLM.

This is the single computer-control surface exposed to the model,
regardless of whether the driving model has vision.  It merges the
former ``computer_use``, ``vlm_computer_use``, ``desktop`` and
``device_display`` tools into one ``action`` enum with a ``target``
field that picks the browser or desktop backend behind the scenes.

What makes this tool work for *both* text and vision models:

1. ``observe`` -- one call returns the current screen as a screenshot
   (injected back into the model context as an image content block via
   the loop's pending-images bypass slot), plus an OCR text digest, the
   cursor position, DPI scale, and an optional accessibility-tree
   digest.  Vision models read the pixels; text models read the OCR +
   a11y text.
2. Action results that carry screenshots (``screenshot``,
   ``screenshot_viewport``, ``take_screenshot_png``, ``get_elements``)
   have their base64 payloads rerouted to the same bypass slot, so the
   JSON envelope the model reads stays small while the image still
   reaches vision-capable models.
3. ``delegate`` -- hand a high-level goal to a VLM-driven loop that
   screenshots -> reasons -> acts -> verifies until done (for text-only
   drivers that still want pixel-level automation).

The trajectory buffer, step budget, macros and the persistent macro
library carry over from the former ``computer_use`` tool unchanged.
"""

import base64
import json
import logging
from typing import Any

from encre.tools import runtime as _runtime
from encre.tools.base import build_tool

logger = logging.getLogger("encre.tools.computer")

# Process-wide session.  A long-lived agent process can accumulate a
# lot of actions, so we cap the in-memory trajectory at 200 entries.
_session: Any = None


def _get_session() -> Any:
    """Return the lazily-initialised :class:`EncreComputerUseSession`.

    The session is created on the first call and cached module-wide so
    consecutive tool invocations share the same browser/desktop context
    and trajectory. We also try to reuse the underlying Chromium session
    created by the browser tool -- doing so avoids spinning up two
    separate browser processes for the same tab, which would otherwise
    confuse the model and waste memory.

    If the process-wide engine-requester was registered *after* the
    session was already created, we propagate it now so actions that
    need to install the chromium binary still prompt the caller
    correctly.
    """
    global _session
    if _session is None:
        from encre.computer.computer_use import EncreComputerUseSession

        # Try to share the underlying EncreBrowserSession with the
        # browser tool to avoid creating two separate Chromium processes.
        browser_session = None
        try:
            from encre.tools.runtime import get_browser_session as _browser_get_session

            browser_session = _browser_get_session()
        except Exception:
            pass

        _session = EncreComputerUseSession(
            engine_requester=_runtime.get_engine_requester(),
            browser_session=browser_session,
        )
    elif (
        _runtime.get_engine_requester() is not None
        and getattr(
            _session,
            "_engine_requester",
            None,
        )
        is None
    ):
        # Session pre-dated the requester registration; propagate now.
        _session.set_engine_requester(_runtime.get_engine_requester())
    return _session


# Module-level engine-requester storage.  The agent's
# ``_install_requester_on_computer_use`` calls
# ``set_engine_requester(req)`` once at startup; from then on
# any new / existing singleton session picks it up via
# ``_get_session``.  This is necessary because the tool wrapper
# (``computer_tool``) is a thin object built by
# ``build_tool`` and cannot carry the requester as an attribute.
def set_engine_requester(requester: Any) -> None:
    """Install an engine-install requester on the lazily-created
    computer-use session.

    Called by :class:`EncreAgent` during ``__init__`` so any
    subsequent browser action that needs the chromium binary
    will route the install prompt through the requester instead
    of raising an error to the LLM.

    Does NOT eagerly create the session - the requester is stored
    and propagated on first actual use via :func:`_get_session`.
    """
    _runtime.set_engine_requester(requester)
    if _session is not None and hasattr(_session, "set_engine_requester"):
        _session.set_engine_requester(requester)


def _action_enum() -> list[str]:
    """Canonical action enum shown in the tool schema."""
    from encre.computer.computer_use import VALID_ACTIONS

    return sorted(set(VALID_ACTIONS) | set(_META_ACTIONS) | _TOOL_ACTIONS)


#: Meta-actions handled in-process (never dispatched to a backend).
_META_ACTIONS: frozenset[str] = frozenset(
    {
        "list_actions",
        "trajectory",
        "reset",
        "cancel",
        "recent_screenshots",
        "compress_trajectory",
        "save_state",
        "load_state",
        "register_macro",
        "execute_macro",
        "replay_trajectory",
        "metrics",
        "set_no_op_as_failure",
        "library_register",
        "library_search",
        "library_list",
        "library_save",
        "library_remove",
        "export_bundle",
        "replay_bundle",
    }
)

#: Tool-level actions implemented in this file (not by the session).
_TOOL_ACTIONS: frozenset[str] = frozenset(
    {
        "observe",
        "display_info",
        "delegate",
    }
)


# ---------------------------------------------------------------------------
# Screenshot bypass slot
# ---------------------------------------------------------------------------


def _attach_screenshots(*b64_frames: str | None) -> int:
    """Write base64 PNG frames to the loop's pending-images bypass slot.

    The loop's tools phase pops the slot after the tool returns and
    stores the frames as ``msg["images"]`` on the tool-result message;
    the model phase then expands them into real image content blocks.
    This is how screenshots reach vision-capable models without ever
    flowing through the text pipeline (safety / hooks / redaction all
    operate on strings only).

    Returns the number of frames attached.
    """
    images = [{"media_type": "image/png", "data": str(b)} for b in b64_frames if isinstance(b, str) and b]
    if not images:
        return 0
    try:
        _runtime.set_pending_tool_images(
            _runtime.get_session_id(),
            _runtime.get_current_tool_call_id(),
            images,
        )
    except Exception as exc:
        logger.warning("[computer] failed to attach screenshots: %s", exc)
        return 0
    return len(images)


# ---------------------------------------------------------------------------
# VLM delegation helpers (absorbed from the former vlm_computer_use tool)
# ---------------------------------------------------------------------------

#: Default system prompt used when no task template is specified.
#: Mirrors the generic VLM prompt in :mod:`encre.computer.vlm` so the
#: behaviour matches whether you go through the session or the tool.
_DEFAULT_SYSTEM_PROMPT = (
    "You are a vision-guided computer-use agent.  Look at the screenshot "
    "and the user's goal, then respond with **exactly one** JSON object "
    "(optionally inside a ```json``` fence) describing the next action. "
    "Schema:\n"
    "{\n"
    '  "action": "click" | "double_click" | "right_click" | "type" | '
    '"press" | "key" | "hotkey" | "scroll" | "drag" | "move" | "hover" | '
    '"wait" | "screenshot" | "triple_click" | "done",\n'
    '  "x": <int pixel coordinate>,\n'
    '  "y": <int pixel coordinate>,\n'
    '  "x2": <int pixel coordinate, for drag>,\n'
    '  "y2": <int pixel coordinate, for drag>,\n'
    '  "text": "<for type action: the literal text to type>",\n'
    '  "key": "<for press / key: the key name, e.g. \\"Enter\\">",\n'
    '  "keys": ["<for hotkey: ordered list of key names, e.g. [\\"Control\\", \\"c\\"]>"],\n'
    '  "scroll_amount": <for scroll: positive=down, negative=up>,\n'
    '  "ms": <for wait: milliseconds to sleep>,\n'
    '  "reasoning": "<one sentence on why this action advances the goal>",\n'
    '  "confidence": <float 0-1>\n'
    "}\n"
    'Set action to "done" once the goal is achieved.'
)

_vlm_backend: Any = None


def _resolve_vlm_backend() -> Any:
    """Return a cached VLM backend, resolving it from environment variables
    on first call.

    Priority: OpenAI-compatible (``OPENAI_API_KEY``) -> Anthropic
    (``ANTHROPIC_API_KEY``).  The model, base URL, and timeout are
    customisable via ``VLM_*`` environment variables.
    """
    global _vlm_backend
    if _vlm_backend is not None:
        return _vlm_backend

    from encre.computer.vlm import AnthropicVLM, OpenAICompatibleVLM

    errors: list[str] = []

    # -- OpenAI-compatible --
    api_key = __import__("os").environ.get("OPENAI_API_KEY", "")
    if api_key:
        try:
            _vlm_backend = OpenAICompatibleVLM(
                api_key=api_key,
                base_url=__import__("os").environ.get(
                    "VLM_OPENAI_BASE_URL",
                    __import__("os").environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                ),
                model=__import__("os").environ.get("VLM_MODEL", "gpt-4o-mini"),
                timeout=float(__import__("os").environ.get("VLM_TIMEOUT", "60")),
            )
            return _vlm_backend
        except Exception as exc:
            errors.append(f"OpenAI: {exc}")

    # -- Anthropic --
    api_key = __import__("os").environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            _vlm_backend = AnthropicVLM(
                api_key=api_key,
                base_url=__import__("os").environ.get(
                    "VLM_ANTHROPIC_BASE_URL",
                    __import__("os").environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                ),
                model=__import__("os").environ.get("VLM_MODEL", "claude-3-5-sonnet-20241022"),
                timeout=float(__import__("os").environ.get("VLM_TIMEOUT", "60")),
            )
            return _vlm_backend
        except Exception as exc:
            errors.append(f"Anthropic: {exc}")

    raise RuntimeError(
        "No VLM backend configured.  Set OPENAI_API_KEY or "
        "ANTHROPIC_API_KEY environment variable." + (f"  Errors: {'; '.join(errors)}" if errors else "")
    )


def _resolve_prompt_bundle(template_name: str) -> dict[str, Any]:
    """Return the prompt bundle for the given template name.

    Falls back to the generic defaults when ``template_name`` is empty
    or unknown -- callers can use whatever the model returns without
    having to handle the error themselves.
    """
    bundle: dict[str, Any] = {
        "system": _DEFAULT_SYSTEM_PROMPT,
        "step_preamble": "",
        "done_criteria": "",
        "action_hints": {},
        "template": "",
    }
    if not template_name:
        return bundle
    try:
        from encre.computer.vlm import get_task_template

        template = get_task_template(template_name)
    except (ImportError, KeyError) as exc:
        logger.warning(
            "[computer] template %r unavailable (%s); falling back to generic prompt",
            template_name,
            exc,
        )
        return bundle
    bundle["system"] = template.get("system", _DEFAULT_SYSTEM_PROMPT)
    bundle["step_preamble"] = template.get("step_preamble", "")
    bundle["done_criteria"] = template.get("done_criteria", "")
    bundle["action_hints"] = dict(template.get("action_hints") or {})
    bundle["template"] = template_name
    return bundle


def _decision_to_action_dict(d: Any) -> dict[str, Any]:
    """Convert a VLM decision into the unified computer-use action payload.

    Different VLM backends return slightly different field names and
    types for the same intent (e.g. ``press`` vs ``key`` vs
    ``press_key``, integer vs float coordinates, ``+``-separated hotkey
    strings vs lists). This adapter normalises every known shape into
    the canonical schema the session's dispatcher understands.

    The default branch -- for actions the VLM never learned to emit --
    falls back to a click at the given (x, y) because that is by far
    the most common intent and gives the model a plausible result
    rather than an error.
    """
    act = str(getattr(d, "action", "") or "").strip().lower()
    payload: dict[str, Any] = {}
    if act in ("press", "key", "press_key"):
        payload = {"action": "press_key", "key": str(getattr(d, "key", "") or "Return")}
    elif act == "type":
        payload = {"action": "type", "text": str(getattr(d, "text", "") or "")}
    elif act == "hotkey":
        keys_attr = getattr(d, "keys", None) or []
        if isinstance(keys_attr, str):
            keys_attr = [k for k in keys_attr.split("+") if k]
        payload = {"action": "hotkey", "keys": [str(k) for k in keys_attr]}
    elif act == "scroll":
        payload = {
            "action": "scroll",
            "scroll_amount": int(getattr(d, "scroll_amount", 0) or 0),
        }
    elif act == "drag":
        payload = {
            "action": "drag",
            "x": int(getattr(d, "x", 0) or 0),
            "y": int(getattr(d, "y", 0) or 0),
            "x2": int(getattr(d, "x2", 0) or 0),
            "y2": int(getattr(d, "y2", 0) or 0),
        }
    elif act in ("move", "move_mouse", "hover"):
        payload = {
            "action": "move_mouse" if act != "hover" else "hover",
            "x": int(getattr(d, "x", 0) or 0),
            "y": int(getattr(d, "y", 0) or 0),
        }
    elif act in ("double_click", "right_click"):
        payload = {
            "action": act,
            "x": int(getattr(d, "x", 0) or 0),
            "y": int(getattr(d, "y", 0) or 0),
        }
    elif act == "triple_click":
        payload = {
            "action": "triple_click",
            "x": int(getattr(d, "x", 0) or 0),
            "y": int(getattr(d, "y", 0) or 0),
        }
    elif act == "wait":
        ms_attr = getattr(d, "ms", None)
        if ms_attr is None:
            # Older models used `text` to encode wait seconds.
            try:
                ms = int(float(getattr(d, "text", 0.5) or 0.5) * 1000)
            except (TypeError, ValueError):
                ms = 500
        else:
            try:
                ms = int(ms_attr)
            except (TypeError, ValueError):
                ms = 500
        payload = {"action": "wait", "ms": max(0, ms)}
    elif act == "screenshot":
        payload = {"action": "screenshot"}
    elif act == "done":
        payload = {"action": "done"}
    else:
        # Default to a click -- the most common action.
        payload = {
            "action": "click",
            "x": int(getattr(d, "x", 0) or 0),
            "y": int(getattr(d, "y", 0) or 0),
        }
    return payload


async def _run_delegate(
    goal: str,
    max_steps: int,
    template_name: str,
) -> dict[str, Any]:
    """Run the VLM-driven perceive/act loop (former vlm_computer_use).

    Uses a dedicated desktop-targeted session so the delegation loop
    never perturbs the caller's browser trajectory.
    """
    import asyncio
    import time as _time

    prompt_bundle = _resolve_prompt_bundle(template_name)
    system_prompt = str(prompt_bundle.get("system", _DEFAULT_SYSTEM_PROMPT))
    step_preamble = str(prompt_bundle.get("step_preamble", "") or "")
    done_criteria = str(prompt_bundle.get("done_criteria", "") or "")
    action_hints: dict[str, str] = dict(prompt_bundle.get("action_hints") or {})

    try:
        vlm = _resolve_vlm_backend()
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}

    from encre.computer.computer_use import EncreComputerUseSession
    from encre.computer.vlm import parse_decision

    d_session = EncreComputerUseSession(
        default_target="desktop",
        max_steps=max_steps + 5,
        auto_screenshot=True,
        retry_on_failure=1,
        fallback_enabled=True,
    )

    decisions: list[Any] = []
    last_error = ""
    last_action_verb = ""
    last_png_b64 = ""
    for step in range(max_steps):
        # Take a fresh screenshot via the delegation session.
        shot = await d_session.dispatch({"action": "screenshot"})
        if not shot.get("success") or not shot.get("result"):
            last_error = shot.get("error") or "screenshot failed"
            break
        png_b64 = str(shot["result"].get("screenshot_b64", "") or "")
        if not png_b64:
            last_error = "screenshot returned no image"
            break
        last_png_b64 = png_b64
        try:
            png_bytes = base64.b64decode(png_b64)
        except Exception as exc:
            last_error = f"screenshot b64 decode failed: {exc}"
            break

        # Build the action-history block.  We feed both a textual
        # summary (so the model sees what was tried) and the last
        # 2 step screenshots (so the model can correlate the
        # current frame with what just happened).  This is the
        # same shape Codex/Manus use.
        recent_steps = d_session.trajectory.recent_with_screenshots(10)
        history_lines: list[str] = []
        attached_screenshots: list[str] = []
        for i, entry in enumerate(recent_steps[:-1]):  # exclude the
            # screenshot we just took -- it's already the model input
            history_lines.append(f"  {i + 1}. {entry['summary']}")
            if entry.get("screenshot_b64"):
                attached_screenshots.append(f"[past-frame-{i + 1} attached below]")
        history_text = "\n".join(history_lines) if history_lines else "  (no prior actions)"

        # Bound the in-memory trajectory before it grows unbounded.
        # 5 recent screenshots ~= 5 * 1 MB base64 = 5 MB; plenty of
        # room for hundreds of action descriptions in between.
        if d_session.trajectory.total_screenshot_bytes() > 8 * 1024 * 1024:
            d_session.trajectory.compress(strategy="window", keep_screenshots=3)

        # Per-action hint: surface the hint for the *most recent*
        # action so the VLM gets a soft "you did X, be aware of Y"
        # nudge on the next step.
        hint_text = ""
        if last_action_verb and last_action_verb in action_hints:
            hint_text = f"\nHint for the previous action ({last_action_verb}): {action_hints[last_action_verb]}"
        preamble = step_preamble or (
            "The attached image is the current desktop.  Past frames "
            "from earlier in this session are attached below in "
            "chronological order.  Respond with a single JSON object "
            "describing the next action."
        )
        user_prompt = (
            f"Goal: {goal}\n"
            f"{preamble}\n"
            f"Step {step + 1}/{max_steps}.  "
            f"{done_criteria}\n"
            f"Recent actions (most recent last):\n{history_text}\n"
            f"Past frames: {'; '.join(attached_screenshots) or '(none)'}"
            f"{hint_text}"
        )
        try:
            raw = await asyncio.to_thread(vlm, png_bytes, system_prompt, user_prompt)
        except Exception as exc:
            last_error = f"VLM call failed: {exc}"
            break
        try:
            decision = parse_decision(raw)
        except ValueError as exc:
            last_error = str(exc)
            break
        decisions.append(decision)
        last_action_verb = decision.action

        if decision.action == "done":
            return {
                "success": True,
                "steps_taken": step + 1,
                "decisions": [d.to_dict() for d in decisions],
                "final_message": decision.reasoning or "done",
                "trajectory": d_session.trajectory_dict(),
                "template": prompt_bundle.get("template", ""),
            }

        # Translate decision -> unified action and dispatch.
        action_payload = _decision_to_action_dict(decision)
        try:
            dispatch_result = await d_session.dispatch(action_payload)
        except Exception as exc:
            logger.warning("[computer] delegate dispatch raised: %s", exc, exc_info=True)
            last_error = f"dispatch failed: {exc}"
            continue
        if not dispatch_result.get("success"):
            last_error = (
                f"{dispatch_result.get('error') or 'dispatch failed'} "
                f"(retries={dispatch_result.get('retries_used', 0)}, "
                f"fallback={dispatch_result.get('fallback_used', False)})"
            )
        # Brief settle pause so animations / network calls have time.
        await asyncio.to_thread(_time.sleep, 0.3)

    return {
        "success": False,
        "steps_taken": len(decisions),
        "decisions": [d.to_dict() for d in decisions],
        "error": last_error or f"max_steps={max_steps} reached without a 'done' action",
        "trajectory": d_session.trajectory_dict(),
        "template": prompt_bundle.get("template", ""),
        "_last_png_b64": last_png_b64,
    }


# ---------------------------------------------------------------------------
# Observe helper
# ---------------------------------------------------------------------------


async def _run_observe(session: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Capture the screen and return a text digest + image bypass.

    One call gives the model everything it needs to reason about the
    screen: the screenshot itself (bypass slot -> image content block),
    screen geometry (size / DPI / cursor), an OCR text digest (so
    text-only models can "see"), and optionally the Windows UIA
    accessibility tree.
    """
    shot = await session.dispatch({"action": "screenshot", "target": "desktop"})
    if not shot.get("success"):
        return {
            "success": False,
            "action": "observe",
            "error": shot.get("error") or "screenshot failed",
        }

    screen_meta = dict(shot.get("result") or {})
    main_b64 = str(screen_meta.pop("screenshot_b64", "") or "")

    # Active-window capture as a second frame -- catches DirectX /
    # Direct2D content and occluded regions the compositor strips
    # from the full-desktop screenshot.
    window_meta: dict[str, Any] = {}
    window_b64 = ""
    try:
        desk = session._ensure_desktop()
        cap = desk._capture_active_window_png()
        if cap is not None and getattr(cap, "png_bytes", None):
            window_b64 = base64.b64encode(cap.png_bytes).decode("ascii")
            window_meta = {
                "left": int(cap.left),
                "top": int(cap.top),
                "width": int(cap.width),
                "height": int(cap.height),
            }
    except Exception:
        window_meta = {}

    attached = _attach_screenshots(main_b64, window_b64)
    payload: dict[str, Any] = {
        "success": True,
        "action": "observe",
        "screenshots_attached": attached,
        "screen": screen_meta,
        "active_window": window_meta,
    }

    # OCR text digest (on by default): text-only models read the
    # screen through this; vision models use it as a clickable
    # coordinate index.  Always run on the main screenshot so the
    # returned bounding boxes live in the same coordinate space the
    # click actions use (pixels of the last screenshot).
    if bool(kwargs.get("include_ocr", True)) and main_b64:
        try:
            from encre.computer.ocr import ocr_image

            try:
                min_len = int(kwargs.get("min_text_length", 2))
            except (TypeError, ValueError):
                min_len = 2
            elements = [e for e in ocr_image(base64.b64decode(main_b64)) if len(str(e.get("text", ""))) >= min_len]
            payload["ocr_source"] = "screenshot"
            payload["ocr_count"] = len(elements)
            payload["ocr_elements"] = elements[:80]
        except Exception as exc:
            payload["ocr_error"] = str(exc)

    # Optional accessibility tree (Windows UIA) -- structured element
    # data beats OCR for native applications.
    if bool(kwargs.get("include_a11y", False)):
        try:
            try:
                max_depth = int(kwargs.get("max_depth", 6))
            except (TypeError, ValueError):
                max_depth = 6
            try:
                max_nodes = int(kwargs.get("max_nodes", 300))
            except (TypeError, ValueError):
                max_nodes = 300
            tree = await session.dispatch(
                {
                    "action": "accessibility_tree",
                    "target": "desktop",
                    "max_depth": max_depth,
                    "max_nodes": max_nodes,
                }
            )
            payload["accessibility"] = tree.get("result") if tree.get("success") else {"error": tree.get("error") or "unavailable"}
        except Exception as exc:
            payload["accessibility_error"] = str(exc)

    return payload


# ---------------------------------------------------------------------------
# Tool execute
# ---------------------------------------------------------------------------


async def _computer_execute(**kwargs: Any) -> str:
    """Dispatch one computer action and return a JSON envelope.

    The envelope is always a JSON object.  Special ``action`` values
    handled in-process (everything else routes through the session):

    * ``"observe"`` -- screenshot + OCR digest + geometry (+ optional
      a11y tree); the screenshot is attached to the model context as
      an image, not inlined as base64 text.
    * ``"display_info"`` -- monitor layout / DPI metadata (former
      ``device_display`` tool).
    * ``"delegate"`` -- run a VLM-driven perceive/act loop toward a
      natural-language goal (former ``vlm_computer_use`` tool).
    * ``"list_actions"`` / ``"trajectory"`` / ``"reset"`` / ... --
      trajectory & macro-library meta-actions.
    """
    action = str(kwargs.get("action") or "").strip().lower()
    session = _get_session()

    if action == "list_actions":
        return json.dumps({"actions": _action_enum()}, ensure_ascii=False)

    if action == "trajectory":
        return json.dumps(
            {
                "trajectory": session.trajectory_dict(),
                "recent_summary": session.trajectory.recent_summary(20),
            },
            ensure_ascii=False,
        )

    if action == "reset":
        try:
            await session.close()
        finally:
            global _session
            _session = None
        return json.dumps({"success": True, "reset": True}, ensure_ascii=False)

    if action == "cancel":
        session.cancel()
        return json.dumps(
            {
                "success": True,
                "cancelled": True,
                "is_cancelled": session.is_cancelled,
            },
            ensure_ascii=False,
        )

    if action == "observe":
        return json.dumps(await _run_observe(session, kwargs), ensure_ascii=False)

    if action == "display_info":
        from encre.device.context.providers.display import DisplayInfoProvider

        data = await DisplayInfoProvider().collect_async()
        if data is None:
            return json.dumps(
                {
                    "success": False,
                    "action": "display_info",
                    "error": "Display information unavailable on this device.",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "action": "display_info",
                "displays": data,
            },
            ensure_ascii=False,
            default=str,
        )

    if action == "delegate":
        goal = str(kwargs.get("goal") or "").strip()
        if not goal:
            return json.dumps(
                {
                    "success": False,
                    "action": "delegate",
                    "error": "goal parameter is required",
                },
                ensure_ascii=False,
            )
        try:
            max_steps = int(kwargs.get("max_steps", 20))
        except (TypeError, ValueError):
            max_steps = 20
        max_steps = max(1, min(max_steps, 60))
        result = await _run_delegate(
            goal,
            max_steps,
            str(kwargs.get("template_name", "") or "").strip(),
        )
        # The final frame goes to the model context as an image, not
        # as base64 text inside the JSON envelope.
        last_b64 = str(result.pop("_last_png_b64", "") or "")
        attached = _attach_screenshots(last_b64)
        result["action"] = "delegate"
        result["screenshot_attached"] = attached > 0
        return json.dumps(result, ensure_ascii=False)

    if action == "recent_screenshots":
        try:
            n = int(kwargs.get("n") or 5)
        except (TypeError, ValueError):
            n = 5
        n = max(0, min(n, session.max_steps))
        raw_steps = session.trajectory.recent_with_screenshots(n)
        frames: list[str] = []
        steps: list[dict[str, Any]] = []
        for st in raw_steps:
            st = dict(st)
            b64 = str(st.pop("screenshot_b64", "") or "")
            if b64:
                frames.append(b64)
            steps.append(st)
        attached = _attach_screenshots(*frames)
        return json.dumps(
            {
                "success": True,
                "steps": steps,
                "screenshots_attached": attached,
                "total_screenshot_bytes": session.trajectory.total_screenshot_bytes(),
            },
            ensure_ascii=False,
        )

    if action == "compress_trajectory":
        try:
            keep = int(kwargs.get("keep_screenshots") or 5)
        except (TypeError, ValueError):
            keep = 5
        keep = max(0, min(keep, session.max_steps))
        cleared = session.trajectory.compress(
            strategy="window",
            keep_screenshots=keep,
        )
        return json.dumps(
            {
                "success": True,
                "cleared": cleared,
                "total_screenshot_bytes": session.trajectory.total_screenshot_bytes(),
            },
            ensure_ascii=False,
        )

    if action == "save_state":
        state = await session.save_state()
        # The desktop screenshot is binary -- reroute it to the image
        # bypass slot instead of stuffing bytes into the JSON envelope.
        if "desktop_screenshot_png" in state:
            png = state.pop("desktop_screenshot_png")
            if isinstance(png, bytes | bytearray):
                state["desktop_screenshot_attached"] = (
                    _attach_screenshots(
                        base64.b64encode(bytes(png)).decode("ascii"),
                    )
                    > 0
                )
        return json.dumps(
            {
                "success": True,
                "state": state,
            },
            ensure_ascii=False,
        )

    if action == "load_state":
        state = kwargs.get("state")
        if not isinstance(state, dict):
            return json.dumps(
                {
                    "success": False,
                    "error": "load_state requires a 'state' object",
                },
                ensure_ascii=False,
            )
        try:
            await session.load_state(state)
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": f"load_state failed: {exc}",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "loaded": True,
            },
            ensure_ascii=False,
        )

    if action == "register_macro":
        name = kwargs.get("name") or kwargs.get("macro_name")
        actions = kwargs.get("actions")
        if not isinstance(name, str) or not name:
            return json.dumps(
                {
                    "success": False,
                    "error": "register_macro requires 'name'",
                },
                ensure_ascii=False,
            )
        if not isinstance(actions, list):
            return json.dumps(
                {
                    "success": False,
                    "error": "register_macro requires 'actions' as a list",
                },
                ensure_ascii=False,
            )
        try:
            session.register_macro(name, actions)
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "registered": name,
                "step_count": len(actions),
            },
            ensure_ascii=False,
        )

    if action == "execute_macro":
        name = kwargs.get("name") or kwargs.get("macro_name")
        if not isinstance(name, str) or not name:
            return json.dumps(
                {
                    "success": False,
                    "error": "execute_macro requires 'name'",
                },
                ensure_ascii=False,
            )
        try:
            results = await session.execute_macro(
                name,
                stop_on_failure=bool(kwargs.get("stop_on_failure", True)),
            )
        except KeyError as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "macro": name,
                "results": results,
            },
            ensure_ascii=False,
        )

    if action == "replay_trajectory":
        steps = kwargs.get("steps")
        if not isinstance(steps, list):
            return json.dumps(
                {
                    "success": False,
                    "error": "replay_trajectory requires 'steps' as a list",
                },
                ensure_ascii=False,
            )
        try:
            start = int(kwargs.get("start") or 0)
        except (TypeError, ValueError):
            start = 0
        results = await session.replay(
            steps,
            start=start,
            stop_on_failure=bool(kwargs.get("stop_on_failure", True)),
        )
        return json.dumps(
            {
                "success": True,
                "results": results,
            },
            ensure_ascii=False,
        )

    if action == "metrics":
        return json.dumps(
            {
                "success": True,
                "metrics": session.metrics(),
            },
            ensure_ascii=False,
        )

    if action == "set_no_op_as_failure":
        # Toggle whether expect_change == False turns into a
        # ``NO_CHANGE`` failure (True) or a success-with-warning (False).
        enabled = bool(kwargs.get("enabled", True))
        session.no_op_as_failure = enabled
        return json.dumps(
            {
                "success": True,
                "no_op_as_failure": session.no_op_as_failure,
            },
            ensure_ascii=False,
        )

    # ---- macro library meta-actions ----
    # These expose the persistent MacroLibrary so an agent can
    # register / search / list / save shared action sequences
    # across sessions.

    if action == "library_register":
        from encre.computer.computer_use import MacroLibrary

        name = kwargs.get("name")
        actions = kwargs.get("actions")
        if not isinstance(name, str) or not name:
            return json.dumps(
                {
                    "success": False,
                    "error": "library_register requires 'name'",
                },
                ensure_ascii=False,
            )
        if not isinstance(actions, list):
            return json.dumps(
                {
                    "success": False,
                    "error": "library_register requires 'actions' as a list",
                },
                ensure_ascii=False,
            )
        try:
            lib = session.ensure_macro_library()
            entry = lib.register(
                name,
                actions,
                category=str(kwargs.get("category", "general")),
                description=str(kwargs.get("description", "")),
                author=str(kwargs.get("author", "")),
                tags=list(kwargs.get("tags") or []),
                overwrite=bool(kwargs.get("overwrite", False)),
            )
            lib.save()
        except (KeyError, TypeError, ValueError) as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "entry": entry.to_dict(),
            },
            ensure_ascii=False,
        )

    if action == "library_search":
        from encre.computer.computer_use import MacroLibrary

        try:
            lib = session.ensure_macro_library()
        except Exception:
            lib = MacroLibrary()
        results = lib.search(
            query=kwargs.get("query"),
            category=kwargs.get("category"),
            tag=kwargs.get("tag"),
        )
        return json.dumps(
            {
                "success": True,
                "results": [e.to_dict() for e in results],
                "total": len(results),
            },
            ensure_ascii=False,
        )

    if action == "library_list":
        from encre.computer.computer_use import MacroLibrary

        try:
            lib = session.ensure_macro_library()
        except Exception:
            lib = MacroLibrary()
        return json.dumps(
            {
                "success": True,
                "entries": [e.to_dict() for e in lib.list()],
                "total": len(lib),
            },
            ensure_ascii=False,
        )

    if action == "library_save":
        from encre.computer.computer_use import MacroLibrary

        try:
            lib = session.ensure_macro_library()
            count = lib.save()
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "saved": count,
                "path": lib.path,
            },
            ensure_ascii=False,
        )

    if action == "library_remove":
        from encre.computer.computer_use import MacroLibrary

        name = kwargs.get("name")
        if not isinstance(name, str) or not name:
            return json.dumps(
                {
                    "success": False,
                    "error": "library_remove requires 'name'",
                },
                ensure_ascii=False,
            )
        try:
            lib = session.ensure_macro_library()
            removed = lib.remove(name)
            if removed:
                lib.save()
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "removed": removed,
            },
            ensure_ascii=False,
        )

    # ---- cross-session bundle ----
    if action == "export_bundle":
        include_state = bool(kwargs.get("include_state", True))
        source = str(kwargs.get("source", ""))
        try:
            bundle = await session.export_bundle(
                include_state=include_state,
                source=source,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": f"export_bundle failed: {exc}",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "bundle": bundle,
            },
            ensure_ascii=False,
        )

    if action == "replay_bundle":
        bundle = kwargs.get("bundle")
        if not isinstance(bundle, dict):
            return json.dumps(
                {
                    "success": False,
                    "error": "replay_bundle requires 'bundle' as an object",
                },
                ensure_ascii=False,
            )
        try:
            start = int(kwargs.get("start") or 0)
        except (TypeError, ValueError):
            start = 0
        variables = kwargs.get("variables")
        results = await session.replay_bundle(
            bundle,
            start=start,
            stop_on_failure=bool(kwargs.get("stop_on_failure", True)),
            variables=variables if isinstance(variables, dict) else None,
            restore_state=bool(kwargs.get("restore_state", True)),
        )
        return json.dumps(
            {
                "success": True,
                "results": results,
            },
            ensure_ascii=False,
        )

    # All other actions go through the dispatcher.
    raw_result = await session.dispatch(kwargs)
    # Return a *model-friendly* envelope: reroute screenshots to the
    # image bypass slot, truncate bulky text fields, and attach a
    # one-line ``summary`` so the LLM has something it can actually
    # parse.  ``include_full_result`` / ``max_text_chars`` remain
    # available for debugging.
    return _summarize_for_model(raw_result, action, kwargs)


# ---------------------------------------------------------------------------
# Model-facing summary helper
# ---------------------------------------------------------------------------

#: Hard ceiling for embedded text fields (``get_all_text``, ``get_text``,
#: ``get_html``, ``evaluate_js`` string results, etc.).  Anything longer
#: is truncated with a trailing ``"...(truncated, N bytes omitted)"``
#: marker so the model can ask for the full result on demand.
MAX_TEXT_CHARS = 8_000

#: Keys whose string values are treated as "text" payloads by the
#: summariser.  When we see one of these in ``result`` (top level or
#: nested) we truncate to :data:`MAX_TEXT_CHARS`.
_TEXT_RESULT_KEYS = frozenset(
    {
        "text",
        "html",
        "innerText",
        "innerHTML",
        "inner_html",
        "value",
        "output",
        "data",
        "markdown",
        "rendered",
        "extracted",
    }
)

#: Keys whose string values are base64 screenshots.  These are popped
#: out of the envelope and rerouted to the image bypass slot so vision
#: models still receive them as real image content blocks.
_IMAGE_B64_KEYS = frozenset(
    {
        "screenshot_b64",
        "active_window_b64",
        "screenshot_base64",
        "window_screenshot_base64",
    }
)


def _truncate_string(value: Any, limit: int = MAX_TEXT_CHARS) -> tuple[Any, bool]:
    """Return ``(value, was_truncated)`` with strings bounded to ``limit``."""
    if isinstance(value, str) and len(value) > limit:
        return (
            value[:limit] + f"\n...(truncated, {len(value) - limit} bytes omitted)",
            True,
        )
    return value, False


def _scrub_payload(value: Any, *, limit: int) -> tuple[Any, dict[str, int]]:
    """Walk a JSON-like value, truncating long strings and counting cuts.

    Returns the cleaned value plus a small stats dict the summariser
    uses to build a human-readable note.  Truncation only happens for
    string values that look like text payloads (base64 screenshots are
    rerouted to the bypass slot before this runs).
    """
    stats = {"strings_truncated": 0, "bytes_saved": 0}
    if isinstance(value, str):
        if len(value) > limit and _looks_like_text(value):
            saved = len(value) - limit
            stats["strings_truncated"] += 1
            stats["bytes_saved"] += saved
            return (
                value[:limit] + f"\n...(truncated, {saved} bytes omitted)",
                stats,
            )
        return value, stats
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k in _TEXT_RESULT_KEYS:
                new_v, sub = _scrub_payload(v, limit=limit)
                stats["strings_truncated"] += sub["strings_truncated"]
                stats["bytes_saved"] += sub["bytes_saved"]
                cleaned[k] = new_v
            else:
                new_v, sub = _scrub_payload(v, limit=limit)
                stats["strings_truncated"] += sub["strings_truncated"]
                stats["bytes_saved"] += sub["bytes_saved"]
                cleaned[k] = new_v
        return cleaned, stats
    if isinstance(value, list):
        cleaned_list: list[Any] = []
        for item in value:
            new_item, sub = _scrub_payload(item, limit=limit)
            stats["strings_truncated"] += sub["strings_truncated"]
            stats["bytes_saved"] += sub["bytes_saved"]
            cleaned_list.append(new_item)
        return cleaned_list, stats
    return value, stats


def _looks_like_text(value: str) -> bool:
    """Heuristic: is this string a text payload (not a base64 blob)?

    Base64 screenshots are pure ASCII alphanumerics + ``+/=`` and have
    a very low whitespace / non-alnum ratio; natural text has a lot
    more variation.  We use a simple character-class ratio instead of
    a regex so this stays fast on large strings.
    """
    if not value:
        return False
    sample = value[:512]
    alnum = sum(c.isalnum() for c in sample)
    slash_eq = sum(c in "+/=" for c in sample)
    if alnum + slash_eq == 0:
        return False
    # base64 -> about 8% alnum+slash+eq, no whitespace, no newlines
    if "\n" in sample or "  " in sample:
        return True
    text_like = alnum + slash_eq
    return not (text_like / max(1, len(sample)) > 0.98 and slash_eq < 2)


def _make_summary(result: dict[str, Any], action: str) -> str:
    """Build a one-line human-readable summary of an action result.

    The summary is what the LLM actually reads; we keep it short
    (<= 200 chars) and structured (``verb: target -> outcome``) so
    it's easy to scan when the model is building a multi-step plan.
    """
    if not isinstance(result, dict):
        return f"{action}: done"
    if not result.get("success", True):
        kind = result.get("failure_kind") or result.get("error") or "failed"
        return f"{action}: failed ({kind})"
    page_changed = result.get("page_changed")
    if page_changed is True:
        return f"{action}: ok, page changed"
    if page_changed is False:
        return f"{action}: ok but page did NOT change (possible dead element)"
    return f"{action}: ok"


def _summarize_for_model(
    result: Any,
    action: str,
    kwargs: dict[str, Any],
) -> str:
    """Convert a raw dispatch result into a model-friendly JSON envelope.

    Goals
    -----
    1. Never hand the LLM a multi-kilobyte base64 string.  Screenshots
       are rerouted to the loop's pending-images bypass slot, which the
       tools phase attaches to the tool-result message as real image
       content blocks (vision models see them; text models just get the
       ``screenshot_attached`` marker).
    2. Never hand the LLM 200 KB of page text.  String fields under
       :data:`_TEXT_RESULT_KEYS` are truncated to :data:`MAX_TEXT_CHARS`
       bytes.
    3. Always include a one-line ``summary`` so the model can read the
       outcome without parsing the full envelope.
    4. Honour the caller's opt-ins (``include_full_result``,
       ``max_text_chars``) for debugging.
    """
    include_full = bool(kwargs.get("include_full_result", False))
    max_chars = max(0, int(kwargs["max_text_chars"])) if isinstance(kwargs.get("max_text_chars"), int) else MAX_TEXT_CHARS

    if not isinstance(result, dict):
        # Non-dict result (shouldn't happen, but be defensive).
        envelope: dict[str, Any] = {
            "success": True,
            "action": action,
            "result": result,
            "summary": _make_summary({"success": True}, action),
        }
        return json.dumps(envelope, ensure_ascii=False)

    envelope = dict(result)  # shallow copy so we don't mutate caller's view

    # 1. Reroute every base64 screenshot field to the image bypass
    #    slot; leave a tiny marker in the text envelope.
    frames: list[str] = []
    top_b64 = envelope.pop("screenshot_b64", None)
    if isinstance(top_b64, str) and top_b64:
        frames.append(top_b64)
        envelope["screenshot_captured"] = True
    else:
        envelope.setdefault("screenshot_captured", False)
    aw_b64 = envelope.pop("active_window_b64", None)
    if isinstance(aw_b64, str) and aw_b64:
        frames.append(aw_b64)
        envelope["active_window_captured"] = True

    # 1b. The ``result`` sub-payload may also carry images (e.g. the
    #     browser ``screenshot_viewport`` report).  Pop those too.
    sub = envelope.get("result")
    if isinstance(sub, dict):
        cleaned_sub = dict(sub)
        for key in _IMAGE_B64_KEYS:
            b64 = cleaned_sub.pop(key, None)
            if isinstance(b64, str) and b64:
                frames.append(b64)
        if len(cleaned_sub) != len(sub):
            envelope["result"] = cleaned_sub
    attached = _attach_screenshots(*frames)
    if frames:
        envelope["screenshot_attached"] = attached > 0
        envelope.setdefault(
            "notes",
            [],
        ).append(
            "screenshot(s) attached to this tool result as image(s); look at them before deciding the next action"
            if attached
            else "screenshot captured but could not be attached (no tool-call context); it remains in the session trajectory"
        )

    # 2. Truncate bulky text in the ``result`` sub-payload.
    if "result" in envelope and not include_full:
        cleaned, stats = _scrub_payload(envelope["result"], limit=max_chars)
        envelope["result"] = cleaned
        if stats["strings_truncated"]:
            envelope.setdefault("notes", []).append(
                f"truncated {stats['strings_truncated']} string(s), "
                f"saved {stats['bytes_saved']} bytes; ask for the full "
                "result by calling computer with "
                "include_full_result=true"
            )

    # 3. Build / refresh the summary line.
    envelope["summary"] = _make_summary(envelope, action)

    return json.dumps(envelope, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


# Lazy list so the schema is correct at tool-construction time without
# having to import computer_use at module load.
def _build_schema() -> dict[str, Any]:
    """Build the JSON schema describing every parameter the tool accepts.

    The schema is built lazily because the action enum comes from
    :mod:`encre.computer.computer_use` (which we do not want to import at
    module load -- that would pull in a lot of platform-specific
    bindings). Each parameter description is written for the LLM to
    read, so it doubles as documentation.
    """
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": _action_enum(),
                "description": (
                    "Required. The computer action to perform. Cross-target "
                    "actions (click, type, screenshot, ...) honour `target`; "
                    "browser-only actions (navigate, fill_form, ...) "
                    "auto-route to the browser; desktop-only actions "
                    "(triple_click, clipboard_*, ...) route to the desktop. "
                    "Start every screen-interaction task with `observe` -- "
                    "it returns the current screen as an image (vision "
                    "models) plus an OCR text digest (all models)."
                ),
            },
            "target": {
                "type": "string",
                "enum": ["browser", "desktop", "auto"],
                "description": (
                    "Which backend a cross-target action routes to. "
                    "'browser' (default) uses the Chromium session, "
                    "'desktop' uses OS-level input, 'auto' infers from "
                    "the action's canonical target."
                ),
            },
            "x": {
                "type": "integer",
                "description": (
                    "X coordinate for click / double_click / right_click / "
                    "triple_click / move_mouse / hover / drag start / scroll "
                    "anchor / file_drop target. Physical screenshot pixels "
                    "for desktop, viewport pixels for browser."
                ),
            },
            "y": {
                "type": "integer",
                "description": "Y coordinate, same space as `x`.",
            },
            "x2": {
                "type": "integer",
                "description": "End X coordinate for drag (drop target).",
            },
            "y2": {
                "type": "integer",
                "description": "End Y coordinate for drag (drop target).",
            },
            "text": {
                "type": "string",
                "description": (
                    "type / type_at: the literal text to type. click_text / "
                    "find_text: the text to search for. clipboard_set: the "
                    "content to place on the clipboard."
                ),
            },
            "query": {
                "type": "string",
                "description": ("click_text / find_text / click_by_role: text or accessible role/name to match."),
            },
            "tag": {
                "type": "string",
                "description": "get_elements / get_all_text: optional HTML tag filter.",
            },
            "key": {
                "type": "string",
                "description": ("press_key / key: single key name (e.g. 'enter', 'tab', 'escape', 'f1'). Use `keys` for combinations."),
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": ('hotkey: ordered key list, pressed sequentially then released in reverse (e.g. ["ctrl", "c"]).'),
            },
            "scroll_amount": {
                "type": "integer",
                "description": ("scroll: positive scrolls down, negative scrolls up. Out-of-range values return an error."),
            },
            "selector": {
                "type": "string",
                "description": (
                    "Browser CSS / XPath selector for click, type, hover, "
                    "fill_form, screenshot_viewport and friends. When set "
                    "the coordinate fields are ignored."
                ),
            },
            "fields": {
                "type": "object",
                "description": ("fill_form: mapping of selector -> value to fill multiple inputs in one call."),
            },
            "tab_index": {
                "type": "integer",
                "description": ("switch_tab / close_tab: 0-based index of the tab. Out-of-range values return an error."),
            },
            "option_value": {
                "type": "string",
                "description": (
                    "select_option: value to match against the option. "
                    "Interpreted according to `option_by` (default "
                    "matches the option's `value` attribute)."
                ),
            },
            "option_by": {
                "type": "string",
                "enum": ["value", "label", "index"],
                "description": (
                    "select_option: how option_value is matched. "
                    "'value' (default) matches the value attribute, "
                    "'label' matches visible text, 'index' treats "
                    "option_value as a 0-based position."
                ),
            },
            "attr_name": {
                "type": "string",
                "description": ("get_attribute / get_property: name of the DOM attribute or element property to read."),
            },
            "js_code": {
                "type": "string",
                "description": (
                    "execute_js: JavaScript source to evaluate in the active page context. Return value is serialised back to the caller."
                ),
            },
            "fuzzy": {
                "type": "boolean",
                "description": (
                    "click_text / find_text: when True, treat `query` as "
                    "ordered whitespace-separated tokens that must all "
                    "appear in the matched text. Default False."
                ),
            },
            "exact": {
                "type": "boolean",
                "description": (
                    "click_text / find_text / click_by_role: when True, "
                    "require the match to equal `query` exactly rather "
                    "than as a substring. Default False."
                ),
            },
            "occurrence": {
                "type": "integer",
                "description": (
                    "click_text / find_text: 1-based index of the match to use when the same text appears multiple times. Default 1."
                ),
            },
            "ms": {
                "type": "integer",
                "description": (
                    "wait: milliseconds to sleep before returning. Use short values for animation settle, longer for network waits."
                ),
            },
            "button": {
                "type": "string",
                "enum": ["left", "middle", "right"],
                "description": ("Mouse button for click / click_text. Default 'left'."),
            },
            "coord_space": {
                "type": "string",
                "enum": ["auto", "physical", "logical"],
                "description": (
                    "Desktop coordinate system. 'physical' = raw "
                    "screenshot pixels, 'logical' = OS-scaled pixels, "
                    "'auto' (default) infers from magnitude. Ignored "
                    "for browser actions."
                ),
            },
            "accept": {
                "type": "boolean",
                "description": ("set_dialog_handler: True to auto-accept alert / confirm dialogs, False to dismiss them."),
            },
            "prompt_text": {
                "type": "string",
                "description": ("set_dialog_handler: text to inject into a prompt() dialog before accepting. Optional."),
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("set_file_chooser_handler / file_drop: list of absolute file paths to upload or drop."),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "wait_for_selector timeout in milliseconds. The action fails if the selector does not appear within this window."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "When True, dispatch returns a preview report "
                    "(selector_present / text_match_count / would_send) "
                    "without mutating the UI. Use to sanity-check a "
                    "click target before committing. Default False."
                ),
            },
            "keep_screenshots": {
                "type": "integer",
                "description": (
                    "compress_trajectory: number of most recent steps "
                    "to keep screenshots for; older screenshots are "
                    "dropped (text action logs are retained). Defaults "
                    "to 5."
                ),
            },
            "n": {
                "type": "integer",
                "description": (
                    "recent_screenshots: how many recent steps to "
                    "return. Screenshots ride along as attached images. "
                    "Defaults to 5. Clamped to max_steps."
                ),
            },
            "expect_change": {
                "type": "boolean",
                "description": (
                    "When True, the dispatcher verifies the DOM / "
                    "screen state hash changed after the action and "
                    "returns page_changed: bool plus a warning if not. "
                    "Use to catch no-op clicks on dead elements. "
                    "Default False."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": (
                    "Per-action timeout in milliseconds. Overrides the session default. Omit / null to use the session default."
                ),
            },
            "state": {
                "type": "object",
                "description": ("load_state: dict returned from a prior save_state call. Restores cookies, URL, and localStorage."),
            },
            "name": {
                "type": "string",
                "description": (
                    "register_macro / execute_macro / library_register / "
                    "library_remove: name of the macro. "
                    "find_element_by_name: substring to match against "
                    "the accessible name (case-insensitive)."
                ),
            },
            "actions": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "register_macro / library_register: list of action "
                    "dicts (each with at least an 'action' key) to "
                    "register as a named sequence."
                ),
            },
            "stop_on_failure": {
                "type": "boolean",
                "description": (
                    "execute_macro / replay_trajectory / replay_bundle: "
                    "if True (default), stop at the first failed step "
                    "and skip the remainder; if False, continue "
                    "dispatching remaining steps."
                ),
            },
            "steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": ("replay_trajectory: list of step dicts (each with an 'action' field) to re-execute in order."),
            },
            "start": {
                "type": "integer",
                "description": (
                    "replay_trajectory / replay_bundle: 0-based index to start from; earlier steps are skipped. Defaults to 0."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "library_register / library_search: category to tag "
                    "the macro with (e.g. 'login', 'search') or to "
                    "filter by. Defaults to 'general'."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": ("library_register: list of free-form tags for later search."),
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "library_register: when True, replace an existing macro with the same name (still bumps version). Defaults to False."
                ),
            },
            "bundle": {
                "type": "object",
                "description": (
                    "replay_bundle: bundle dict produced by a prior "
                    "export_bundle call. Contains recorded actions and "
                    "optional state checkpoint."
                ),
            },
            "variables": {
                "type": "object",
                "description": (
                    "replay_bundle: mapping used to substitute "
                    "'{{key}}' placeholders inside the recorded action "
                    "payloads (e.g. {'username': 'alice'}). Missing "
                    "keys are left in place rather than raising."
                ),
            },
            "restore_state": {
                "type": "boolean",
                "description": (
                    "replay_bundle: when True (default), load the "
                    "bundle's saved browser state (cookies, URL, "
                    "localStorage) before the first replay step."
                ),
            },
            "include_state": {
                "type": "boolean",
                "description": (
                    "export_bundle: when True (default), include the "
                    "browser state checkpoint in the bundle. Set False "
                    "to keep the bundle tiny (replay then uses a fresh "
                    "session)."
                ),
            },
            "include_full_result": {
                "type": "boolean",
                "description": (
                    "Envelope: by default bulky string fields under "
                    "result (text, html, value, output, data, ...) are "
                    "truncated to max_text_chars. Set True to return "
                    "the raw result untouched. Default False."
                ),
            },
            "max_text_chars": {
                "type": "integer",
                "description": (
                    "Envelope: override the per-string truncation cap "
                    "(default 8000). Use 0 to disable truncation "
                    "entirely (equivalent to include_full_result=true)."
                ),
            },
            "source": {
                "type": "string",
                "description": ("export_bundle: free-form label for the producer (e.g. an agent identifier). Informational only."),
            },
            "enabled": {
                "type": "boolean",
                "description": (
                    "set_no_op_as_failure: when True (default), an "
                    "action that returns success but produces an "
                    "unchanged DOM / screen hash is converted to a "
                    "NO_CHANGE failure so the retry policy can fire. "
                    "Set False to revert to success-with-warning."
                ),
            },
            "author": {
                "type": "string",
                "description": ("library_register: free-form author or origin tag. Informational."),
            },
            "description": {
                "type": "string",
                "description": (
                    "library_register: human-readable summary of what "
                    "the macro does. Surfaced to the VLM so it knows "
                    "when to invoke the macro by name."
                ),
            },
            # -- observe options --
            "include_ocr": {
                "type": "boolean",
                "description": (
                    "observe: include an OCR text digest of the screen "
                    "(text boxes with clickable coordinates). On by "
                    "default -- this is how text-only models see."
                ),
            },
            "include_a11y": {
                "type": "boolean",
                "description": (
                    "observe: also walk the OS accessibility tree "
                    "(Windows UIA) and include structured element data. "
                    "Better than OCR for native apps. Default False."
                ),
            },
            "min_text_length": {
                "type": "integer",
                "description": ("observe OCR filter: ignore text fragments shorter than this many characters. Default 2."),
            },
            "max_depth": {
                "type": "integer",
                "description": (
                    "observe (include_a11y) / accessibility_tree: maximum tree recursion depth from the desktop root. Default 6."
                ),
            },
            "max_nodes": {
                "type": "integer",
                "description": (
                    "observe (include_a11y) / accessibility_tree: cap "
                    "on the number of tree nodes returned. Default 300 "
                    "for observe, 500 for accessibility_tree."
                ),
            },
            "control_type": {
                "type": "string",
                "description": ("find_element_by_name: filter by UIA control type (e.g. ButtonControl, EditControl). Optional."),
            },
            # -- desktop locate_on_screen --
            "template": {
                "type": "string",
                "description": (
                    "locate_on_screen: base64-encoded PNG template to find on screen via template matching. Required for locate_on_screen."
                ),
            },
            "confidence": {
                "type": "number",
                "description": (
                    "locate_on_screen: minimum match confidence in [0.0, 1.0]. Matches below this threshold are discarded. Default 0.9."
                ),
            },
            # -- delegate options --
            "goal": {
                "type": "string",
                "description": (
                    "delegate: high-level natural-language goal, phrased "
                    "in terms of what should be visible on screen when "
                    "done (e.g. 'open Notepad and type hello world')."
                ),
            },
            "max_steps": {
                "type": "integer",
                "description": (
                    "delegate: maximum VLM decision steps before giving "
                    "up. Each step is one screenshot + reason + act "
                    "cycle. Default 20, clamped to [1, 60]."
                ),
                "default": 20,
            },
            "template_name": {
                "type": "string",
                "description": (
                    "delegate: optional VLM task template that "
                    "specialises the system prompt. One of 'navigate', "
                    "'fill_form', 'extract_data', 'login', 'search'. "
                    "Empty string (default) or unknown names fall back "
                    "to the generic prompt."
                ),
                "enum": [
                    "",
                    "navigate",
                    "fill_form",
                    "extract_data",
                    "login",
                    "search",
                ],
                "default": "",
            },
        },
        "required": ["action"],
    }


#: Actions that only read state -- safe to run concurrently.
_READONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "list_actions",
        "trajectory",
        "recent_screenshots",
        "metrics",
        "library_search",
        "library_list",
        "observe",
        "display_info",
        "screenshot",
        "screenshot_viewport",
        "get_url",
        "get_title",
        "get_html",
        "get_text",
        "get_all_text",
        "get_attribute",
        "get_property",
        "a11y_snapshot",
        "click_by_role",
        "get_by_text_count",
        "get_page_structure",
        "get_screen_size",
        "get_cursor_position",
        "accessibility_tree",
        "find_element_by_name",
        "get_elements",
        "find_text",
        "locate_on_screen",
        "clipboard_get",
        "wait",
    }
)

#: Actions that mutate the UI, filesystem or clipboard.
_DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "click_at",
        "click_text",
        "double_click",
        "double_click_at",
        "right_click",
        "right_click_at",
        "triple_click",
        "type",
        "type_at",
        "press_key",
        "key",
        "hotkey",
        "scroll",
        "scroll_page",
        "drag",
        "move_mouse",
        "hover",
        "hover_at",
        "navigate",
        "go_back",
        "go_forward",
        "reload",
        "execute_js",
        "select_option",
        "fill_form",
        "clipboard_set",
        "file_drop",
        "set_dialog_handler",
        "set_file_chooser_handler",
        "register_macro",
        "execute_macro",
        "replay_trajectory",
        "replay_bundle",
        "library_register",
        "library_remove",
        "load_state",
        "reset",
        "set_no_op_as_failure",
    }
)


computer_tool = build_tool(
    name="computer",
    description=(
        "WHAT: The single computer-control tool -- one curated action "
        "schema that routes each action to the browser or desktop "
        "backend, returns screenshots as attached images (not base64 "
        "text), includes an OCR digest so text-only models can see the "
        "screen, and can delegate multi-step pixel-level work to a VLM "
        "loop. "
        "WHEN: Use for ANY interaction with the computer -- screen "
        "inspection (`observe`), browser automation (navigate, "
        "fill_form, click...), OS-level control (type, hotkey, "
        "clipboard...), or goal-driven delegation (`delegate`). "
        "WHEN NOT: Use `browser` directly only for fine-grained "
        "in-page scripting you know you need. "
        "TIPS: ALWAYS start a screen task with `observe` (returns the "
        "screen as an image + OCR text + cursor/DPI geometry); re-"
        "`observe` after actions to verify the result. Read the OCR "
        "element coordinates to pick click targets. Set "
        "`include_a11y: true` on observe for native Windows apps. Set "
        "`dry_run: true` to preview an action without mutating the UI; "
        "set `expect_change: true` to flag dead-element clicks; call "
        "`compress_trajectory` periodically to bound context size. "
        "PITFALLS: The session enforces a step budget (default 200) -- "
        "beyond it the tool refuses to dispatch and returns an "
        "explicit error, so call `reset` once a task is complete. "
        "Coordinates for desktop actions are physical screenshot "
        "pixels by default (`coord_space`). `delegate` requires a VLM "
        "backend (OPENAI_API_KEY or ANTHROPIC_API_KEY) and is much "
        "slower -- prefer direct actions when you already know what "
        "to click. "
        "Interaction actions: click, double_click, right_click, "
        "triple_click, type, press_key / key, hotkey, scroll, drag, "
        "move_mouse, hover, screenshot, click_text, find_text, wait, "
        "navigate, fill_form, select_option, get_attribute, "
        "get_property, get_html, get_text, get_all_text, execute_js, "
        "list_tabs, switch_tab, new_tab, close_tab, "
        "set_dialog_handler, set_file_chooser_handler, a11y_snapshot, "
        "click_by_role, get_by_text_count, get_page_structure, "
        "clipboard_get, clipboard_set, file_drop, get_screen_size, "
        "get_cursor_position, accessibility_tree, find_element_by_name, "
        "get_elements, take_screenshot_png, locate_on_screen, done. "
        "Tool-level actions: 'observe' (screen -> image + OCR + "
        "geometry + optional a11y tree), 'display_info' (monitor "
        "layout / DPI metadata), 'delegate' (VLM-driven goal loop). "
        "Meta-actions: 'trajectory' (return past actions), 'reset' "
        "(forget history), 'list_actions' (return the action enum), "
        "'cancel' (abort further dispatches), 'recent_screenshots' "
        "(return the last N steps; screenshots arrive as attached "
        "images), 'compress_trajectory' (drop screenshots older than "
        "keep_screenshots to free context), 'save_state' / "
        "'load_state' (browser cookie/storage checkpoint), "
        "'register_macro' / 'execute_macro' (in-session named action "
        "sequences), 'replay_trajectory' (re-execute a recorded "
        "sequence), 'metrics' (success rate, latency, failure "
        "breakdown), 'set_no_op_as_failure' (toggle the expect_change "
        "no-op conversion), 'library_register' / 'library_search' / "
        "'library_list' / 'library_save' / 'library_remove' "
        "(persistent macro library on disk, versioned and searchable), "
        "'export_bundle' / 'replay_bundle' (cross-session export with "
        "optional state restore and {{key}} variable substitution). "
        "Browser-only actions (navigate, fill_form, ...) auto-route to "
        "the browser backend; desktop-only actions (triple_click, "
        "clipboard_*, ...) route to the desktop. Cross-target actions "
        "(click, type, screenshot, ...) honour the `target` field -- "
        "default 'browser'."
    ),
    input_schema=_build_schema(),
    execute=_computer_execute,
    intents=["coding", "system"],
    category="system",
    semantic_type="exec",
    is_concurrency_safe=lambda args: args.get("action") in _READONLY_ACTIONS,
    is_destructive=lambda args: args.get("action", "") in _DESTRUCTIVE_ACTIONS,
)
