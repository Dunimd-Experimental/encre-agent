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

"""GPU info provider — platform-specific GPU detection."""

import subprocess
import sys
from typing import Any

from encre.device.context.base import DeviceProvider



from encre.logging_config import get_logger
logger = get_logger(__name__)


class GPUInfoProvider(DeviceProvider):
    name = "gpu_info"
    sensitive = False

    def collect(self) -> dict[str, Any] | None:
        try:
            gpus = self._detect_gpus()
            return {"gpus": gpus} if gpus else None
        except Exception:
            return None

    def _detect_gpus(self) -> list[dict[str, Any]]:
        gpus: list[dict[str, Any]] = []

        # NVIDIA via nvidia-smi
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                 "--format=csv,noheader,nounits"],
                timeout=5, text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    gpus.append({
                        "name": parts[0],
                        "memory_mb": parts[1] if len(parts) > 1 else "",
                        "driver": parts[2] if len(parts) > 2 else "",
                        "vendor": "nvidia",
                    })
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

        if gpus:
            return gpus

        # Windows: WMI via pywin32
        if sys.platform == "win32":
            try:
                import win32com.client
                wmi = win32com.client.Dispatch("WbemScripting.SWbemLocator")
                svc = wmi.ConnectServer(".", "root\\cimv2")
                for gpu in svc.ExecQuery("SELECT Name,AdapterRAM,DriverVersion FROM Win32_VideoController"):
                    gpus.append({
                        "name": gpu.Name or "",
                        "memory_mb": round(int(gpu.AdapterRAM or 0) / (1024 ** 2)),
                        "driver": gpu.DriverVersion or "",
                        "vendor": "unknown",
                    })
            except Exception:
                logger.debug("exception suppressed", exc_info=True)

            return gpus

        # macOS: system_profiler
        if sys.platform == "darwin":
            try:
                out = subprocess.check_output(
                    ["system_profiler", "SPDisplaysDataType", "-detailLevel", "mini"],
                    timeout=10, text=True, stderr=subprocess.DEVNULL,
                )
                current: dict[str, Any] = {}
                for line in out.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("Chipset Model:"):
                        if current:
                            gpus.append(current)
                        current = {"name": stripped.split(":", 1)[1].strip(), "vendor": "apple"}
                    elif stripped.startswith("VRAM (") and current:
                        current["memory_mb"] = stripped.split(":", 1)[1].strip()
                    elif stripped.startswith("Vendor:") and "name" not in current:
                        current["vendor"] = stripped.split(":", 1)[1].strip().lower()
                if current:
                    gpus.append(current)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)

        return gpus
