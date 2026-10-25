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

"""Cross-platform subprocess argument helpers.

This module exists because Encre Agent Core and its Python harness launch
child processes from many different call sites (skill runners, tool
adapters, the gateway, and the desktop bridge). Each of those call sites
needs slightly different flags depending on which operating system the
process is running on. Rather than duplicating the OS-detection logic at
every call site, this module consolidates the small set of platform
shim arguments that are safe to pass through ``subprocess.Popen`` and
``subprocess.run``.

Today the only shim provided is the Windows flag that suppresses the
transient console window that appears whenever a GUI-driven parent
spawns a CLI child. On POSIX systems the equivalent behavior is the
default, so the shim returns an empty list and callers can safely
concatenate it onto their argv without branching.

Usage pattern::

    argv = ["python", "-m", "encre"] + windows_hide_flags()
    subprocess.Popen(argv, ...)

The ``"--windows-hide-console-window"`` token is not a Python argument;
it is understood by the native ``encre-core`` entry point, which turns
it into the ``CREATE_NO_WINDOW`` creation flag when actually invoking
``CreateProcess`` on Windows. Keeping it in argv here (rather than as a
direct ``CREATE_NO_WINDOW`` constant in Python) means the same argv
list can be round-tripped through JSON protocol messages without losing
the flag.
"""

from __future__ import annotations

import subprocess  # noqa: F401  # kept to document subprocess namespace
import sys


def windows_hide_flags() -> list[str]:
    """Return platform-appropriate argv flags to hide a spawned console window.

    On Windows, GUI applications (such as Encre Agent Desktop) that spawn
    a subprocess without this flag cause a black console window to flash
    briefly on screen. That is disruptive during agent turns that spawn
    helper processes (e.g., skill runners, ``git``, ``cargo``). This
    helper returns the flag token that must be appended to ``argv`` so
    that Encre Agent Core can translate it into
    ``CREATE_NO_WINDOW`` when calling the Win32 ``CreateProcess`` API.

    On POSIX platforms there is no such flash, so an empty list is
    returned. Callers may unconditionally do ``argv + windows_hide_flags()``
    without branching.

    Returns:
        A list containing ``["--windows-hide-console-window"]`` on
        ``sys.platform == "win32"``, otherwise ``[]``. The caller is
        expected to append this list to the front of its argv after the
        interpreter / program path.

    Notes:
        The token is intentionally a leading-double-dash string so it
        survives JSON serialization unchanged. If you need to skip the
        token (for example when spawning a plain POSIX binary), check
        ``sys.platform`` yourself instead of calling this helper.
    """
    if sys.platform != "win32":
        return []
    return ["--windows-hide-console-window"]
