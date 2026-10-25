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

"""
Vision-Language-Model driven computer use.

This module powers the ``computer`` tool's ``delegate`` action.  It builds on top
of :class:`encre.computer.desktop.EncreDesktopSession` (which
produces screenshots and lets the agent click / type) by adding a
**visual perception + decision** layer: an external VLM is shown
the screenshot, asked to identify a target element on the screen,
and the resulting bounding box is converted into a click coordinate
that the desktop session can act on.

Why a dedicated module?
-----------------------
``computer`` style tasks (browser/desktop automation driven by
a multimodal LLM) are a flagship capability of Manus, Claude Code
and Codex.  Implementing it in a single tool would mix three
concerns -- screen capture, visual prompting, and action dispatch --
which makes the code hard to test, hard to swap backends on, and
hard to debug.  This module exposes those three concerns as
narrowly-scoped methods on :class:`VLMComputerUseSession` and the
tool layer above just orchestrates them.

VLM backends
------------
The session is **backend-agnostic**.  A backend is a callable that
takes a list of ``{"type": "image_url", ...}`` content blocks plus
a text prompt and returns a textual response that conforms to a
small structured schema.  The two production-ready backends are:

- :class:`OpenAICompatibleVLM` -- talks to any OpenAI-compatible
  ``/v1/chat/completions`` endpoint that accepts image inputs
  (OpenAI, Azure, OpenRouter, vLLM with a multimodal model, etc.).
- :class:`AnthropicVLM` -- talks to the Anthropic Messages API for
  Claude 3 / 3.5 / 3.7 with vision.

A backend is selected at construction time.  If neither is
configured (no API keys, no provider URL), the tool refuses to run
rather than fall back to a stub -- VLM-driven UI automation is
unsafe without a real model in the loop.
"""

