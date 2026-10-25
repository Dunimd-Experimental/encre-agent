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

"""Canonical, versioned event envelope.

The runtime's append-only backbone is :class:`encre.events.ledger.EventSourcedBus`
and its record type is :class:`~encre.events.ledger.LedgerEvent`.  This module
defines the *wire* shape those records take when they leave the process: the
telemetry exporter, the desktop transport, an OTLP collector, or a replay file
read by a different harness version.

Why a separate type?
====================
``LedgerEvent`` is allowed to evolve with the in-process bus (private fields,
in-memory conveniences).  :class:`EventEnvelope` is a **contract**: it carries
an explicit ``schema_version`` and only JSON-native values, so a consumer can
validate and migrate a record without importing the bus.  The two are kept in
sync by :meth:`EventEnvelope.from_ledger` and
:meth:`encre.events.ledger.LedgerEvent.from_envelope`: the contract never
imports the bus, so it stays usable by an exporter or a replay reader that
does not ship the runtime.

Compatibility
=============
``from_dict`` accepts records written by older harness versions (missing
``trace_id``, ``turn``, or ``schema_version``) and fills the defaults.  It never
raises on unknown extra keys: forward-compatible fields are preserved under
``payload["_extra"]`` so a newer writer's data is not silently dropped by an
older reader.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from encre.contracts.version import EVENT_SCHEMA_VERSION

__all__ = ["EventEnvelope", "new_envelope"]

#: Fields owned by the envelope; anything else is preserved as extra payload.
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "seq",
        "ts",
        "kind",
        "session_id",
        "turn",
        "trace_id",
        "event_id",
        "refs",
        "payload",
    }
)


def _json_safe(value: Any) -> Any:
    """Return a JSON-native copy of ``value`` (non-native leaves become repr)."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError, RecursionError):
        return repr(value)
    return value


@dataclass
class EventEnvelope:
    """A versioned, JSON-native event record.

    Args:
        kind: Event type (``"turn_started"``, ``"tool_call"``, ...).
        seq: Monotonic sequence number within the producing bus.  ``0`` means
            "not assigned" and is legal for events created before publication.
        ts: Wall-clock timestamp (``time.time()``).
        session_id: Session the event belongs to (may be empty).
        turn: Turn number within the session, or ``0`` when not turn-scoped.
        trace_id: Correlation id shared by every event of one causal chain.
        event_id: Unique id; generated when omitted.
        refs: Related ids (tool calls, memory entries, prior events).
        payload: Arbitrary JSON-native load.
        schema_version: Envelope schema version; defaults to the current one.
    """

    kind: str
    seq: int = 0
    ts: float = field(default_factory=time.time)
    session_id: str = ""
    turn: int = 0
    trace_id: str = ""
    event_id: str = field(default_factory=lambda: secrets.token_hex(8))
    refs: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("EventEnvelope.kind must be a non-empty string")
        if self.seq < 0:
            raise ValueError("EventEnvelope.seq must be >= 0")
        if self.schema_version < 1:
            raise ValueError("EventEnvelope.schema_version must be >= 1")
        if not isinstance(self.payload, dict):
            raise TypeError("EventEnvelope.payload must be a dict")
        self.refs = [str(r) for r in self.refs]
        self.payload = _json_safe(self.payload)

    def to_dict(self) -> dict[str, Any]:
        """Return the envelope as a JSON-native dict."""
        return {
            "schema_version": self.schema_version,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "session_id": self.session_id,
            "turn": self.turn,
            "trace_id": self.trace_id,
            "event_id": self.event_id,
            "refs": list(self.refs),
            "payload": _json_safe(self.payload),
        }

    def to_json(self) -> str:
        """Serialise the envelope to a single-line JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventEnvelope:
        """Build an envelope from a possibly older or newer record.

        Unknown keys are preserved under ``payload["_extra"]`` rather than
        dropped, so an older reader can round-trip a newer writer's record.
        """
        if not isinstance(data, dict):
            raise TypeError("EventEnvelope.from_dict expects a dict")
        payload = dict(data.get("payload") or {})
        extra = {k: v for k, v in data.items() if k not in _ENVELOPE_FIELDS}
        if extra:
            payload.setdefault("_extra", {}).update(extra)
        return cls(
            kind=str(data.get("kind", "")),
            seq=int(data.get("seq", 0) or 0),
            ts=float(data.get("ts", 0.0) or 0.0),
            session_id=str(data.get("session_id", "") or ""),
            turn=int(data.get("turn", 0) or 0),
            trace_id=str(data.get("trace_id", "") or ""),
            event_id=str(data.get("event_id", "") or ""),
            refs=[str(r) for r in (data.get("refs") or [])],
            payload=payload,
            schema_version=int(data.get("schema_version", 1) or 1),
        )

    @classmethod
    def from_json(cls, raw: str) -> EventEnvelope:
        """Parse a single JSON record produced by :meth:`to_json`."""
        return cls.from_dict(json.loads(raw))

    @classmethod
    def from_ledger(cls, event: Any) -> EventEnvelope:
        """Project a :class:`~encre.events.ledger.LedgerEvent` onto the contract."""
        return cls.from_dict(event.to_dict())


def new_envelope(
    kind: str,
    *,
    payload: dict[str, Any] | None = None,
    session_id: str = "",
    turn: int = 0,
    trace_id: str = "",
    refs: list[str] | None = None,
    ts: float | None = None,
) -> EventEnvelope:
    """Create an unpublished envelope with a fresh id and timestamp."""
    return EventEnvelope(
        kind=kind,
        ts=time.time() if ts is None else ts,
        session_id=session_id,
        turn=turn,
        trace_id=trace_id,
        refs=list(refs or []),
        payload=payload or {},
    )
