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

"""Masked secret input for platform setup wizards.

Adapters prompt for API tokens during first-run setup.  Secrets must not echo
to the terminal, so :func:`masked_secret_prompt` reads them without echo and
falls back to a visible prompt only when no TTY is available.
"""


import getpass
import sys


def masked_secret_prompt(prompt: str = "Secret: ") -> str:
    """Read a secret from the terminal without echoing it.

    Args:
        prompt: The label displayed to the user.

    Returns:
        The entered value with surrounding whitespace stripped.  Returns an
        empty string when input is unavailable (non-interactive stdin).
    """
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass(prompt).strip()
        # Non-interactive: fall back to a plain read so piped input still works.
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
