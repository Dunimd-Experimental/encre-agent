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

"""Docker CLI tool for Encre Agent.

This module implements the ``docker`` tool, which wraps the Docker CLI
to allow the agent to build, run, inspect, and tear down containers
and images, including compose orchestration.

The tool constructs a ``docker <command> [options] [target]`` command
line from user-provided parameters, executes it as a subprocess, and
returns the combined stdout/stderr output. A 300-second timeout
protects against hanging builds or pulls.

Supported subcommands include ``ps``, ``run``, ``stop``, ``logs``,
``build``, ``pull``, ``push``, ``rm``, ``rmi``, and ``compose``.
"""

import asyncio
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes


async def _docker_execute(**kwargs: Any) -> str:
    """Execute a Docker CLI command and return the output.

    This is the internal executor function bound to the ``docker``
    tool's ``execute`` field. It is invoked by the Encre Agent tool
    dispatcher whenever the model calls this tool.

    The executor builds a command array in the following order:
    ``["docker", <command>, <options...>, <image_or_container>]``.
    Options are split on whitespace and appended verbatim, which
    preserves flag grouping as intended by the caller.

    The subprocess is launched with ``hidden_subprocess_kwargs`` to
    prevent it from appearing in process managers or task lists on
    Windows, keeping the agent's background processes invisible to the
    user.

    Args:
        **kwargs: Arbitrary keyword arguments passed through by the
            tool dispatcher. Recognised keys:

            * ``command`` (str): The Docker subcommand to execute
              (e.g. ``"ps"``, ``"build"``). Defaults to ``"ps"``.
            * ``image_or_container`` (str): The target image name or
              container ID/name.
            * ``options`` (str): Whitespace-separated CLI flags to
              append after the subcommand.

    Returns:
        The combined stdout and stderr output of the Docker command,
        followed by the exit code if non-zero, or an error message if
        Docker is not installed or the command times out.
    """
    command = kwargs.get("command", "ps")
    image_or_container = kwargs.get("image_or_container", "")
    options = kwargs.get("options", "")

    cmd_parts = ["docker", command]

    if options:
        cmd_parts.extend(options.split())

    if image_or_container:
        cmd_parts.append(image_or_container)

    try:
        # Lazy import: the process capability module may not be
        # available at module load time in all deployment contexts.
        from encre.capabilities.process import (
            hidden_subprocess_kwargs as _hidden,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hidden(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300
            )
        except TimeoutError:
            # Kill the subprocess immediately on timeout to prevent
            # it from continuing to consume resources.
            proc.kill()
            await proc.wait()
            return "Error: Docker command timed out after 300 seconds"
        output = decode_bytes(stdout) if stdout else ""
        if stderr:
            err_text = decode_bytes(stderr)
            if err_text:
                output += "\n" + err_text
        if proc.returncode and proc.returncode != 0:
            output += f"\nDocker command exited with code {proc.returncode}"
        return output or "(no output)"
    except FileNotFoundError:
        return "Error: Docker CLI not found. Is Docker installed and in PATH?"
    except Exception as e:
        return f"Error executing docker command: {e}"


docker_tool = build_tool(
    name="docker",
    description=(
        "Run Docker CLI commands to build, run, inspect, or tear down containers and "
        "images, including compose orchestration. "
        "Use this for local container workflows such as `ps`, `logs`, `run`, `build`, "
        "`pull`, `push`, `stop`, `rm`, `rmi`, or `compose` invocations. "
        "Do NOT use this for Kubernetes orchestration, remote registry browsing, or "
        "long-running service logs - prefer a dedicated k8s/SSH tool. "
        "Tips: pass flags verbatim through `options` (e.g. \"-d -p 8080:80\"); combine "
        "with `image_or_container` to target the named image or container. "
        "Pitfalls: the command runs with a 300s timeout so heavy builds/pulls may be "
        "killed; destructive ops (rm, rmi, stop, compose down) are flagged destructive."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["ps", "run", "stop", "logs", "build", "pull", "push", "rm", "rmi", "compose"],
                "description": "The Docker subcommand to execute (e.g. \"ps\" to list containers, \"build\" to build an image, \"compose\" to drive a compose project).",
            },
            "image_or_container": {
                "type": "string",
                "description": "Name or ID of the target image (for run/build/pull/push/rmi) or container (for stop/logs/rm).",
            },
            "options": {
                "type": "string",
                "description": "Whitespace-separated CLI flags appended to the subcommand (e.g. \"-d --restart unless-stopped -p 8080:80\").",
            },
        },
        "required": ["command"],
    },
    execute=_docker_execute,
    intents=["coding", "system"],
    is_concurrency_safe=lambda data: data.get("command") in ("ps", "logs"),
    category="infra",
    semantic_type="exec",
    is_destructive=lambda args: args.get("action", "") in ("rm", "rmi", "stop", "compose", "prune", "system"),
)
