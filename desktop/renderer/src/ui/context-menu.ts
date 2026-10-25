// ─────────────────────────────────────────────────────────────────────────────
// Context Menu — positioning helpers + unified declarative component.
//
// The positioning helpers (clamp*, clampIntoViewport, capDropdownHeight) are
// shared by dropdown menus, mention popups and floating panels; they are pure
// math and know nothing about DOM menus.
//
// The menu *component* is a small declarative layer on top of them: callers
// describe rows with `ContextMenuItemSpec` and the component renders them,
// delegates a single click listener, positions the menu, highlights the row
// that was right-clicked (through the fixed `.context-target-overlay`), and
// closes on outside click, outside right-click, Esc or item click. No knowledge
// of a specific caller (session tree, workspace, terminal, editor, logs) lives
// here.
// ─────────────────────────────────────────────────────────────────────────────

import { hideAnimated, showAnimated } from "./animate.js";
import { registerSurface, unregisterSurface } from "../core/scope.js";

const FLOAT_MARGIN = 8;
export { FLOAT_MARGIN };

export function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

/** Swap `data-lucide` placeholders for their SVGs. The renderer loads lucide
 *  as a global script (no package import), so look it up on `window`. */
function swapIcons(root: HTMLElement): void {
  const lucide = (window as any).lucide;
  if (lucide && typeof lucide.createIcons === "function") {
    lucide.createIcons({ root });
  }
}

/** Keep a left edge inside the viewport (left-aligned floats: file-tree menus, mention popups). */
export function clampFloatLeft(left: number, width: number): number {
  return clamp(left, FLOAT_MARGIN, Math.max(FLOAT_MARGIN, window.innerWidth - width - FLOAT_MARGIN));
}

/** Keep a right edge inside the viewport (right-aligned floats: input-area menus). */
export function clampFloatRight(right: number, width: number): number {
  return clamp(right, FLOAT_MARGIN, Math.max(FLOAT_MARGIN, window.innerWidth - width - FLOAT_MARGIN));
}

/** Clamp a `left` coordinate for an already-rendered element. Pure: the caller
 *  writes the returned px value into `style.left`. */
export function clampElLeft(el: HTMLElement, left: number): number {
  const width = el.getBoundingClientRect().width || 0;
  return clampFloatLeft(left, width);
}

/** Clamp a `right` coordinate (`style.right`) for an already-rendered element.
 *  Same bounds as {@link clampElLeft}; a larger value means further from the
 *  right edge, so both saturate at `innerWidth - width - FLOAT_MARGIN`. */
export function clampElRight(el: HTMLElement, right: number): number {
  const width = el.getBoundingClientRect().width || 0;
  return clampFloatRight(right, width);
}

/** Nudge an already-rendered floating element fully inside the viewport. It
 *  only pulls an overflowing edge back in; it never repositions an element that
 *  already fits. */
export function clampIntoViewport(el: HTMLElement): void {
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return;
  if (rect.right > window.innerWidth - FLOAT_MARGIN) {
    el.style.left = `${clampElLeft(el, rect.left)}px`;
  }
  if (rect.bottom > window.innerHeight - FLOAT_MARGIN) {
    el.style.top = `${Math.max(FLOAT_MARGIN, rect.bottom - rect.height - FLOAT_MARGIN)}px`;
  }
}

/** THE one placement rule for every body-attached dropdown menu: the menu is
 *  pinned DIRECTLY below (default) or above (`dir: "up"`) its trigger, with
 *  the trigger's left edge as the anchor, clamped into the viewport.  This
 *  mirrors what the CSS-anchored dropdowns do (`.settings-dropdown` /
 *  `.mention-dropdown` inside a `position: relative` wrap) — same visual
 *  contract, used where the menu must live under document.body to escape a
 *  clipping ancestor.  Call it AFTER the menu is in the DOM so its size is
 *  measurable; no other coordinate math is allowed at call sites. */
export function showAnchoredDropdown(
  menu: HTMLElement,
  trigger: HTMLElement,
  dir: "down" | "up" = "down",
): void {
  const r = trigger.getBoundingClientRect();
  menu.style.position = "fixed";
  if (dir === "up") {
    menu.style.top = "auto";
    menu.style.bottom = `${Math.max(FLOAT_MARGIN, window.innerHeight - r.top + 4)}px`;
  } else {
    menu.style.bottom = "auto";
    menu.style.top = `${r.bottom + 4}px`;
  }
  menu.style.margin = "0";
  menu.style.right = "auto";
  menu.style.left = `${clampElLeft(menu, r.left)}px`;
}

