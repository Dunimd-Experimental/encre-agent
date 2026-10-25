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

"""Unified device tool for the Encre Agent system information subsystem.

This module consolidates device queries — info, battery, location, network,
sensor — into a single ``device`` tool that uses an ``action`` parameter to
select the operation.

Previously these were five separate EA packages (``ea-tool-device-info``,
``ea-tool-device-battery``, etc.). The merged tool ensures device queries
are atomic: install or uninstall the whole thing.
"""

import json
from typing import Any

from encre.tools.base import build_tool


# ── Action: info ─────────────────────────────────────────────────────────

async def _device_info(**kwargs: Any) -> str:
    """Return system hardware information — CPU, RAM, disk, GPU."""
    detail = kwargs.get("detail", False)

    from encre.device.context.providers.hardware import HardwareInfoProvider
    from encre.device.context.providers.gpu import GPUInfoProvider
    from encre.device.context.providers.platform_ import PlatformInfoProvider

    data: dict[str, Any] = {}

    platform_p = PlatformInfoProvider()
    hw = HardwareInfoProvider()
    gpu = GPUInfoProvider()

    data["platform"] = await platform_p.collect_async()
    data["hardware"] = await hw.collect_async()
    data["gpu"] = await gpu.collect_async()

    if not detail:
        hw_d = data.get("hardware") or {}
        cpu = hw_d.get("cpu", {})
        mem = hw_d.get("memory", {})
        disk = hw_d.get("disk", {})
        parts = disk.get("partitions", [])

        total = sum(p.get("total_gb", 0) for p in parts)
        cwd = disk.get("current_working_directory", "")

        return json.dumps({
            "cpu": f"{cpu.get('model', '')} ({cpu.get('logical_cores', '')} cores)",
            "ram": f"{mem.get('total_gb', '')} GB",
            "disk": f"{total:.0f} GB total across {len(parts)} partition(s)",
            "current_directory": cwd,
        }, ensure_ascii=False)

    return json.dumps(data, ensure_ascii=False, default=str)


# ── Action: battery ──────────────────────────────────────────────────────

async def _device_battery(**_kwargs: Any) -> str:
    """Return the current battery state of the host."""
    from encre.device.context.providers.battery import BatteryInfoProvider

    p = BatteryInfoProvider()
    data = await p.collect_async()

    if data is None:
        return "No battery detected on this device."

    return json.dumps(data, ensure_ascii=False, default=str)


# ── Action: location ─────────────────────────────────────────────────────

async def _device_location(**_kwargs: Any) -> str:
    """Return geographic location and timezone data."""
    from encre.device.context.providers.location import LocationInfoProvider

    p = LocationInfoProvider()
    data = await p.collect_async()

    if data is None:
        return "Location information unavailable on this device."

    return json.dumps(data, ensure_ascii=False, default=str)


# ── Action: network ──────────────────────────────────────────────────────

async def _device_network(**_kwargs: Any) -> str:
    """Return network interface and adapter info."""
    from encre.device.context.providers.network import NetworkInfoProvider

    p = NetworkInfoProvider()
    data = await p.collect_async()

    if data is None:
        return "Network information unavailable on this device."

    return json.dumps(data, ensure_ascii=False, default=str)


# ── Action: sensor ───────────────────────────────────────────────────────

async def _device_sensor(**_kwargs: Any) -> str:
    """Return live readings from hardware sensors."""
    from encre.device.context.providers.sensors import SensorInfoProvider

    p = SensorInfoProvider()
    data = await p.collect_async()

    if data is None:
        return "No sensor data available on this device."

    return json.dumps(data, ensure_ascii=False, default=str)


# ── Unified dispatcher ───────────────────────────────────────────────────

async def _device_execute(**kwargs: Any) -> str:
    """Dispatch the device action and return the result."""
    action = kwargs.get("action", "")
    if not action:
        return "Error: 'action' is required. Valid actions: info, battery, location, network, sensor."

    dispatch = {
        "info": _device_info,
        "battery": _device_battery,
        "location": _device_location,
        "network": _device_network,
        "sensor": _device_sensor,
    }

    handler = dispatch.get(action)
    if handler is None:
        return f"Error: unknown action '{action}'. Valid actions: {', '.join(dispatch.keys())}."

    return await handler(**kwargs)


# ── Tool definition ──────────────────────────────────────────────────────

device_tool = build_tool(
    name="device",
    description=(
        "Query device and system information through the unified device tool.\n\n"
        "Use the 'action' parameter to select the operation:\n"
        "  - info: return hardware info (CPU, RAM, disk, GPU) with optional detail mode\n"
        "  - battery: return battery state (charge %, AC status, time remaining)\n"
        "  - location: return geographic location and timezone\n"
        "  - network: return network interfaces (IPs, MACs, link state)\n"
        "  - sensor: return hardware sensor readings (accelerometer, gyroscope, etc.)\n\n"
        "WHEN to use: to answer 'what kind of machine am I on', to size workloads, "
        "to decide whether to defer work on battery, or to diagnose connectivity.\n"
        "WHEN NOT to use: for live CPU load or free RAM (use a monitoring tool), "
        "for precise tracking (accuracy depends on OS location service).\n"
        "TIPS: start with 'info' (detail=false) for a quick overview; use 'battery' "
        "to gate long-running tasks; 'location' always returns timezone even when GPS is denied.\n"
        "PITFALLS: GPU info may be empty when no driver is exposed; sensor data is "
        "unavailable on most desktop servers and VMs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["info", "battery", "location", "network", "sensor"],
                "description": "The device query to perform.",
            },
            # Info params
            "detail": {
                "type": "boolean",
                "description": "[info] When true, return the full hardware report including GPU name, per-partition disk stats, and CPU flags. When false (default), return a short summary.",
            },
        },
        "required": ["action"],
    },
    execute=_device_execute,
    intents=["general", "system"],
    category="system",
    semantic_type="read",
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)
