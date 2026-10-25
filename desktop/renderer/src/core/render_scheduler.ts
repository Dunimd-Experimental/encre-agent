/**
 * One render scheduler for every surface in the renderer.
 *
 * Surfaces register a keyed render closure instead of running their own
 * requestAnimationFrame loop or painting synchronously from a state handler.
 * Within one frame:
 *
 *   - multiple requests for the same key collapse into one call (latest
 *     closure, first position),
 *   - surfaces run in a fixed priority order, so the chat always paints
 *     before the sidebar, panels and menus,
 *   - a surface that throws cannot stop the remaining surfaces.
 *
 * This is what stops a burst of backend frames from re-rendering the same
 * panel several times in one tick, and keeps a heavy surface from interleaving
 * arbitrarily with the rest of the UI.
 */

import { reportUiFault } from "./ui_boundary.js";

interface PendingRender {
  fn: () => void;
  priority: number;
  order: number;
}

const pending = new Map<string, PendingRender>();
let scheduled = 0;
let flushing = false;
let orderSeq = 0;
let lastSlowReport = 0;

/** Priorities: lower runs first. */
export const RENDER_PRIORITY = {
  chat: 10,
  sidebar: 20,
  panel: 30,
  tools: 40,
  market: 50,
  automation: 60,
} as const;

/**
 * Queue a keyed render for the next animation frame.
 *
 * @param key Stable surface key (one render per key per frame).
 * @param fn Render closure evaluated at paint time against current state.
 * @param priority Lower values run first.
 */
export function scheduleRender(key: string, fn: () => void, priority = 50): void {
  const existing = pending.get(key);
  if (existing) {
    // Keep the original ordering slot; use the newest state reader.
    existing.fn = fn;
    existing.priority = priority;
  } else {
    pending.set(key, { fn, priority, order: orderSeq++ });
  }
  if (!scheduled && !flushing) scheduled = requestAnimationFrame(flush);
}

/** Drop a queued render for one key (e.g. a forced synchronous repaint). */
export function cancelScheduledRender(key: string): void {
  pending.delete(key);
}

/**
 * Run one keyed render immediately through the same boundary as a frame
 * flush, discarding any queued version of that key. Used by structural
 * actions that must be visible before the next statement (session switch,
 * opening a menu, starting a progress animation).
 */
export function runScheduledRenderNow(key: string, fn: () => void, priority = 50): void {
  pending.delete(key);
  void priority;
  try {
    fn();
  } catch (e) {
    reportUiFault("surface", key, e);
  }
}

/** Run every queued render now (used only where a synchronous paint is required). */
export function flushScheduledRenders(): void {
  if (scheduled) {
    cancelAnimationFrame(scheduled);
    scheduled = 0;
  }
  flush();
}

/** Frame budget for one flush. Boot coalesces many surfaces into one pass;
 *  without slicing that becomes a single 200ms+ frame and startup feels
 *  slower than before unification. */
const FRAME_BUDGET_MS = 12;

function flush(): void {
  scheduled = 0;
  if (flushing) return;
  flushing = true;
  const started = performance.now();
  const batch = Array.from(pending.entries()).sort(
    (a, b) => a[1].priority - b[1].priority || a[1].order - b[1].order,
  );
  pending.clear();
  let surfaces = 0;
  for (let i = 0; i < batch.length; i++) {
    const [key, task] = batch[i];
    try {
      task.fn();
    } catch (e) {
      // One broken surface must not stop the others in this frame.
      reportUiFault("surface", key, e);
    }
    surfaces++;
    // Time-slice: park the remaining (lower-priority) surfaces on the next
    // frame instead of building one giant startup frame.
    if (i < batch.length - 1 && performance.now() - started >= FRAME_BUDGET_MS) {
      for (let j = i + 1; j < batch.length; j++) pending.set(batch[j][0], batch[j][1]);
      break;
    }
  }
  flushing = false;
  const elapsed = performance.now() - started;
  if (elapsed >= 50 && performance.now() - lastSlowReport > 1000) {
    lastSlowReport = performance.now();
    console.warn(
      `[perf] render flush ${Math.round(elapsed)}ms surfaces=${batch.slice(0, surfaces).map(([k]) => k).join(",")}`,
    );
  }
  if (pending.size > 0 && !scheduled) scheduled = requestAnimationFrame(flush);
}
