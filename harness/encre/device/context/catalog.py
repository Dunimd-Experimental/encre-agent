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

"""Catalog builder for the L1 device context block.

Takes the collected provider data and produces a compact, human-readable
string that tells the model what device information and tools are
available.  This catalog is injected into the system prompt and is
always visible to the model (survives compaction).
"""

from typing import Any


_DEVICE_TOOL_MAP: dict[str, str] = {
    "platform_info": "device",
    "hardware_info": "device",
    "gpu_info": "device",
    "battery_info": "device",
    "display_info": "computer",
    "network_info": "device",
    "location_info": "device",
    "sensor_info": "device",
}


def build_catalog(provider_data: dict[str, dict[str, Any] | None]) -> str:
    lines: list[str] = []
    lines.append("## Device Context")

    platform_ = provider_data.get("platform_info")
    if platform_:
        os_info = platform_.get("os", {})
        lines.append(f"OS: {os_info.get('system', '')} {os_info.get('release', '')} ({platform_.get('arch', '')})")

    hardware_ = provider_data.get("hardware_info")
    if hardware_:
        disk = hardware_.get("disk", {})
        parts = disk.get("partitions", [])
        if parts:
            total = sum(p.get("total_gb", 0) for p in parts)
            lines.append(f"Disk: {total:.0f} GB total across {len(parts)} partition(s)")
        cwd = disk.get("current_working_directory", "")
        drive = disk.get("current_drive", "")
        if cwd:
            lines.append(f"Current directory: {cwd}")
        elif drive:
            lines.append(f"Current drive: {drive}")

    location_ = provider_data.get("location_info")
    if location_:
        tz = location_.get("timezone", "")
        offset = location_.get("utc_offset", "")
        gps = location_.get("gps") or {}
        tz_parts = [p for p in [tz, offset] if p]
        tz_suffix = f" ({', '.join(tz_parts)})" if tz_parts else ""
        if gps.get("latitude") is not None and gps.get("longitude") is not None:
            lat = gps["latitude"]
            lon = gps["longitude"]
            # Semantic framing is essential: a bare "Latitude: ..., Longitude: ..."
            # line reads as opaque numbers and models routinely ignore it --
            # users then hear "I don't know where you are" even though the
            # coordinates were right there in the prompt.  State explicitly
            # that this is the user's live physical location, and tell the
            # model HOW to turn raw coordinates into a usable place name.
            lines.append(
                "### User Location (authoritative)\n"
                f"The user's CURRENT PHYSICAL LOCATION, reported by this device's "
                f"OS location service: Latitude {lat:.4f}, Longitude {lon:.4f}"
                f"{tz_suffix}.\n"
                "This is ground truth about where the user is right now. When asked "
                '("where am I", "我的位置", nearby places, local weather/time), '
                "answer from these coordinates -- never claim you have no access to "
                "the user's location while this section exists. You are expected to "
                "recognise the approximate city/region yourself and say so directly "
                "(city-level certainty is enough); optionally refine to a more "
                f'precise place via web_search ("{lat:.4f} {lon:.4f}") when a finer '
                "answer is needed.\n"
                "IMPORTANT: never determine the user's location from IP addresses "
                'or network lookups (curl ipinfo, "my ip location" searches, '
                "etc.) -- proxy/VPN egress makes IP geolocation meaningless and it "
                "WILL contradict these coordinates. When sources conflict, these "
                "OS-provided coordinates always win."
            )
        elif tz_parts:
            lines.append(
                "Location (user's timezone): " + ", ".join(tz_parts) + " (GPS coordinates unavailable -- OS location permission denied. "
                "Use the timezone for locale/time questions; never guess the "
                "user's city from IP addresses -- proxy/VPN egress is unreliable.)"
            )

    have_tools: list[str] = []
    for provider_name, tool_name in _DEVICE_TOOL_MAP.items():
        data = provider_data.get(provider_name)
        if data is not None:
            label = tool_name
            if label not in have_tools:
                have_tools.append(label)
    if have_tools:
        lines.append("Device tools available: " + ", ".join(sorted(have_tools)))
        lines.append(
            "The above device context is already available in this prompt. Answer questions about device/location directly from this information. Call device_* tools only for real-time data or more detail beyond what is listed above."
        )

    return "\n".join(lines) if len(lines) > 1 else ""
