/**
 * Deduplicating dispatch for backend-derived DOM signals.
 *
 * Several surfaces (market, settings plugin panel, skill reader, plugin UI)
 * subscribe through `window` CustomEvents instead of the state store. The
 * backend can legitimately deliver the same snapshot twice (the connect-time
 * replay followed by the post-warmup snapshot, or a no-op refresh), and each
 * replay used to repaint every subscriber.
 *
 * Consecutive identical payloads are dropped here. A different payload always
 * passes, so this is event-level idempotence, not a time-based throttle.
 */

const lastSignature = new Map<string, string>();

/**
 * Dispatch one backend-derived signal, skipping a consecutive duplicate.
 *
 * @param name DOM event name.
 * @param detail Payload.
 * @param signature Cheap identity of the payload. Omit for fire-and-forget
 *   signals that must always reach listeners.
 */
export function dispatchServerSignal(name: string, detail: unknown, signature?: string): void {
  if (signature) {
    if (lastSignature.get(name) === signature) return;
    lastSignature.set(name, signature);
  }
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

/** Forget a signal's last signature (new connection / explicit refresh). */
export function resetServerSignal(name: string): void {
  lastSignature.delete(name);
}
