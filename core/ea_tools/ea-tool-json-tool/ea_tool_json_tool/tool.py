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

"""JSON utility tool (validate / format / query / transform).

Validates, pretty-prints, minifies, and queries/transforms JSON documents with
a small expression language.
"""


import json
import os
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool


async def _json_execute(**kwargs: Any) -> str:
    """Json execute.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    action = kwargs.get("action", "")
    data = kwargs.get("data", "")
    file_path = kwargs.get("file_path", "")
    query = kwargs.get("query", "")
    indent = kwargs.get("indent", 2)
    sort_keys = kwargs.get("sort_keys", False)

    if action == "validate":
        if file_path:
            if not os.path.exists(file_path):
                return f"File not found: {file_path}"
            try:
                content = Path(file_path).read_text(encoding="utf-8")
                json.loads(content)
                return "Valid JSON"
            except json.JSONDecodeError as e:
                return f"Invalid JSON: {e}"
        if data:
            try:
                json.loads(data)
                return "Valid JSON"
            except json.JSONDecodeError as e:
                return f"Invalid JSON: {e}"
        return "Missing required field: data or file_path"

    elif action == "format":
        if file_path:
            if not os.path.exists(file_path):
                return f"File not found: {file_path}"
            try:
                content = Path(file_path).read_text(encoding="utf-8")
                parsed = json.loads(content)
                formatted = json.dumps(parsed, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
                Path(file_path).write_text(formatted + "\n", encoding="utf-8")
                return f"Formatted {file_path} ({indent=}, {sort_keys=})"
            except (json.JSONDecodeError, OSError) as e:
                return f"Failed to format: {e}"
        if data:
            try:
                parsed = json.loads(data)
                return json.dumps(parsed, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
            except json.JSONDecodeError as e:
                return f"Invalid JSON: {e}"
        return "Missing required field: data or file_path"

    elif action == "minify":
        if file_path:
            if not os.path.exists(file_path):
                return f"File not found: {file_path}"
            try:
                content = Path(file_path).read_text(encoding="utf-8")
                parsed = json.loads(content)
                minified = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                Path(file_path).write_text(minified + "\n", encoding="utf-8")
                original_size = len(content)
                new_size = len(minified)
                return f"Minified {file_path} ({original_size} -> {new_size} bytes, saved {original_size - new_size} bytes)"
            except (json.JSONDecodeError, OSError) as e:
                return f"Failed to minify: {e}"
        if data:
            try:
                parsed = json.loads(data)
                return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            except json.JSONDecodeError as e:
                return f"Invalid JSON: {e}"
        return "Missing required field: data or file_path"

    elif action == "transform":
        if not data and not file_path:
            return "Missing required field: data or file_path"
        try:
            raw = Path(file_path).read_text(encoding="utf-8") if file_path else data
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                lines = []
                for k, v in parsed.items():
                    lines.append(f"{k}: {json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}")
                return "\n".join(lines)
            elif isinstance(parsed, list):
                return "\n".join(json.dumps(item, ensure_ascii=False) for item in parsed)
            return str(parsed)
        except (json.JSONDecodeError, OSError) as e:
            return f"Failed to transform: {e}"

    elif action == "schema":
        if not data and not file_path:
            return "Missing required field: data or file_path"
        try:
            raw = Path(file_path).read_text(encoding="utf-8") if file_path else data
            parsed = json.loads(raw)
            schema = _infer_schema(parsed)
            return json.dumps(schema, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, OSError) as e:
            return f"Failed to infer schema: {e}"

    elif action == "query":
        if not data and not file_path:
            return "Missing required field: data or file_path"
        if not query:
            return "Missing required field: query"
        try:
            raw = Path(file_path).read_text(encoding="utf-8") if file_path else data
            parsed = json.loads(raw)
            result = _json_query(parsed, query)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, OSError, KeyError, IndexError, TypeError) as e:
            return f"Query failed: {e}"

    return f"Unknown action: {action}. Supported: validate, format, minify, transform, schema, query"


def _infer_schema(value: Any) -> dict[str, Any]:
    """Infer schema.

    Args:
        value: Description of the value parameter.
    """
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        if not value:
            return {"type": "array"}
        item_schemas = [_infer_schema(v) for v in value[:100]]
        return {"type": "array", "items": _merge_schemas(item_schemas)}
    if isinstance(value, dict):
        props = {}
        required = []
        for k, v in value.items():
            props[k] = _infer_schema(v)
            required.append(k)
        return {"type": "object", "properties": props, "required": required}
    return {"type": "unknown"}


def _merge_schemas(schemas: list[dict]) -> dict:
    """Merge schemas.

    Args:
        schemas: Description of the schemas parameter.
    """
    types = set(s.get("type") for s in schemas)
    if len(types) == 1:
        result = schemas[0].copy()
        if result.get("type") == "object" and all("properties" in s for s in schemas):
            merged_props = {}
            for s in schemas:
                for k, v in s.get("properties", {}).items():
                    if k in merged_props:
                        merged_props[k] = _merge_schemas([merged_props[k], v])
                    else:
                        merged_props[k] = v
            result["properties"] = merged_props
        return result
    return {"type": next(iter(types))}


def _json_query(obj: Any, query: str) -> Any:
    """Json query.

    Args:
        obj: Description of the obj parameter.
        query: Description of the query parameter.
    """
    parts = query.strip(".").split(".")
    current = obj
    for part in parts:
        if "[" in part and part.endswith("]"):
            key, _, idx = part.partition("[")
            idx = int(idx.rstrip("]"))
            current = current[key][idx]
        elif part.isdigit() and isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"Cannot resolve '{part}' on {type(current).__name__}")
    return current


json_tool = build_tool(
    name="json_tool",
    description=(
        "Validate, format, minify, transform, infer a schema from, or query "
        "JSON documents. Accepts input either as an inline 'data' string or "
        "via 'file_path'. Use this instead of piping through jq/python in "
        "bash -- it returns structured output and supports a dot-notation "
        "query language (e.g. 'data.items[0].name') and JSON-Schema-style "
        "type inference. Actions: 'validate' checks well-formedness; 'format' "
        "pretty-prints (and rewrites the file when given file_path); 'minify' "
        "removes whitespace (and rewrites the file); 'transform' renders a "
        "dict as key:value lines or a list as newline-delimited JSON; "
        "'schema' infers a JSON-Schema-style type tree; 'query' resolves a "
        "dot-notation path. "
        "TIP: For format/minify on a file, the file is rewritten in place; "
        "pass 'data' instead to get the result without touching the file. "
        "AVOID: Querying huge JSON with deep paths -- extract the slice you "
        "need first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["validate", "format", "minify", "transform", "schema", "query"],
                "description": "JSON action (required). 'validate' checks well-formedness; 'format' pretty-prints; 'minify' strips whitespace; 'transform' renders a flat view; 'schema' infers types; 'query' resolves a dot-notation path.",
            },
            "data": {"type": "string", "description": "JSON string to process (optional). Required when file_path is not given."},
            "file_path": {"type": "string", "description": "Path to a JSON file (optional). Required when data is not given. For 'format'/'minify', the file is rewritten in place."},
            "query": {"type": "string", "description": "Dot-notation query for action='query' (required for query). Example: 'data.items[0].name'."},
            "indent": {"type": "integer", "description": "Indentation spaces for format output (optional, default 2)."},
            "sort_keys": {"type": "boolean", "description": "Sort object keys in the output (optional, default false)."},
        },
        "required": ["action"],
    },
    execute=_json_execute,
    intents=["general", "coding", "data"],
    category="data",
    semantic_type="transform",
    cost_level="low",
    retryability="auto",
    is_concurrency_safe=lambda _: True,
    is_readonly=lambda _: True,
)
