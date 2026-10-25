/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * DISCLAIMER: Users must comply with applicable AI regulations.
 * Non-compliance may result in service termination or legal liability.
 */

/**
 * The single owner of "who does this floating surface belong to".
 *
 * Every surface that is painted outside its logical parent — modal dialogs,
 * progress cards, inline panel editors, body-level menus — registers here with
 * exactly one scope, and the two view switches (session, settings panel) are
 * the only things that act on it:
 *
 *   app        global preference / lifecycle actions. Never shelved: switching
 *              conversations or panels cannot make it vanish or reappear.
 *   session    belongs to one conversation. Closed when the user leaves it
 *              (`restore: false`, the default for anything the user clicked);
 *              surfaces the backend is still waiting on declare `restore: true`
 *              and are shelved, then re-shown only while that session is live.
 *   panel      belongs to one settings panel. Always shelved and restored —
 *              an editor draft must survive a hop across panels and back.
 *   ephemeral  click-away popups (menus, tooltips, dropdowns). Closed by every
 *              switch; never restored.
 *
 * Scope is DECLARED, never inferred from the mount node: `parent` only says
 * where the surface paints. Conflating the two is what made a theme-switch
 * confirm follow the active conversation, and it is why the per-surface close
 * lists in `cleanupContentArea()` could never be complete.
 *
 * This module must not import `core/state.js` or `ui/dialog.js`: both are
 * above it. The session id / panel id mirrors are pushed in via
 * {@link setSurfaceSession} and {@link setSurfacePanel}, and the liveness
 * question is answered by a predicate the caller supplies.
 */

// ── Types ────────────────────────────────────────────────────────────────

/** What a surface belongs to. */
export type SurfaceScope = "app" | "session" | "panel" | "ephemeral";

/** How a hidden surface is put away: `detach` removes the node, `self` lets
 *  the owner hide it (a subtree that must keep its caret/animations). */
export type SurfaceMode = "detach" | "self";

export interface SurfaceOptions {
  /** Element the surface paints into. Defaults to {@link surfaceRoot}. */
  parent?: HTMLElement;
  /** Owner-managed visibility (`self`) instead of removing the node. */
  mode?: SurfaceMode;
  /** Session the surface belongs to; defaults to the active one. */
  sessionKey?: string;
  /** Settings panel the surface belongs to; defaults to the active one. */
  panelKey?: string;
  /** Backend is still waiting on this surface: shelve it instead of closing
   *  it when the user switches away, and re-show it only while live. */
  restore?: boolean;
  /** `mode: "self"` partners. Hide / show / finally discard the surface. */
  onHide?: () => void;
  onShow?: () => void;
  onClose?: () => void;
}

interface Surface extends SurfaceOptions {
  el: HTMLElement;
  scope: SurfaceScope;
  key: string;
  parent: HTMLElement;
  mode: SurfaceMode;
  restore: boolean;
  shelved: boolean;
  /** Shelf array the surface currently sits on, if any. */
  shelf: Surface[] | null;
}

// ── Registry ─────────────────────────────────────────────────────────────

const _surfaces = new Map<HTMLElement, Surface>();
/** Shelved session surfaces, keyed by the session they came from. */
const _sessionShelf = new Map<string, Surface[]>();
/** Shelved panel surfaces, keyed by the panel they came from. */
const _panelShelf = new Map<string, Surface[]>();

let _session = "";
let _panel = "";

function fallbackRoot(): HTMLElement {
  return document.getElementById("app") || document.body;
}

/** The element a scope paints into: whole window for global surfaces, the
 *  content area for the two scoped-to-a-view kinds. */
export function surfaceRoot(scope: SurfaceScope): HTMLElement {
  if (scope === "app") return fallbackRoot();
  return document.getElementById("main-content") || fallbackRoot();
}

/** The session new `session` surfaces bind to. */
export function currentSurfaceSession(): string {
  return _session;
}

/** The panel new `panel` surfaces bind to ("" outside settings). */
export function currentSurfacePanel(): string {
  return _panel;
}

/** Scope a surface gets when its caller does not name one: the settings panel
 *  the user is looking at, otherwise the conversation they are in. */
export function defaultSurfaceScope(): "session" | "panel" {
  return _panel ? "panel" : "session";
}

/** Mirror of `AppState.sessionId`. Pushed in by `setSessionId()` before the
 *  swap, so a surface restored by `onShow()` during the handover registers
 *  under the incoming session rather than the one being left. */
export function setSurfaceSession(sid: string): void {
  _session = sid || "";
}

/** Mirror of the active settings panel, pushed in by the settings lifecycle. */
export function setSurfacePanel(id: string | null): void {
  _panel = id || "";
}

/** True while `el` is a registered surface (including a shelved one). */
export function hasSurface(el: Element): boolean {
  return _surfaces.has(el as HTMLElement);
}

/**
 * Take ownership of an already-mounted surface.
 *
 * Re-registering the same node replaces the previous declaration, so a caller
 * that reuses a persistent node (a dropdown it shows again elsewhere) cannot
 * end up shelved under a stale key.
 */
