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

"""Pre-call capability degradation planning (capability-matrix adaptive routing).

The kernel loop no longer assumes that every model supports tool calling, image
input, prompt caching, and streaming responses.  This module takes
:class:`~encre.backends.registry.ModelInfo` as its primary signal, and layers
the backend's own ``supports_*`` capability methods on top as a **stricter**
secondary constraint (the two are AND-combined: if either says no, the
capability is degraded).  Before a backend is actually called, a
:class:`CapabilityPlan` is computed that decides, per dimension:

- ``use_tools``: when False, no tools schema is sent to the backend.  The tool
  catalogue is instead compressed into a plain-text instruction and injected
  into the system prompt (see :func:`tool_intent_fallback`).
- ``use_vision``: when False, image references in messages are stripped and
  replaced with a text description (see :func:`apply_to_messages`).
- ``use_caching``: when False, no caching boundary is injected.
- ``use_streaming``: when False, the request is made non-streamingly.

Design constraint: this module **never raises**.  Any malformed or missing
input must degrade to a conservative plan, and every degraded dimension must
record its reason in :attr:`CapabilityPlan.reasons` so audits and debugging can
see exactly what was turned off and why.
"""

from dataclasses import dataclass, field
from typing import Any

from encre.backends.registry import ModelInfo


# Type markers for image-bearing content parts: OpenAI-style ``image_url``,
# Anthropic-style ``image``, and the OpenAI Responses API's ``input_image``.
_IMAGE_PART_TYPES: frozenset[str] = frozenset({"image_url", "image", "input_image"})

# Placeholder text written into a message when an image part has been stripped.
_IMAGE_OMITTED_TEXT = "[图片已省略：当前模型不支持图像输入]"

# Heading for the tool-intent block injected into the system prompt.
_TOOL_INTENT_HEADER = (
    "[工具意图说明] 当前模型不支持原生工具调用。若需要调用下列工具，"
    "请用文本明确表达意图，格式为："
    '<tool_call>{"name": "工具名", "arguments": { ... }}</tool_call>。'
)


@dataclass
class CapabilityPlan:
    """Four-axis degradation plan computed before a backend call.

    Attributes:
        use_tools: whether to send the tools schema to the backend.
        use_vision: whether messages are allowed to carry image references.
        use_caching: whether prompt-caching boundaries may be injected.
        use_streaming: whether the request should stream.
        reasons: per-degraded-dimension reason (dimension name -> explanation),
            retained for audit and troubleshooting.
    """

    use_tools: bool = True
    use_vision: bool = True
    use_caching: bool = True
    use_streaming: bool = True
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """Whether any dimension has been turned down."""
        return bool(self.reasons)


def _call_backend_flag(backend: Any, method_name: str) -> bool | None:
    """Safely ask a backend for one of its capability flags.

    Args:
        backend: the concrete backend instance; ``None`` means unavailable.
        method_name: a capability method name such as
            ``supports_tool_calling``.

    Returns:
        The boolean result when the method exists and is callable.
        ``None`` when the method is missing or the call raises -- treated as
        "no extra constraint", so that the ModelInfo-based decision stands on
        its own.
    """
    if backend is None:
        return None
    method = getattr(backend, method_name, None)
    if not callable(method):
        return None
    try:
        return bool(method())
    except Exception:
        return None