import base64
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("encre.computer.vlm")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class VLMDecision:
    """A single decision emitted by a VLM.

    The agent loop turns these into physical actions (click, type,
    press key) and re-screenshots after each action to confirm the
    effect.
    """

    action: str  # "click" | "double_click" | "right_click" | "type" | "press" | "scroll" | "wait" | "done"
    x: int = 0
    y: int = 0
    text: str = ""
    key: str = ""
    scroll_amount: int = 0
    confidence: float = 0.0
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise this decision to a JSON-friendly dictionary."""
        return {
            "action": self.action,
            "x": self.x,
            "y": self.y,
            "text": self.text,
            "key": self.key,
            "scroll_amount": self.scroll_amount,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


@dataclass
class VLMUseResult:
    """Aggregate result of a ``computer`` delegate call."""

    success: bool
    steps_taken: int = 0
    decisions: list[VLMDecision] = field(default_factory=list)
    last_screenshot_b64: str = ""
    final_message: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise this result (including all decisions) to a dictionary."""
        return {
            "success": self.success,
            "steps_taken": self.steps_taken,
            "decisions": [d.to_dict() for d in self.decisions],
            "last_screenshot_b64": self.last_screenshot_b64,
            "final_message": self.final_message,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# VLM backends
# ---------------------------------------------------------------------------


VLMBackendFn = Callable[[bytes, str, str], str]


class OpenAICompatibleVLM:
    """Backend that talks to any OpenAI-compatible ``/v1/chat/completions`` API.

    Works with OpenAI, Azure OpenAI, OpenRouter, vLLM, LM-Studio, and
    any other provider that exposes the OpenAI Chat schema with
    ``image_url`` content blocks.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-5.6-luna",
        timeout: float = 60.0,
    ) -> None:
        """Store OpenAI-compatible endpoint config and validate the API key.

        Args:
            api_key: API key; falls back to ``OPENAI_API_KEY`` env var.
            base_url: Base URL of the OpenAI-compatible API.
            model: Vision-capable chat model name.
            timeout: Per-request timeout in seconds.
        """
        self.api_key: str = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "OpenAICompatibleVLM requires an API key.  Pass api_key=... or "
                "set the OPENAI_API_KEY environment variable."
            )
        self.base_url: str = base_url.rstrip("/")
        self.model: str = model
        self.timeout: float = timeout

    def __call__(self, screenshot_bytes: bytes, system_prompt: str, user_prompt: str) -> str:
        """Send a screenshot + prompts to the chat API and return the reply text.

        Args:
            screenshot_bytes: Raw PNG bytes of the current screen.
            system_prompt: System instruction describing the schema/role.
            user_prompt: Per-step user instruction.

        Returns:
            The model's textual response (expected to contain a JSON decision).
        """
        import httpx  # lazy: httpx is a hard dep but defer import for diagnostics

        image_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                },
            ],
            "max_tokens": 800,
            "temperature": 0.0,
        }
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"VLM request to {url} failed: {e}") from e
        if resp.status_code >= 400:
            raise RuntimeError(f"VLM API error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"VLM returned unexpected payload: {data!r}") from exc


class AnthropicVLM:
    """Backend that talks to the Anthropic Messages API for Claude vision."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-5",
        timeout: float = 60.0,
    ) -> None:
        """Store Anthropic endpoint config and validate the API key.

        Args:
            api_key: API key; falls back to ``ANTHROPIC_API_KEY`` env var.
            base_url: Base URL of the Anthropic API.
            model: Vision-capable Claude model name.
            timeout: Per-request timeout in seconds.
        """
        self.api_key: str = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "AnthropicVLM requires an API key.  Pass api_key=... or set the "
                "ANTHROPIC_API_KEY environment variable."
            )
        self.base_url: str = base_url.rstrip("/")
        self.model: str = model
        self.timeout: float = timeout

    def __call__(self, screenshot_bytes: bytes, system_prompt: str, user_prompt: str) -> str:
        """Send a screenshot + prompts to Claude and return the reply text."""
        import httpx

        image_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "max_tokens": 800,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": user_prompt},
                    ],
                }
            ],
        }
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Anthropic VLM request failed: {e}") from e
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Anthropic VLM API error {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        try:
            blocks = data["content"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"Anthropic returned unexpected payload: {data!r}") from exc
        text_parts: list[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        return "".join(text_parts)


# ---------------------------------------------------------------------------
# Decision parsing
# ---------------------------------------------------------------------------


_DECISION_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL
)


def parse_decision(raw: str) -> VLMDecision:
    """Parse a VLM response into a :class:`VLMDecision`.

    The VLM is asked to respond with a single JSON object wrapped in
    a `` ```json``` `` fence.  We accept a few common variants:

    - Strict JSON in a code fence.
    - JSON without a fence (when the model is well-behaved).
    - Fallback "action at x=N, y=N" prose, when the model is not
      well-behaved -- we still recover a useful click.

    Raises:
        ValueError: when neither path can recover a usable action.
    """
    text = raw.strip()
    payload: dict[str, Any] | None = None
    fence_match = _DECISION_RE.search(text)
    if fence_match:
        try:
            payload = json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        # Try to recover a JSON object from the whole response
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        # Prose fallback -- pull out action + coordinates
        m_action = re.search(r'"?action"?\s*[:=]\s*"?([a-z_]+)"?', text, re.IGNORECASE)
        m_x = re.search(r'"?x"?\s*[:=]\s*(\d+)', text, re.IGNORECASE)
        m_y = re.search(r'"?y"?\s*[:=]\s*(\d+)', text, re.IGNORECASE)
        if m_action and m_x and m_y:
            payload = {
                "action": m_action.group(1),
                "x": int(m_x.group(1)),
                "y": int(m_y.group(1)),
            }
    if not isinstance(payload, dict):
        raise ValueError(f"VLM response is not parseable as a decision: {raw[:200]}")
    action = str(payload.get("action") or "").strip().lower()
    if not action:
        raise ValueError(f"VLM response missing 'action' field: {raw[:200]}")
    return VLMDecision(
        action=action,
        x=int(payload.get("x") or 0),
        y=int(payload.get("y") or 0),
        text=str(payload.get("text") or ""),
        key=str(payload.get("key") or ""),
        scroll_amount=int(payload.get("scroll_amount") or 0),
        confidence=float(payload.get("confidence") or 0.0),
        reasoning=str(payload.get("reasoning") or payload.get("reason") or ""),
    )


# ---------------------------------------------------------------------------
# Main session
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are a vision-guided computer-use agent.  Look at the screenshot "
    "and the user's goal, then respond with **exactly one** JSON object "
    "(optionally inside a ```json``` fence) describing the next action. "
    "Schema:\n"
    "{\n"
    '  "action": "click" | "double_click" | "right_click" | "type" | '
    '"press" | "scroll" | "wait" | "done",\n'
    '  "x": <int pixel coordinate>,\n'
    '  "y": <int pixel coordinate>,\n'
    '  "text": "<for type action: the literal text to type>",\n'
    '  "key": "<for press action: the key name, e.g. \"Enter\">",\n'
    '  "scroll_amount": <for scroll: positive=down, negative=up>,\n'
    '  "reasoning": "<one sentence on why this action advances the goal>",\n'
    '  "confidence": <float 0-1>\n'
    "}\n"
    "Set action to \"done\" once the goal is achieved."
)

