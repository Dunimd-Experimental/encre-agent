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

"""Skill activation tool.

Lets the model activate a domain skill (``travel-flights``, ``pdf``,
``data-viz``, ...) by name when the user's request matches the skill's
purpose. The activated skill's guidance is injected into the next
turn's system prompt, the same channel used by auto-activated
document skills. This closes the loop: the model sees the skill
catalogue (auto-discovered), decides a skill fits, and activates it
itself -- without requiring the user to type ``/skill-name``.

Design notes
------------

Two kinds of skills are addressed by the same tool:

* **Domain skills** (``travel-flights``, ``pdf``, ``data-viz``,
  ...) add expertise for a specific request shape. Their guidance
  is routed to the *document* skill cache so it appears alongside
  auto-activated document skills.
* **Tool guides** (``tool-<name>``, e.g. ``tool-glob``) document
  the exact parameters, best practices, and pitfalls for a tool.
  Their guidance is routed to the *tool* skill cache so the model
  sees usage guidance alongside the tool schema.

The channel choice is made at activation time by inspecting the
canonical name: any name starting with ``tool-`` goes to the
tool-channel cache, everything else goes to the document cache.
This is what makes a single tool cover both skill families without
two separate tools in the model's tool list.

Aliasing: ``registry.lookup(name)`` returns the canonical skill
object whether the caller passed an alias or the exact name. The
tool resolves to the canonical name before activating and caching
so that aliases and exact names share one cache entry rather than
splitting the guidance across multiple keys.

The ``list`` action is a special-case dispatch: when ``name`` is
empty or ``"list"`` / ``"ls"``, the tool enumerates every
user-invocable skill registered on the loop and returns it as
plain text. This is needed because the initial skill catalogue
in the system prompt is capped to keep the prompt under budget,
so the model needs an on-demand enumeration path for the tail of
a large catalogue.
"""

import json
from typing import Any

from encre.tools.base import build_tool
from encre.tools.runtime import _resolve_loop


async def _skill_execute(**kwargs: Any) -> str:
    """Activate a domain skill by name.

    The skill's guidance body is cached on the loop so it persists
    into subsequent turns (same mechanism as auto-activated document
    skills). The returned JSON payload tells the model the
    activation succeeded, which channel the guidance was written
    to, and a preview of the first line so the model can confirm
    it activated the right skill without dumping the whole body
    into the transcript.

    Args:
        kwargs: Tool input keyed by the JSON schema declared on
            ``skill_tool.input_schema`` below. ``name`` is required;
            pass ``"list"`` to enumerate available skills instead of
            activating one.

    Returns:
        A JSON payload describing the activation result on success,
        or an ``"Error: ..."`` string when the loop, registry, or
        skill cannot be resolved.
    """
    name = (kwargs.get("name") or "").strip()
    if not name or name in ("list", "ls"):
        # Enumerate all user-invocable skills so the model can discover the
        # tail of a large catalogue that was not inlined into the system
        # prompt (catalogue is capped to keep the prompt under budget).
        # Tool usage guides (``tool-*``) live outside the main catalogue but
        # are exactly what the model needs before a first tool call, so they
        # are listed here under their own heading.
        registry = getattr(_resolve_loop(), "skill_registry", None)
        if registry is None:
            return "Error: no skill registry is available on this loop."
        all_names = sorted(
            s.name for s in registry.list_all() if s.user_invocable
        )
        if not all_names:
            return "No user-invocable skills registered."
        domain = [n for n in all_names if not n.startswith("tool-")]
        guides = [n for n in all_names if n.startswith("tool-")]
        lines = ["Available skills (invoke via `/name` or the `skill` tool):"]
        lines.extend(f"- {n}" for n in domain)
        if guides:
            lines.append("")
            lines.append(
                "Tool usage guides (activate with `skill(name=\"tool-...\")` "
                "before using the matching tool):"
            )
            lines.extend(f"- {n}" for n in guides)
        return "\n".join(lines)
    args = kwargs.get("args")
    if isinstance(args, str):
        args = args.strip() or None

    loop = _resolve_loop()
    if loop is None:
        return "Error: skill activation requires a parent loop reference."

    registry = getattr(loop, "skill_registry", None)
    if registry is None:
        return "Error: no skill registry is available on this loop."

    skill = registry.lookup(name)
    if skill is None:
        return (
            f"Error: skill '{name}' not found. "
            "Check the catalogue for the exact name (use a listed /name or alias)."
        )
    # Normalise to the canonical skill name so aliases and exact names share
    # one cache entry.
    canonical = skill.name

    try:
        body = await registry.activate(canonical, args)
    except Exception as exc:
        return f"Error activating skill '{canonical}': {exc}"
    if not body or body.startswith("Error: "):
        return f"Error: skill '{canonical}' could not be activated: {body}"

    # Cache on the loop so the guidance surfaces in the next step's system
    # prompt. Tool guides go to the tool-skill channel (rendered with the
    # other usage guides); domain skills go to the document channel.
    channel = ""
    cacher = getattr(loop, "_cache_activated_skill", None)
    if callable(cacher):
        try:
            channel = cacher(canonical, body) or ""
        except Exception:
            channel = ""
    else:
        # Older loops without the bridge: write straight to the doc cache.
        cache = getattr(loop, "_active_doc_skills", None)
        if isinstance(cache, dict):
            cache[canonical] = body
            channel = "doc"
    if not channel and canonical.startswith("tool-"):
        channel = "tool"

    preview = body.strip().splitlines()[0][:120] if body.strip() else ""
    payload = {
        "activated": canonical,
        "status": "active",
        "channel": channel or "doc",
        "guidance_injected": True,
        "preview": preview,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


skill_tool = build_tool(
    name="skill",
    description=(
        "Activate a skill by name so its detailed guidance is injected into the "
        "next step's system prompt. Two kinds exist: domain skills (e.g. "
        "travel-flights for flight search, pdf for PDF processing, data-viz for "
        "charting) that add expertise for a request, and tool usage guides "
        "named `tool-<tool>` (e.g. `tool-glob` for the glob tool) that document "
        "a tool's exact parameters, best practices and pitfalls. "
        "Load a tool guide before the first call to that tool whenever its "
        "arguments are not obvious from the schema; load a domain skill when "
        "the user's request matches its purpose. "
        "Tips: exact names are listed in the Tool Usage Guides and Skills "
        "sections of the system prompt; `name=\"list\"` enumerates everything; "
        "pass relevant request context via `args`. "
        "Pitfalls: an unknown name returns an error - verify the spelling "
        "against the catalogue; activating a guide already loaded is a no-op."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name: a domain skill (e.g. 'travel-flights', 'pdf', 'data-viz') or a tool guide ('tool-<tool>', e.g. 'tool-glob').",
            },
            "args": {
                "type": "string",
                "description": "Optional argument string forwarded to the skill (e.g. the user's request context or parameters).",
            },
        },
        "required": ["name"],
    },
    execute=_skill_execute,
    intents=["general", "coding", "research", "data", "communication"],
    category="meta",
    # Trigger keywords guide the model's prompt-side tool selection;
    # they are surfaced to the LLM alongside the description.
    triggers=["activate skill", "use skill", "skill"],
    # The skill tool must be available whenever the agent is running
    # because it is the primary path for the model to discover and
    # load additional guidance.
    always_available=True,
    # Activation is idempotent and only touches in-memory caches on
    # the loop, so concurrent calls are safe to interleave.
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)
