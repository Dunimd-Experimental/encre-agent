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

"""Prompt system package.

Exposes the prompt template base classes (:class:`EncreBasePrompt`,
:class:`EncrePromptTemplate`) and the :class:`PromptLoader` used to load
prompt text blocks at runtime.

The prompt subsystem assembles the layered system prompt that is sent to
the language model.  Callers pick a specialty template (general, coding,
research, data) and call ``build_system_prompt`` with the session context.
"""

from encre.prompts.base import EncreBasePrompt, EncrePromptTemplate
from encre.prompts.loader import PromptLoader

__all__ = [
    "EncreBasePrompt",
    "EncrePromptTemplate",
    "PromptLoader",
]
