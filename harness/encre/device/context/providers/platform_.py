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

"""Platform info provider — cross-platform OS/arch/hostname/Python."""

import platform
import socket
import sys
from typing import Any

from encre.device.context.base import DeviceProvider


class PlatformInfoProvider(DeviceProvider):
    name = "platform_info"
    sensitive = False

    def collect(self) -> dict[str, Any] | None:
        try:
            uname = platform.uname()
            return {
                "os": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "version": platform.version(),
                    "node": socket.gethostname(),
                },
                "arch": platform.machine(),
                "python": {
                    "version": sys.version.split()[0],
                    "implementation": platform.python_implementation(),
                },
                "platform": sys.platform,
            }
        except Exception:
            return None
