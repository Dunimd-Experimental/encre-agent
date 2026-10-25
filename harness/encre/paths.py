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

"""Single source of truth for the Encre on-disk data root.

This module is deliberately dependency-free: it sits **below** both
:mod:`encre.config` and :mod:`encre.config_store`, which would otherwise
form an import cycle (the store needs the data root, the config model
needs the store).

Why it exists: the data tree used to be resolved by two independent
implementations -- :func:`encre.config.get_data_dir` and a second, ad-hoc
``_get_yim_data_dir`` in ``core/encre/protocol/handlers/workspace_store``.
The two had drifted apart (the latter skipped ``expanduser``, never
created the directory, and duplicated the environment-variable lookup
with subtly different semantics).  Every path under the data tree must
now be built from here.
"""

import os
from pathlib import Path

# Data directory root -- all Encre user data (config, sessions, memory,
# skills, logs) lives under this single tree.  Override via ENCRE_DATA_DIR.
_DATA_DIR = Path("~/.dunimd/encre").expanduser()
_DATA_DIR_ENV_VAR = "ENCRE_DATA_DIR"


def get_data_dir(*parts: str, ensure: bool = True) -> Path:
    """Return a path inside the Encre data directory (``~/.dunimd/encre``).

    This is the **single** entry point for resolving anything under the
    data tree.  Callers must not join fragments by hand.

    Args:
        *parts: Path fragments appended to the data root, e.g.
            ``get_data_dir("sessions", session_id)``.
        ensure: Create the resulting directory (default).  Pass ``False``
            for pure path computation.

    Returns:
        The resolved :class:`pathlib.Path`.  Set ``ENCRE_DATA_DIR`` to place
        the tree elsewhere (containers / CI).
    """
    env = os.environ.get(_DATA_DIR_ENV_VAR)
    base = Path(env).expanduser() if env else _DATA_DIR
    path = base.joinpath(*parts) if parts else base
    if ensure:
        path.mkdir(parents=True, exist_ok=True)
    return path
