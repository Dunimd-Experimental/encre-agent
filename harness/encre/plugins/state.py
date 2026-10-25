#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Per-plugin persistent state store.

One JSON file per plugin under ``<data>/plugin_state/<safe-name>.json``.
The store is deliberately tiny and strict:

- JSON-only values, a hard size cap, atomic replace on every write;
- key lookup is confined to the owning plugin's file — there is no global
  namespace a plugin can read or overwrite;
- a process-transport worker receives the same path so its state survives
  worker restarts and is identical to the in-process view.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from encre.paths import get_data_dir

_MAX_BYTES = 1024 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def state_path(plugin: str) -> Path:
    """Absolute state-file path for *plugin* (never shared across plugins)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", plugin)[:120] or "plugin"
    return get_data_dir("plugin_state") / f"{safe}.json"


class PluginStateStore:
    """JSON key/value store scoped to one plugin."""

    def __init__(self, plugin: str, path: Path | None = None) -> None:
        self.plugin = plugin
        self.path = Path(path) if path else state_path(plugin)
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self._data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._data = {}
        return self._data

    def _save(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > _MAX_BYTES:
            raise ValueError(f"plugin state exceeds {_MAX_BYTES} bytes")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.stem}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self._data = data

    @staticmethod
    def _check_key(key: str) -> str:
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError("plugin state key must match [A-Za-z0-9_.:-]{1,128}")
        return key

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load().get(self._check_key(key), default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = dict(self._load())
            data[self._check_key(key)] = value
            self._save(data)

    def delete(self, key: str) -> None:
        with self._lock:
            data = dict(self._load())
            if data.pop(self._check_key(key), None) is not None:
                self._save(data)

    def clear(self) -> None:
        with self._lock:
            self._save({})

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._load())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._load())