// ── Open-menu bookkeeping ────────────────────────────────────────────────────
// At most one context menu is open, so the dismiss listeners live on a single
// module-level record instead of being recreated per call.

interface OpenMenu {
  menu: HTMLElement;
  dismiss: (ev: Event) => void;
  onKey: (ev: KeyboardEvent) => void;
  onClose?: () => void;
}

let _open: OpenMenu | null = null;

function detach(): void {
  if (!_open) return;
  document.removeEventListener("click", _open.dismiss, true);
  document.removeEventListener("contextmenu", _open.dismiss, true);
  document.removeEventListener("keydown", _open.onKey, true);
  _open = null;
}

/** Hide a menu, clear the row it highlighted, drop its dismiss listeners and
 *  run an optional close callback. Safe to call on an already-hidden menu. */
export function closeContextMenu(menu: HTMLElement, onClose?: () => void): void {
  if (_open && _open.menu !== menu) detach();
  unregisterSurface(menu);
  const prev = _open?.onClose;
  if (_open) detach();
  hideAnimated(menu);
  clearContextTargets();
  (onClose ?? prev)?.();
}

/** Hide a menu and clear the highlight it was pointing at. */
export function hideContextMenu(menu: HTMLElement): void {
  closeContextMenu(menu);
}

// ── Target highlight overlay ─────────────────────────────────────────────────
// The row/cell/panel the open menu points at is marked by ONE fixed overlay
// element instead of a class on the target. A class cannot work here for two
// reasons: (1) the terminal and code-editor panes paint an opaque canvas over
// their own background, so a background painted on the target would never
// show; (2) the in-place row reconcilers (Session.render → patchRow) rewrite
// the whole `class` attribute, which used to wipe a class-based highlight
// while the menu was still open. The overlay tracks the target's box every
// frame — the list can scroll or stream underneath it — and disappears the
// moment the target leaves the DOM or the menu closes.

let _overlayEl: HTMLElement | null = null;
let _targetEl: HTMLElement | null = null;
let _followRaf = 0;
let _radius = "";

function ensureOverlay(): HTMLElement {
  if (_overlayEl && _overlayEl.isConnected) return _overlayEl;
  const el = document.createElement("div");
  el.className = "context-target-overlay";
  el.setAttribute("aria-hidden", "true");
  document.body.appendChild(el);
  _overlayEl = el;
  return el;
}

/** Position the overlay over `_targetEl`, or hide it when there is nothing
 *  valid to mark. A style property is written only when its value changed, so
 *  the per-frame follow loop never forces redundant style recalcs. */
function paintOverlay(): void {
  const target = _targetEl;
  const overlay = _overlayEl;
  if (!overlay) return;
  if (!target || !target.isConnected) {
    overlay.classList.remove("visible");
    return;
  }
  const rect = target.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) {
    overlay.classList.remove("visible");
    return;
  }
  const setPx = (prop: "left" | "top" | "width" | "height", value: number): void => {
    const next = `${Math.round(value)}px`;
    if (overlay.style[prop] !== next) overlay.style[prop] = next;
  };
  setPx("left", rect.left);
  setPx("top", rect.top);
  setPx("width", rect.width);
  setPx("height", rect.height);
  if (_radius && overlay.style.borderRadius !== _radius) overlay.style.borderRadius = _radius;
  overlay.classList.add("visible");
}

function followTarget(): void {
  _followRaf = 0;
  paintOverlay();
  if (_targetEl && _targetEl.isConnected) _followRaf = requestAnimationFrame(followTarget);
}

/**
 * Show a context menu at the given viewport coordinates, flipping it inside
 * the viewport when it would overflow.
 *
 * `opts.target` is the row/cell that was right-clicked: the highlight of every
 * previous target is cleared first and the new target is marked, so the
 * highlighted container always tracks the open menu. `opts.onClose` fires once
 * the menu hides (item click, outside click, outside right-click or Esc).
 *
 * The 3-argument signature is unchanged for existing callers.
 */