def compute_plan(
    model_info: ModelInfo | None,
    *,
    backend: Any = None,
    config: Any = None,
) -> CapabilityPlan:
    """Compute the pre-call degradation plan from the model capability matrix.

    Decision rules:

    1. ``ModelInfo`` is the primary signal (``supports_tools`` /
       ``supports_images`` / ``supports_prompt_caching`` /
       ``supports_streaming``).
    2. The backend's capability methods (``supports_tool_calling`` /
       ``supports_vision_input`` / ``supports_prompt_caching``) act as a
       stricter secondary constraint, AND-combined with the primary signal.
    3. When ``model_info`` is ``None`` we must not be blindly optimistic:
       tools / vision / caching are all degraded conservatively; streaming is
       kept (every current backend supports it).

    Args:
        model_info: current model metadata; pass ``None`` when unknown.
        backend: optional backend instance used to layer stricter capability
            constraints.
        config: optional configuration object; explicitly disabling prompt
            caching degrades the caching dimension as well.

    Returns:
        A :class:`CapabilityPlan`. This function never raises.
    """
    reasons: dict[str, str] = {}

    if model_info is None:
        # Unknown model -> conservative defaults: no tools, no images, and no
        # caching boundary. Streaming stays on (see rule 3 above).
        reasons["tools"] = "unknown model -> conservative default"
        reasons["vision"] = "unknown model -> conservative default"
        reasons["caching"] = "unknown model -> conservative default"
        plan = CapabilityPlan(
            use_tools=False,
            use_vision=False,
            use_caching=False,
            use_streaming=True,
            reasons=reasons,
        )
    else:
        name = getattr(model_info, "name", "") or "model"
        use_tools = bool(getattr(model_info, "supports_tools", False))
        use_vision = bool(getattr(model_info, "supports_images", False))
        use_caching = bool(getattr(model_info, "supports_prompt_caching", False))
        use_streaming = bool(getattr(model_info, "supports_streaming", True))
        if not use_tools:
            reasons["tools"] = f"model {name} does not support tool calling"
        if not use_vision:
            reasons["vision"] = f"model {name} does not support image input"
        if not use_caching:
            reasons["caching"] = f"model {name} does not support prompt caching"
        if not use_streaming:
            reasons["streaming"] = f"model {name} does not support streaming"
        plan = CapabilityPlan(
            use_tools=use_tools,
            use_vision=use_vision,
            use_caching=use_caching,
            use_streaming=use_streaming,
            reasons=reasons,
        )

    # Backend capability methods: a stricter secondary constraint (AND). A
    # dimension already degraded above keeps its original reason -- these
    # branches only fire while the plan still had the capability enabled.
    if _call_backend_flag(backend, "supports_tool_calling") is False and plan.use_tools:
        plan.use_tools = False
        plan.reasons["tools"] = "backend does not support tool calling"
    if _call_backend_flag(backend, "supports_vision_input") is False and plan.use_vision:
        plan.use_vision = False
        plan.reasons["vision"] = "backend does not support image input"
    if _call_backend_flag(backend, "supports_prompt_caching") is False and plan.use_caching:
        plan.use_caching = False
        plan.reasons["caching"] = "backend does not support prompt caching"

    # Config override: an explicit "disable prompt caching" setting degrades
    # the caching dimension too.
    if config is not None and not bool(getattr(config, "enable_prompt_caching", True)):
        if plan.use_caching:
            plan.use_caching = False
            plan.reasons["caching"] = "prompt caching disabled by config"

    return plan


def apply_to_tools(
    tools: list[dict[str, Any]] | None,
    plan: CapabilityPlan,
) -> list[dict[str, Any]] | None:
    """Trim the tools schema according to the plan.

    When ``plan.use_tools`` is False, returns ``None`` so that no tools schema
    reaches the backend at all.
    """
    if not plan.use_tools:
        return None
    return tools


def apply_to_messages(
    messages: list[dict[str, Any]],
    plan: CapabilityPlan,
) -> list[dict[str, Any]]:
    """Strip image references from messages according to the plan.

    When ``plan.use_vision`` is False, OpenAI-style
    ``{"type": "image_url", ...}`` and Anthropic-style
    ``{"type": "image", "source": {...}}`` image parts are replaced with a
    text description (preserving alt / text / filename metadata). Returns a
    new message structure; does **not** mutate the input in place.
    """
    if plan.use_vision or not messages:
        return messages

    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            result.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            result.append(message)
            continue
        new_content: list[Any] = []
        for part in content:
            if isinstance(part, dict) and _is_image_part(part):
                new_content.append({"type": "text", "text": _describe_image_part(part)})
            else:
                new_content.append(part)
        new_message = dict(message)
        new_message["content"] = new_content
        result.append(new_message)
    return result


def tool_intent_fallback(tool_payload: list[dict[str, Any]] | None) -> str:
    """Compress the tool catalogue into a plain-text instruction for the system prompt.

    When the tools dimension has been degraded off, the model still needs to
    know which tools exist and how to express a call as text. This reuses the
    shape returned by ``discovery.get_active_tools_payload(...)``
    (OpenAI-style ``{"type": "function", "function": {...}}`` or
    Anthropic-style ``{"name": ..., "description": ...}``) and renders each
    tool as "name + one-sentence purpose".

    Args:
        tool_payload: the active tool list; returns the empty string when
            empty.

    Returns:
        A plain-text instruction block to append to the end of the system
        prompt; ``""`` when there are no usable tools.
    """
    if not tool_payload:
        return ""

    lines: list[str] = []
    for item in tool_payload:
        name, description = _tool_name_and_description(item)
        if not name:
            continue
        summary = _first_sentence(description)
        lines.append(f"- {name}: {summary}" if summary else f"- {name}")
    if not lines:
        return ""

    return _TOOL_INTENT_HEADER + "\n" + "\n".join(lines)


def _is_image_part(part: dict[str, Any]) -> bool:
    """Whether a content part is an image reference."""
    if part.get("type") in _IMAGE_PART_TYPES:
        return True
    # Fallback: some providers emit an image_url/source field without a type.
    if isinstance(part.get("image_url"), dict):
        return True
    source = part.get("source")
    if isinstance(source, dict) and source.get("type") in ("base64", "url"):
        return True
    return False


