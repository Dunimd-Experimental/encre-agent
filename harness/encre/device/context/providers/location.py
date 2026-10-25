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

"""Location info provider — timezone + native OS geolocation.

Uses the operating system's built-in location service (NOT IP geolocation,
which resolves to the ISP egress node and is unreliable):
  * Windows  -> Windows.Devices.Geolocation (WinRT, true async)
  * macOS    -> CoreLocation (CLLocationManager)
  * Linux    -> GeoClue2 (org.freedesktop.GeoClue2 over D-Bus)

Timezone/UTC-offset come from the local system clock.  The native location
API provides real GPS/Wi-Fi/cell-tower derived coordinates.

``collect_async`` is the primary entry point and is fully non-blocking on
the event loop: the Windows path ``await``\\s the WinRT API directly, while
the macOS/Linux paths are offloaded to a thread-pool executor.  The
synchronous :meth:`collect` wraps the same logic for callers that cannot
``await``.
"""

import asyncio
import sys
import time
from typing import Any

from encre.device.context.base import DeviceProvider


class LocationInfoProvider(DeviceProvider):
    name = "location_info"
    sensitive = True

    def collect(self) -> dict[str, Any] | None:
        """Synchronous wrapper — runs the async collection on a fresh loop."""
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self.collect_async())
            finally:
                loop.close()
        except Exception:
            return None

    async def collect_async(self) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = {
                "timezone": self._get_timezone(),
                "utc_offset": self._get_utc_offset(),
            }

            gps = await self._get_gps()
            if gps:
                result["gps"] = gps

            return result
        except Exception:
            return None

    def _get_timezone(self) -> str:
        try:
            import zoneinfo

            return str(zoneinfo.ZoneInfo(time.tzname[0] if time.tzname else "UTC"))
        except Exception:
            return time.tzname[0] if time.tzname else "UTC"

    def _get_utc_offset(self) -> str:
        offset = -time.timezone
        hours = offset // 3600
        minutes = (offset % 3600) // 60
        sign = "+" if hours >= 0 else "-"
        return f"UTC{sign}{abs(hours):02d}:{minutes:02d}"

    async def _get_gps(self) -> dict[str, Any] | None:
        """Fetch coordinates from the OS-native location service (non-blocking)."""
        if sys.platform == "win32":
            return await self._get_gps_windows()
        if sys.platform == "darwin":
            return await asyncio.to_thread(self._get_gps_macos)
        if sys.platform.startswith("linux"):
            return await asyncio.to_thread(self._get_gps_linux)
        return None

    # -- Windows: Windows.Devices.Geolocation ---------------------------
    async def _get_gps_windows(self) -> dict[str, Any] | None:
        try:
            import winrt.windows.devices.geolocation as wdg

            loc = await wdg.Geolocator().get_geoposition_async()
            coord = loc.coordinate
            p = coord.point.position
            return {
                "latitude": p.latitude,
                "longitude": p.longitude,
                "altitude": p.altitude,
                "accuracy": coord.accuracy,
                "source": "Windows.Geolocation",
            }
        except Exception:
            return None

    # -- macOS: CoreLocation (CLLocationManager) ------------------------
    def _get_gps_macos(self) -> dict[str, Any] | None:
        try:
            import objc
            from Cocoa import NSRunLoop, NSDate
            import CoreLocation

            manager = CoreLocation.CLLocationManager.alloc().init()
            manager.requestWhenInUseAuthorization()
            # Spin the run loop briefly to allow a fix.
            deadline = NSDate.dateWithTimeIntervalSinceNow_(4.0)
            while NSDate.date().timeIntervalSinceDate(deadline) < 0:
                NSRunLoop.currentRunLoop().runMode_beforeDate_(
                    "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.1)
                )
                loc = manager.location
                if loc is not None:
                    coord = loc.coordinate
                    if coord.latitude != 0.0 or coord.longitude != 0.0:
                        return {
                            "latitude": coord.latitude,
                            "longitude": coord.longitude,
                            "altitude": loc.altitude,
                            "accuracy": loc.horizontalAccuracy,
                            "source": "CoreLocation",
                        }
            loc = manager.location
            if loc is not None:
                coord = loc.coordinate
                if coord.latitude != 0.0 or coord.longitude != 0.0:
                    return {
                        "latitude": coord.latitude,
                        "longitude": coord.longitude,
                        "altitude": loc.altitude,
                        "accuracy": loc.horizontalAccuracy,
                        "source": "CoreLocation",
                    }
            return None
        except Exception:
            return None

    # -- Linux: GeoClue2 (D-Bus) ----------------------------------------
    def _get_gps_linux(self) -> dict[str, Any] | None:
        try:
            import dbus  # python-dbus
            from dbus.mainloop.glib import DBusGMainLoop
            import gi  # PyGObject
            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
            obj = bus.get_object("org.freedesktop.GeoClue2", "/org/freedesktop/GeoClue2/Client")
            client = dbus.Interface(obj, "org.freedesktop.GeoClue2.Client")
            client.set_desktop_id("encre")
            client.Start()
            location_path = client.get_location()
            loc_obj = bus.get_object("org.freedesktop.GeoClue2", location_path)
            loc = dbus.Interface(loc_obj, "org.freedesktop.GeoClue2.Location")
            lat = float(loc.get_latitude())
            lon = float(loc.get_longitude())
            if lat != 0.0 or lon != 0.0:
                return {
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": float(loc.get_altitude()),
                    "accuracy": float(loc.get_accuracy()),
                    "source": "GeoClue2",
                }
            return None
        except Exception:
            return None
