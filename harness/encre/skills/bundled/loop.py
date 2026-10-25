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

"""Loop-execution skill prompt loader.

Parses a ``[interval] <prompt>`` command and loads the ``loop`` prompt from
the ``skills`` category, substituting the prompt text and resolved interval.

The regex pattern captures two groups: the numeric interval (in seconds)
and the free-text prompt that follows the closing bracket.  When the input
does not match the expected syntax, a usage-guidance prompt is returned
instead so the user can correct the invocation.
"""

import re
from typing import Any

from encre.prompts.loader import PromptLoader

_loader = PromptLoader()
# Matches the "[interval] <prompt>" syntax where interval is a number of seconds.
# Anchored at start-of-string (^) and supports fractional intervals (e.g. "1.5").
_LOOP_PATTERN = re.compile(r"^\s*\[(\d+(?:\.\d+)?)\]\s*(.+)", re.DOTALL)


async def _loop_prompt(args: str | None, _ctx: dict[str, Any]) -> str:
    """Render the loop-execution skill prompt.

    Parses the ``[interval] <prompt>`` syntax from *args*.  When the syntax is
    missing or malformed, returns usage guidance instead of a loop prompt.
    This fallback path is important because the loop skill is hidden from the
    user-facing palette, so the agent discovers it through trial and error;
    returning actionable guidance (rather than an empty string) helps the
    agent recover and retry with correct syntax.

    Args:
        args: The raw user input after the skill name, e.g.
            ``"10 Run the build and report any errors"``.
        _ctx: Additional context dict (unused for this skill).

    Returns:
        The fully rendered loop prompt with interval and task substituted,
        or a usage-guidance string when the input is malformed.
    """
    if args is None:
        # Normalise to empty string so the regex match can proceed uniformly.
        args = ""
    match = _LOOP_PATTERN.match(args)
    if match is None:
        # Return a detailed usage message that teaches the agent the correct
        # syntax, including interval recommendations and how to stop.
        return (f"You are in a loop execution mode. The command syntax is: [interval] <prompt>\n\n"
                f"The interval specifies how frequently (in seconds) to execute the prompt. "
                f"Minimum interval is 1 second.\n\n"
                f"However, the input provided did not match the expected format. Received:\n"
                f'  "{args}"\n\n'
                f"Please ask the user to specify the command in the format: [interval] <prompt>\n"
                f"Example: [10] Run the build and report any errors\n\n"
                f"Also provide the following guidance to the user:\n"
                f"- Use shorter intervals (1-5s) for rapid feedback loops like file watching\n"
                f"- Use medium intervals (10-30s) for build/test cycles\n"
                f"- Use longer intervals (60-300s) for monitoring tasks\n"
                f"- Add [stop] or press Ctrl+C to terminate the loop\n")

    interval_str = match.group(1).strip()
    prompt_text = match.group(2).strip()

    try:
        interval_seconds = float(interval_str)
    except ValueError:
        # Defensive fallback: if the captured "number" fails to parse (should
        # not happen given the regex), default to a safe 5-second interval.
        interval_seconds = 5.0

    # Enforce a minimum interval to prevent rapid-fire loops that would
    # overwhelm the agent with context and exhaust API rate limits.
    if interval_seconds < 1.0:
        interval_seconds = 1.0

    return _loader.load_with_context(
        "loop", category="skills",
        prompt_text=prompt_text,
        interval_seconds=str(interval_seconds),
    )
