# Encre Agent Harness — Architecture

Status: **v0.6 target, partially implemented**. This document is the single source
of truth for how the harness is allowed to be organised. Code that contradicts it
is a defect, not a style choice.

The gates that enforce this document live in three places, in increasing order of
cost:

| Gate | Cost | What it proves |
| --- | --- | --- |
| `python -m pytest harness/tests -q` | ~10 s | API, contracts, kernel identity, architecture ratchet, standalone installability |
| `python harness/tools/arch_check.py --strict` | ~10 s | Dependency matrix + debt ratchet (silent `except`, file size, duplicate class names) |
| `python harness/evals/run_evals.py` | ~25 s | Behaviour: turns, tool dispatch, recovery, compaction (20 scripted tasks, offline) |

Run all three from `harness/`. Always set `PYTHONPATH=D:\encre\harness` (or run
from `harness/`) so the gateway tree (`core/encre/`) does not shadow the package.

---

## 1. Layering

```
L5  Surfaces        agent.py, sdk.py, lifecycle.py, contract.py
L4  Orchestration   loop/ (turn runner), modes/, dag/, task/, swarm/
L3  Domain          compact/, context_compiler, tools/, backends/, skills/, plugins/
L2  Kernel          kernel/ (process-wide services, ports)
L1  Contracts       contracts/ (versioned datatypes, schemas)
L0  Infra           crypto, secure_io, logging_config, config, telemetry, paths
```

**Dependency rule: a layer may only import from the layer beneath it.**
`contracts/` is the floor: it imports nothing outside itself. `kernel/` sits above
contracts and is allowed to reach into infra + registry objects, but never into a
surface module.

### Hard rules (enforced, no exceptions)

1. **`encre.loop` must not import `encre.agent`.** The turn loop is orchestration;
   the agent facade is a surface. Before this fix the loop imported
   `_get_shared_plugin_registry_raw` and `_PROJECT_SKILLS_CANDIDATE_DIRS` from
   `agent.py`, which inverted the dependency and made the loop untestable on its
   own. Those slots now live in `kernel/services.py`.
2. **`encre.contracts` imports nothing outside itself.** A contract is a *wire
   format*. If `EventEnvelope` needed the bus, it would pull the whole runtime into
   every exporter. Conversion lives in the bus: `LedgerEvent.from_envelope(...)`.
3. **`encre.kernel` never imports `encre.agent`, `encre.sdk`, `encre.lifecycle`,
   `encre.contract`.**
4. **No silent `except` in new code.** See `docs/audit/silent-exceptions.md`.
5. **No duplicate top-level class names across the harness** (ratcheted at 8;
   see the audit).

### The ratchet

Kernel → feature imports that are not yet paid down are listed explicitly in
`harness/tests/test_architecture.py::RATCHET`. The list may only shrink. Adding an
entry requires a deliberate edit to that file, which is the review gate. Current
debt: `loop/wiring.py`, `loop/phases/end_of_turn.py`, `loop/phases/prompt.py`,
`loop/phases/recovery.py` reach into `improve/`, `memdir/` and `device/` directly.

---

## 2. The three buses

A harness is "whole" when there is exactly **one** mechanism per cross-cutting
concern. Today the harness has three, and they are the seams every new feature
plugs into.

### 2.1 Event bus — `encre.events.ledger.EventSourcedBus`

Append-only, `seq` monotonic across the bus, in-memory window + JSONL ledger,
thread-safe, replay is side-effect free. Well-known kinds and phase names are
module constants (`KIND_CONFIG_RELOADED`, `PHASE_TURN_START`, ...).

Every event carries a schema version. `LedgerEvent` gained `trace_id`, `turn` and
`schema_version` (default `EVENT_SCHEMA_VERSION = 2`) in this cycle. Readers of
older records get the legacy defaults (`schema_version=1`), so existing ledgers
keep replaying.

### 2.2 Context bus — `encre.context_compiler`

The single place where a model request is assembled. It owns section ordering,
token budgeting, the `ContextSection` taxonomy, and the boundary blocks emitted
when a section is truncated. **Do not format prompts anywhere else.** The compaction
phase and the loop ask the compiler for a `ContextRequest` result rather than
concatenating strings.

