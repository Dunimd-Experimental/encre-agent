# ADR-0001: Six-layer model with typed contracts and kernel-owned services

- Status: **Accepted** (enforced by tests; full port injection still pending)
- Date: 2026-10-25
- Scope: `harness/encre/` only. Gateway (`core/encre/`), native core
  (`native/crates/encre-core/`) and desktop (`desktop/`) are out of scope.

## Context

`harness/encre/` has grown to 30 sub-packages and 311 Python files. During a
full-scope audit the following defects made the harness unsafe to refactor and
difficult to embed:

1. **Orchestration depended on the surface layer.** `loop/control.py`,
   `loop/wiring.py`, `loop/hot_reload.py`, `loop/phases/model.py` and
   `loop/phases/prompt.py` imported `_get_shared_plugin_registry_raw` and
   `_PROJECT_SKILLS_CANDIDATE_DIRS` from `encre.agent`. The turn runner — the
   most test-sensitive code in the project — could not be imported without the
   agent facade, and process-wide registries were hidden as module globals in a
   UI-facing module.
2. **The public API was not importable in a standalone install.**
   `harness/encre/__init__.py` did `from encre.spec import ...`, but
   `spec.py` existed only in `core/encre/`. A clean `pip install encre-harness`
   produced a package whose own `__init__` raised `ImportError`.
3. **Events, tools and permissions had no versioned wire format.**
   `events/ledger.py` persisted ad-hoc dicts; tool schemas were re-described in
   each registration path; permission decisions were scattered across
   `safety.py`, `autosafety.py` and `safety_policy.py` with no shared type.
4. **Package import was expensive.** Importing `encre` executed 681 module
   imports (1.35 s), which the desktop backend pays on every launch.
5. **Nothing stopped a regression.** The harness had no test suite of its own
   and no architecture gate.

## Decision

Adopt a six-layer model in which dependencies only point downwards, and put all
cross-cutting types behind a versioned, dependency-free contracts package:

| Layer | Contents |
| --- | --- |
| L5 Surfaces | `agent.py`, `sdk.py`, `lifecycle.py`, `contract.py` |
| L4 Orchestration | `loop/`, `modes/`, `dag/`, `task/`, `swarm/` |
| L3 Domain | `compact/`, `context_compiler.py`, `tools/`, `backends/`, `skills/`, `plugins/` |
| L2 Kernel | `kernel/` — process-wide services, port definitions |
| L1 Contracts | `contracts/` — versioned datatypes only |
| L0 Infra | `crypto.py`, `secure_io.py`, `logging_config.py`, `config.py`, `telemetry.py`, `paths.py` |

Four rules are now mechanical:

1. `encre.loop` must not import `encre.agent`.
2. `encre.contracts` must not import anything outside itself.
3. `encre.kernel` must not import any surface module.
4. Kernel → feature imports (L2 reaching L4/L3 features such as `improve`,
   `memdir`, `device`) are allowed **only** for modules listed in
   `harness/tests/test_architecture.py::RATCHET`, and that list may only shrink.

In addition:

- Process-wide singletons move from `agent.py` into `encre.kernel.services`,
  which owns `_SHARED_PLUGIN_REGISTRY`, `_SHARED_SKILL_REGISTRY`, the discovery
  lock and the project skills/agent candidate directories. `agent.py` keeps
  thin aliases so existing callers do not break.
- `encre.contracts` defines the wire types: `EventEnvelope` (schema v2),
  `ToolSpec` (v1), `PolicyDecision`, and eight `typing.Protocol` ports
  (`ModelPort`, `ToolPort`, `ContextPort`, `MemoryPort`, `EventPort`,
  `PolicyPort`, `StorePort`, `ClockPort`). All versions live in one module,
  `encre.contracts.version` (`API_VERSION`, `EVENT_SCHEMA_VERSION`,
  `PLUGIN_API_VERSION`, `TOOL_SPEC_VERSION`).
- The kernel→feature imports that remain are **ratcheted, not ignored**: they
  are enumerated, counted, and fail the build if any new one appears.

## Consequences

Positive:

- The loop is importable and testable without the surface layer.
- A standalone `encre-harness` install imports and resolves its entire public
  API (asserted by `harness/tests/test_standalone_install.py`).
- New event producers must emit an `EventEnvelope`; new tools must describe
  themselves as a `ToolSpec`; new permission checks return a `PolicyDecision`.
  Downstream code can now distinguish "unknown field" from "unknown type".
- Fast import (`__init__.py` uses PEP 562 lazy exports): 1.35 s / 681 modules
  → 0.04 s / 95 modules for `import encre`, with `__all__` unchanged at 250
  names.

Accepted cost:

- Kernel → feature imports still exist. They are visible and bounded, not gone.
  Removing them requires the next batch of port-injection work
  (see `docs/architecture/HARNESS.md`, §8).
- **Amendment (same day):** closed by the feature seam, `encre.features`. The
  loop resolves `improve`/`memdir`/`device` symbols through a declared 15-entry
  `SOURCES` table in `features/adapters.py` and no longer imports those packages
  at module scope — so the ratchet list is empty and any *undeclared* feature
  dependency fails the build. Direct `loop → improve|memdir|device` edges: 0.
  ADR-0002 is now about *typing* the seam (injecting ports per agent instance)
  rather than about removing the edges.

## Alternatives considered

- **Keep the shared registries in `agent.py`.** Rejected: it forces every
  feature to depend on a surface module and makes the agent facade untestable.
- **Add everything to a single `contracts/__init__.py` and forbid subpackages.**
  Rejected: a 500-line contract module is worse for review than
  `events.py` / `tools.py` / `decisions.py` / `ports.py` with one small
  re-export file.
- **Move the harness into its own top-level namespace.** Rejected for this
  cycle: renaming the package is a breaking change for `encre-harness`
  consumers and for the desktop backend, and buys no architectural value.

## Enforcement

| Gate | Location |
| --- | --- |
| AST layer checks + ratchet | `harness/tests/test_architecture.py` |
| Dependency matrix + debt counters + `--strict` | `harness/tools/arch_check.py` |
| Import-linter contracts (optional, same rules) | `harness/.importlinter` |
| Standalone-installability | `harness/tests/test_standalone_install.py` |
| Behaviour regression (20 tasks) | `harness/evals/run_evals.py` |

## Follow-up ADRs required

- ADR-0002: inject feature ports into the loop instead of importing `improve`/
  `memdir`/`device` directly.
- ADR-0003: split `session.py` (2,608 lines) and `plugins/registry.py`
  (2,643 lines).
- ADR-0004: choose the single authority for context assembly
  (`context_compiler` vs `compact/engine.py`).
- ADR-0005: merge the duplicated `MemoryConsolidator` in `memdir/semantic.py`
  and `improve/learning/consolidator.py`.
