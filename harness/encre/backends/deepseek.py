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

"""
DeepSeek backend -- V4-Flash, V4-Pro (2026 lineup).

As of May 2026, DeepSeek's model lineup has been updated:

- **DeepSeek V4-Flash**: The default chat model with 1M context window,
  384K output tokens, and aggressive cache discounts (92% off cache hits).
  Pricing: $0.14/$0.28 per 1M tokens (input/output).  Replaces the
  deprecated ``deepseek-chat`` model.

- **DeepSeek V4-Pro**: The enhanced reasoning model with 1M context window,
  384K output tokens, and 80% cache hit discount.  Pricing: $1.74/$3.48 per
  1M tokens.  Replaces the deprecated ``deepseek-reasoner`` model.

- **Legacy models**: ``deepseek-chat`` and ``deepseek-reasoner`` are
  deprecated as of July 2026.  They are kept in the registry for backward
  compatibility but map to their V4 equivalents.

Both V4 models support:
- Tool/function calling (OpenAI-compatible format)
- Thinking/reasoning tokens (emitted as ``reasoning_content`` in the API)
- Prompt caching (80-92% discount on cache hits)
- 1M token context windows
- 384K token output limits

This backend extends :class:`OpenAISSEBackend` because DeepSeek uses an
OpenAI-compatible API.  The only customisation is the extraction of
``reasoning_content`` from the response delta, which is emitted as
:class:`BackendThinking` events.
"""

import hashlib
import re
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

from encre.backends.openai_sse import OpenAISSEBackend
from encre.utils.types import BackendToolCall

# DeepSeek supports a subset of JSON Schema for tool parameters.  Keep the
# standard structural keywords and value-type keywords; drop anything that
# may have been valid in JSON Schema but is not documented/supported.
_SUPPORTED_SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "anyOf",
        "allOf",
        "oneOf",
        "$ref",
        "$def",
        "description",
        "title",
        "default",
        # string
        "pattern",
        "format",
        # number / integer
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }
)


