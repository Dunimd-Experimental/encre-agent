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

"""Relay capability descriptor (handshake payload).

The descriptor is the immutable capability profile the connector returns at
handshake.  It is what lets a single
:class:`encre.gateway.relay.adapter.RelayAdapter` front Discord, Telegram,
Matrix, or any other vendor without knowing which one it is: the gateway reads
the descriptor to decide how to render, stream, chunk, and truncate output, and
delegates every vendor-specific detail to the connector.

Concrete dependents:

- :attr:`encre.gateway.relay.adapter.RelayAdapter.max_message_length` reads
  :attr:`max_message_length`, which
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` and
  :class:`encre.gateway.delivery.DeliveryRouter` consult before chunking or
  truncating an outbound payload.
- :attr:`encre.gateway.relay.adapter.RelayAdapter.splits_long_messages` reads
  :attr:`supports_edit`, because edit-based delta streaming is what makes
  self-chunking safe on the fronted platform.
- ``markdown_dialect`` and ``len_unit`` tell the renderer which markup to emit
  and how to count characters, which matters because Discord bills messages in
  UTF-16 code units rather than code points.

The descriptor is frozen so it cannot mutate after handshake -- the adapter
advertises a fixed capability profile for the life of the connection, and a
changing profile mid-conversation would let the delivery router decide one
thing while the platform enforces another.

``contract_version`` (currently :data:`CONTRACT_VERSION`) is carried in the
descriptor.  The gateway ignores unknown fields (forward compatibility) and
fills missing optional fields from dataclass defaults.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

# Relay connector contract version. Experimental -- additive-only within a
# version; a breaking change requires a coordinated update of both repos and
# a version bump.
CONTRACT_VERSION = 1

# Substituted when the connector reports max_message_length as 0, which is the
# connector's "unspecified" sentinel.  The base adapter contract reads 0 as
# "no limit", so the two zero meanings must be disambiguated here rather than
# letting an unbounded payload reach the platform.
DEFAULT_MAX_MESSAGE_LENGTH = 4096


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Immutable capability profile negotiated at relay handshake.

    All required fields are populated by the connector; the gateway configures
    its adapter (character limit, length unit, draft/edit/thread/markdown
    capabilities) from this descriptor. Optional fields default sensibly.

    Freeness: instances are created once per connection during
    :meth:`encre.gateway.relay.ws_transport.WebSocketRelayTransport.connect`
    and only read afterwards, so no locking is involved.

    Attributes:
        contract_version: Contract version of this descriptor.  Compared against
            :data:`CONTRACT_VERSION` by a caller that wants to refuse an
            incompatible connector.
        platform: Machine name of the fronted platform, e.g. ``"discord"``.
            Used for logging and for the connector's egress guard routing.
        label: Human-readable platform label for logs and the adapter panel.
        max_message_length: Per-message size cap on the fronted platform.  This
            is what the stream consumer and delivery router use to chunk.
        supports_draft_streaming: The platform can incrementally stream draft
            text, which enables token-by-token delivery instead of one final
            message.
        supports_edit: The platform supports editing a previously sent message.
            This is the bit that gates self-chunking, since chunks are emitted
            by editing one message rather than sending N.
        supports_threads: The platform has first-class threads, which affects
            how a reply is placed and how ``reply_to`` is interpreted.
        markdown_dialect: Which markup the renderer should emit for this
            platform.
        len_unit: How to count characters for the message limit.
        emoji: Display glyph for CLI/UI surfaces that render a platform marker.
            The connector may override it with the fronted platform's own glyph.
        platform_hint: Optional hint injected into the agent system prompt so a
            model knows it is inside this platform.
        pii_safe: True when the connector strips personally identifiable data
            before forwarding, which changes how the gateway may log payloads.
    """

    contract_version: int
    platform: str
    label: str
    max_message_length: int
    supports_draft_streaming: bool
    supports_edit: bool
    supports_threads: bool
    markdown_dialect: str  # "plain" | "markdown_v2" | "discord" | ...
    len_unit: str  # "chars" | "utf16"
    emoji: str = "\U0001f50c"  # default marker glyph (see Attributes)
    platform_hint: str = ""
    pii_safe: bool = False

    def to_json(self) -> str:
        """Serialize to a compact, stable JSON string for the handshake frame.

        Keys are sorted for a stable wire representation
        (``json.dumps(asdict(self), sort_keys=True, ...)``), so two descriptors
        with identical fields produce byte-identical strings.  That matters if
        either side ever caches or fingerprints the handshake payload, and it
        keeps diffs meaningful when the connector adds fields.

        Returns:
            A single-line JSON string.  ``ensure_ascii=False`` is passed so the
            emoji and label stay human-readable on the wire.
        """
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for embedding in a larger frame.

        Returns:
            A fresh dict on every call.  Because the dataclass is frozen the
            caller can mutate the result without affecting this instance, which
            is the desired separation when the dict is stuffed into an outbound
            frame envelope.
        """
        return asdict(self)

    @classmethod
    def from_json(cls, raw: str) -> "CapabilityDescriptor":
        """Reconstruct from a JSON string; unknown keys are ignored (forward-compat).

        Args:
            raw: The JSON string produced by :meth:`to_json` or the connector.

        Returns:
            A new descriptor.

        Raises:
            json.JSONDecodeError: When ``raw`` is not valid JSON.
            TypeError: When a required field is missing (see :meth:`from_dict`).
        """
        return cls.from_dict(json.loads(raw))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CapabilityDescriptor":
        """Reconstruct from a dict; unknown keys are ignored, missing optionals are defaulted.

        Forward-compatible -- a connector that adds a new optional field does
        not break an older gateway, because unrecognized keys are filtered out
        before the dataclass is constructed.

        Args:
            d: The descriptor payload, typically ``frame["descriptor"]`` from a
                ``descriptor`` handshake frame.

        Returns:
            A new descriptor with defaults applied for omitted optional fields.

        Raises:
            TypeError: When a required field is absent.  Deliberate -- a
                handshake missing a required field is a contract violation and
                must fail loudly, not degrade gracefully into an adapter that
                advertises no limits.

        Note:
            A ``max_message_length`` of 0 is rewritten to
            :data:`DEFAULT_MAX_MESSAGE_LENGTH`.  The connector uses 0 as its
            "unspecified" sentinel, whereas the base adapter contract treats 0
            as "no limit"; passing it through would let an oversized message
            reach the platform unchunked.
        """
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in known}
        # max_message_length == 0 -> default.
        if filtered.get("max_message_length") == 0:
            filtered["max_message_length"] = DEFAULT_MAX_MESSAGE_LENGTH
        return cls(**filtered)
