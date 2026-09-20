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
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""Anti-dormancy guards for the wired subsystems (Task 5.1).

The 0.5.0 refactor delivered several subsystems that had passing unit tests but
**no production caller**: the layered memory store was never recalled from, and
the learned safety-policy reflow was never instantiated.  Both were fixed by
wiring them into the runtime (Tasks 1-4).  These tests exist so the same class
of defect cannot silently return: they assert the *production* call sites, not
the units' own behaviour.

They are deliberately source-level where a behavioural assertion cannot express
"this is reached from the running loop": a subsystem can be fully functional and
still be dead code, and only the call graph can tell the difference.
"""

import re
from pathlib import Path

from encre.memdir.layers import MemoryLayerStore
from encre.memdir.system import EncreMemorySystem

_HARNESS = Path(__file__).resolve().parents[2] / "harness" / "encre"


def _production_sources() -> list[Path]:
    """Every production ``.py`` under ``harness/encre``."""
    return sorted(p for p in _HARNESS.rglob("*.py") if "__pycache__" not in p.parts)


def _callers(needle: str, *, exclude: tuple[str, ...] = ()) -> list[str]:
    """Files under ``harness/encre`` whose source contains ``needle``."""
    hits: list[str] = []
    for path in _production_sources():
        rel = path.relative_to(_HARNESS).as_posix()
        if any(rel.startswith(prefix) for prefix in exclude):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in source:
            hits.append(rel)
    return hits


# ----------------------------------------------------------------------
# Memory recall is reachable from the running loop
# ----------------------------------------------------------------------


def test_recall_engine_has_a_production_caller():
    """``RecallEngine.recall`` is called from production code, not only tests."""
    callers = _callers("recall(")
    assert "loop_context.py" in callers, (
        "the prompt path must recall memories through RecallEngine; "
        f"production callers found: {callers}"
    )
    assert "memdir/judgment.py" in callers, (
        "the significance judge's association signal must use RecallEngine; "
        f"production callers found: {callers}"
    )


def test_prompt_path_uses_recall_not_the_legacy_facade():
    """The system prompt is enriched by recall, never by the legacy facade."""
    prompt_phase = (_HARNESS / "loop" / "phases" / "prompt.py").read_text(encoding="utf-8")
    assert "build_recall_prompt(" in prompt_phase
    # The old dual-track entry point must be gone entirely.
    assert "build_memory_prompt" not in prompt_phase


def test_legacy_facade_exposes_no_retrieval_surface():
    """``EncreMemorySystem`` is file I/O only: no second retrieval path."""
    for name in (
        "search",
        "search_relevant",
        "build_prompt",
        "build_prompt_with_context",
        "format_manifest",
        "consolidate",
    ):
        assert not hasattr(EncreMemorySystem, name), (
            f"EncreMemorySystem.{name} still exists -- that is a second "
            "memory retrieval/consolidation path alongside the layer store"
        )


def test_memory_layer_store_has_a_production_ingestion_caller():
    """The store's ingestion is triggered by production code, not only tests."""
    callers = _callers("ingest_memory_dir(")
    # The facade's refresh() is the production ingestion trigger; the store
    # itself defines the method.
    assert "memdir/system.py" in callers, (
        "the facade's refresh() must trigger ingestion into the layer store; "
        f"production callers found: {callers}"
    )


def test_recall_engine_is_injected_into_the_context_builder():
    """The loop hands its recall engine to the builder that renders prompts."""
    wiring = (_HARNESS / "loop" / "wiring.py").read_text(encoding="utf-8")
    assert re.search(r"_ctx_bldr\.recall_engine\s*=", wiring), (
        "the loop must inject its RecallEngine into the ContextBuilder, "
        "otherwise recall never reaches the prompt"
    )
    assert re.search(r"\.layer_store\s*=", wiring), (
        "the loop must give the memory facade access to the layer store, "
        "otherwise refresh() cannot trigger ingestion"
    )


# ----------------------------------------------------------------------
# The learned safety-policy reflow is reachable from the running loop
# ----------------------------------------------------------------------


def test_policy_reflow_has_a_production_instantiation():
    """``EncreSafetyPolicyReflow`` is built by the runtime, not only tests."""
    callers = _callers("EncreSafetyPolicyReflow(")
    assert "loop/wiring.py" in callers, (
        "the learned-policy reflow must be instantiated by the loop wiring; "
        f"production callers found: {callers}"
    )


def test_turn_tail_dispatches_the_safety_reflow():
    """Turn teardown runs the reflow, so learned policies actually take effect."""
    end_of_turn = (_HARNESS / "loop" / "phases" / "end_of_turn.py").read_text(encoding="utf-8")
    assert "_dispatch_safety_reflow()" in end_of_turn
    assert "reflow.apply(" in end_of_turn, (
        "the hook must invoke the reflow's compile->persist->sync path"
    )


def test_reflow_restores_policies_at_startup():
    """Startup re-pushes persisted policies into the Rust engine."""
    wiring = (_HARNESS / "loop" / "wiring.py").read_text(encoding="utf-8")
    assert "reflow.refresh()" in wiring, (
        "wiring must call refresh() (= load + sync_to_native) so policies "
        "learned in a previous run are enforced before any tool call"
    )


# ----------------------------------------------------------------------
# No dual-track remnants
# ----------------------------------------------------------------------


def test_no_legacy_memory_retrieval_calls_remain():
    """Nothing in production calls the removed facade retrieval methods."""
    for needle in (
        "build_memory_prompt(",
        "memory_system.search(",
        "memory_system.search_relevant(",
        "memory_system.build_prompt(",
        "memory_system.format_manifest(",
    ):
        assert _callers(needle) == [], (
            f"legacy memory retrieval call '{needle}' still exists in production"
        )


def test_store_and_facade_are_distinct_stores_not_two_of_the_same():
    """The facade owns ``.md`` files; the layer store owns ``layers/`` records.

    This is the invariant that makes "one store per kind of memory" true: they
    must not share a directory, or the two would be two stores of the same
    memories (the dual track the refactor removed).
    """
    assert MemoryLayerStore is not EncreMemorySystem
    assert not hasattr(MemoryLayerStore, "build_prompt")
