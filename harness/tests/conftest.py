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

"""Test bootstrap for the standalone Encre Agent Harness.

The repository also contains ``core/encre`` (the gateway distribution) and an
editable install of it can register a namespace finder for the top-level
``encre`` package.  Tests here must exercise **this** tree
(``harness/encre``), never the core one, so the harness root is put at the
front of ``sys.path`` before anything imports ``encre``.

Running the suite::

    python -m pytest harness/tests -q

from the repository root, or ``python -m pytest -q`` from ``harness/``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PACKAGE = HARNESS_ROOT / "encre"

# ``encre`` must resolve to harness/encre.  Inserting at index 0 is enough
# because harness/encre is a regular package (has __init__.py); a namespace
# finder for core/encre only wins when no regular package is reachable.
sys.path.insert(0, str(HARNESS_ROOT))

# Keep every test isolated from the developer's real data directory.
_TEST_DATA_DIR = Path(
    os.environ.get("ENCRE_TEST_DATA_DIR")
    or (Path(os.environ.get("TEMP", "/tmp")) / "encre-harness-tests")
)
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("ENCRE_DATA_DIR", str(_TEST_DATA_DIR))


def pytest_configure(config: object) -> None:  # noqa: ARG001 - pytest hook name
    """Fail fast when the core tree shadowed the harness."""
    import encre

    resolved = Path(getattr(encre, "__file__", "") or "").resolve()
    if resolved.parent != HARNESS_PACKAGE:
        raise RuntimeError(
            "tests resolved the wrong 'encre' package: "
            f"{resolved} (expected {HARNESS_PACKAGE})"
        )
