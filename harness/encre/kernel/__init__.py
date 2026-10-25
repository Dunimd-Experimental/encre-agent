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

"""Kernel-owned services and ports.

``encre.kernel`` is the seam between the turn engine and the rest of the
harness.  It owns process-wide shared state (``services``) and re-exports the
ports (``contracts.ports``) the kernel is allowed to depend on.  It must not
import the agent surface, feature packages, or any backend.
"""

from __future__ import annotations

from encre.kernel.services import (
    PROJECT_SKILLS_CANDIDATE_DIRS,
    PROJECT_SUB_AGENT_CANDIDATE_DIRS,
    ensure_plugins_discovered,
    install_shared_plugin_registry,
    reset_shared_services,
    shared_plugin_registry,
    shared_plugin_registry_raw,
    shared_skill_registry_raw,
)

__all__ = [
    "PROJECT_SKILLS_CANDIDATE_DIRS",
    "PROJECT_SUB_AGENT_CANDIDATE_DIRS",
    "ensure_plugins_discovered",
    "install_shared_plugin_registry",
    "reset_shared_services",
    "shared_plugin_registry",
    "shared_plugin_registry_raw",
    "shared_skill_registry_raw",
]
