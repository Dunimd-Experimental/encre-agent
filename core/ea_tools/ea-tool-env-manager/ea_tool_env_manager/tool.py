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

"""Environment variable / .env configuration manager.

Reads, writes and loads environment variables and .env files so the model can
inspect and adjust process configuration safely.
"""


import asyncio
import json
import os
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool


async def _env_execute(**kwargs: Any) -> str:
    """Env execute.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    action = kwargs.get("action", "")
    key = kwargs.get("key", "")
    value = kwargs.get("value", "")
    file_path = kwargs.get("file_path", "")
    format_type = kwargs.get("format", "env")
    scope = kwargs.get("scope", "process")

    loop = asyncio.get_event_loop()

    if action == "get":
        if not key:
            return json.dumps(dict(os.environ), ensure_ascii=False, indent=2)
        return os.environ.get(key, f"Key not found: {key}")

    elif action == "set":
        if not key:
            return "Missing required field: key"
        if scope == "process":
            os.environ[key] = str(value)
            return f"Set {key}={value} (process scope)"
        elif scope == "file":
            if not file_path:
                return "Missing required field: file_path for file scope"
            return await _modify_env_file(file_path, [(key, str(value))], loop)
        else:
            return f"Unsupported scope: {scope}"

    elif action == "delete":
        if not key:
            return "Missing required field: key"
        if scope == "process":
            os.environ.pop(key, None)
            return f"Deleted {key} from environment (process scope)"
        elif scope == "file":
            if not file_path:
                return "Missing required field: file_path for file scope"
            return await _modify_env_file(file_path, [(key, None)], loop)
        else:
            return f"Unsupported scope: {scope}"

    elif action == "list":
        return json.dumps(dict(os.environ), ensure_ascii=False, indent=2, sort_keys=True)

    elif action == "load":
        if not file_path:
            return "Missing required field: file_path"
        if not os.path.exists(file_path):
            return f"File not found: {file_path}"

        def _load() -> str:
            """Load."""
            try:
                content = Path(file_path).read_text(encoding="utf-8")
                ext = os.path.splitext(file_path)[1].lower()
                loaded = 0

                if ext == ".json":
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        for k, v in parsed.items():
                            os.environ[str(k)] = str(v)
                            loaded += 1
                elif ext in (".yaml", ".yml"):
                    try:
                        import yaml
                        parsed = yaml.safe_load(content)
                        if isinstance(parsed, dict):
                            for k, v in parsed.items():
                                os.environ[str(k)] = str(v)
                                loaded += 1
                    except ImportError:
                        return "PyYAML not installed. Install with: pip install pyyaml"
                elif ext == ".toml":
                    try:
                        import tomllib
                    except ImportError:
                            import tomli as tomllib
                    parsed = tomllib.loads(content)
                    if isinstance(parsed, dict):
                        for k, v in parsed.items():
                            os.environ[str(k)] = str(v)
                            loaded += 1
                else:
                    for line in content.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        k = k.strip()
                        v = v.strip().strip("\"'")
                        os.environ[k] = v
                        loaded += 1

                return f"Loaded {loaded} variables from {file_path}"
            except Exception as e:
                return f"Failed to load {file_path}: {e}"

        return await loop.run_in_executor(None, _load)

    elif action == "save":
        if not file_path:
            return "Missing required field: file_path"

        def _save() -> str:
            """Save."""
            try:
                p = Path(file_path)
                p.parent.mkdir(parents=True, exist_ok=True)

                format_lower = format_type.lower()
                if format_lower == "json":
                    p.write_text(json.dumps(dict(os.environ), ensure_ascii=False, indent=2), encoding="utf-8")
                elif format_lower == "env":
                    lines = []
                    for k, v in sorted(os.environ.items()):
                        lines.append(f"{k}={v}")
                    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
                elif format_lower in ("yaml", "yml"):
                    try:
                        import yaml
                        with open(str(p), "w", encoding="utf-8") as f:
                            if hasattr(yaml, "dump"):
                                yaml.dump(dict(os.environ), f, default_flow_style=False)
                            else:
                                yaml.dump(dict(os.environ), f)
                    except ImportError:
                        return "PyYAML not installed. Install with: pip install pyyaml"
                else:
                    return f"Unsupported format: {format_type}. Supported: env, json, yaml"
                return f"Environment saved to {file_path} ({len(os.environ)} variables)"
            except Exception as e:
                return f"Failed to save environment: {e}"

        return await loop.run_in_executor(None, _save)

    return f"Unknown action: {action}. Supported: get, set, delete, list, load, save"


async def _modify_env_file(file_path: str, pairs: list[tuple[str, str | None]], loop: asyncio.AbstractEventLoop) -> str:
    """Modify env file.

    Args:
        file_path: Description of the file_path parameter.
        pairs: Description of the pairs parameter.
        loop: Description of the loop parameter.
    """
    def _modify() -> str:
        """Modify."""
        p = Path(file_path)
        lines = [] if not p.exists() else p.read_text(encoding="utf-8").splitlines(keepends=True)

        updated = 0
        added = 0

        for key, val in pairs:
            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                existing_key = stripped.split("=", 1)[0].strip()
                if existing_key == key:
                    if val is None:
                        lines[i] = f"# {stripped}  # deleted by env_manager\n"
                    else:
                        lines[i] = f"{key}={val}\n"
                    found = True
                    updated += 1
                    break
            if not found and val is not None:
                lines.append(f"{key}={val}\n")
                added += 1

        p.write_text("".join(lines), encoding="utf-8")
        parts = []
        if added:
            parts.append(f"added {added}")
        if updated:
            parts.append(f"updated {updated}")
        return f"Environment file {file_path}: {', '.join(parts)}"

    return await loop.run_in_executor(None, _modify)


env_manager_tool = build_tool(
    name="env_manager",
    description=(
        "Read and modify environment variables and config files. The 'process' "
        "scope mutates the current process environment (get/set/delete/list); "
        "the 'file' scope persists changes to a .env file. The 'load' action "
        "imports variables from .env, JSON, YAML, or TOML files into the "
        "process environment, and 'save' exports the current environment to a "
        "file in env, JSON, or YAML format. Use this instead of bash "
        "`export`/`set`/`unset` and hand-editing .env files -- it handles "
        "multiple formats, preserves comments on file writes, and keeps "
        "config changes auditable. "
        "TIP: Use scope='file' with action='set' to update a specific key in "
        "an existing .env without rewriting unrelated lines. "
        "AVOID: Storing secrets in plain .env files committed to version "
        "control -- prefer environment variables or a secrets manager."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "set", "delete", "list", "load", "save"],
                "description": "Action to perform (required). 'get' returns one variable or all; 'set'/'delete' modify by scope; 'list' dumps all process env; 'load' reads a config file into the process env; 'save' writes the process env to a file.",
            },
            "key": {"type": "string", "description": "Environment variable name (required for 'set' and 'delete'; optional for 'get', which returns all variables when omitted)."},
            "value": {"type": "string", "description": "Value to assign (required for action='set')."},
            "file_path": {"type": "string", "description": "Path to the config file (required for 'load' and 'save'; required for 'set'/'delete' when scope='file')."},
            "data": {"type": "object", "description": "Key-value pairs for batch operations (optional, reserved for bulk set/write flows)."},
            "format": {
                "type": "string",
                "enum": ["env", "json", "yaml"],
                "description": "Config file format for action='save' (optional, default 'env'). 'load' infers the format from the file extension.",
            },
            "scope": {
                "type": "string",
                "enum": ["process", "file"],
                "description": "Scope for 'set'/'delete' (optional, default 'process'). 'process' mutates the current process env; 'file' persists the change to the .env file at file_path.",
            },
        },
        "required": ["action"],
    },
    execute=_env_execute,
    intents=["general", "coding", "system"],
    category="system",
    semantic_type="read",
    cost_level="low",
    retryability="auto",
    is_concurrency_safe=lambda _: True,
    is_destructive=lambda args: args.get("action", "") in ("set", "delete", "save"),
)