export function showContextMenu(
  menu: HTMLElement,
  x: number,
  y: number,
  opts?: { target?: HTMLElement | null; onClose?: () => void },
): void {
  // Close any other open context menu so at most one is visible.
  document.querySelectorAll<HTMLElement>(".context-menu:not(.hidden)").forEach((other) => {
    if (other !== menu) closeContextMenu(other);
  });

  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  menu.style.display = "";
  // Cancel an exit that is still running: the document-level `dismiss`
  // listener fires on this very `contextmenu` event, so re-right-clicking the
  // same row closes and re-opens the menu inside one dispatch. Without this
  // the menu would finish fading out (~120ms) at the new position before the
  // enter motion replayed — it reads as the menu sticking.
  showAnimated(menu);

  setContextTarget(opts?.target ?? null);

  requestAnimationFrame(() => {
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth - FLOAT_MARGIN) {
      menu.style.left = `${clampElLeft(menu, clampFloatRight(rect.right, rect.width))}px`;
    }
    if (rect.bottom > window.innerHeight - FLOAT_MARGIN) {
      menu.style.top = `${Math.max(FLOAT_MARGIN, y - rect.height - FLOAT_MARGIN)}px`;
    }
  });

  detach();
  const dismiss = (ev: Event): void => {
    if (menu.contains(ev.target as Node)) return;
    closeContextMenu(menu);
  };
  const onKey = (ev: KeyboardEvent): void => {
    if (ev.key === "Escape") closeContextMenu(menu);
  };
  _open = { menu, dismiss, onKey, onClose: opts?.onClose };
  // Owned as an EPHEMERAL popup: any conversation or panel switch discards it
  // through the scope host, which runs the same close path a click-away does.
  registerSurface(menu, {
    scope: "ephemeral",
    onClose: () => closeContextMenu(menu),
  });
  // Defer so a `contextmenu` handler calling this is not immediately dismissed
  // by its own bubbled click.
  setTimeout(() => {
    if (_open?.menu !== menu) return;
    document.addEventListener("click", dismiss, true);
    document.addEventListener("contextmenu", dismiss, true);
    document.addEventListener("keydown", onKey, true);
  }, 0);
}

// ── Component API ────────────────────────────────────────────────────────────

/** One context-menu row. `label` is trusted markup (callers already escape via
 *  `esc()`/`escapeHtml()`), so the component does not re-escape it and existing
 *  callers can migrate without double-encoding their i18n strings. */
export interface ContextMenuItemSpec {
  divider?: boolean;
  id?: string;
  action?: string;
  label?: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
  title?: string;
  className?: string;
  data?: Record<string, string>;
  onClick?: (spec: ContextMenuItemSpec, el: HTMLElement, ev: MouseEvent) => void;
}

const _delegated = new WeakSet<HTMLElement>();
const _rowSpecs = new WeakMap<HTMLElement, ContextMenuItemSpec>();

function specFromRow(el: HTMLElement): ContextMenuItemSpec {
  return {
    id: el.id || undefined,
    action: el.dataset.action || undefined,
    danger: el.classList.contains("context-menu-item-danger"),
    disabled: el.classList.contains("disabled"),
    className: el.className,
    data: Object.fromEntries(
      Object.entries(el.dataset).filter(([, v]) => v !== undefined) as [string, string][],
    ),
  };
}

/** Delegate one click listener per menu (idempotent). `onAction` is an
 *  optional menu-level action -> callback map; a row's own `onClick` from its
 *  original spec always runs first, then the menu closes. */
export function attachMenuClick(
  menu: HTMLElement,
  onAction?: (spec: ContextMenuItemSpec, el: HTMLElement, ev: MouseEvent) => void,
): void {
  if (_delegated.has(menu)) return;
  _delegated.add(menu);
  menu.addEventListener("click", (ev) => {
    const el = (ev.target as HTMLElement).closest?.(".context-menu-item") as HTMLElement | null;
    if (!el || !menu.contains(el) || el.classList.contains("disabled")) return;
    ev.stopPropagation();
    const spec = specFromRow(el);
    const original = _rowSpecs.get(el);
    if (original) spec.onClick = original.onClick;
    const close = () => closeContextMenu(menu);
    original?.onClick?.(spec, el, ev);
    onAction?.(spec, el, ev);
    close();
  });
}

/** Build a single row of markup (shared by the component and any caller that
 *  still wants to assemble strings by hand). */
