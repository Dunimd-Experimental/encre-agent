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

"""ToolSpec: the single registration contract for every tool."""

from __future__ import annotations

import json

import pytest

from encre.contracts.tools import ToolSpec
from encre.contracts.version import TOOL_SPEC_VERSION


def _spec(**overrides: object) -> ToolSpec:
    base: dict[str, object] = {
        "name": "file_read",
        "description": "Read a UTF-8 file from disk.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "permissions": ("fs.read",),
    }
    base.update(overrides)
    return ToolSpec(**base)  # type: ignore[arg-type]


def test_minimal_spec_is_valid() -> None:
    spec = _spec()
    assert spec.spec_version == TOOL_SPEC_VERSION
    assert spec.timeout_s == 60.0
    assert spec.permissions == ("fs.read",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": "1bad"},
        {"name": "has space"},
        {"description": "   "},
        {"input_schema": {"type": "array"}},
        {"timeout_s": 0},
        {"concurrency_safe": True, "concurrency_key": ""},
        {"spec_version": TOOL_SPEC_VERSION + 1},
    ],
)
def test_invalid_specs_are_rejected(overrides: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        _spec(**overrides)


def test_openai_schema_shape() -> None:
    schema = _spec().to_openai_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "file_read"
    assert fn["parameters"]["required"] == ["path"]
    json.dumps(schema)


def test_roundtrip() -> None:
    spec = _spec(
        timeout_s=5,
        cancellable=False,
        idempotent=True,
        concurrency_safe=True,
        concurrency_key="fs:{path}",
        version="2.1",
        metadata={"category": "fs"},
    )
    again = ToolSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert again.to_dict() == spec.to_dict()


def test_mcp_style_names_are_allowed() -> None:
    assert _spec(name="mcp__github__search").name == "mcp__github__search"
    assert _spec(name="pkg.tool:variant-1").name == "pkg.tool:variant-1"
