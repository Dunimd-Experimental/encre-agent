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

"""Persistent snapshot of installed-plugin lifecycle states.

The market's 已安装 tab and the settings plugin panel need this list as soon
as the window opens. Building it requires plugin discovery (hundreds of
module imports in the worst case), so the last authoritative snapshot is
persisted here and replayed on connect; the live snapshot replaces it once
warmup finishes. This is the plugin analogue of the session index: a fast
read of known state instead of a rescan.
"""

from __future__ import annotations

import json
from typing import Any

from encre.logging_config import get_logger
from encre.paths import get_data_dir

logger = get_logger(__name__)

FILENAME = "plugin_states.json"


def _path():
    """Path of the persisted snapshot under the data tree."""
    return get_data_dir(FILENAME, ensure=False)


def read_plugin_states() -> list[dict[str, Any]] | None:
    """Return the last persisted plugin snapshot, or ``None`` when absent."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    return plugins if isinstance(plugins, list) else None


def write_plugin_states(plugins: list[dict[str, Any]]) -> None:
    """Persist an authoritative snapshot atomically.

    An empty list is ignored: the first post-warmup broadcast can be empty in
    a broken registry, and overwriting a good cache with it would make the
    next launch's market start empty.
    """
    if not plugins:
        return
    try:
        target = _path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": 1, "plugins": plugins}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)
    except OSError as exc:
        logger.debug("plugin_state_cache: write failed: %s", exc)