export function contextMenuItemHtml(spec: ContextMenuItemSpec): string {
  if (spec.divider) return '<div class="context-menu-divider"></div>';
  const cls = ["context-menu-item"];
  if (spec.danger) cls.push("context-menu-item-danger");
  if (spec.disabled) cls.push("disabled");
  if (spec.className) cls.push(spec.className);
  const attrs = [
    spec.id ? `id="${escAttr(spec.id)}"` : "",
    spec.action ? `data-action="${escAttr(spec.action)}"` : "",
    spec.title ? `title="${escAttr(spec.title)}"` : "",
    ...(spec.data
      ? Object.entries(spec.data).map(([k, v]) => `data-${escAttr(k)}="${escAttr(v)}"`)
      : []),
  ]
    .filter(Boolean)
    .join(" ");
  const icon = spec.icon ? `<i class="lucide lucide-sm" data-lucide="${escAttr(spec.icon)}"></i>` : "";
  return `<div class="${cls.join(" ")}" ${attrs}>${icon}<span>${spec.label ?? ""}</span></div>`;
}

function escAttr(v: string): string {
  return v.replace(/"/g, "&quot;");
}

/**
 * Fill (or create) a menu element from a spec list. Rows are rendered with the
 * exact markup the app already styles, a single delegated listener is attached
 * (idempotent per element), and lucide icons are swapped in afterwards.
 *
 * `container` reuses an existing menu element (innerHTML replaced); omit it and
 * a fresh `<div class="context-menu hidden">` is created and appended to `<body>`.
 */
export function renderContextMenuItems(
  items: ContextMenuItemSpec[],
  opts: { container?: HTMLElement; id?: string; onAction?: (spec: ContextMenuItemSpec, el: HTMLElement, ev: MouseEvent) => void } = {},
): HTMLElement {
  const menu =
    opts.container ??
    (() => {
      const m = document.createElement("div");
      m.className = "context-menu hidden";
      if (opts.id) m.id = opts.id;
      document.body.appendChild(m);
      return m;
    })();

  if (opts.id && !opts.container) menu.id = opts.id;
  menu.innerHTML = items.map(contextMenuItemHtml).join("");

  const rows = Array.from(menu.querySelectorAll<HTMLElement>(".context-menu-item"));
  let i = 0;
  for (const spec of items) {
    if (spec.divider) continue;
    const el = rows[i++];
    if (el) _rowSpecs.set(el, spec);
  }

  attachMenuClick(menu, opts.onAction);
  swapIcons(menu);
  return menu;
}

/** Render rows from a name -> callback map keyed by `data-action` (or `id`). */
export function renderMenu(
  items: ContextMenuItemSpec[],
  actions: Record<string, (el: HTMLElement, ev: MouseEvent) => void> = {},
  opts: { container?: HTMLElement; id?: string } = {},
): HTMLElement {
  return renderContextMenuItems(items, {
    ...opts,
    onAction: (spec, el, ev) => {
      const key = spec.action || spec.id;
      if (key && actions[key]) actions[key](el, ev);
    },
  });
}

/** Mark one container as the right-click target, clearing any previous one. */
export function setContextTarget(target: HTMLElement | null | undefined): void {
  clearContextTargets();
  _targetEl = target ?? null;
  if (!_targetEl) return;
  ensureOverlay();
  // Cache the corner radius once per target so the follow loop never has to
  // read computed styles per frame.
  _radius = getComputedStyle(_targetEl).borderRadius || "";
  paintOverlay();
  if (!_followRaf) _followRaf = requestAnimationFrame(followTarget);
}

/** Remove the target highlight and stop tracking the target. */
export function clearContextTargets(): void {
  _targetEl = null;
  _radius = "";
  if (_followRaf) {
    cancelAnimationFrame(_followRaf);
    _followRaf = 0;
  }
  _overlayEl?.classList.remove("visible");
  // Defensive sweep: earlier builds marked the target with a class on the
  // element itself. Drop any that are still lying around so no stale
  // highlight survives the switch to the overlay.
  document.querySelectorAll<HTMLElement>(".context-target").forEach((el) => {
    el.classList.remove("context-target");
  });
}

// ── Dropdown sizing helpers (shared by settings dropdowns) ───────────────────

export const DROPDOWN_ITEM_H = 34;

/** Cap a scrollable dropdown to a max number of visible rows. */
export function capDropdownHeight(el: HTMLElement, maxRows = 8): void {
  el.style.setProperty("--dd-max-h", `${maxRows * DROPDOWN_ITEM_H + 8}px`);
}