def expand_tool_images(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Project the ``images`` side-key on tool messages into image content parts.

    :meth:`encre.session.Session.add_tool_result` stores screenshots produced by
    tools under the message's ``images`` side-key (``content`` stays a plain
    string, so frontend rendering, persistence, and compaction need no
    changes). When the backend request is being built, this function unfolds
    those side-keys into OpenAI-style ``image_url`` parts appended to the
    matching tool message's content. Vision degradation is applied afterwards
    by :func:`apply_to_messages`.

    Non-mutating: messages without an ``images`` side-key are returned as-is;
    messages with images produce a new dict.
    """
    if not messages:
        return messages
    out: list[Any] = []
    changed = False
    for message in messages:
        if not (isinstance(message, dict) and message.get("images")):
            out.append(message)
            continue
        content = message.get("content")
        blocks: list[Any] = []
        if isinstance(content, list):
            blocks.extend(content)
        elif content:
            blocks.append({"type": "text", "text": str(content)})
        for img in message.get("images") or []:
            if not isinstance(img, dict) or not img.get("data"):
                continue
            media = str(img.get("media_type") or "image/png")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media};base64,{img['data']}"},
            })
        new_message = dict(message)
        new_message["content"] = blocks if blocks else content
        new_message.pop("images", None)
        out.append(new_message)
        changed = True
    return out if changed else messages


def relocate_tool_images(
    messages: list[dict[str, Any]] | None,
    keep_in_place: bool = False,
) -> list[dict[str, Any]] | None:
    """Re-home image parts inside tool messages per provider rules.

    When ``keep_in_place`` is True (Anthropic family) the messages are returned
    as-is. Otherwise image parts are moved into a synthetic user message placed
    **after the run of tool messages that produced them** -- multiple tool
    results within the same turn stay contiguous (which OpenAI requires for
    tool_call_id response ordering), while images sit immediately next to the
    tool result that produced them.

    Non-mutating: returns the input unchanged when there are no images,
    otherwise returns a new list.
    """
    if keep_in_place or not messages:
        return messages

    def _split_images(content: Any) -> tuple[list[Any], list[Any]]:
        images: list[Any] = []
        rest: list[Any] = []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and _is_image_part(part):
                    images.append(part)
                else:
                    rest.append(part)
        return images, rest

    out: list[Any] = []
    changed = False
    pending: list[Any] = []

    def _flush() -> None:
        nonlocal pending, changed
        if pending:
            out.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "[Screenshot attached to the tool result above]"},
                    *pending,
                ],
            })
            pending = []
            changed = True

    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            images, rest = _split_images(message.get("content"))
            if images:
                new_message = dict(message)
                new_message["content"] = rest if rest else ""
                out.append(new_message)
                pending.extend(images)
                changed = True
            else:
                out.append(message)
            continue
        _flush()
        out.append(message)
    _flush()
    return out if changed else messages


def _describe_image_part(part: dict[str, Any]) -> str:
    """Render a stripped image part as a text description that keeps its metadata."""
    meta: list[str] = []
    for key in ("alt", "text", "filename", "name", "title"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            meta.append(value.strip())

    source = part.get("image_url") if isinstance(part.get("image_url"), dict) else part.get("source")
    if isinstance(source, dict):
        url = source.get("url")
        if isinstance(url, str) and url:
            meta.append(_describe_url(url))
        media_type = source.get("media_type") or source.get("mime_type")
        if isinstance(media_type, str) and media_type:
            meta.append(media_type)

    if not meta:
        return _IMAGE_OMITTED_TEXT
    return f"{_IMAGE_OMITTED_TEXT}（{'，'.join(meta)}）"


def _describe_url(url: str) -> str:
    """Compress an image URL into a short description, avoiding inline base64."""
    if url.startswith("data:"):
        header = url.split(",", 1)[0]
        return f"内联图片数据（{header}）"
    if len(url) > 120:
        return url[:117] + "..."
    return url


def _tool_name_and_description(item: Any) -> tuple[str, str]:
    """Extract the name and description from an OpenAI- or Anthropic-style tool definition."""
    if not isinstance(item, dict):
        return "", ""
    function = item.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or ""), str(function.get("description") or "")
    return str(item.get("name") or ""), str(item.get("description") or "")


def _first_sentence(text: str, limit: int = 120) -> str:
    """Compress a tool description to one sentence so it does not bloat the prompt."""
    text = " ".join(str(text).split())
    if not text:
        return ""
    for sep in ("。", ". ", "\n"):
        index = text.find(sep)
        if 0 <= index < limit:
            return text[: index + 1].strip()
    return text[:limit].strip()


__all__ = [
    "CapabilityPlan",
    "apply_to_messages",
    "apply_to_tools",
    "compute_plan",
    "tool_intent_fallback",
]
