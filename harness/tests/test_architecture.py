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

"""Architecture is enforced by tests, not by convention.

Two kinds of checks live here:

* **Hard rules** -- the kernel must never import the agent facade, contracts
  must not import the rest of the runtime, and new code must not swallow
  exceptions silently.
* **Ratchet** -- known kernel -> feature imports are listed explicitly.  The
  list may only shrink; adding an entry requires a deliberate edit to this
  file, which is the review gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

HARNESS_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = HARNESS_ROOT / "encre"

#: Kernel -> feature imports that are still being paid down.  Shrink only.
#:
#: Empty since the feature seam landed: the loop resolves every feature symbol
#: through ``encre.features.adapters`` instead of importing ``improve``/
#: ``memdir``/``device`` at module scope (annotation-only imports under
#: ``TYPE_CHECKING`` do not count as edges).
RATCHET: dict[str, frozenset[str]] = {}

#: Packages whose modules the kernel must not reach into directly.
FEATURE_ROOTS = frozenset({"improve", "memdir", "device", "agent"})


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _is_type_checking_test(test: ast.AST) -> bool:
    """Whether an ``if`` test is ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    if isinstance(test, ast.Name):
        return test.id in ("TYPE_CHECKING", "TYPE_CHECKING_BLOCK")
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _imports(path: Path) -> set[str]:
    """Module-level ``encre.*`` imports, excluding ``TYPE_CHECKING`` blocks.

    ``TYPE_CHECKING`` imports are annotations only: they are never executed at
    runtime, so they are not a dependency edge.  The loop uses them for
    feature-package types it resolves through ``encre.features.adapters``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = list(ast.walk(tree))
    parent = {id(child): node for node in nodes for child in ast.iter_child_nodes(node)}
    type_checking: set[int] = set()
    for node in nodes:
        cur = node
        while id(cur) in parent:
            cur = parent[id(cur)]
            if isinstance(cur, ast.If) and _is_type_checking_test(cur.test):
                type_checking.add(id(node))
                break
    found: set[str] = set()
    for node in nodes:
        if id(node) in type_checking:
            continue
        if isinstance(node, ast.ImportFrom) and getattr(node, "module", None):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {name for name in found if name == "encre" or name.startswith("encre.")}


def _feature_imports(path: Path) -> set[str]:
    result = set()
    for module in _imports(path):
        parts = module.split(".")
        if len(parts) > 1 and parts[1] in FEATURE_ROOTS:
            result.add(module)
    return result


def test_kernel_never_imports_the_agent_facade() -> None:
    offenders: list[str] = []
    for path in _python_files(PACKAGE_ROOT / "loop"):
        for module in _imports(path):
            if module == "encre.agent" or module.startswith("encre.agent."):
                offenders.append(f"{path.relative_to(HARNESS_ROOT)} -> {module}")
    assert not offenders, "kernel imports the surface layer:\n" + "\n".join(offenders)


def test_feature_imports_are_a_shrinking_ratchet() -> None:
    violations: dict[str, set[str]] = {}
    for path in _python_files(PACKAGE_ROOT / "loop"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        found = _feature_imports(path)
        allowed = RATCHET.get(relative, frozenset())
        extra = found - allowed
        if extra:
            violations[relative] = extra

    assert not violations, (
        "new kernel -> feature imports (inject a port instead, or extend the "
        f"ratchet deliberately): {violations}"
    )

    # The ratchet must also not contain stale entries: if the code no longer
    # imports something, the allowlist entry should be deleted.
    stale: dict[str, set[str]] = {}
    for relative, allowed in RATCHET.items():
        path = PACKAGE_ROOT / relative
        if not path.exists():
            stale[relative] = set(allowed)
            continue
        missing = set(allowed) - _feature_imports(path)
        if missing:
            stale[relative] = missing
    assert not stale, f"remove stale ratchet entries: {stale}"


def test_contracts_depend_only_on_contracts() -> None:
    for path in _python_files(PACKAGE_ROOT / "contracts"):
        for module in _imports(path):
            assert module == "encre.contracts" or module.startswith("encre.contracts."), (
                f"{path.name} imports outside contracts: {module}"
            )


def test_kernel_does_not_import_surfaces() -> None:
    forbidden = ("encre.sdk", "encre.agent", "encre.lifecycle", "encre.contract")
    for path in _python_files(PACKAGE_ROOT / "kernel"):
        for module in _imports(path):
            assert not any(module == f or module.startswith(f + ".") for f in forbidden), (
                f"{path.name} imports a surface module: {module}"
            )


@pytest.mark.parametrize("package", ["contracts", "kernel"])
def test_new_packages_stay_small(package: str) -> None:
    oversized = []
    for path in _python_files(PACKAGE_ROOT / package):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > 800:
            oversized.append(f"{path.name}: {lines}")
    assert not oversized, f"{package} files over 800 lines: {oversized}"


@pytest.mark.parametrize("package", ["contracts", "kernel"])
def test_new_packages_do_not_swallow_exceptions(package: str) -> None:
    offenders: list[str] = []
    for path in _python_files(PACKAGE_ROOT / package):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = [
                stmt
                for stmt in node.body
                if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
            ]
            if not body:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"silent except blocks: {offenders}"


def test_no_top_level_duplicate_class_names_in_new_packages() -> None:
    """Contracts and kernel must not reintroduce the duplicate-class problem."""
    seen: dict[str, list[str]] = {}
    for package in ("contracts", "kernel"):
        for path in _python_files(PACKAGE_ROOT / package):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    seen.setdefault(node.name, []).append(path.name)
    duplicates = {name: files for name, files in seen.items() if len(files) > 1}
    assert not duplicates, f"duplicate class definitions: {duplicates}"
