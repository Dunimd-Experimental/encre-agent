#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
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

"""OTLP/HTTP (JSON) exporter for session telemetry — zero external deps.

Opt-in: only active when ``ENCRE_OTEL_ENDPOINT`` is set (e.g.
``http://localhost:4318/v1/metrics``).  Session summaries are converted to
OTLP gauge/sum metrics and POSTed from a daemon thread so a slow or absent
collector never blocks or crashes the agent loop.

Resource attributes identify the session; metric names are prefixed with
``encre.`` to avoid collisions in shared collectors.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_OTLP_TIMEOUT_S = 5.0
#: Upper bound on buffered records; oldest records are dropped beyond this.
_MAX_BUFFER = 2048


def _endpoint() -> str:
    """Return the configured OTLP endpoint, or an empty string when disabled."""
    return os.environ.get("ENCRE_OTEL_ENDPOINT", "").strip()


def _gauge(name: str, value: float, attrs: dict[str, str], ts_ns: int) -> dict[str, Any]:
    """Build one OTLP gauge metric data point.

    Wraps a scalar *value* in the OTLP ``metric.data.gauge`` envelope with
    resource attributes (*attrs*) and a nanosecond timestamp (*ts_ns*).
    Used by :func:`_otlp_payload` to serialise each session-summary field.
    """
    return {
        "name": name,
        "gauge": {
            "dataPoints": [{
                "asDouble": float(value),
                "timeUnixNano": str(ts_ns),
                "attributes": [
                    {"key": k, "value": {"stringValue": v}} for k, v in attrs.items()
                ],
            }],
        },
    }


def _otlp_payload(summary: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Build an OTLP/HTTP JSON metrics payload from a session summary."""
    ts_ns = time.time_ns()
    attrs = {"session.id": session_id, "service.name": "encre"}
    gauges = [
        _gauge("encre.session.duration_s", summary.get("session_duration_s", 0), attrs, ts_ns),
        _gauge("encre.session.turns", summary.get("total_turns", 0), attrs, ts_ns),
        _gauge("encre.session.tool_calls", summary.get("total_tool_calls", 0), attrs, ts_ns),
        _gauge("encre.session.failed_tool_calls", summary.get("failed_tool_calls", 0), attrs, ts_ns),
        _gauge("encre.session.avg_tool_latency_ms", summary.get("avg_tool_latency_ms", 0), attrs, ts_ns),
        _gauge("encre.session.avg_turn_latency_ms", summary.get("avg_turn_latency_ms", 0), attrs, ts_ns),
        _gauge("encre.session.compactions", summary.get("compactions", 0), attrs, ts_ns),
        _gauge("encre.session.retries", summary.get("total_retries", 0), attrs, ts_ns),
        _gauge("encre.session.cost_usd", summary.get("session_cost_usd", 0), attrs, ts_ns),
        _gauge("encre.session.input_tokens", summary.get("session_input_tokens", 0), attrs, ts_ns),
        _gauge("encre.session.cache_hit_tokens", summary.get("session_cache_hit_tokens", 0), attrs, ts_ns),
        _gauge("encre.session.cache_hit_rate", summary.get("session_cache_hit_rate", 0), attrs, ts_ns),
    ]
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": k, "value": {"stringValue": v}} for k, v in attrs.items()
            ]},
            "scopeMetrics": [{
                "scope": {"name": "encre.telemetry", "version": "1.0.0"},
                "metrics": gauges,
            }],
        }],
    }


