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

"""Unified error taxonomy for the agent platform.

Consolidates the three previously-independent classification systems
(tool-level :class:`ErrorCategory`, loop-level :class:`RecoveryKind`,
and HTTP-level :class:`ErrorClass`) into a single ``ErrorCode`` enum
and a structured ``AgentError`` dataclass.

Frontend contract
-----------------
Every error emitted from the agent loop (via ``BackendError``,
``Finish(error=...)``, or ``ToolResult(is_error=True)``) carries a
``code`` field with an ``ErrorCode`` value and a ``category`` string.
The frontend SHOULD use ``category`` for icon/color selection and
``code`` for precise error handling, NEVER parse the ``message`` text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Unified, exhaustive error taxonomy spanning all platform layers.

    Grouped by category; each code maps to exactly one category in
    ``ERROR_METADATA``.
    """

    # 鈹€鈹€ Auth (4) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    AUTH_INVALID_KEY = "auth_invalid_key"
    AUTH_EXPIRED = "auth_expired"
    AUTH_PERMISSION = "auth_permission"
    AUTH_QUOTA_EXCEEDED = "auth_quota_exceeded"

    # 鈹€鈹€ Rate limit (2) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    RATE_LIMIT = "rate_limit"
    RATE_LIMIT_UPSTREAM = "rate_limit_upstream"

    # 鈹€鈹€ Context (3) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    CONTEXT_OVERFLOW = "context_overflow"
    CONTEXT_TOO_LONG = "context_too_long"
    MAX_OUTPUT_TOKENS = "max_output_tokens"

    # 鈹€鈹€ Network (4) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_CONNECTION = "network_connection"
    NETWORK_DNS = "network_dns"
    NETWORK_SSL = "network_ssl"

    # 鈹€鈹€ Server (2) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    SERVER_ERROR = "server_error"
    SERVER_OVERLOADED = "server_overloaded"

    # 鈹€鈹€ Model (3) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_THINKING_SIGNATURE = "model_thinking_signature"

    # 鈹€鈹€ Tool (5) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_EXECUTION = "tool_execution"
    TOOL_PERMISSION_DENIED = "tool_permission_denied"
    TOOL_INPUT_INVALID = "tool_input_invalid"
    TOOL_SANDBOX = "tool_sandbox"

    # 鈹€鈹€ Session (2) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_CANCELLED = "session_cancelled"

    # 鈹€鈹€ Unknown 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    UNKNOWN = "unknown"


class ErrorCategory(str, Enum):
    """High-level category for frontend rendering (icon, color)."""
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    CONTEXT = "context"
    NETWORK = "network"
    SERVER = "server"
    MODEL = "model"
    TOOL = "tool"
    SESSION = "session"
    UNKNOWN = "unknown"


@dataclass
class ErrorMetadata:
    """Static metadata associated with each ``ErrorCode``."""
    category: ErrorCategory
    retryable: bool
    message: str


