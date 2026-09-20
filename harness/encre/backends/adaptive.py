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

from __future__ import annotations

"""调用前能力降级规划（capability matrix adaptive routing）。

内核 loop 不再假设每个模型都支持工具调用、图像输入、提示缓存与流式响应。
本模块以 :class:`~encre.backends.registry.ModelInfo` 为主判据，并结合 backend
自身暴露的 ``supports_*`` 能力方法（作为**更严格的**补充约束，两者取"与"，
任一为假即降级），在真正调用 backend 之前算出一份 :class:`CapabilityPlan`：

- ``use_tools``：为假时不向 backend 传 tools schema，改为把工具清单压缩成
  纯文本指令注入 system prompt（见 :func:`tool_intent_fallback`）。
- ``use_vision``：为假时剥离消息中的图片引用并替换为文本描述
  （见 :func:`apply_to_messages`）。
- ``use_caching``：为假时不注入缓存边界。
- ``use_streaming``：为假时以非流式方式请求。

设计约束：本模块**绝不抛异常**。任何畸形/缺失输入都必须退化为保守方案，并把
降级原因记录在 :attr:`CapabilityPlan.reasons` 中，以便审计与排障。
"""

from dataclasses import dataclass, field
from typing import Any

from encre.backends.registry import ModelInfo


# 图片类内容块的类型标识：OpenAI 风格 ``image_url``、Anthropic 风格
# ``image``、OpenAI Responses API 风格 ``input_image``。
_IMAGE_PART_TYPES: frozenset[str] = frozenset({"image_url", "image", "input_image"})

# 图片被剥离后写入消息的占位文本。
_IMAGE_OMITTED_TEXT = "[图片已省略：当前模型不支持图像输入]"

# 注入 system prompt 的工具意图说明标题。
_TOOL_INTENT_HEADER = (
    "[工具意图说明] 当前模型不支持原生工具调用。若需要调用下列工具，"
    "请用文本明确表达意图，格式为："
    '<tool_call>{"name": "工具名", "arguments": { ... }}</tool_call>。'
)


@dataclass
class CapabilityPlan:
    """调用前算出的四维降级方案。

    Attributes:
        use_tools: 是否向 backend 传 tools schema。
        use_vision: 是否允许消息携带图片引用。
        use_caching: 是否允许注入提示缓存边界。
        use_streaming: 是否以流式方式请求。
        reasons: 每个被降级维度的原因（维度名 -> 说明），用于审计/排障。
    """

    use_tools: bool = True
    use_vision: bool = True
    use_caching: bool = True
    use_streaming: bool = True
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """是否有任一维度被降级。"""
        return bool(self.reasons)


