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

"""Versioned tool specification (ToolSpec v1).

The harness has three tool sources -- builtins, EA plugins, and MCP servers --
and historically each described itself slightly differently.  :class:`ToolSpec`
is the one description every source must produce before a tool can be
registered.  It is deliberately a *contract*, not a registry: it answers
"what is this tool, what may it do, and how must it be invoked?" so the
executor, the permission engine, and the model-facing schema all read the same
answers.

Invariants enforced by :meth:`ToolSpec.validate`:

* ``name`` is stable, non-empty, and safe to put in a JSON schema.
* ``description`` is non-empty (the model needs it to choose the tool).
* ``input_schema`` is a JSON Schema object (``{"type": "object", ...}``).
* ``timeout_s`` is positive; ``0`` would mean "hang forever".
* ``concurrency_key`` is non-empty when the tool declares ``concurrency_safe``
  so the executor can serialise conflicting calls deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from encre.contracts.version import TOOL_SPEC_VERSION

__all__ = ["ToolSpec"]

#: Tool names are model-visible identifiers: letters, digits, ``_ . : -``.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


@dataclass
class ToolSpec:
    """The contract every registrable tool must satisfy.

    Args:
        name: Stable identifier (``file_read``, ``mcp__github__search``).
        description: One-paragraph, model-facing explanation.
        input_schema: JSON Schema object describing the arguments.
        permissions: Capability labels the tool needs (``fs.read``, ``net``).
        timeout_s: Wall-clock budget for one invocation, in seconds.
        cancellable: Whether the executor may cancel an in-flight call.
        idempotent: Whether repeating the same call is side-effect free.
        concurrency_safe: Whether calls may overlap with the same key.
        concurrency_key: Key used to serialise unsafe concurrent calls.
        version: Tool implementation version (not the spec version).
        metadata: Free-form, JSON-native annotations.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    permissions: tuple[str, ...] = ()
    timeout_s: float = 60.0
    cancellable: bool = True
    idempotent: bool = False
    concurrency_safe: bool = False
    concurrency_key: str = ""
    version: str = "1.0"
    metadata: dict[str, Any] = field(default_factory=dict)
    spec_version: int = TOOL_SPEC_VERSION

    def __post_init__(self) -> None:
        self.permissions = tuple(str(p) for p in self.permissions)
        self.validate()

    def validate(self) -> None:
        """Raise :class:`ValueError` when the spec cannot be registered."""
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise ValueError(
                f"invalid tool name {self.name!r}: expected "
                "'^[A-Za-z][A-Za-z0-9_.:-]{0,127}$'"
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError(f"tool {self.name!r}: description must be non-empty")
        if not isinstance(self.input_schema, dict):
            raise TypeError(f"tool {self.name!r}: input_schema must be a dict")
        if self.input_schema.get("type") != "object":
            raise ValueError(
                f"tool {self.name!r}: input_schema.type must be 'object'"
            )
        if not isinstance(self.timeout_s, int | float) or self.timeout_s <= 0:
            raise ValueError(f"tool {self.name!r}: timeout_s must be > 0")
        if self.concurrency_safe and not self.concurrency_key:
            raise ValueError(
                f"tool {self.name!r}: concurrency_safe requires concurrency_key"
            )
        if self.spec_version != TOOL_SPEC_VERSION:
            raise ValueError(
                f"tool {self.name!r}: unsupported spec_version {self.spec_version}"
            )

    def to_openai_schema(self) -> dict[str, Any]:
        """Return the model-facing function schema (OpenAI tool format)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-native dict representation."""
        return {
            "spec_version": self.spec_version,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "permissions": list(self.permissions),
            "timeout_s": self.timeout_s,
            "cancellable": self.cancellable,
            "idempotent": self.idempotent,
            "concurrency_safe": self.concurrency_safe,
            "concurrency_key": self.concurrency_key,
            "version": self.version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolSpec:
        """Build a spec from :meth:`to_dict` output (unknown keys ignored)."""
        return cls(
            name=str(data["name"]),
            description=str(data["description"]),
            input_schema=dict(
                data.get("input_schema") or {"type": "object", "properties": {}}
            ),
            permissions=tuple(data.get("permissions") or ()),
            timeout_s=float(data.get("timeout_s", 60.0)),
            cancellable=bool(data.get("cancellable", True)),
            idempotent=bool(data.get("idempotent", False)),
            concurrency_safe=bool(data.get("concurrency_safe", False)),
            concurrency_key=str(data.get("concurrency_key", "")),
            version=str(data.get("version", "1.0")),
            metadata=dict(data.get("metadata") or {}),
            spec_version=int(data.get("spec_version", TOOL_SPEC_VERSION)),
        )
