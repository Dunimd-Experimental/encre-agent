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

"""Submit Review Tool — plan/spec artifact review builtin.

The model writes its plan/spec artifacts to disk (paths of its own
choosing) and then calls this tool to present them for user review.
The real work (card emission + blocking wait for the decision) happens
in the loop's tools phase special-case; this executor is only reached
if the special-case did not run, and simply reports that.
"""
from __future__ import annotations

from typing import Any

from encre.tools.base import build_tool
from encre.tools.tool_ui import ui_dict


async def _submit_review_execute(**kwargs: Any) -> str:  # noqa: ARG001
    return "Error: submit_review must be executed inside an agent turn."


submit_review_tool = build_tool(
    name="submit_review",
    description=(
        "Submit the plan or specification files you have written for user "
        "review. Call this AFTER writing the artifact files, passing the "
        "exact paths you chose (any names/locations). The user sees a "
        "review card listing the files and the summary; this tool blocks "
        "until they approve or reject. On approval the review becomes the "
        "session's standing goal: the mode stays on and the work continues "
        "until every checklist item is done, so the artifact MUST carry a "
        "'## Checklist' section with one '- [ ]' line per deliverable, and "
        "the same items must already be in the todo tool. On rejection the "
        "user has NOT told you what is wrong: ask them (the question tool) "
        "what the plan must change to match their requirements, revise, and "
        "submit again -- never silently rewrite the files and resubmit. Only "
        "available in plan or spec mode. "
        "AVOID: pasting the plan/spec content into the chat reply -- the "
        "card already shows the files."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "Artifact files under review, in reading order (required).",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path (relative to the workspace or absolute). "
                                           "The review card titles the entry with this file's "
                                           "name, so no separate label is needed.",
                        },
                    },
                    "required": ["path"],
                },
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph summary of what is being proposed.",
            },
        },
        "required": ["files"],
    },
    execute=_submit_review_execute,
    # ``hidden`` -- the review card the loop projects from this call IS the
    # presentation; a tool strip next to it only duplicates it, and once the
    # turn is torn down that strip shows the internal cancellation tombstone
    # as a red error line.
    ui=ui_dict(icon="clipboard-check", label="Submit Review", result="text", hidden=True).as_dict(),
    intents=["general", "coding", "data"],
    category="workflow",
    triggers=["submit review", "review plan", "review spec", "提交审查"],
    semantic_type="control",
    cost_level="low",
    retryability="guarded",
    safe_fallback="Write the artifact files first, then retry with their real paths.",
    is_destructive=False,
)
