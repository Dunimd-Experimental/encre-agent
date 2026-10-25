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

"""Sensor info provider — accelerometer, gyroscope, magnetometer, ambient light.

``collect_async`` is the primary entry point and is non-blocking on the
event loop: the Windows path uses WinRT directly with ``await``, while
other platforms return ``None`` (no sensor API available).
"""

import asyncio
import sys
from typing import Any

from encre.device.context.base import DeviceProvider



from encre.logging_config import get_logger
logger = get_logger(__name__)


class SensorInfoProvider(DeviceProvider):
    name = "sensor_info"
    sensitive = True

    def collect(self) -> dict[str, Any] | None:
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
            sensors = await self._get_sensors()
            return sensors if sensors else None
        except Exception:
            return None

    async def _get_sensors(self) -> dict[str, Any] | None:
        if sys.platform == "win32":
            return await self._get_windows_sensors()
        return None

    async def _get_windows_sensors(self) -> dict[str, Any] | None:
        import winrt.windows.devices.sensors as wds

        result: dict[str, Any] = {}

        try:
            s = wds.Accelerometer.get_default()
            if s:
                r = s.get_current_reading()
                result["accelerometer"] = {
                    "x": r.acceleration_x,
                    "y": r.acceleration_y,
                    "z": r.acceleration_z,
                }
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

        try:
            s = wds.Gyrometer.get_default()
            if s:
                r = s.get_current_reading()
                result["gyroscope"] = {
                    "x": r.angular_velocity_x,
                    "y": r.angular_velocity_y,
                    "z": r.angular_velocity_z,
                }
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

        try:
            s = wds.Magnetometer.get_default()
            if s:
                r = s.get_current_reading()
                result["magnetometer"] = {
                    "x": r.magnetic_field_x,
                    "y": r.magnetic_field_y,
                    "z": r.magnetic_field_z,
                }
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

        try:
            s = wds.LightSensor.get_default()
            if s:
                r = s.get_current_reading()
                result["ambient_light"] = {"illuminance_lux": r.illuminance_in_lux}
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

        return result if result else None