### 2.3 Policy bus — `safety.py` / `autosafety.py` / `safety_policy.py`

Commands are either allowed, blocked, or escalated. Every decision produces a
`PolicyDecision` (ALLOW / ASK / DENY) that can be logged and replayed.

---

## 3. Contracts (`encre.contracts`, API v1)

`encre.contracts.version` is the only place versions live:

| Constant | Value | Meaning |
| --- | --- | --- |
| `API_VERSION` | `(1, 0)` | Public `encre.__all__` surface version |
| `EVENT_SCHEMA_VERSION` | `2` | Ledger / envelope schema |
| `PLUGIN_API_VERSION` | `(1, 0)` | Plugin registry ABI |
| `TOOL_SPEC_VERSION` | `1` | ToolSpec schema |

### `EventEnvelope`

The wire shape of an event: versioned, JSON-native, forward compatible. Unknown
fields written by a newer producer are preserved under `payload["_extra"]` rather
than dropped, so a reader that does not know about them still round-trips them.
Non-JSON leaves are downgraded to `repr(...)` at construction time, never at
serialization time (serialization must never raise).

```python
env = EventEnvelope.from_ledger(event)      # runtime -> wire
back = LedgerEvent.from_envelope(env)       # wire -> runtime (lives in the bus)
```

### `ToolSpec`

The single registration contract for every tool (builtin, MCP, or plugin):

```python
spec = ToolSpec(name="mcp__github__search",
                description="...",
                input_schema={"type": "object", "properties": {...}, "required": [...]})
spec.to_openai_schema()   # -> {"type": "function", "function": {...}}
```

Names must match `^[A-Za-z][A-Za-z0-9_.:-]{0,127}$`; `concurrency_safe` requires a
`concurrency_key`; an unsupported `spec_version` is rejected, not silently
migrated.

### `PolicyDecision`

Fail-closed by construction: an unknown decision value deserialises to `DENY`, not
to `ALLOW`. `is_allowed` is true only for `ALLOW`; `ASK` needs confirmation.

### Ports

`ModelPort`, `ToolPort`, `ContextPort`, `MemoryPort`, `EventPort`, `PolicyPort`,
`StorePort`, `ClockPort` — `typing.Protocol`s with no methods beyond the seams the
kernel needs. They exist so an embedding application can replace a backend, the
memory layer or the clock without subclassing the loop.

---

## 4. Kernel services (`encre.kernel.services`)

Process-wide singletons, created lazily and shared by every agent in the process:

```python
from encre.kernel.services import shared_plugin_registry_raw
registry = shared_plugin_registry_raw()   # no filesystem scan
```

`shared_plugin_registry_raw()` returns the registry **without** discovery; callers
that must not block use it. `encre.agent._get_shared_plugin_registry_raw` is kept
as an alias for compatibility, but the state lives in `kernel/`.
`reset_shared_services()` exists for tests.

---

## 5. Fast import (`harness/encre/__init__.py`)

The package entry point is a **PEP 562 lazy facade**: it owns only `__path__`
(native `_native` patch), the Windows console patch, `__all__`, a
`_LAZY_IMPORTS` table and a module-level `__getattr__`. Nothing else is imported
at package-import time.

Measured on this machine (`python -c "import encre"`, harness root on `PYTHONPATH`):

| | before | after |
| --- | --- | --- |
| import-only time | 1.35 s | **0.039 s** |
| modules loaded | 681 | **95** |
| `__all__` entries | 250 | 250 (all resolve) |

A standalone temp copy of `harness/encre` — no gateway tree, no editable
finders — imports in 0.05 s and resolves all 250 exports across 689 modules
with zero unresolved names. `harness/tests/test_standalone_install.py` asserts
this on every test run, so the old `ImportError` cannot come back.

`__all__` is unchanged at 250 names plus 14 extras, so nothing is hidden from
introspection — the work is just deferred to first attribute access.

---

## 6. Packaging hygiene

