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

"""Programmatically-registered bundled skills.

Most built-in skills are now static ``SKILL.md`` files under
``encre/skills/builtin/`` (loaded by :func:`encre.skills.builtin.builtin_skills_dir`
via :meth:`EncreSkillRegistry.load_from_dir`).  Only skills that need
runtime logic — e.g. argument parsing or conditional prompt rendering —
stay here as code.

Currently that is just ``loop``, which parses the ``[interval] <prompt>``
syntax before rendering its prompt.
"""

from encre.skills.types import BundledSkillDefinition, SkillContext, SkillSource


def create_bundled_skills(registry):
    """Instantiate every programmatically-registered skill and register it.

    Each skill in this package requires custom argument-parsing or prompt
    rendering logic that cannot be expressed as a static ``SKILL.md``
    template.  New code-driven skills should be added here as additional
    :class:`BundledSkillDefinition` instances, then registered on the
    supplied *registry*.

    Args:
        registry: The :class:`EncreSkillRegistry` to register skills onto.
    """
    # Lazy import to avoid a circular dependency: this module is imported
    # during registry initialisation, and the loop module in turn imports
    # from the registry types package.
    from encre.skills.bundled.loop import _loop_prompt

    loop_skill = BundledSkillDefinition(
        name="loop",
        description="Execute a command repeatedly on a schedule using [interval] <prompt> syntax",
        # The loop prompt is rendered at call time by _loop_prompt, which
        # parses the [interval] prefix and substitutes it into the template.
        get_prompt_for_command=_loop_prompt,
        # Aliases give users multiple familiar ways to trigger the same skill.
        aliases=["repeat", "schedule", "watch"],
        # No file-extension triggers -- the loop skill is always user-invoked.
        when_to_use="",
        argument_hint="[seconds] <task description>",
        disable_model_invocation=False,
        user_invocable=True,
        context=SkillContext.INLINE,
        source=SkillSource.BUNDLED,
        # Hidden from the user-facing skill palette because it is a power-user
        # feature; the agent discovers it through the slash-command layer.
        hidden=True,
    )

    registry.register(loop_skill)
