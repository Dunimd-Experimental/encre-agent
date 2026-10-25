# Silent `except` audit and policy

- Snapshot: 2026-10-25
- Detector: `harness/tools/arch_check.py::is_silent_handler` — an `except`
  handler counts as silent when its entire body is `pass` statements or bare
  constant expressions (e.g. a `pass  # noqa`).
- Baseline: **150**, ratcheted. `--strict` fails when the count grows.

## 1. Why this matters

A silent handler is a place where the harness decides that failure is
informationless. In a turn runner that is often the worst possible decision: the
event is already gone, the ledger has no record, and the next session inherits
the corrupted state. Two failure modes follow:

1. **Hidden regressions.** The model/backend contract changes, a call starts
   returning `None`, and the harness keeps running with an empty result forever.
2. **Unfalsifiable bugs.** A report says "it just stopped replying" and there is
   no log line that could confirm or refute any hypothesis.

The fix is never "delete the `except`". The fix is one of three explicit
outcomes, listed below.

## 2. Policy: exactly three outcomes

For each silent handler, pick one and leave a marker so the next reviewer does
not have to rediscover it:

| Outcome | Use when | Marker |
| --- | --- | --- |
| **R-Remove** | The failure is a programming error, or the resource has already been cleaned up. The `try` should not exist. | No marker needed. |
| **L-Log** | The operation is best-effort and the turn can continue with a degraded result (cache read, optional metadata, non-critical enrichment). | `logger.debug`/`warning` **with** the exception: `logger.warning("plugin cache read failed", exc_info=True)` |
| **P-Re-raise** | The failure is on the critical path of a turn (session load, tool execution, backend call, ledger append). | Wrap in a typed error from `encre.errors` so `loop_stability` can classify it. |

Rules:

- `L-Log` requires a real logger call in the handler. `pass` plus a comment does
  not count.
- `L-Log` is only allowed for best-effort paths. If the caller has no way to
  know the operation failed, the handler is not best-effort — it is `P-Re-raise`.
- New packages must stay at zero: `harness/tests/test_architecture.py` fails if
  `encre/contracts/` or `encre/kernel/` gains a silent handler.

## 3. Where the 150 live

58 files. Top offenders (start here; fixing these four removes 38 of the 150):

| Count | File | Likely class |
| --- | --- | --- |
| 13 | `computer/desktop.py` | mostly L-Log (screen capture retries) |
| 9 | `backends/multimodal.py` | mostly L-Log (per-provider media fallback) |
| 8 | `loop_context.py` | review individually — loop-adjacent, likely P |
| 8 | `sandbox/container.py` | L-Log (process teardown) |
| 7 | `loop/phases/prompt.py` | review individually — loop-adjacent, likely P |
| 5 | `crypto.py` | L-Log, but key-related paths are P |
| 5 | `improve/evolution/event_store.py` | L-Log |
| 4 | `backends/local.py` | L-Log |
| 4 | `capabilities/document_manager.py` | L-Log |
| 4 | `device/context/providers/sensors.py` | L-Log |
| 3 | `computer/browser.py`, `device/context/providers/gpu.py`, `events/stream.py`, `migration.py`, `telemetry.py`, `tools/_encoding.py`, `utils/loop_helpers.py` | mixed |

Everything else is 1–2 per file across 41 further files (full list from
`python harness/tools/arch_check.py --json | jq -r .silent_excepts[]`).

### The one to watch

`plugins/state.py:90` (`except OSError: pass` around `os.unlink(tmp)` after
`os.replace`) is a legitimate R-Remove candidate: the `finally` clause should
delete the tempfile only if `os.replace` has not consumed it. It is a correct
idiom, but it does not carry a logger call, so it is counted.

## 4. Target trajectory

| Version | Silent handlers | Notes |
| --- | --- | --- |
| 0.5.0 (now) | 150 | Ratcheted |
| 0.6 | ≤ 120 | Classify the loop-adjacent files (`loop_context.py`, `loop/phases/prompt.py`, `loop/phases/execute.py`, `loop_stability.py`, `loop/wiring.py`, `loop/runner.py`, `loop_sub_agent.py`) — 15 handlers, highest blast radius |
| 0.7 | ≤ 80 | `computer/` + `backends/multimodal.py` + `sandbox/` converted to `L-Log` with `exc_info` |
| 0.8 | ≤ 50 | Remaining `capabilities/`, `device/`, `improve/`, `plugins/` |

Lowering a baseline is a reviewable change: edit `BASELINES` in
`harness/tools/arch_check.py` and update this table in the same commit.

## 5. Operational note

`harness/encre/kernel/` and `harness/encre/contracts/` are held at **zero**
silent handlers by `harness/tests/test_architecture.py`. When you add a new
package at that level, that test applies to it automatically because it walks
the package tree — keep new low-layer code free of `pass`-only handlers.
