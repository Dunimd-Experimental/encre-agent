/**
 * Application-wide UI error boundary.
 *
 * One component — first-party or plugin-provided — must never be able to take
 * down another: every render entry point goes through these guards. A failure
 * is recorded in one bounded registry, logged at most once per key per window,
 * and the caller gets its fallback (usually "render nothing for this item").
 *
 * This is the same pattern plugin slots already used, promoted to every
 * surface: chat items, session rows, market cards, settings panels, side
 * panels, scheduled surface renders, async plugin mounts and window-level
 * errors all report here.
 */

export interface UiFault {
  kind: string;
  key: string;
  message: string;
  count: number;
  firstAt: number;
  lastAt: number;
}

const faults = new Map<string, UiFault>();
const LOG_COOLDOWN_MS = 2000;
const MAX_FAULTS = 200;
let installed = false;

// Item-level circuit breaker: a component that fails repeatedly is skipped
// for a short window instead of being re-rendered (and re-thrown) every frame.
const strikes = new Map<string, number>();
const blockedUntil = new Map<string, number>();
/** Escalation level per quarantined key, so a permanently broken item does
 *  not flicker back in every few seconds. */
const banLevel = new Map<string, number>();
const STRIKE_LIMIT = 3;
const QUARANTINE_MS = 4000;
const QUARANTINE_MAX_MS = 60000;

function isBlocked(id: string): boolean {
  const until = blockedUntil.get(id) || 0;
  if (until <= Date.now()) {
    if (until) blockedUntil.delete(id);
    return false;
  }
  return true;
}

function noteFailure(id: string): void {
  const next = (strikes.get(id) || 0) + 1;
  if (next >= STRIKE_LIMIT) {
    strikes.set(id, 0);
    const level = (banLevel.get(id) || 0) + 1;
    banLevel.set(id, level);
    const ms = Math.min(QUARANTINE_MS * 2 ** (level - 1), QUARANTINE_MAX_MS);
    blockedUntil.set(id, Date.now() + ms);
    console.warn(`[ui-boundary] ${id} quarantined for ${ms}ms (level ${level})`);
    return;
  }
  strikes.set(id, next);
}

function noteSuccess(id: string): void {
  strikes.delete(id);
  blockedUntil.delete(id);
  banLevel.delete(id);
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message || error.name;
  if (typeof error === "string") return error;
  try {
    return String(error);
  } catch {
    return "unknown error";
  }
}

/** Record one UI failure (deduped by kind+key). Never throws. */
export function reportUiFault(kind: string, key: string, error: unknown): void {
  try {
    const id = `${kind}:${key}`;
    const now = Date.now();
    const prev = faults.get(id);
    const shouldLog = !prev || now - prev.lastAt >= LOG_COOLDOWN_MS;
    if (prev) {
      prev.count += 1;
      prev.lastAt = now;
      prev.message = messageOf(error);
    } else {
      faults.set(id, {
        kind,
        key,
        message: messageOf(error),
        count: 1,
        firstAt: now,
        lastAt: now,
      });
      if (faults.size > MAX_FAULTS) {
        let oldestKey = "";
        let oldestAt = Infinity;
        for (const [k, f] of faults) {
          if (f.lastAt < oldestAt) {
            oldestAt = f.lastAt;
            oldestKey = k;
          }
        }
        if (oldestKey) faults.delete(oldestKey);
      }
    }
    if (shouldLog) {
      console.error(`[ui-boundary] ${id} failed:`, error);
      try {
        window.dispatchEvent(new CustomEvent("encre-ui-fault", {
          detail: { kind, key, message: messageOf(error) },
        }));
      } catch {
        /* diagnostics only */
      }
    }
  } catch {
    /* a boundary must never throw */
  }
}

/** Run a value-producing render; return *fallback* on failure. */
export function guardValue<T>(kind: string, key: string, fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch (e) {
    reportUiFault(kind, key, e);
    return fallback;
  }
}

/** Run an HTML-producing render; return *fallbackHtml* on failure. */
export function guardHtml(kind: string, key: string, fn: () => string, fallbackHtml = ""): string {
  const id = `${kind}:${key}`;
  if (isBlocked(id)) return fallbackHtml;
  try {
    const out = fn();
    noteSuccess(id);
    return out;
  } catch (e) {
    reportUiFault(kind, key, e);
    noteFailure(id);
    return fallbackHtml;
  }
}

/** Run a void render (surface paint / DOM update); isolate failures. */
export function guardRender(kind: string, key: string, fn: () => void): void {
  try {
    fn();
  } catch (e) {
    reportUiFault(kind, key, e);
  }
}

/** Await a render/mount promise; isolate rejections. */
export async function guardAsync(kind: string, key: string, fn: () => Promise<unknown>): Promise<void> {
  try {
    await fn();
  } catch (e) {
    reportUiFault(kind, key, e);
  }
}

/** Snapshot of recorded faults (diagnostics / future fault panel). */
export function uiFaults(): ReadonlyArray<UiFault> {
  return Array.from(faults.values());
}

/**
 * Install the window-level safety net once. Unhandled render/async errors are
 * recorded instead of escaping as an uncaught exception that can abort a
 * whole event handler chain.
 */
export function installUiErrorBoundaries(): void {
  if (installed) return;
  installed = true;
  // Diagnostics hook: inspect recorded faults from DevTools without a build.
  (window as any).__encreUiFaults = uiFaults;
  window.addEventListener("error", (event) => {
    // Resource load errors have no Error object and are handled by their own
    // element (img/video fallbacks); they are not component failures.
    if (!event.error) return;
    reportUiFault("window", event.filename || "error", event.error);
  });
  window.addEventListener("unhandledrejection", (event) => {
    reportUiFault("async", "unhandled", event.reason);
    // The boundary has already logged it; avoid the duplicate browser report.
    event.preventDefault();
  });
}

/**
 * Lucide icon hydration is used by almost every surface; a malformed icon
 * name or a library fault must not abort the surrounding render.
 */
export function safeCreateIcons(root: HTMLElement | null | undefined): void {
  if (!root) return;
  const lucide = (window as any).lucide;
  if (!lucide || typeof lucide.createIcons !== "function") return;
  try {
    lucide.createIcons({ root });
  } catch (e) {
    reportUiFault("icons", "lucide", e);
  }
}
