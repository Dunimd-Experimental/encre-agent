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
OpenTelemetry integration for Encre.

Provides a single ``Tracer`` wrapper that is a no-op when
``opentelemetry-api`` is not installed or when tracing is disabled
in config.  When active it emits traces compatible with
OpenInference / Arize Phoenix / Jaeger / any OTLP collector.

Usage in ``loop.py``::

    from encre.tracing import maybe_get_tracer  # noqa: E402
    tracer = maybe_get_tracer()

    with tracer.start_span("agent_turn") as span:
        span.set_attribute("turn", n)
        ...
"""
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.tracing")

# ---------------------------------------------------------------------------
# Lazy OTel imports -- the module works without opentelemetry installed
# ---------------------------------------------------------------------------

from opentelemetry import trace as otel_trace
from opentelemetry.trace import SpanKind
_OTEL_AVAILABLE = True


# ---------------------------------------------------------------------------
# No-op span when OTel is unavailable or disabled
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """Context manager that does nothing -- drops all attributes and events."""

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_attributes(self, attrs: dict[str, Any]) -> None:
        pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exc: Exception) -> None:
        logger.debug("exception suppressed", exc_info=True)

    def end(self) -> None:
        pass


class _NoOpTracer:
    """Tracer that returns ``_NoOpSpan`` for every call."""

    def start_span(self, *_args: Any, **_kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_as_current_span(self, *_args: Any, **_kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    @contextmanager
    def start_span_cm(self, _name: str, **_kwargs: Any) -> Generator[_NoOpSpan, None, None]:
        yield _NoOpSpan()


# ---------------------------------------------------------------------------
# Real tracer (wraps opentelemetry-sdk)
# ---------------------------------------------------------------------------


class _RealTracer:
    """Thin wrapper around an OTel ``Tracer``.

    Created once by :func:`setup_tracing`.  Falls back to ``_NoOpTracer``
    silently on init failure.
    """

    def __init__(self, service_name: str = "encre", endpoint: str = "") -> None:
        self._tracer: Any = None
        try:
            if not _OTEL_AVAILABLE:
                logger.info("[tracing] opentelemetry-api not installed; traces are no-ops")
                return
            resource = otel_trace._Resource.create(  # type: ignore[attr-defined]
                {"service.name": service_name}
            )
            if endpoint:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                provider = TracerProvider(resource=resource)
                exporter = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                otel_trace.set_tracer_provider(provider)
            else:
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import SimpleSpanProcessor
                from opentelemetry.sdk.trace.export.in_memory import (
                    InMemorySpanExporter,
                )

                provider = TracerProvider(resource=resource)
                # In-memory exporter keeps spans accessible for the desktop UI
                # telemetry panel without requiring an OTLP collector.
                _mem_exporter = InMemorySpanExporter()
                provider.add_span_processor(SimpleSpanProcessor(_mem_exporter))
                otel_trace.set_tracer_provider(provider)
                self._mem_exporter = _mem_exporter

            self._tracer = otel_trace.get_tracer("encre", "0.1.0")
            logger.info("[tracing] OpenTelemetry tracer initialised (service=%s, endpoint=%s)",
                        service_name, endpoint or "<console>")
        except Exception as exc:
            logger.warning("[tracing] Failed to init OTel tracer: %s", exc)

    def start_span(self, name: str, **kwargs: Any) -> Any:
        t = self._tracer
        if t is None:
            return _NoOpSpan()
        return t.start_span(name, **kwargs)

    @contextmanager
    def start_span_cm(self, name: str, **kwargs: Any) -> Generator[Any, None, None]:
        t = self._tracer
        if t is None:
            yield _NoOpSpan()
            return
        with t.start_as_current_span(name, **kwargs) as span:
            yield span


# ---------------------------------------------------------------------------
# Global tracer singleton
# ---------------------------------------------------------------------------

_tracer: _RealTracer | _NoOpTracer = _NoOpTracer()


def setup_tracing(
    enabled: bool = False,
    service_name: str = "encre",
    endpoint: str = "",
) -> None:
    """Configure the global tracer singleton.

    Call once at application startup (e.g. after config is loaded).
    When ``enabled`` is ``False`` or ``opentelemetry-api`` is not
    installed, all subsequent :func:`maybe_get_tracer` calls return a
    no-op tracer.
    """
    global _tracer
    if not enabled:
        _tracer = _NoOpTracer()
        return
    _tracer = _RealTracer(service_name=service_name, endpoint=endpoint)


def maybe_get_tracer() -> _RealTracer | _NoOpTracer:
    """Return the global tracer (no-op if tracing is disabled or
    ``setup_tracing`` was never called)."""
    return _tracer


# ---------------------------------------------------------------------------
# Convenience helpers for loop.py
# ---------------------------------------------------------------------------


def trace_tool_call(tracer: Any, tool_name: str, tool_args: dict[str, Any]) -> Any:
    """Start a span for a single tool execution.

    Returns the span (already active).  Caller **must** exit the span
    after the tool completes::

        span = trace_tool_call(tracer, "file_read", {"path": "..."})
        try:
            result = await tool.execute(...)
            span.set_attribute("success", True)
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise
        finally:
            span.end()
    """
    span = tracer.start_span(
        f"tool.{tool_name}",
        kind=SpanKind.INTERNAL if _OTEL_AVAILABLE else None,
        attributes={
            "tool.name": tool_name,
            "tool.args_summary": _truncate(str(tool_args), 200),
        },
    )
    return span


def trace_llm_call(tracer: Any, model: str, prompt_preview: str) -> Any:
    """Start a span for an LLM backend call.

    Follows the OpenInference semantic convention for LLM spans::

        span.set_attribute("llm.model", model)
        span.set_attribute("llm.input_messages", ...)
        span.set_attribute("llm.token_count.prompt", N)
        span.set_attribute("llm.token_count.completion", M)
    """
    span = tracer.start_span(
        f"llm.{model}",
        kind=SpanKind.CLIENT if _OTEL_AVAILABLE else None,
        attributes={
            "llm.model": model,
            "llm.input_messages": _truncate(prompt_preview, 500),
            "llm.provider": "openai" if "openai" in model.lower() else "unknown",
        },
    )
    return span


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to at most *max_len* characters for span attributes.

    OTLP providers may reject spans whose attributes exceed size limits.
    Truncating long prompts, tool arguments, and error messages before
    export prevents rejected spans and keeps telemetry payloads small.
    The ``"..."`` suffix makes it obvious to consumers that the value
    was cut off rather than ending naturally.

    Args:
        text: The string to truncate.
        max_len: Maximum allowed length. The suffix ``"..."`` is
            appended to the truncated result, so the total length may
            be up to ``max_len + 3`` characters.

    Returns:
        The original string if it is already within the limit,
        otherwise the first *max_len* characters plus ``"..."``.
    """
    return text if len(text) <= max_len else text[:max_len] + "..."
