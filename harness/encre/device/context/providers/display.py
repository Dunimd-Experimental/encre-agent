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

"""Display info provider — resolution, refresh rate, DPI.

Returns only the **active** display(s) (not all enumerated modes).
"""

import subprocess
import sys
from typing import Any

from encre.device.context.base import DeviceProvider


class DisplayInfoProvider(DeviceProvider):
    name = "display_info"
    sensitive = False

    def collect(self) -> dict[str, Any] | None:
        try:
            displays = self._detect_displays()
            return {"displays": displays} if displays else None
        except Exception:
            return None

    def _detect_displays(self) -> list[dict[str, Any]]:
        displays: list[dict[str, Any]] = []

        if sys.platform == "win32":
            displays = self._windows_displays()
        elif sys.platform == "darwin":
            displays = self._macos_displays()
        else:
            displays = self._linux_displays()

        return displays

    def _windows_displays(self) -> list[dict[str, Any]]:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            ENUM_CURRENT_SETTINGS = -1

            class DEVMODE(ctypes.Structure):
                _fields_ = [
                    ("dmDeviceName", ctypes.c_wchar * 32),
                    ("dmSpecVersion", ctypes.c_ushort),
                    ("dmDriverVersion", ctypes.c_ushort),
                    ("dmSize", ctypes.c_ushort),
                    ("dmDriverExtra", ctypes.c_ushort),
                    ("dmFields", ctypes.c_ulong),
                    ("dmOrientation", ctypes.c_short),
                    ("dmPaperSize", ctypes.c_short),
                    ("dmPaperLength", ctypes.c_short),
                    ("dmPaperWidth", ctypes.c_short),
                    ("dmScale", ctypes.c_short),
                    ("dmCopies", ctypes.c_short),
                    ("dmDefaultSource", ctypes.c_short),
                    ("dmPrintQuality", ctypes.c_short),
                    ("dmColor", ctypes.c_short),
                    ("dmDuplex", ctypes.c_short),
                    ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short),
                    ("dmCollate", ctypes.c_short),
                    ("dmFormName", ctypes.c_wchar * 32),
                    ("dmLogPixels", ctypes.c_ushort),
                    ("dmBitsPerPel", ctypes.c_ulong),
                    ("dmPelsWidth", ctypes.c_ulong),
                    ("dmPelsHeight", ctypes.c_ulong),
                    ("dmDisplayFlags", ctypes.c_ulong),
                    ("dmDisplayFrequency", ctypes.c_ulong),
                    ("dmICMMethod", ctypes.c_ulong),
                    ("dmICMIntent", ctypes.c_ulong),
                    ("dmICMediaType", ctypes.c_ulong),
                    ("dmDitherType", ctypes.c_ulong),
                    ("dmReserved1", ctypes.c_ulong),
                    ("dmReserved2", ctypes.c_ulong),
                    ("dmPanningWidth", ctypes.c_ulong),
                    ("dmPanningHeight", ctypes.c_ulong),
                ]

            class DISPLAY_DEVICE(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("DeviceName", ctypes.c_wchar * 32),
                    ("DeviceString", ctypes.c_wchar * 128),
                    ("StateFlags", wintypes.DWORD),
                    ("DeviceID", ctypes.c_wchar * 128),
                    ("DeviceKey", ctypes.c_wchar * 128),
                ]

            displays = []
            dev = DISPLAY_DEVICE()
            dev.cb = ctypes.sizeof(DISPLAY_DEVICE)
            i = 0
            while user32.EnumDisplayDevicesW(None, i, ctypes.byref(dev), 0):
                if dev.StateFlags & 1:
                    dm = DEVMODE()
                    dm.dmSize = ctypes.sizeof(DEVMODE)
                    if user32.EnumDisplaySettingsW(dev.DeviceName, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
                        hdc = user32.GetDC(None)
                        dpi = 96
                        if hdc:
                            dpi = gdi32.GetDeviceCaps(hdc, 88)
                            user32.ReleaseDC(None, hdc)
                        displays.append({
                            "name": dev.DeviceString,
                            "width": dm.dmPelsWidth,
                            "height": dm.dmPelsHeight,
                            "refresh_rate_hz": dm.dmDisplayFrequency,
                            "bits_per_pixel": dm.dmBitsPerPel,
                            "dpi": dpi,
                        })
                dev = DISPLAY_DEVICE()
                dev.cb = ctypes.sizeof(DISPLAY_DEVICE)
                i += 1

            return displays
        except Exception:
            return []

    def _macos_displays(self) -> list[dict[str, Any]]:
        try:
            import Quartz
            screens = Quartz.NSScreen.screens()
            displays = []
            for idx, screen in enumerate(screens):
                frame = screen.frame()
                backing = screen.backingScaleFactor() if hasattr(screen, "backingScaleFactor") else 1.0
                dpi = backing * 72.0
                displays.append({
                    "index": idx,
                    "width": int(frame.size.width),
                    "height": int(frame.size.height),
                    "dpi": dpi,
                })
            return displays
        except Exception:
            return []

    def _linux_displays(self) -> list[dict[str, Any]]:
        try:
            out = subprocess.check_output(
                ["xrandr", "--query"],
                timeout=5, text=True, stderr=subprocess.DEVNULL,
            )
            import re
            displays = []
            for line in out.splitlines():
                if " connected " in line:
                    m = re.search(r"(\d+)x(\d+).*?(\d+\.?\d*)\*", line)
                    if m:
                        displays.append({
                            "width": int(m.group(1)),
                            "height": int(m.group(2)),
                            "refresh_rate_hz": float(m.group(3)),
                        })
            return displays
        except Exception:
            return []
