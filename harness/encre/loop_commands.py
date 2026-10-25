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

from typing import Any


class CommandManager:
    """Manages the active slash command lifecycle.

    A slash *command* (built-in action or user-defined ``*.md`` command)
    is a sticky prompt injection that is NOT a mode: it does not intercept
    write tools and does not run the spec approval gate.  Once activated
    it stays in effect across turns (its ``command_instructions`` block is
    re-injected on every run) until explicitly cleared.  This mirrors the
    persistence model of :meth:`set_mode` so a command survives session
    reload / reconnect / restart: ``config.active_command`` is the
    in-memory mirror, ``StateManager.active_command`` is the
    on-disk source of truth.  A command and a mode (plan/spec) may be
    active at the same time -- they are independent slots.
    """

    def __init__(self, config: Any, state_mgr: Any) -> None:
        self._config = config
        self._state_mgr = state_mgr

    def set_command(self, name: str, prompt: str, icon: str = "",
                    title: str = "") -> None:
        """Activate (or replace) the persistent slash command.

        Stores ``{name, prompt, icon, title}`` in both
        ``config.active_command`` and ``StateManager.active_command``
        so the command's instructions are re-injected every turn until
        :meth:`clear_command` is called.  An empty ``name`` clears the slot.
        """
        name = (name or "").strip()
        if not name:
            self.clear_command()
            return
        payload = {
            "name": name,
            "prompt": prompt or "",
            "icon": icon or "",
            "title": title or name,
        }
        self._config.active_command = payload
        self._state_mgr.active_command = payload

    def clear_command(self) -> None:
        """Deactivate the persistent slash command (no-op if none active)."""
        self._config.active_command = None
        self._state_mgr.active_command = None

    @property
    def active_command_name(self) -> str:
        """Name of the active slash command, or ``""`` if none is active."""
        cmd = getattr(self._config, "active_command", None)
        return cmd.get("name", "") if cmd else ""