def _normalize_tool_schema_for_deepseek(schema: Any) -> Any:
    """Make a JSON Schema object compliant with DeepSeek's supported subset.

    DeepSeek validates tool schemas more strictly than the OpenAI default:
    top-level ``parameters`` must be an ``object`` with ``properties``,
    every property must be listed in ``required``, and
    ``additionalProperties`` must be ``False``.  Unsupported keywords such as
    ``minLength``/``maxLength``/``minItems``/``maxItems`` are stripped.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SUPPORTED_SCHEMA_KEYWORDS:
            continue
        if key in ("properties", "items"):
            if isinstance(value, dict):
                out[key] = {k: _normalize_tool_schema_for_deepseek(v) for k, v in value.items()}
            else:
                out[key] = value
        elif key in ("anyOf", "allOf", "oneOf"):
            if isinstance(value, list):
                out[key] = [_normalize_tool_schema_for_deepseek(v) for v in value]
            else:
                out[key] = value
        else:
            out[key] = value

    if out.get("type") == "object" and "properties" in out:
        # All object properties must be required for DeepSeek.
        required = set(out.get("required", []))
        required.update(out["properties"].keys())
        out["required"] = sorted(required)
        out.setdefault("additionalProperties", False)
    return out


_TOOL_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_tool_name(name: str) -> str:
    """Return a tool name DeepSeek accepts (``^[a-zA-Z0-9_-]{1,64}$``).

    Illegal characters (dots in MCP names, colons in plugin names, CJK
    characters, ...) are replaced with underscores.  A name that ends up
    empty (fully illegal) falls back to a short deterministic hash so the
    request still passes validation.
    """
    original = str(name)
    name = _TOOL_NAME_PATTERN.sub("_", original)[:64]
    if not name:
        name = "tool_" + hashlib.md5(original.encode()).hexdigest()[:8]
    return name


def _resolve_tool_names(
    tools: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """Resolve every function tool to its final API-visible name.

    Single source of truth shared by :func:`_sanitize_tools` (renaming the
    request payload) and ``DeepSeekBackend.chat`` (building the
    ``sanitized -> original`` reverse map), so the names the API sees can
    never drift from the names the reverse map knows about.

    Collision rule: when two DIFFERENT original names sanitize to the same
    value (e.g. ``a.b`` and ``a_b``), a deterministic ``_`` + 8-hex md5
    suffix of the original name is appended (truncated to 64 chars) so both
    stay distinct API-visible names and the reverse map stays injective --
    without this, the naive ``map[sanitized] = original`` assignment let the
    later tool silently steal the other's calls.  Identical duplicate
    originals keep the first occurrence.

    Returns:
        Ordered ``sanitized -> original`` mapping covering every function
        tool (identity entries included).
    """
    remap: dict[str, str] = {}
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        func = tool.get("function")
        if not isinstance(func, dict):
            continue
        original = str(func.get("name", ""))
        sanitized = _sanitize_tool_name(original)
        if sanitized in remap and remap[sanitized] != original:
            suffix = "_" + hashlib.md5(original.encode("utf-8")).hexdigest()[:8]
            candidate = sanitized[: 64 - len(suffix)] + suffix
            while candidate in remap:
                candidate = candidate[: 64 - len(suffix)] + "_" + hashlib.md5((original + candidate).encode("utf-8")).hexdigest()[:8]
            sanitized = candidate
        remap[sanitized] = original
    return remap


def _sanitize_tools(
    tools: list[dict[str, Any]],
    name_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Sanitize tool definitions for DeepSeek's stricter validation.

    When *name_map* (the ``sanitized -> original`` map from
    :func:`_resolve_tool_names`) is given, function tools are renamed with
    the exact same disambiguation the reverse map was built from; when it
    is omitted the map is recomputed (deterministic, so both paths agree).
    Identical duplicate function names keep only the first definition --
    the API rejects duplicate names with HTTP 400.
    """
    if name_map is None:
        name_map = _resolve_tool_names(tools)
    # Invert to original -> final sanitized name for per-tool lookup.
    rename: dict[str, str] = {}
    for sanitized, original in name_map.items():
        rename.setdefault(original, sanitized)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            out.append(tool)
            continue
        func = tool.get("function", {})
        if not isinstance(func, dict):
            out.append(tool)
            continue
        original = str(func.get("name", ""))
        name = rename.get(original) or _sanitize_tool_name(original)
        if name in seen:
            continue
        seen.add(name)
        parameters = func.get("parameters")
        if isinstance(parameters, dict):
            parameters = _normalize_tool_schema_for_deepseek(parameters)
        else:
            parameters = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(func.get("description", "")),
                    "parameters": parameters,
                },
            }
        )
    return out