def _call_backend_flag(backend: Any, method_name: str) -> bool | None:
    """安全地询问 backend 的能力方法。

    Args:
        backend: 具体 backend 实例；``None`` 表示不可用。
        method_name: 形如 ``supports_tool_calling`` 的能力方法名。

    Returns:
        方法存在且可调用时返回其布尔结果；方法缺失或调用抛异常时返回
        ``None``（视为"无额外约束"，以 ModelInfo 的判定为准）。
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
    """按模型能力矩阵计算调用前的降级方案。

    判定规则：

    1. 以 ``ModelInfo`` 为主判据（``supports_tools`` / ``supports_images`` /
       ``supports_prompt_caching`` / ``supports_streaming``）。
    2. backend 的能力方法（``supports_tool_calling`` / ``supports_vision_input`` /
       ``supports_prompt_caching``）作为更严格的补充约束，与主判据取"与"。
    3. ``model_info`` 为 ``None`` 时不得盲目乐观：tools / vision / caching 一律
       保守降级，streaming 保留（现有 backend 均支持流式）。

    Args:
        model_info: 当前模型元数据；未知时传 ``None``。
        backend: 可选的 backend 实例，用于叠加更严格的能力约束。
        config: 可选配置对象；显式关闭提示缓存时同样降级 caching。

    Returns:
        一份 :class:`CapabilityPlan`。本函数绝不抛异常。
    """
    reasons: dict[str, str] = {}

    if model_info is None:
        # 未知模型 -> 保守默认：不传 tools、不发图片、不注入缓存边界。
        reasons["tools"] = "unknown model → conservative default"
        reasons["vision"] = "unknown model → conservative default"
        reasons["caching"] = "unknown model → conservative default"
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

    # backend 能力方法：更严格的补充约束（取"与"）。已降级的维度保留原有原因。
    if _call_backend_flag(backend, "supports_tool_calling") is False and plan.use_tools:
        plan.use_tools = False
        plan.reasons["tools"] = "backend does not support tool calling"
    if _call_backend_flag(backend, "supports_vision_input") is False and plan.use_vision:
        plan.use_vision = False
        plan.reasons["vision"] = "backend does not support image input"
    if _call_backend_flag(backend, "supports_prompt_caching") is False and plan.use_caching:
        plan.use_caching = False
        plan.reasons["caching"] = "backend does not support prompt caching"

    # config 覆盖：显式关闭提示缓存时同样降级。
    if config is not None and not bool(getattr(config, "enable_prompt_caching", True)):
        if plan.use_caching:
            plan.use_caching = False
            plan.reasons["caching"] = "prompt caching disabled by config"

    return plan


def apply_to_tools(
    tools: list[dict[str, Any]] | None,
    plan: CapabilityPlan,
) -> list[dict[str, Any]] | None:
    """按降级方案裁剪 tools schema。

    ``plan.use_tools`` 为假时返回 ``None``，即不向 backend 传任何 tools schema。
    """
    if not plan.use_tools:
        return None
    return tools


def apply_to_messages(
    messages: list[dict[str, Any]],
    plan: CapabilityPlan,
) -> list[dict[str, Any]]:
    """按降级方案剥离消息中的图片引用。

    ``plan.use_vision`` 为假时，把 OpenAI 风格 ``{"type": "image_url", ...}``
    与 Anthropic 风格 ``{"type": "image", "source": {...}}`` 等图片内容块替换
    为文本描述（保留 alt / text / filename 等元信息）。返回新的消息结构，**不**
    就地修改入参。
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
    """把工具清单压缩成一段纯文本指令，供注入 system prompt。

    当 tools 被降级关闭时，模型仍需要知道有哪些工具可用、如何用文本表达调用
    意图。这里复用 ``discovery.get_active_tools_payload(...)`` 的返回结构
    （OpenAI 风格 ``{"type": "function", "function": {...}}`` 或 Anthropic 风格
    ``{"name": ..., "description": ...}``），逐条渲染"工具名 + 一句话用途"。

    Args:
        tool_payload: 活跃工具清单；为空时返回空字符串。

    Returns:
        可追加到 system prompt 末尾的纯文本指令；无可用工具时返回 ``""``。
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
    """判断一个内容块是否为图片引用。"""
    if part.get("type") in _IMAGE_PART_TYPES:
        return True
    # 兜底：部分 provider 只给出 image_url/source 字段而未带 type。
    if isinstance(part.get("image_url"), dict):
        return True
    source = part.get("source")
    if isinstance(source, dict) and source.get("type") in ("base64", "url"):
        return True
    return False


def _describe_image_part(part: dict[str, Any]) -> str:
    """把被剥离的图片内容块渲染成保留元信息的文本描述。"""
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
    """把图片 URL 压缩成可读的短描述（避免内联 base64 污染 prompt）。"""
    if url.startswith("data:"):
        header = url.split(",", 1)[0]
        return f"内联图片数据（{header}）"
    if len(url) > 120:
        return url[:117] + "..."
    return url


def _tool_name_and_description(item: Any) -> tuple[str, str]:
    """从 OpenAI 或 Anthropic 风格的工具定义中取出名称与描述。"""
    if not isinstance(item, dict):
        return "", ""
    function = item.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or ""), str(function.get("description") or "")
    return str(item.get("name") or ""), str(item.get("description") or "")


def _first_sentence(text: str, limit: int = 120) -> str:
    """把工具描述压缩成一句话，避免污染 system prompt。"""
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