#: Per-task prompt templates.  Each entry customises the system prompt,
#: the per-step user prompt, and the "done" criteria for a specific
#: category of computer-use work.  This is what Codex/Manus expose via
#: their task-specific drivers (login, search, fill_form, etc.).
#:
#: A template is a dict with:
#: - ``system``      : system prompt sent on every step
#: - ``step_preamble``: prefix prepended to the per-step user prompt
#: - ``done_criteria``: extra text appended to the user prompt that
#:                      tells the VLM when to emit ``done``
#: - ``action_hints``: dict of action_name -> extra hint string for
#:                      user prompt (shown only when that action was
#:                      just taken, so the VLM avoids repeating it).
VLM_TASK_TEMPLATES: dict[str, dict[str, Any]] = {
    "navigate": {
        "system": (
            "You are a navigation specialist.  Drive the browser / desktop "
            "to the URL or application the user asked for.  Prefer "
            "'navigate' / 'go_back' / 'go_forward' / 'reload' actions over "
            "clicking through menus.  If a launcher bar / dock is visible, "
            "use it; otherwise use the address bar / start menu.  Set "
            '"done" once the target is on screen and the page has '
            "settled (no spinner visible)."
        ),
        "step_preamble": (
            "Navigate the user to the requested destination.  If the "
            "current screen already shows the destination, set "
            '"done" immediately with a short confirmation.'
        ),
        "done_criteria": (
            "Emit 'done' when the target page / app is visible AND "
            "any loading spinner has finished."
        ),
        "action_hints": {
            "click": (
                "If you just clicked a link, wait one step for the page "
                "to settle before deciding what's next."
            ),
        },
    },
    "fill_form": {
        "system": (
            "You are a form-filling specialist.  For each visible form "
            "field, click into it, then 'type' the value from the user's "
            "goal.  Use 'press Tab' to move between fields, and 'press "
            "Enter' only when explicitly told to submit.  NEVER guess "
            "values for required fields -- if the user did not supply a "
            "value, leave the field empty and emit 'done' with a "
            '"missing_fields" reasoning.  Validate visible errors after '
            "every submission."
        ),
        "step_preamble": (
            "Fill in the form fields the user specified.  Track which "
            "fields are still empty; never invent values."
        ),
        "done_criteria": (
            "Emit 'done' after either (a) every specified field has been "
            "filled and the form is submitted, OR (b) you encountered a "
            "required field the user did not specify."
        ),
        "action_hints": {
            "type": (
                "After typing, advance with Tab unless told otherwise."
            ),
        },
    },
    "extract_data": {
        "system": (
            "You are a data-extraction specialist.  Identify the structured "
            "data on screen (tables, lists, cards, search results).  When "
            "you can read every field, emit 'done' with a 'data' field "
            "containing the structured result as a JSON array of objects. "
            " Do NOT summarise; copy the on-screen values verbatim.  If "
            "the data spans multiple pages, scroll / click 'Next' and "
            "accumulate the rows."
        ),
        "step_preamble": (
            "Extract the structured data visible on the current screen.  "
            "If the table / list extends past the viewport, scroll to "
            "load more rows."
        ),
        "done_criteria": (
            "Emit 'done' only after the full dataset is in 'data'.  "
            "Pagination is OK; just keep going."
        ),
        "action_hints": {
            "scroll": (
                "After scrolling, re-read the entire visible region -- "
                "rows may have re-shuffled."
            ),
        },
    },
    "login": {
        "system": (
            "You are a login specialist.  Detect the login form (username, "
            "password, optional 2FA / CAPTCHA).  Use 'fill_form' style "
            "behaviour: click each field, type, then submit.  If a "
            "CAPTCHA appears, STOP and emit 'done' with reasoning "
            "'captcha encountered' so a human can take over.  Do not "
            "store or echo the password back in your reasoning."
        ),
        "step_preamble": (
            "Log the user in.  If a CAPTCHA / 2FA challenge appears, "
            "abort and report it -- do not attempt to solve it."
        ),
        "done_criteria": (
            "Emit 'done' after a successful login (post-login dashboard "
            "visible) OR when a CAPTCHA / 2FA challenge blocks progress."
        ),
        "action_hints": {
            "press": (
                "Avoid pressing Enter to submit if it might trigger a "
                "premature submit before all fields are filled."
            ),
        },
    },
    "search": {
        "system": (
            "You are a search specialist.  Locate the search input, focus "
            "it, type the query, and submit (press Enter or click the "
            "search button).  Once results are visible, emit 'done' with "
            "a 'result_count' reasoning if you can count them, or a short "
            "summary of the top results."
        ),
        "step_preamble": (
            "Search for the user's query.  Use the site's native search "
            "control rather than the address bar."
        ),
        "done_criteria": (
            "Emit 'done' once search results are visible.  Don't click "
            "through into a result unless the user asked for the top hit."
        ),
        "action_hints": {
            "type": (
                "After typing the query, press Enter to submit; only "
                "click the search button if Enter didn't work."
            ),
        },
    },
}