class DeepSeekBackend(OpenAISSEBackend):
    """DeepSeek backend for the 2026 V4 model lineup.

    Supports DeepSeek V4-Flash (default) and V4-Pro via the OpenAI-compatible
    API at ``https://api.deepseek.com``.  The legacy ``deepseek-chat`` and
    ``deepseek-reasoner`` model names are automatically mapped to their V4
    equivalents because DeepSeek deprecated them in July 2026.

    The key difference from the base :class:`OpenAISSEBackend` is the
    extraction of ``reasoning_content`` from the response delta, which
    contains the model's chain-of-thought reasoning.  This is emitted as
    :class:`BackendThinking` events so the agent loop can surface them
    appropriately.

    2026 pricing summary:
        - DeepSeek V4-Flash: $0.14/$0.28 per 1M tokens (92% cache discount)
        - DeepSeek V4-Pro: $1.74/$3.48 per 1M tokens (80% cache discount)
    """

    DEFAULT_BASE_URL = "https://api.deepseek.com"

    # Deprecated model IDs and their V4 replacements.
    _DEPRECATED_MODEL_MAP: ClassVar[dict[str, str]] = {
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-v4-pro",
    }

    # Tokens that mark a vision-capable model id in DeepSeek's naming
    # (the deepseek-vl lineage, V4 vision variants, omni-style multimodal).
    # Text-only ids (deepseek-v4-flash / -pro / legacy chat) match none.
    _VISION_MODEL_TOKENS: ClassVar[tuple[str, ...]] = ("vision", "vl", "omni", "multimodal")

    def finalize_history(
        self,
        messages: list[dict[str, Any]],
        session: Any = None,
    ) -> list[dict[str, Any]] | None:
        """Finalize ``reasoning_content`` once at the tool-loop boundary.

        Called by the harness at the start of a fresh user turn, when every
        previous tool loop is closed.  Official thinking-mode rule
        (api-docs.deepseek.com "Thinking Mode / Tool Calls"): the
        ``reasoning_content`` of an assistant message that carries
        ``tool_calls`` must be passed back to the API in ALL subsequent
        requests, no matter how many user turns have passed since the tool
        loop ran -- so it is NEVER stripped here.  The previous policy
        (strip once the loop is closed) followed an obsolete rule and
        caused HTTP 400 "reasoning_content must be passed back".  Reasoning
        on assistants WITHOUT ``tool_calls`` is ignored by the API;
        stripping it once here keeps the model context byte-stable instead
        of dropping it per request, preserving the provider prompt cache.

        Sessions compacted by older builds may already carry a view whose
        tool-call assistants lost their ``reasoning_content`` (the
        append-only branch history still has it).  When ``session`` is
        given, such messages are healed: the original reasoning is looked
        up in the active branch by ``seq_in_branch`` and restored, so
        sessions poisoned by the stale pre-fix view recover instead of
        failing with HTTP 400 forever.

        This method is PURE with respect to the persisted history: it never
        mutates ``messages`` (the turn files must stay append-only).

        Args:
            messages: The effective context (view + branch tail).  Read-only.
            session: The harness session, used to heal reasoning that a
                stale pre-fix view had stripped.  May be ``None``.

        Returns:
            A cleaned copy of ``messages``, or ``None`` when unchanged.
        """
        cleaned: list[dict[str, Any]] | None = None

        # ---- Heal: restore reasoning stripped by a stale pre-fix view ----
        if session is not None:
            branch_by_seq: dict[int, dict[str, Any]] | None = None
            for idx, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                    continue
                if msg.get("reasoning_content"):
                    continue
                seq = msg.get("seq_in_branch")
                if not isinstance(seq, int):
                    continue
                if branch_by_seq is None:
                    try:
                        branch_msgs = session.get_branch_messages(session.active_branch_id)
                    except Exception:
                        branch_msgs = []
                    branch_by_seq = {
                        m["seq_in_branch"]: m for m in branch_msgs if isinstance(m, dict) and isinstance(m.get("seq_in_branch"), int)
                    }
                original = branch_by_seq.get(seq)
                reasoning = original.get("reasoning_content") if original else None
                if isinstance(reasoning, str) and reasoning:
                    if cleaned is None:
                        cleaned = [dict(m) if isinstance(m, dict) else m for m in messages]
                    cleaned[idx]["reasoning_content"] = reasoning

        # ---- Strip: reasoning on non-tool-call assistants is ignored ----
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant" or msg.get("tool_calls"):
                continue
            if "reasoning_content" not in msg:
                continue
            if cleaned is None:
                cleaned = [dict(m) if isinstance(m, dict) else m for m in messages]
            cleaned[idx] = {k: v for k, v in cleaned[idx].items() if k != "reasoning_content"}
        return cleaned

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "deepseek-v4-flash",
        **kwargs: Any,
    ) -> None:
        """Initialise the DeepSeek backend.

        Args:
            api_key: DeepSeek API key.
            base_url: Custom API base URL.  Defaults to
                ``https://api.deepseek.com``.
            model: Model name.  Defaults to ``deepseek-v4-flash``.  Legacy
                values ``deepseek-chat`` and ``deepseek-reasoner`` are
                automatically mapped to ``deepseek-v4-flash`` and
                ``deepseek-v4-pro`` respectively.
            **kwargs: Additional arguments passed to :class:`OpenAISSEBackend`.
        """
        if not base_url:
            base_url = self.DEFAULT_BASE_URL
        # DeepSeek deprecated deepseek-chat/reasoner in July 2026.  Map them
        # to the current V4 IDs so existing user settings keep working.
        model = self._DEPRECATED_MODEL_MAP.get(model, model)
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)

    # ------------------------ Overrides --------------------------------------------------------------------------------------------------

    def context_window_size(self) -> int:
        """Return the context window size for DeepSeek V4 models.

        Both V4-Flash and V4-Pro support 1,048,576 (1M) token context windows.
        Legacy models (deepseek-chat/reasoner) had 64K context.
        """
        return 1048576

    def supports_thinking(self) -> bool:
        """DeepSeek V4 models support reasoning/thinking tokens."""
        return True

    def supports_prompt_caching(self) -> bool:
        """DeepSeek V4 models support prompt caching (80-92% discount)."""
        return True

    # ── Multimodal/Extended API capabilities ──────────────────────
    # The V4 lineup includes vision-capable variants that take standard
    # OpenAI ``image_url`` content blocks.  Every other extended endpoint
    # (images generation, audio, embeddings, moderation, files, batch,
    # fine-tuning, responses, realtime) stays unavailable.

    def supports_image_generation(self) -> bool:
        return False

    def supports_image_edit(self) -> bool:
        return False

    def supports_image_variation(self) -> bool:
        return False

    def supports_embeddings(self) -> bool:
        return False

    def supports_moderation(self) -> bool:
        return False

    def supports_files(self) -> bool:
        return False

    def supports_batch(self) -> bool:
        return False

    def supports_fine_tuning(self) -> bool:
        return False

    def supports_responses_api(self) -> bool:
        return False

    def supports_realtime(self) -> bool:
        return False

    def supports_vision_input(self) -> bool:
        """Return True for vision-capable DeepSeek models, False otherwise.

        The declared flag gates the offline shortcut in the capability
        probe: text-only models (V4-Flash / V4-Pro, legacy chat) must stay
        hard-False because the endpoint can accept ``image_url`` blocks
        without honouring them, while vision variants must reach the live
        probe instead of being excluded by a stale blanket negative.
        """
        model = (self.model or "").lower()
        return any(token in model for token in self._VISION_MODEL_TOKENS)

    def _build_request_data(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
    ) -> dict[str, Any]:
        """Build the DeepSeek request body with sanitized tools.

        Message sanitization (unknown-field stripping, ``reasoning_content``
        pass-back per the official thinking-mode rule, ``None``-content
        coercion, ``tool_calls`` rebuild) is handled centrally by the base
        ``_build_request_data`` via :func:`sanitize_openai_messages`, keyed
        off ``thinking_enabled``.  Tools still need DeepSeek-specific schema
        normalization, and tool names the reverse rename map.

        DeepSeek V4 models also accept ``reasoning_effort`` (e.g. ``"high"``
        or ``"max"``) to control reasoning intensity.
        """
        # Reuse the reverse map chat() already resolved for this request so
        # payload renaming and tool-call remapping share one disambiguation
        # (recomputation is deterministic, this just removes any drift risk).
        name_map = getattr(self, "_tool_name_remap", None)
        data = super()._build_request_data(
            messages=messages,
            tools=_sanitize_tools(tools, name_map) if tools else None,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
        )
        if getattr(self, "reasoning_effort", ""):
            data["reasoning_effort"] = self.reasoning_effort
        return data

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Any]:
        """Stream events, mapping sanitized tool names back to originals.

        ``_resolve_tool_names`` rewrites tool names that violate DeepSeek's
        ``^[a-zA-Z0-9_-]+$`` validation (MCP dots, plugin colons, CJK
        names, ...) and disambiguates collisions deterministically.  The
        model echoes those sanitized names in its tool calls, so without a
        reverse mapping the harness would dispatch the call against a name
        that does not exist in the registry.  The per-request
        ``sanitized -> original`` map recorded here restores the registry
        name on every emitted :class:`BackendToolCall`.
        """
        full_map = _resolve_tool_names(tools)
        self._tool_name_remap = {sanitized: original for sanitized, original in full_map.items() if sanitized != original}
        async for event in super().chat(messages, tools=tools, **kwargs):
            if isinstance(event, BackendToolCall) and self._tool_name_remap:
                original = self._tool_name_remap.get(event.name)
                if original:
                    event = BackendToolCall(id=event.id, name=original, arguments=event.arguments)
            yield event
