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

"""Network info provider — interfaces, IP, MAC via psutil."""

import socket
from typing import Any

import psutil

from encre.device.context.base import DeviceProvider


class NetworkInfoProvider(DeviceProvider):
    name = "network_info"
    sensitive = True

    def collect(self) -> dict[str, Any] | None:
        try:
            ifaces = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            io = psutil.net_io_counters()
            hostname = socket.gethostname()

            interfaces = []
            for name, addrs in ifaces.items():
                info: dict[str, Any] = {"name": name, "addrs": []}
                s = stats.get(name)
                if s:
                    info["is_up"] = s.isup
                    info["speed_mbps"] = s.speed
                    info["duplex"] = str(s.duplex)
                for addr in addrs:
                    info["addrs"].append({
                        "family": str(addr.family),
                        "address": addr.address,
                        "netmask": addr.netmask,
                        "broadcast": addr.broadcast,
                    })
                interfaces.append(info)

            return {
                "hostname": hostname,
                "interfaces": interfaces,
                "io": {
                    "bytes_sent": io.bytes_sent,
                    "bytes_recv": io.bytes_recv,
                    "packets_sent": io.packets_sent,
                    "packets_recv": io.packets_recv,
                },
            }
        except Exception:
            return None
