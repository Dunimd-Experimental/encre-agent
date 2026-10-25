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

"""``encre-harness`` must work with only its own tree on ``sys.path``.

This is the regression test for the historical ``from encre.spec import ...``
bug: the public API imported a module that only existed in ``core/encre``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HARNESS_PACKAGE = Path(__file__).resolve().parents[1] / "encre"

_PROBE = r"""
import json
import sys
import time

import encre

failures = []
for name in encre.__all__:
    try:
        getattr(encre, name)
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")

from encre.contracts.version import API_VERSION
from encre.spec import EncreSpecEngine  # noqa: F401

print(json.dumps({
    "file": encre.__file__,
    "modules": len(sys.modules),
    "api_version": list(API_VERSION),
    "failures": failures,
}))
"""


def test_standalone_tree_imports_and_exports_everything(tmp_path: Path) -> None:
    target = tmp_path / "standalone"
    shutil.copytree(
        HARNESS_PACKAGE,
        target / "encre",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyd", "*.so"),
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(target)
    # Do not let the developer's data directory leak into the probe.
    env["ENCRE_DATA_DIR"] = str(tmp_path / "data")

    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(target),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    elapsed = time.perf_counter() - started

    assert result.returncode == 0, result.stderr[-4000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    resolved = Path(payload["file"]).resolve()
    assert resolved.is_relative_to(target.resolve()), (
        f"imported the wrong tree: {resolved}"
    )
    assert payload["api_version"] == [1, 0]
    assert payload["failures"] == [], payload["failures"]
    assert payload["modules"] < 1200, payload["modules"]
    assert elapsed < 60, f"standalone import+resolve took {elapsed:.1f}s"
