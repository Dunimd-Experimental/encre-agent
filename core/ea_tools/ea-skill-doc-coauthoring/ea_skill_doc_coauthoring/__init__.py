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

"""Package entry point for the ea-skill-doc-coauthoring plugin.

This module acts as the public surface of the plugin package. The host
(Encre Agent) imports :func:`create_plugin` from this package rather
than reaching into the internal ``plugin`` submodule, which lets the
implementation swap out freely without breaking the discovery contract.

Re-exporting here (instead of defining the factory twice) keeps a single
source of truth for the manifest metadata declared in ``plugin.py``.
"""

# Pull the factory out of the internal module so consumers can do
# ``from ea_skill_doc_coauthoring import create_plugin`` directly.
from ea_skill_doc_coauthoring.plugin import create_plugin

# Explicit export list: lets ``from ... import *`` stay predictable and
# documents the package's intended public API to static analysers.
__all__ = ["create_plugin"]