* `harness/encre/spec.py` exists so `import encre.spec` resolves in a standalone
  `encre-harness` install. Previously the public API imported
  `from encre.spec import ...`, a module that only existed in `core/encre/`, so a
  clean wheel install of the harness could not even be imported.
* `harness/tests/test_standalone_install.py` copies `harness/encre` to a temp dir
  and imports it with *only* that directory on `sys.path`, then resolves every
  name in `__all__`. It is the regression test for that class of bug.
* `harness/encre/events/ledger.py` imports `EVENT_SCHEMA_VERSION` from contracts
  rather than hard-coding `2`.

---

## 7. Eval harness (`harness/evals/`)

20 offline tasks against the real loop with a scripted backend
(`harness/evals/run_evals.py`), four categories:

| Category | Tasks | What it pins down |
| --- | --- | --- |
| `text` | 6 | streaming, unicode, long output, multi-line, no-tools, empty-response retry |
| `tool` | 7 | single/sequential/parallel calls, unknown tool, raising tool, bad args, big output |
| `recovery` | 4 | retryable error, non-retryable error, tool-then-empty, tool error then tool |
| `compaction` | 3 | compaction trigger, compaction before/after tool calls, multi-turn compaction |

The scripted backend answers three kinds of request differently — this is why the
harness behaves well under a fake model:

1. **Main turn** → next scripted step (`text`, `tools`, `error`, `text_repeat`).
2. **Compaction request** (detected by the summarisation prompt marker) → a
   canned structured summary.
3. **Memory-importance classifier** (detected by its prompt marker) → a bare
   `0.0`.

`baseline.json` records per-task pass status, backend call count and output size.
The runner exits non-zero on any failure, on a task that passed in the baseline but
fails now, or when a task's backend call count more than doubles.

---

## 8. Where this is not yet done

Honest gaps, tracked for the next batch:

1. ~~**Kernel → feature imports.**~~ **Closed in this cycle** — see §9
   (`encre.features`). The loop now has **zero** direct feature import edges and the
   ratchet list is empty.
2. **`session.py` is still 2,608 lines** and should be split along
   open / save / stream / restore.
3. **`plugins/registry.py` is still 2,641 lines.**
4. **`compact/` has not been merged into a single context authority** with
   `context_compiler`; both still exist side by side.
5. **Memory consolidation is duplicated** (`memdir/semantic.py` and
   `improve/learning/consolidator.py` both define `MemoryConsolidator`).
6. **The memory classifier shares the main model backend** and asks for a bare
   decimal. A model that answers in prose would pollute the next turn's context;
   the classifier should get an explicit small-model config.
7. **150 silent `except` blocks** remain (ratcheted, not removed). See the audit.

---

## 9. The feature seam (`encre.features`)

Layer L2.5, between the kernel and the feature domains:

```
L2.5  Features (seam)   features/adapters.py — declared symbol → owning module map
```

The seam exports one module with a `SOURCES: dict[str, tuple[str, str]]` table
of 15 symbols pointing at their owners — `improve.evolution.config`,
`improve.evolution.plan_do_review`, `improve.feedback.learner`,
`memdir.{curator,layers,recall,system,judgment}`, `device.context`. Symbols are
resolved lazily (PEP 562 `__getattr__` + `globals()` cache), so importing the
seam pulls in **nothing**; `has(name)` and `source_of(name)` can be queried
without an import at all.

`loop/wiring.py`, `loop/phases/{prompt,recovery,end_of_turn}.py` now use
`_features.<Symbol>` for every runtime feature reference. Type-only imports sit
in `if TYPE_CHECKING:` blocks, which the architecture tooling does not count as
dependency edges.

This replaced the old 44-entry ratchet: the seam is the contract now, so any
undeclared feature dependency fails the build instead of being listed.

Measured: `import encre.loop.wiring` used to pull 21 `improve`/`memdir` modules
into `sys.modules`; it now pulls 3, and those come transitively from top-level
modules rather than from `loop/`. Direct `loop → improve|memdir|device` import
edges: **0** (was 17 module imports). `harness/tests/test_features.py` asserts
this in a subprocess.

Gate status: **69 tests passed**, `arch_check.py --strict` → `strict: OK`,
`run_evals.py` → **20/20 passed**.
