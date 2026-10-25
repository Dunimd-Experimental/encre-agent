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

"""Question tool -- ask the user for clarification or additional information.

Use this when the user's request is ambiguous, incomplete, or when you
need more context to proceed confidently. Instead of guessing, ask
directly.

Returns structured JSON so the frontend can render an interactive
Question card with the question text, optional context, and optional
choice buttons. The envelope is deliberately tiny -- ``{"_type":
"question", "questions": [...]}`` -- because the frontend matches on
``_type`` to decide how to render the tool result, and no other
metadata is needed for the card to appear.

Design notes
------------

The tool accepts either a single ``question`` (plus optional
``details`` / ``options``) or a ``questions`` array of the same
shape. Batching via ``questions`` is preferred for multi-step
clarification because it keeps the user's attention in one round
rather than a chain of prompts.

The function is ``async`` even though the body is pure CPU work
because ``build_tool`` expects an async callable so the dispatcher
can ``await`` uniformly across all tools. The synchronous nature
also makes the tool trivially ``is_concurrency_safe=True`` and
``is_readonly=True`` -- there is no state to corrupt and no side
effect to serialise.

Every field is coerced with ``str(...)`` and ``.strip()`` so a
caller-supplied non-string (e.g. a numeric option) never raises and
trailing whitespace never leaks into the rendered card.
"""

import json
from typing import Any

from encre.tools.base import build_tool


async def _question_execute(**kwargs: Any) -> str:
    """Ask the user a question (single or multiple) and return a JSON envelope for the frontend.

    The function collects one or more question items into a list and
    emits a single JSON envelope. When neither ``question`` nor
    ``questions`` is supplied, the tool returns an error string
    rather than raising -- the caller (the model) can then adapt its
    output rather than crashing an otherwise successful turn.

    Args:
        kwargs: Tool input keyed by the JSON schema declared on
            ``question_tool.input_schema`` below. Exactly one of
            ``question`` (single) or ``questions`` (list) should be
            supplied; when both are present ``questions`` wins.

    Returns:
        A JSON string of the form ``{"_type": "question",
        "questions": [...]}`` on success, or an ``"Error: ..."``
        string when no valid question is supplied.
    """
    questions_raw = kwargs.get("questions")
    single_question = (kwargs.get("question") or "").strip()

    # Build a list of question items (either from `questions` or single `question`).
    # The two input shapes are normalised to the same list-of-dicts so the
    # downstream envelope does not need to branch on which one the caller
    # used.
    items: list[dict[str, Any]] = []

    if questions_raw and isinstance(questions_raw, list):
        for q in questions_raw:
            if isinstance(q, dict):
                text = (q.get("question") or "").strip()
                if text:
                    item: dict[str, Any] = {"question": text}
                    if q.get("details"):
                        item["details"] = str(q["details"]).strip()
                    if q.get("options") and isinstance(q["options"], list):
                        item["options"] = [str(o) for o in q["options"]]
                    items.append(item)
    elif single_question:
        item = {"question": single_question}
        if kwargs.get("details"):
            item["details"] = str(kwargs["details"]).strip()
        if kwargs.get("options") and isinstance(kwargs["options"], list):
            item["options"] = [str(o) for o in kwargs["options"]]
        items.append(item)

    if not items:
        return 'Error: Provide at least one question via "question" or "questions".'

    return json.dumps({"_type": "question", "questions": items}, ensure_ascii=False)


question_tool = build_tool(
    name="question",
    description=(
        "Ask the user for clarification, a decision, or missing information; "
        "the conversation pauses until the user answers.\n\n"
        "WHEN to use: the user's request is ambiguous, incomplete, or has "
        "multiple valid interpretations; a destructive action needs "
        "confirmation; required input (credentials, file path, preference) is "
        "missing.\n"
        "WHEN NOT to use: when you can reasonably infer the answer from "
        "context; for open-ended exploration where guessing and iterating is "
        "cheaper than a round-trip to the user.\n"
        "TIPS: provide options whenever the choice set is small and known -- "
        "the frontend renders them as clickable buttons; batch related "
        "questions via the 'questions' array so the user answers all at once; "
        "include a 'details' field explaining WHY you're asking.\n"
        "PITFALLS: asking too many low-value questions frustrates users -- "
        "only ask when truly blocked."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "A single question to ask the user. Use 'questions' instead when you have more than one to ask at once.",
            },
            "details": {
                "type": "string",
                "description": "Optional context explaining why the question is being asked. Shown alongside the question to help the user decide.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional predefined choices rendered as clickable buttons. Prefer this over free-text input when the valid answers are a small known set.",
            },
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question text to display to the user."},
                        "details": {"type": "string", "description": "Optional context shown with this question."},
                        "options": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Optional predefined choices for this question.",
                        },
                    },
                    "required": ["question"],
                },
                "description": "Multiple questions sent together; the user must answer all of them before the model continues. Prefer this over repeated single-question calls.",
            },
        },
    },
    execute=_question_execute,
    intents=["general", "coding", "research", "data", "system"],
    category="communication",
    # Trigger keywords guide the model's prompt-side tool selection;
    # they are surfaced to the LLM alongside the description.
    triggers=["ask user", "question", "clarify", "confirm", "ambiguity"],
    # `always_available` overrides the tool tier check: the frontend
    # surfaces the question tool even when other tools are disabled,
    # because an agent that cannot ask a clarifying question is
    # effectively stalled.
    always_available=True,
    # The tool only formats a JSON envelope, so it never mutates state
    # and is trivially safe to interleave.
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)


__all__ = ["question_tool"]
