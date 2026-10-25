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

"""Package entry point for the ``ea-skill-find-skills`` skill.

Re-exports :func:`create_plugin` from the :mod:`plugin` submodule so that the
Encre Agent plugin loader can import the factory via the short path
``from ea_skill_find_skills import create_plugin``. The ``__all__`` variable
explicitly declares the public API surface, ensuring that wildcard imports
(``from ea_skill_find_skills import *``) only expose the intended symbols and
nothing else.
"""

from ea_skill_find_skills.plugin import create_plugin

__all__ = ["create_plugin"]
