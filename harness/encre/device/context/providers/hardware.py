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

"""Hardware info provider — CPU, RAM, disk via psutil.

Disk reports ALL partitions, not just the current drive.
"""

import os
import platform
from typing import Any

import psutil

from encre.device.context.base import DeviceProvider


class HardwareInfoProvider(DeviceProvider):
    name = "hardware_info"
    sensitive = False

    def collect(self) -> dict[str, Any] | None:
        try:
            vm = psutil.virtual_memory()
            cpu_freq = psutil.cpu_freq()
            cwd = os.getcwd()

            partitions = []
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    partitions.append({
                        "device": part.device,
                        "mountpoint": part.mountpoint,
                        "fstype": part.fstype,
                        "total_gb": round(usage.total / (1024 ** 3), 2),
                        "used_gb": round(usage.used / (1024 ** 3), 2),
                        "free_gb": round(usage.free / (1024 ** 3), 2),
                        "percent_used": usage.percent,
                    })
                except Exception:
                    continue

            return {
                "cpu": {
                    "model": platform.processor() or "",
                    "physical_cores": psutil.cpu_count(logical=False),
                    "logical_cores": psutil.cpu_count(),
                    "frequency_mhz": cpu_freq.current if cpu_freq else None,
                    "architecture": platform.machine(),
                },
                "memory": {
                    "total_gb": round(vm.total / (1024 ** 3), 2),
                    "available_gb": round(vm.available / (1024 ** 3), 2),
                    "percent_used": vm.percent,
                },
                "disk": {
                    "partitions": partitions,
                    "current_working_directory": cwd,
                    "current_drive": os.path.splitdrive(cwd)[0] if cwd else "",
                },
                "boot_time": psutil.boot_time(),
            }
        except Exception:
            return None