ERROR_METADATA: dict[ErrorCode, ErrorMetadata] = {
    # Auth
    ErrorCode.AUTH_INVALID_KEY: ErrorMetadata(
        ErrorCategory.AUTH, False, "Invalid API key",
    ),
    ErrorCode.AUTH_EXPIRED: ErrorMetadata(
        ErrorCategory.AUTH, False, "API key or token has expired",
    ),
    ErrorCode.AUTH_PERMISSION: ErrorMetadata(
        ErrorCategory.AUTH, False, "Insufficient permissions for this action",
    ),
    ErrorCode.AUTH_QUOTA_EXCEEDED: ErrorMetadata(
        ErrorCategory.AUTH, False, "Quota exceeded for the current billing period",
    ),
    # Rate limit
    ErrorCode.RATE_LIMIT: ErrorMetadata(
        ErrorCategory.RATE_LIMIT, True, "Rate limit exceeded",
    ),
    ErrorCode.RATE_LIMIT_UPSTREAM: ErrorMetadata(
        ErrorCategory.RATE_LIMIT, True, "Upstream provider rate limit exceeded",
    ),
    # Context
    ErrorCode.CONTEXT_OVERFLOW: ErrorMetadata(
        ErrorCategory.CONTEXT, True, "Context window overflow",
    ),
    ErrorCode.CONTEXT_TOO_LONG: ErrorMetadata(
        ErrorCategory.CONTEXT, True, "Prompt exceeds maximum context length",
    ),
    ErrorCode.MAX_OUTPUT_TOKENS: ErrorMetadata(
        ErrorCategory.CONTEXT, True, "Maximum output tokens reached",
    ),
    # Network
    ErrorCode.NETWORK_TIMEOUT: ErrorMetadata(
        ErrorCategory.NETWORK, True, "Request timed out",
    ),
    ErrorCode.NETWORK_CONNECTION: ErrorMetadata(
        ErrorCategory.NETWORK, True, "Connection error",
    ),
    ErrorCode.NETWORK_DNS: ErrorMetadata(
        ErrorCategory.NETWORK, True, "DNS resolution failed",
    ),
    ErrorCode.NETWORK_SSL: ErrorMetadata(
        ErrorCategory.NETWORK, False, "SSL certificate verification failed",
    ),
    # Server
    ErrorCode.SERVER_ERROR: ErrorMetadata(
        ErrorCategory.SERVER, True, "Server error",
    ),
    ErrorCode.SERVER_OVERLOADED: ErrorMetadata(
        ErrorCategory.SERVER, True, "Server overloaded",
    ),
    # Model
    ErrorCode.MODEL_NOT_FOUND: ErrorMetadata(
        ErrorCategory.MODEL, False, "Model not found or unavailable",
    ),
    ErrorCode.MODEL_UNAVAILABLE: ErrorMetadata(
        ErrorCategory.MODEL, False, "Model is temporarily unavailable",
    ),
    ErrorCode.MODEL_THINKING_SIGNATURE: ErrorMetadata(
        ErrorCategory.MODEL, True, "Extended thinking signature mismatch, retrying",
    ),
    # Tool
    ErrorCode.TOOL_NOT_FOUND: ErrorMetadata(
        ErrorCategory.TOOL, False, "Tool not found",
    ),
    ErrorCode.TOOL_EXECUTION: ErrorMetadata(
        ErrorCategory.TOOL, True, "Tool execution failed",
    ),
    ErrorCode.TOOL_PERMISSION_DENIED: ErrorMetadata(
        ErrorCategory.TOOL, False, "Permission denied for tool",
    ),
    ErrorCode.TOOL_INPUT_INVALID: ErrorMetadata(
        ErrorCategory.TOOL, False, "Invalid tool input",
    ),
    ErrorCode.TOOL_SANDBOX: ErrorMetadata(
        ErrorCategory.TOOL, True, "Sandbox execution failed",
    ),
    # Session
    ErrorCode.SESSION_NOT_FOUND: ErrorMetadata(
        ErrorCategory.SESSION, False, "Session not found",
    ),
    ErrorCode.SESSION_CANCELLED: ErrorMetadata(
        ErrorCategory.SESSION, False, "Session was cancelled",
    ),
    # Unknown
    ErrorCode.UNKNOWN: ErrorMetadata(
        ErrorCategory.UNKNOWN, True, "An unknown error occurred",
    ),
}


def classify_error_code(
    error_text: str,
    status_code: int | None = None,
    *,
    finish_reason: str | None = None,
) -> ErrorCode:
    """Classify an error string + optional HTTP status into an ``ErrorCode``.

    This is the SINGLE classification function that replaces the three
    previous independent classifiers:
      - ``recovery.py:classify_error``
      - ``loop_stability.py:classify_error``
      - ``backends/retry.py:ErrorClass``
    """
    if finish_reason in ("max_tokens", "length"):
        return ErrorCode.MAX_OUTPUT_TOKENS

    if status_code:
        if status_code == 429:
            if "upstream" in error_text.lower() or "aggregator" in error_text.lower():
                return ErrorCode.RATE_LIMIT_UPSTREAM
            return ErrorCode.RATE_LIMIT
        if status_code in (502, 503, 504):
            return ErrorCode.SERVER_ERROR
        if status_code == 529:
            return ErrorCode.SERVER_OVERLOADED
        if status_code in (401, 403):
            return ErrorCode.AUTH_INVALID_KEY
        if status_code == 404:
            return ErrorCode.MODEL_NOT_FOUND
        if status_code == 400:
            lower = error_text.lower()
            if "context" in lower or "length" in lower or "too long" in lower or "token" in lower:
                return ErrorCode.CONTEXT_TOO_LONG
            return ErrorCode.TOOL_INPUT_INVALID

    lower = error_text.lower()

    # Auth patterns
    if any(k in lower for k in ("invalid api key", "authentication", "unauthorized",
                                 "401", "403", "permission denied", "access denied",
                                 "quota", "billing", "insufficient_quota")):
        if "quota" in lower or "billing" in lower or "insufficient_quota" in lower:
            return ErrorCode.AUTH_QUOTA_EXCEEDED
        return ErrorCode.AUTH_INVALID_KEY

    # Rate limit patterns
    if any(k in lower for k in ("rate limit", "too many requests", "429")):
        return ErrorCode.RATE_LIMIT

    # Context overflow patterns
    if any(k in lower for k in ("context length", "context window", "prompt too long",
                                 "maximum context", "413", "payload too large")):
        return ErrorCode.CONTEXT_OVERFLOW

    # Network patterns
    if any(k in lower for k in ("timeout", "timed out")):
        return ErrorCode.NETWORK_TIMEOUT
    if any(k in lower for k in ("connection", "connection refused", "connection reset",
                                 "econnreset", "econnrefused")):
        return ErrorCode.NETWORK_CONNECTION
    if any(k in lower for k in ("dns", "name resolution")):
        return ErrorCode.NETWORK_DNS
    if any(k in lower for k in ("ssl", "certificate", "certificate verify failed")):
        return ErrorCode.NETWORK_SSL

    # Server patterns
    if any(k in lower for k in ("502", "503", "504", "bad gateway", "service unavailable",
                                 "server error", "internal server error")):
        return ErrorCode.SERVER_ERROR
    if any(k in lower for k in ("529", "overloaded", "too many requests")):
        return ErrorCode.SERVER_OVERLOADED

    # Tool patterns
    if any(k in lower for k in ("tool not found", "unknown tool")):
        return ErrorCode.TOOL_NOT_FOUND
    if "sandbox" in lower or "container" in lower:
        return ErrorCode.TOOL_SANDBOX

    return ErrorCode.UNKNOWN