export function registerSurface(el: HTMLElement, opts?: SurfaceOptions & { scope?: SurfaceScope }): void {
  const scope = opts?.scope || defaultSurfaceScope();
  const key =
    scope === "session" ? (opts?.sessionKey ?? _session) :
    scope === "panel" ? (opts?.panelKey ?? _panel) :
    "";
  const parent = opts?.parent || el.parentElement || surfaceRoot(scope);
  const prev = _surfaces.get(el);
  _surfaces.set(el, {
    el,
    scope,
    key,
    parent,
    mode: opts?.mode || "detach",
    restore: opts?.restore ?? prev?.restore ?? false,
    onHide: opts?.onHide ?? prev?.onHide,
    onShow: opts?.onShow ?? prev?.onShow,
    onClose: opts?.onClose ?? prev?.onClose,
    shelved: false,
    shelf: prev?.shelf ?? null,
  });
}

/** Release ownership. The node is left exactly where it is — the caller owns
 *  its DOM lifecycle from here (dialog dismissal, view teardown, …). */
export function unregisterSurface(el: HTMLElement): void {
  const s = _surfaces.get(el);
  if (!s) return;
  if (s.shelf) {
    const idx = s.shelf.indexOf(s);
    if (idx >= 0) s.shelf.splice(idx, 1);
    if (!s.shelf.length && s.key) _shelves(s.scope).delete(s.key);
    s.shelf = null;
  }
  _surfaces.delete(el);
}

function _shelves(scope: SurfaceScope): Map<string, Surface[]> {
  return scope === "panel" ? _panelShelf : _sessionShelf;
}

function _detach(s: Surface): void {
  if (s.shelved) return;
  s.shelved = true;
  if (s.mode === "self") s.onHide?.();
  else s.el.remove();
}

function _reattach(s: Surface): void {
  if (!s.shelved) return;
  s.shelved = false;
  if (s.mode !== "self" && !s.el.isConnected) s.parent.appendChild(s.el);
  s.onShow?.();
}

/** Put a surface away for good: the owner's dismissal path runs so its promise
 *  settles and any queue slot is released. */
function _discard(s: Surface): void {
  _surfaces.delete(s.el);
  if (s.shelf) {
    const idx = s.shelf.indexOf(s);
    if (idx >= 0) s.shelf.splice(idx, 1);
    s.shelf = null;
  }
  s.shelved = false;
  s.onClose?.();
}

/** Every registered surface matching `scope`/`key`, in mount order. */
function _find(scope: SurfaceScope, key?: string): Surface[] {
  const out: Surface[] = [];
  for (const s of _surfaces.values()) {
    if (s.scope !== scope) continue;
    if (key !== undefined && s.key !== key) continue;
    out.push(s);
  }
  return out;
}

// ── View switches ────────────────────────────────────────────────────────

/**
 * Move between conversations.
 *
 * `live(sid)` answers "is the backend still waiting on session `sid`?" — it is
 * evaluated for the incoming session only, so a permission prompt that timed
 * out while the user was elsewhere is discarded instead of resurrected.
 */
export function swapSession(oldSid: string, newSid: string, live: (sid: string) => boolean = () => false): void {
  closeEphemeralSurfaces();
  if (!oldSid && !newSid) return;
  if (oldSid && oldSid !== newSid) {
    const keep: Surface[] = [];
    for (const s of _find("session", oldSid)) {
      if (!s.restore) {
        _discard(s);
        continue;
      }
      _detach(s);
      keep.push(s);
    }
    _sessionShelf.set(oldSid, keep);
    if (!keep.length) _sessionShelf.delete(oldSid);
    else keep.forEach((s) => (s.shelf = keep));
  }
  if (newSid) {
    const stored = _sessionShelf.get(newSid);
    _sessionShelf.delete(newSid);
    for (const s of stored || []) {
      s.shelf = null;
      if (live(newSid)) _reattach(s);
      else _discard(s);
    }
  }
}

/**
 * Move between settings panels (or into / out of settings entirely with a null
 * on either side).
 *
 * Panel surfaces always shelve rather than close: the draft, the caret and the
 * panel's own scroll position have to be intact when the user comes back.
 */
export function swapPanel(oldPanel: string, newPanel: string): void {
  closeEphemeralSurfaces();
  if (oldPanel === newPanel) return;
  if (oldPanel) {
    const keep = _find("panel", oldPanel);
    for (const s of keep) _detach(s);
    if (keep.length) {
      _panelShelf.set(oldPanel, keep);
      keep.forEach((s) => (s.shelf = keep));
    } else {
      _panelShelf.delete(oldPanel);
    }
  }
  if (newPanel) {
    const stored = _panelShelf.get(newPanel);
    _panelShelf.delete(newPanel);
    for (const s of stored || []) {
      s.shelf = null;
      _reattach(s);
    }
  }
}

/** Close every click-away popup. Called by both switches. */
export function closeEphemeralSurfaces(): void {
  for (const s of _find("ephemeral")) _discard(s);
}

/**
 * Discard everything that belonged to a view that no longer exists — a deleted
 * session, a disposed panel — so a shelved surface cannot outlive its owner or
 * reappear if the id is ever reused.
 */
export function evictScope(scope: SurfaceScope, key: string): void {
  if (!key) return;
  for (const s of _find(scope, key)) _discard(s);
  const shelf = scope === "session" ? _sessionShelf : scope === "panel" ? _panelShelf : null;
  if (shelf) {
    const stored = shelf.get(key);
    shelf.delete(key);
    for (const s of stored || []) _discard(s);
  }
}

/** Count of registered surfaces — for the dev-time leak assertion only. */
export function surfaceCount(scope?: SurfaceScope): number {
  return scope ? _find(scope).length : _surfaces.size;
}