def export_session_summary(summary: dict[str, Any], session_id: str) -> bool:
    """Fire-and-forget export of a session summary to the OTLP endpoint.

    Runs in a daemon thread; never raises.  Returns ``True`` when an export
    was dispatched.

    Args:
        summary: The session summary produced by ``EncreTelemetry.flush``.
        session_id: The session identifier attached as a resource attribute.

    Returns:
        True when the export thread was started, False when disabled or the
        summary is empty.
    """
    endpoint = _endpoint()
    if not endpoint or not summary:
        return False

    def _post() -> None:
        try:
            payload = json.dumps(_otlp_payload(summary, session_id)).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_OTLP_TIMEOUT_S):
                pass
            logger.debug("[otlp] session summary exported to %s", endpoint)
        except Exception as exc:
            logger.warning("[otlp] export failed: %s", exc)

    threading.Thread(target=_post, name="encre-otlp-export", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# OTLP/HTTP (JSON) logs exporter -- buffered, config-driven, zero deps
# ---------------------------------------------------------------------------

def _otlp_attr(key: str, value: Any) -> dict[str, Any] | None:
    """Map a Python scalar to one OTLP attribute entry (``None`` to skip)."""
    if value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        # OTLP JSON encodes int64 as a decimal string.
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


#: Body size cap so huge tool payloads cannot blow up one export batch.
_PAYLOAD_BODY_LIMIT = 8000


def _log_record(record: dict[str, Any]) -> dict[str, Any]:
    """Convert one telemetry record into an OTLP log record."""
    ts_ns = time.time_ns()
    event = str(record.get("event") or record.get("type") or "unknown")
    severity_number, severity_text = 9, "INFO"
    if event == "tool_call" and not record.get("success", True):
        severity_number, severity_text = 17, "ERROR"
    elif event == "retry":
        severity_number, severity_text = 13, "WARN"
    attributes = []
    for key, value in record.items():
        attr = _otlp_attr(key, value)
        if attr is not None:
            attributes.append(attr)
    body = json.dumps(record, ensure_ascii=False, default=str)
    return {
        "timeUnixNano": str(ts_ns),
        "observedTimeUnixNano": str(ts_ns),
        "severityNumber": severity_number,
        "severityText": severity_text,
        "body": {"stringValue": body[:_PAYLOAD_BODY_LIMIT]},
        "attributes": attributes,
    }


class OTLPExporter:
    """Buffered OTLP/HTTP (JSON) logs exporter with a background flusher.

    Telemetry records are appended to an in-memory buffer at the existing
    write points; a daemon thread posts them as OTLP ``resourceLogs`` every
    ``interval_seconds``.  Push failures degrade to a single log line and
    keep the records buffered (bounded); the exporter never raises into the
    caller.  A best-effort final flush runs at interpreter shutdown via
    ``atexit``.
    """

    def __init__(
        self,
        endpoint: str,
        service_name: str = "encre-harness",
        interval_seconds: int = 30,
        session_id: str = "",
    ) -> None:
        """Create a buffered exporter and start the background flush thread.

        Args:
            endpoint: OTLP/HTTP ``/v1/logs`` URL to POST to.
            service_name: OTLP ``service.name`` resource attribute.
            interval_seconds: How often the flusher thread posts (>= 1s).
            session_id: Session identifier attached to every resource.
        """
        self._endpoint = endpoint
        self._service_name = service_name or "encre-harness"
        self._session_id = session_id
        self._interval = max(1, int(interval_seconds))
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="encre-otlp-logs",
            daemon=True,
        )
        self._thread.start()

    def append(self, record: dict[str, Any]) -> None:
        """Buffer one telemetry record (never raises)."""
        try:
            with self._lock:
                if len(self._buffer) >= _MAX_BUFFER:
                    self._buffer.pop(0)
                self._buffer.append(record)
        except Exception:
            logger.debug("[otlp] append failed", exc_info=True)

    def _run(self) -> None:
        """Flush loop: post the buffer every interval until stopped."""
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except Exception:
                logger.debug("[otlp] periodic flush failed", exc_info=True)

    def _payload(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Build one OTLP logs payload from buffered records."""
        resource_attrs = [
            {"key": "service.name", "value": {"stringValue": self._service_name}},
            {"key": "session.id", "value": {"stringValue": self._session_id}},
        ]
        return {
            "resourceLogs": [{
                "resource": {"attributes": resource_attrs},
                "scopeLogs": [{
                    "scope": {"name": "encre.telemetry", "version": "1.0.0"},
                    "logRecords": [_log_record(r) for r in records],
                }],
            }],
        }

    def flush(self) -> bool:
        """Post the buffered records; returns True when a batch was sent.

        On push failure the records are kept (bounded) so the next interval
        retries; a failure logs one line and never raises.
        """
        with self._lock:
            records = self._buffer
            self._buffer = []
        if not records:
            return False
        try:
            payload = json.dumps(self._payload(records)).encode("utf-8")
            req = urllib.request.Request(
                self._endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_OTLP_TIMEOUT_S):
                pass
            return True
        except Exception as exc:
            logger.warning("[otlp] log export failed: %s", exc)
            with self._lock:
                self._buffer = records + self._buffer
                if len(self._buffer) > _MAX_BUFFER:
                    del self._buffer[_MAX_BUFFER:]
            return False

    def close(self) -> None:
        """Stop the flusher thread and do a best-effort final flush."""
        self._stop.set()
        try:
            self.flush()
        except Exception:
            logger.debug("[otlp] final flush failed", exc_info=True)


def build_otlp_exporter(config: Any, session_id: str = "") -> OTLPExporter | None:
    """Create an :class:`OTLPExporter` from config, or ``None`` when disabled.

    Accepts the flat :class:`~encre.config.EncreConfig` (fields
    ``telemetry_otlp_*``) or a plain dict with the same keys.  Returns
    ``None`` (zero overhead) unless ``enabled`` is true and an endpoint is
    configured.  Registers a best-effort ``atexit`` final flush.
    """
    try:
        if config is None:
            return None
        if isinstance(config, dict):
            enabled = bool(config.get("enabled", False))
            endpoint = str(config.get("endpoint", "") or "").strip()
            service = str(config.get("service_name", "") or "encre-harness")
            interval = int(config.get("export_interval_seconds", 30) or 30)
        else:
            enabled = bool(getattr(config, "telemetry_otlp_enabled", False))
            endpoint = str(
                getattr(config, "telemetry_otlp_endpoint", "") or ""
            ).strip()
            service = str(
                getattr(config, "telemetry_otlp_service_name", "") or "encre-harness"
            )
            interval = int(
                getattr(config, "telemetry_otlp_export_interval_seconds", 30) or 30
            )
        if not enabled or not endpoint:
            return None
        exporter = OTLPExporter(
            endpoint,
            service_name=service,
            interval_seconds=interval,
            session_id=session_id,
        )
        atexit.register(exporter.close)
        return exporter
    except Exception:
        logger.debug("[otlp] exporter build failed", exc_info=True)
        return None
