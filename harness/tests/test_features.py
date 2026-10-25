"""The feature seam: ``loop`` -> ``encre.features.adapters`` -> features.

The turn runner must not import ``improve`` / ``memdir`` / ``device`` at
module scope.  It asks the adapter module for a symbol by name; the real
import happens on first attribute access.  These tests keep that property
honest -- without them the seam could rot back into direct imports while the
architecture tests (which read ``TYPE_CHECKING``-filtered AST) stayed green.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import encre.features.adapters as adapters

HARNESS_ROOT = Path(__file__).resolve().parents[1]


def test_every_declared_symbol_resolves() -> None:
    assert adapters.__all__, "no adapter symbols declared"
    for name in adapters.__all__:
        assert hasattr(adapters, name), name


def test_unknown_symbol_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="not_a_real_adapter"):
        getattr(adapters, "not_a_real_adapter")  # noqa: B009


def test_declarations_are_queryable_without_importing() -> None:
    assert adapters.has("RecallEngine")
    assert not adapters.has("not_a_real_adapter")
    assert adapters.source_of("RecallEngine") == ("encre.memdir.recall", "RecallEngine")
    assert adapters.source_of("not_a_real_adapter") is None
    assert "RecallEngine" in adapters.__dir__()


def test_resolved_symbols_are_the_real_ones_and_are_cached() -> None:
    assert adapters.RecallEngine is adapters.RecallEngine
    assert adapters.RecallEngine.__module__ == "encre.memdir.recall"
    assert adapters.SignificanceJudge.__module__ == "encre.memdir.judgment"
    assert adapters.decompose_task_with_model.__module__.startswith("encre.improve.")


def test_loop_imports_no_feature_packages_at_module_scope() -> None:
    """Fresh interpreter: importing the wiring module must not pull in features."""
    code = (
        "import sys\n"
        "import encre.loop.wiring\n"
        "leaked = [m for m in sys.modules if m.startswith('encre.memdir')\n"
        "    or m.startswith('encre.device')\n"
        "    or m.startswith('encre.improve.evolution')]\n"
        "print('|'.join(sorted(leaked)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=HARNESS_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = [m for m in proc.stdout.strip().split("|") if m]
    assert not leaked, f"loop imported feature modules at import time: {leaked}"
