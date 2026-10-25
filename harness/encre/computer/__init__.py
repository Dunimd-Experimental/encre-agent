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

"""Public API for the ``computer`` package.

This package provides Encre's computer-automation surface: browser
automation (via the Chrome DevTools Protocol), a unified
"computer-use" action dispatcher that routes to either a browser or a
desktop backend, desktop interaction sessions (screenshots, mouse /
keyboard, OCR, UI automation), and VLM-driven UI control.

The package re-exports the key public types -- browser session
primitives, computer-use action/step/trajectory models, and desktop
screen state / locate results -- so callers can import them from
``encre.computer`` directly.
"""

# Public API for the "computer" package: browser automation, computer-use
# (mouse/keyboard/screenshot) control loops, and desktop interaction sessions.
# Re-export the browser session primitives (state, viewport, session handle).
from encre.computer.browser import BrowserState, BrowserViewport, EncreBrowserSession
# Re-export computer-use action/step/trajectory models and the session driver.
from encre.computer.computer_use import (
    VALID_ACTIONS,
    ComputerUseAction,
    ComputerUseStep,
    ComputerUseTrajectory,
    EncreComputerUseSession,
)
# Re-export desktop screen state and locate results plus the desktop session.
from encre.computer.desktop import (
    DesktopLocateResult,
    DesktopScreenState,
    EncreDesktopSession,
)

__all__ = [
    "VALID_ACTIONS",
    "BrowserState",
    "BrowserViewport",
    "ComputerUseAction",
    "ComputerUseStep",
    "ComputerUseTrajectory",
    "DesktopLocateResult",
    "DesktopScreenState",
    "EncreBrowserSession",
    "EncreComputerUseSession",
    "EncreDesktopSession",
]
