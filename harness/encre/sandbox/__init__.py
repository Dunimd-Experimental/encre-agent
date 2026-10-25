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

"""Encre Sandbox Isolation System.

Three-layer sandbox architecture:

1. **Path isolation** (``_sandbox.py``)
   Remaps file paths into a per-session sandbox directory.
   Prevents path traversal attacks and symlink escapes.

2. **Container sandbox** (``container.py``)
   Docker-based isolation with seccomp, capability drop,
   read-only rootfs, and resource limits.  Supports both
   ephemeral (one-shot ``docker run --rm``) and persistent
   (session container) modes.

3. **Permission system** (``safety.py``)
   Static analysis + ML classification of commands before
   execution.  Routes dangerous commands through the
   container sandbox when enabled.

Usage::

    from encre.sandbox.types import SandboxConfig, SandboxMode, NetworkPolicy
    from encre.sandbox.container import EncreContainerSandbox

    # Ephemeral sandbox
    with EncreContainerSandbox("/path/to/workspace") as sb:
        result = sb.execute("ls -la")
        print(result.stdout)

    # With custom config
    config = SandboxConfig(
        mode=SandboxMode.CONTAINER,
        image="python:3.11-slim",
        timeout=60,
    )
    sb = EncreContainerSandbox("/path/to/workspace", config)
    sb.run_container()
    sb.exec_in_container("npm test")
    sb.stop_container()
"""

from encre.sandbox.container import EncreContainerSandbox as EncreContainerSandbox
from encre.sandbox.types import (
    CGroupLimit as CGroupLimit,
)
from encre.sandbox.types import (
    EnvConfig as EnvConfig,
)
from encre.sandbox.types import (
    FileProtection as FileProtection,
)
from encre.sandbox.types import (
    FileProtectionConfig as FileProtectionConfig,
)
from encre.sandbox.types import (
    NetworkConfig as NetworkConfig,
)
from encre.sandbox.types import (
    NetworkPolicy as NetworkPolicy,
)
from encre.sandbox.types import (
    ResourceConfig as ResourceConfig,
)
from encre.sandbox.types import (
    SandboxConfig as SandboxConfig,
)
from encre.sandbox.types import (
    SandboxMode as SandboxMode,
)
from encre.sandbox.types import (
    SandboxResult as SandboxResult,
)
from encre.sandbox.types import (
    SeccompConfig as SeccompConfig,
)
from encre.sandbox.types import (
    SeccompProfile as SeccompProfile,
)

__all__ = [
    "CGroupLimit",
    "EncreContainerSandbox",
    "EnvConfig",
    "FileProtection",
    "FileProtectionConfig",
    "NetworkConfig",
    "NetworkPolicy",
    "ResourceConfig",
    "SandboxConfig",
    "SandboxMode",
    "SandboxResult",
    "SeccompConfig",
    "SeccompProfile",
]
