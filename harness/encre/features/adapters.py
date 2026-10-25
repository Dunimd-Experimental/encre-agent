"""Lazy adapters for the feature packages.

Symbols the loop needs at runtime, mapped to the module that owns them.  The
imports are performed on first access (PEP 562 ``__getattr__``) so importing
this module -- and therefore importing the loop -- stays cheap and dependency
free.

Why this exists
---------------
``encre.loop`` is orchestration.  ``encre.improve`` (evolution/feedback),
``encre.memdir`` (layered memory) and ``encre.device`` (device context) are
features.  Before this module the turn runner imported all three at module
scope from five different files, which made the loop untestable without the
whole feature stack and made ``import encre.loop.wiring`` load 21 extra
modules it does not need.

Adding a symbol here is the only supported way for the loop to touch a feature
package.  Every entry is a reviewed change, and
``harness/tests/test_architecture.py`` fails if a loop file reaches for a
feature package directly instead.
"""

from __future__ import annotations

import importlib
from typing import Any

#: ``symbol -> (owning module, attribute)``.  Keep this sorted by module for
#: reviewability; the order is not semantically meaningful.
SOURCES: dict[str, tuple[str, str]] = {
    # -- evolution / feedback (encre.improve) ------------------------------
    "EvolutionConfig": ("encre.improve.evolution.config", "EvolutionConfig"),
    "PlanDoReviewEngine": ("encre.improve.evolution.plan_do_review", "PlanDoReviewEngine"),
    "ReviewGrade": ("encre.improve.evolution.plan_do_review", "ReviewGrade"),
    "StepStatus": ("encre.improve.evolution.plan_do_review", "StepStatus"),
    "decompose_task_with_model": (
        "encre.improve.evolution.plan_do_review",
        "decompose_task_with_model",
    ),
    "EncreFeedbackLearner": ("encre.improve.feedback.learner", "EncreFeedbackLearner"),
    # -- layered memory (encre.memdir) -------------------------------------
    "MemoryCurator": ("encre.memdir.curator", "MemoryCurator"),
    "MemoryEntry": ("encre.memdir.layers", "MemoryEntry"),
    "MemoryLayerStore": ("encre.memdir.layers", "MemoryLayerStore"),
    "MemoryType": ("encre.memdir.layers", "MemoryType"),
    "RecallEngine": ("encre.memdir.recall", "RecallEngine"),
    "EncreMemorySystem": ("encre.memdir.system", "EncreMemorySystem"),
    "Experience": ("encre.memdir.judgment", "Experience"),
    "SignificanceJudge": ("encre.memdir.judgment", "SignificanceJudge"),
    # -- device context (encre.device) -------------------------------------
    "DeviceContextManager": ("encre.device.context", "DeviceContextManager"),
}

__all__ = sorted(SOURCES)


def __getattr__(name: str) -> Any:
    try:
        module_path, attribute = SOURCES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    module = importlib.import_module(module_path)
    value = getattr(module, attribute)
    globals()[name] = value  # cache: resolve once, alias forever
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(SOURCES))


def source_of(name: str) -> tuple[str, str] | None:
    """Return ``(module, attribute)`` for ``name``, or ``None`` when unknown."""
    return SOURCES.get(name)


def has(name: str) -> bool:
    """Whether ``name`` is a declared adapter symbol (no import performed)."""
    return name in SOURCES