def list_task_templates() -> list[str]:
    """Return the names of all registered VLM task templates."""
    return sorted(VLM_TASK_TEMPLATES.keys())


def get_task_template(name: str) -> dict[str, Any]:
    """Return a copy of the named task template.

    Raises ``KeyError`` if the template doesn't exist -- callers
    should fall back to :data:`_SYSTEM_PROMPT` / generic step
    preamble in that case.
    """
    if name not in VLM_TASK_TEMPLATES:
        raise KeyError(
            f"unknown VLM task template: {name!r}; available: "
            f"{list_task_templates()}"
        )
    # Return a shallow copy so callers can mutate without poisoning
    # the module-level dict.
    return dict(VLM_TASK_TEMPLATES[name])


class VLMComputerUseSession:
    """High-level driver that combines screenshots + VLM + desktop actions."""

    def __init__(
        self,
        vlm: VLMBackendFn,
        desktop: Any | None = None,
        max_steps: int = 20,
    ) -> None:
        """Bind a VLM backend and (optional) desktop session together.

        Args:
            vlm: Callable backend that maps (png, system, user) -> reply text.
            desktop: Optional pre-built desktop session; created lazily if None.
            max_steps: Safety cap on the number of perception/action iterations.
        """
        self.vlm: VLMBackendFn = vlm
        self._desktop = desktop  # EncreDesktopSession instance
        self.max_steps: int = max_steps

    def _ensure_desktop(self) -> Any:
        """Return the desktop session, creating a default one on first use."""
        if self._desktop is None:
            from encre.computer.desktop import EncreDesktopSession
            self._desktop = EncreDesktopSession()
        return self._desktop

    def _screenshot(self) -> bytes:
        """Capture a PNG screenshot of the current desktop.

        Returns the raw PNG bytes (not base64) so they can be sent
        straight to a vision-language-model API.
        """
        desktop = self._ensure_desktop()
        return desktop.take_screenshot_png()

    def _dispatch(self, decision: VLMDecision) -> str:
        """Translate a VLM decision into a real desktop action.

        Returns a short status string the caller can include in its
        trajectory (e.g. "clicked (320, 480)" or "typed 'hello'").
        """
        desktop = self._ensure_desktop()
        action = decision.action
        if action == "click":
            desktop.click(int(decision.x), int(decision.y), button="left",
                          coord_space="physical")
            return f"clicked ({decision.x}, {decision.y})"
        if action == "double_click":
            desktop.double_click(int(decision.x), int(decision.y),
                                 coord_space="physical")
            return f"double-clicked ({decision.x}, {decision.y})"
        if action == "right_click":
            desktop.right_click(int(decision.x), int(decision.y),
                                coord_space="physical")
            return f"right-clicked ({decision.x}, {decision.y})"
        if action == "type":
            desktop.type_text(str(decision.text or ""))
            return f"typed {len(decision.text or '')} chars"
        if action == "press":
            desktop.press_key(str(decision.key or "Return"))
            return f"pressed {decision.key or 'Return'}"
        if action == "scroll":
            desktop.scroll(int(decision.scroll_amount))
            return f"scrolled {decision.scroll_amount}"
        if action == "wait":
            time.sleep(float(decision.text or 0.5))
            return f"waited {decision.text or 0.5}s"
        # "done" and any unknown action is a no-op
        return f"no-op ({action})"

    def run(self, goal: str) -> VLMUseResult:
        """Run the VLM-driven loop until the model emits ``done`` or
        ``max_steps`` is reached."""
        decisions: list[VLMDecision] = []
        history: list[str] = []
        last_b64 = ""
        for step in range(self.max_steps):
            png = self._screenshot()
            last_b64 = base64.b64encode(png).decode("ascii")
            history_lines = "\n".join(
                f"  {i + 1}. {h}" for i, h in enumerate(history[-10:])
            ) or "  (none)"
            user_prompt = (
                f"Goal: {goal}\n"
                f"Step {step + 1}/{self.max_steps}.  Look at the screenshot and "
                f"decide the next single action.  Respond with a single JSON object.\n"
                f"Recent actions (most recent last):\n{history_lines}"
            )
            try:
                raw = self.vlm(png, _SYSTEM_PROMPT, user_prompt)
            except Exception as exc:
                return VLMUseResult(
                    success=False,
                    steps_taken=step,
                    decisions=decisions,
                    last_screenshot_b64=last_b64,
                    final_message="",
                    error=f"VLM call failed: {exc}",
                )
            try:
                decision = parse_decision(raw)
            except ValueError as exc:
                return VLMUseResult(
                    success=False,
                    steps_taken=step,
                    decisions=decisions,
                    last_screenshot_b64=last_b64,
                    final_message="",
                    error=str(exc),
                )
            decisions.append(decision)
            if decision.action == "done":
                return VLMUseResult(
                    success=True,
                    steps_taken=step + 1,
                    decisions=decisions,
                    last_screenshot_b64=last_b64,
                    final_message=decision.reasoning or "done",
                )
            try:
                status = self._dispatch(decision)
            except Exception as exc:
                logger.warning("[computer] dispatch error: %s", exc)
                status = f"error: {exc}"
            history.append(
                f"{decision.action} -> {status}"
                + (f" ({decision.reasoning})" if decision.reasoning else "")
            )
            time.sleep(0.3)
        return VLMUseResult(
            success=False,
            steps_taken=self.max_steps,
            decisions=decisions,
            last_screenshot_b64=last_b64,
            final_message="",
            error=f"max_steps={self.max_steps} reached without a 'done' action",
        )

    def run_with_template(
        self, goal: str, template_name: str,
    ) -> VLMUseResult:
        """Like :meth:`run` but uses a task-specific prompt template.

        The template customises the system prompt, the per-step user
        prompt, the "done" criteria, and the per-action hint strings.
        See :data:`VLM_TASK_TEMPLATES` for the available templates
        and :func:`get_task_template` for their shape.

        Falls back to the generic :meth:`run` prompt if the template
        name is unknown.
        """
        try:
            template = get_task_template(template_name)
        except KeyError:
            logger.warning(
                "[computer] unknown template %r; using generic prompt",
                template_name,
            )
            return self.run(goal)
        system_prompt = template.get("system", _SYSTEM_PROMPT)
        step_preamble = template.get("step_preamble", "")
        done_criteria = template.get("done_criteria", "")
        action_hints: dict[str, str] = template.get("action_hints", {}) or {}
        decisions: list[VLMDecision] = []
        history: list[str] = []
        last_b64 = ""
        for step in range(self.max_steps):
            png = self._screenshot()
            last_b64 = base64.b64encode(png).decode("ascii")
            history_lines = "\n".join(
                f"  {i + 1}. {h}" for i, h in enumerate(history[-10:])
            ) or "  (none)"
            # Per-action hint: surface the hint for the *most recent*
            # action so the VLM gets a soft "you did X, be aware of Y"
            # nudge on the next step.
            last_action_hint = ""
            if history and action_hints:
                last_verb = history[-1].split(" -> ", 1)[0].strip()
                if last_verb in action_hints:
                    last_action_hint = (
                        f"\nHint for the previous action ({last_verb}): "
                        f"{action_hints[last_verb]}"
                    )
            user_prompt = (
                f"Goal: {goal}\n"
                f"{step_preamble}\n"
                f"Step {step + 1}/{self.max_steps}.  Look at the screenshot "
                f"and decide the next single action.  Respond with a single "
                f"JSON object.\n"
                f"{done_criteria}\n"
                f"Recent actions (most recent last):\n{history_lines}"
                f"{last_action_hint}"
            )
            try:
                raw = self.vlm(png, system_prompt, user_prompt)
            except Exception as exc:
                return VLMUseResult(
                    success=False,
                    steps_taken=step,
                    decisions=decisions,
                    last_screenshot_b64=last_b64,
                    final_message="",
                    error=f"VLM call failed: {exc}",
                )
            try:
                decision = parse_decision(raw)
            except ValueError as exc:
                return VLMUseResult(
                    success=False,
                    steps_taken=step,
                    decisions=decisions,
                    last_screenshot_b64=last_b64,
                    final_message="",
                    error=str(exc),
                )
            decisions.append(decision)
            if decision.action == "done":
                return VLMUseResult(
                    success=True,
                    steps_taken=step + 1,
                    decisions=decisions,
                    last_screenshot_b64=last_b64,
                    final_message=decision.reasoning or "done",
                )
            try:
                status = self._dispatch(decision)
            except Exception as exc:
                logger.warning("[computer] dispatch error: %s", exc)
                status = f"error: {exc}"
            history.append(
                f"{decision.action} -> {status}"
                + (f" ({decision.reasoning})" if decision.reasoning else "")
            )
            time.sleep(0.3)
        return VLMUseResult(
            success=False,
            steps_taken=self.max_steps,
            decisions=decisions,
            last_screenshot_b64=last_b64,
            final_message="",
            error=f"max_steps={self.max_steps} reached without a 'done' action",
        )
