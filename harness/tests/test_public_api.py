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

"""The public surface is a contract: every advertised name must resolve."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import encre


def test_public_api_version_is_declared() -> None:
    from encre.contracts.version import API_VERSION, EVENT_SCHEMA_VERSION

    assert API_VERSION == (1, 0)
    assert EVENT_SCHEMA_VERSION >= 2


def test_all_is_stable_and_non_empty() -> None:
    assert isinstance(encre.__all__, list)
    assert len(encre.__all__) == len(set(encre.__all__))
    assert len(encre.__all__) >= 250


def test_every_exported_name_resolves() -> None:
    """A name in ``__all__`` that cannot be imported is a broken release."""
    failures: list[str] = []
    for name in encre.__all__:
        try:
            getattr(encre, name)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "unresolvable public exports:\n" + "\n".join(failures)


def test_dir_lists_lazy_names() -> None:
    listed = dir(encre)
    missing = [name for name in encre.__all__ if name not in listed]
    assert not missing, f"dir() is missing exports: {missing}"


def test_spec_is_owned_by_the_harness_tree() -> None:
    """``encre.spec`` used to live only in core; standalone installs broke."""
    spec = importlib.import_module("encre.spec")
    spec_path = Path(spec.__file__).resolve()
    assert spec_path.parent.name == "encre"
    assert spec_path.parent.parent.name == "harness"
    for name in ("EncreSpecEngine", "SpecDocument", "SpecSection", "SpecStatus"):
        assert hasattr(spec, name), name


def test_lazy_import_does_not_eagerly_load_the_world() -> None:
    """The package entry point must stay cheap (< 200 modules)."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys, encre; "
        "print(len(sys.modules)); "
        "print(encre.__file__)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    count = int(out.stdout.splitlines()[0])
    assert count < 200, f"eager import pulled {count} modules"
    assert "harness" in out.stdout


@pytest.mark.parametrize(
    "module",
    [
        "encre.contracts",
        "encre.contracts.events",
        "encre.contracts.tools",
        "encre.contracts.decisions",
        "encre.contracts.ports",
        "encre.contracts.version",
        "encre.kernel",
        "encre.kernel.services",
    ],
)
def test_contract_modules_are_importable(module: str) -> None:
    assert importlib.import_module(module) is not None