def get_error_metadata(code: ErrorCode) -> ErrorMetadata:
    """Return metadata for *code*, falling back to UNKNOWN."""
    return ERROR_METADATA.get(code, ERROR_METADATA[ErrorCode.UNKNOWN])


@dataclass
class AgentError:
    """Structured, code-classified error for use throughout the platform.

    This is the canonical error type.  All three layers (tool, loop,
    HTTP) produce ``AgentError`` instances instead of raw strings or
    layer-specific enums.

    Serialises to JSON for the frontend as::

        {"message": ..., "code": ..., "category": ...,
         "retryable": ..., "retry_after": ..., "details": ...}
    """
    message: str
    code: ErrorCode = ErrorCode.UNKNOWN
    retryable: bool = True
    retry_after: float | None = None
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.code, str):
            self.code = ErrorCode(self.code)

    @property
    def category(self) -> ErrorCategory:
        return get_error_metadata(self.code).category

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "code": self.code.value,
            "category": self.category.value,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "details": self.details or {},
        }

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        status_code: int | None = None,
        *,
        finish_reason: str | None = None,
    ) -> AgentError:
        text = str(exc)
        code = classify_error_code(text, status_code, finish_reason=finish_reason)
        meta = get_error_metadata(code)
        return cls(
            message=text,
            code=code,
            retryable=meta.retryable,
        )


@dataclass
class WithheldError:
    """A wrapper that suppresses an error from the UI until recovery is
    attempted.  Mirrors Claude Code's withheld error pattern.

    Usage::

        withheld = WithheldError(exception, kind="network")
        if withheld.should_withhold:
            withheld.consume()   # recovery succeeded
            continue
        released = withheld.release()  # surface to user
    """
    exception: BaseException
    kind: str = "unknown"
    _released: bool = False

    @property
    def should_withhold(self) -> bool:
        return not self._released

    def consume(self) -> None:
        self._released = True

    def release(self) -> str:
        self._released = True
        return str(self.exception)


# 鈹€鈹€ Prompt-injection defense helpers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

_UNTRUSTED_DELIMITER_OPEN = "<untrusted_tool_result>"
_UNTRUSTED_DELIMITER_CLOSE = "</untrusted_tool_result>"


def neutralize_delimiters(text: str) -> str:
    """Defang embedded delimiter tokens in *text* so nested-delimiter
    attacks cannot break out of the wrapper.

    Replaces ``<untrusted_tool_result>`` and ``</untrusted_tool_result>``
    with Unicode-homoglyph variants that a human (or model) can still
    read but a naive parser won't match.
    """
    text = text.replace(_UNTRUSTED_DELIMITER_OPEN, "\uff1cuntrusted_tool_result\uff1e")
    text = text.replace(_UNTRUSTED_DELIMITER_CLOSE, "\uff1c/untrusted_tool_result\uff1e")
    return text


_NEUTRALIZE_DELIMITERS = neutralize_delimiters  # public alias, same usage as Hermes
