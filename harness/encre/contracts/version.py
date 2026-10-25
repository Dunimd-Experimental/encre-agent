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

"""Version constants for the harness contracts.

This module is intentionally dependency-free: every other module in
``encre.contracts`` and the event backbone may import it without creating a
cycle or paying for a subsystem import.

Versioning rules
================
* :data:`API_VERSION` covers the public surface exported by ``encre.__all__``.
  A breaking change (removal, rename, signature change) requires a major bump.
* :data:`EVENT_SCHEMA_VERSION` covers the serialised event envelope.  Adding an
  optional field is a minor bump; removing or re-typing a field is a major one.
* :data:`PLUGIN_API_VERSION` covers ``encre.pluginapi``.  Plugins declare the
  version they target; the host refuses to load an incompatible major.
* :data:`TOOL_SPEC_VERSION` covers :class:`encre.contracts.tools.ToolSpec`.
"""

from __future__ import annotations

#: Harness distribution version.  Kept in sync with ``harness/pyproject.toml``.
HARNESS_VERSION = "0.5.0"

#: Public API version for ``encre.__all__``.
API_VERSION = (1, 0)

#: Serialised event envelope schema version.
EVENT_SCHEMA_VERSION = 2

#: Frozen plugin API version (``encre.pluginapi``).
PLUGIN_API_VERSION = (1, 0)

#: Tool specification schema version.
TOOL_SPEC_VERSION = 1

__all__ = [
    "API_VERSION",
    "EVENT_SCHEMA_VERSION",
    "HARNESS_VERSION",
    "PLUGIN_API_VERSION",
    "TOOL_SPEC_VERSION",
]
