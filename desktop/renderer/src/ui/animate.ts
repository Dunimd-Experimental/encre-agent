// ─────────────────────────────────────────────────────────────────────────────
// Unified motion helpers — the one place every floating surface enters and
// leaves through.
//
// styles.css carries the keyframes (`encre-enter` / `encre-exit` / `fade-in` /
// `fade-out`) and one rule per surface class; this module owns the JS side:
//
//   hideAnimated   surface is `.hidden`-toggled  (menus, dropdowns, panels)
//   showAnimated   …and has to come back later
//   removeAnimated surface is detached instead  (dialogs, toast overlays)
//
// Keeping all three together matters: the show path has to cancel an exit that
// is still running, otherwise a surface that is closed and immediately
// re-opened (right-click the same row twice, reopen a dropdown within the
// 120ms fade) finishes fading *out* at the new position before the enter
// motion replays — it reads as the menu sticking or flashing.
// ─────────────────────────────────────────────────────────────────────────────

/** Per-element hide sequence, so a stale `finish()` from an interrupted exit
 *  can never strip the `.closing` marker belonging to a newer one. */
const _hideSeq = new WeakMap<Element, number>();

/**
 * Hide a floating surface with the unified exit animation.
 *
 * `.closing` is applied together with `.hidden`: the surface becomes logically
 * hidden at once (every caller — including a re-open — sees it as closed)
 * while the `.closing` `display` override in styles.css keeps it painted so
 * the keyframes can actually run. When the animation ends (or the 300ms
 * fallback fires) `.closing` is dropped and `.hidden` stays, i.e.
 * `display: none`.
 *
 * Re-opening has to go through {@link showAnimated}, which drops both markers
 * so the enter motion replays instead of leaving the surface blank.
 */
export function hideAnimated(el: Element | null | undefined): void {
  if (!el || el.classList.contains("hidden")) return;
  if (el.classList.contains("closing")) {
    // Shown again mid-exit and now being hidden: the in-flight finish() from
    // that exit still owns `.closing`, so only the logical state has to be
    // restored — it will clean the marker up when it runs.
    el.classList.add("hidden");
    return;
  }
  const seq = (_hideSeq.get(el) ?? 0) + 1;
  _hideSeq.set(el, seq);
  el.classList.add("closing");
  el.classList.add("hidden");
  let done = false;
  const finish = (ev?: Event): void => {
    if (done) return;
    if (ev && ev.target !== el) return; // a child element's animation bubbled up
    done = true;
    el.removeEventListener("animationend", finish);
    window.clearTimeout(timer);
    if ((_hideSeq.get(el) ?? 0) !== seq) return; // a newer hide took over
    el.classList.remove("closing");
  };
  const timer = window.setTimeout(finish, 300);
  el.addEventListener("animationend", finish);
}

/**
 * Show a floating surface, cancelling any exit that is still in flight.
 *
 * Drop-in replacement for `el.classList.remove("hidden")` at every show site
 * of an animated surface.
 */
export function showAnimated(el: Element | null | undefined): void {
  if (!el) return;
  el.classList.remove("closing");
  el.classList.remove("hidden");
}

/**
 * Detach a surface from the DOM *after* the exit animation instead of yanking
 * it out mid-frame (dialogs and toast overlays are removed, not `.hidden`-
 * toggled). `onRemoved` runs exactly once, once the node is gone.
 *
 * `cls` names the class whose CSS rule plays the exit — most surfaces use the
 * shared `.closing`; `.encre-dialog-overlay` keeps its pre-existing
 * `.encre-dialog-dismissing` rule. Calling this twice for the same element is
 * a no-op, so the first dismissal wins and a later one cannot double-resolve.
 */
export function removeAnimated(
  el: HTMLElement | null | undefined,
  onRemoved?: () => void,
  cls = "closing",
): void {
  if (!el || el.classList.contains(cls)) return;
  el.classList.add(cls);
  let done = false;
  const finish = (ev?: Event): void => {
    if (done) return;
    if (ev && ev.target !== el) return;
    done = true;
    el.removeEventListener("animationend", finish);
    window.clearTimeout(timer);
    el.remove();
    onRemoved?.();
  };
  const timer = window.setTimeout(finish, 300);
  el.addEventListener("animationend", finish);
}

/** Per-element view-exit sequence — same guard as {@link _hideSeq}, so a
 *  stale finish() can never land on a newer exit. */
const _viewExitSeq = new WeakMap<Element, number>();

/**
 * Hide a whole-slot sub-view (`#settings-view`, `#child-view`, …) with the
 * exit motion instead of yanking it out of the layout.
 *
 * Unlike {@link hideAnimated} the element may ALREADY carry `.hidden` —
 * `#settings-view` has it from first paint and is only shown while
 * `#app.settings-mode` is on — so the marker, not `.hidden`, decides whether
 * an animation plays. `.hidden` is applied unconditionally (it is what
 * *ends* the exit: once `.view-exit` is gone, display:none takes over) and
 * `.view-exit` is what keeps the panel painted in the meantime: styles.css
 * lifts it out of flow — so the incoming panel can take the slot on the very
 * same frame — and forces `display` back on with id + class specificity.
 */
export function hideViewAnimated(el: Element | null | undefined): void {
  if (!el || el.classList.contains("view-exit")) return; // already exiting
  const seq = (_viewExitSeq.get(el) ?? 0) + 1;
  _viewExitSeq.set(el, seq);
  el.classList.add("hidden");
  el.classList.add("view-exit");
  let done = false;
  const finish = (ev?: Event): void => {
    if (done) return;
    if (ev && ev.target !== el) return; // a child's animation bubbled up
    if (ev && (ev as AnimationEvent).animationName !== "encre-content-exit") return;
    done = true;
    el.removeEventListener("animationend", finish);
    window.clearTimeout(timer);
    if ((_viewExitSeq.get(el) ?? 0) !== seq) return; // a show took over
    if (!el.classList.contains("view-exit")) return;
    el.classList.remove("view-exit");
  };
  const timer = window.setTimeout(finish, 300);
  el.addEventListener("animationend", finish);
}

/**
 * Show a whole-slot sub-view, cancelling an exit still in flight so a panel
 * closed and reopened within the 120ms fade is never re-hidden by the stale
 * finish().
 *
 * `removeHidden` defaults to `true` (the normal `classList.remove("hidden")`
 * replacement). `#settings-view` is the exception: it keeps `.hidden`
 * permanently and is displayed purely by `#app.settings-mode`, so there we
 * only need the marker dropped — clearing `.hidden` would leave settings
 * permanently visible.
 */
export function showViewAnimated(el: Element | null | undefined, removeHidden = true): void {
  if (!el) return;
  _viewExitSeq.set(el, (_viewExitSeq.get(el) ?? 0) + 1); // invalidate finish()
  el.classList.remove("view-exit");
  if (removeHidden) el.classList.remove("hidden");
}
