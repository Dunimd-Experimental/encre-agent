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
 * Pending panel — the floating card above the composer.
 *
 * One card, many sources.  A source is anything that can pile up while a turn
 * is running: the submit queue today, background shells or scheduled runs
 * later.  The panel owns only the chrome — the tab strip along the card's
 * bottom edge, the status line beside it, the row list and drag reorder — and
 * every source supplies its own icon, name, status text, rows and row actions.
 * Adding a source never means touching this file.
 *
 * The card shows one source at a time; a tab appears per source that currently
 * has rows, so the strip grows on its own the moment a second source starts
 * reporting work.
 */

import type { AppState } from "../core/types.js";
import { subscribe } from "../core/state.js";
import { t, onLocaleChange } from "./i18n.js";

/** One button on the right edge of a row. */
export interface PendingRowAction {
  /** lucide icon name, e.g. "arrow-up". */
  icon: string;
  /** Style variant → `pending-row-{variant}` class (e.g. "guide", "send",
   *  "remove").  Omit for the neutral look. */
  variant?: "guide" | "send" | "remove";
  tooltip: string;
  run(index: number): void;
}

/** One row of the active source. */
export interface PendingRow {
  text: string;
  /** Full text shown on hover; defaults to `text`. */
  tooltip?: string;
  /** Extra cache entropy for state a row does not display (e.g. its mode). */
  key?: string;
  actions?: PendingRowAction[];
}

export interface PendingSource {
  id: string;
  /** lucide icon name for this source's tab. */
  icon: string;
  /** Icon shown at the leading edge of each row; defaults to `icon`.
   *  An empty string hides the leading icon entirely. */
  rowIcon?: string;
  /** Tab tooltip — what the source is called. */
  name(): string;
  /** Status line beside the tab strip; `count` is this source's row count. */
  status(count: number): string;
  /** Live read of the entries to list. Empty ⇒ the source loses its tab. */
  rows(): PendingRow[];
  /** AppState fields whose mutation should repaint this source's slice. */
  watch?: readonly (keyof AppState)[];
  /** Present ⇒ rows carry a drag handle and the new order lands here. */
  reorder?(from: number, to: number): void;
}

const COLLAPSE_KEY = "encre.pendingPanel.collapsed";

const sources: PendingSource[] = [];
let activeId = "";
let renderKey = "";
/** Rows of the last paint — actions are closures, so they cannot live in HTML. */
let paintedRows: PendingRow[] = [];
/** Folded list, remembered across hides and session switches. */
let collapsed = (() => {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
})();

function esc(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Register a source.  Safe to call more than once for the same id (the second
 * call is ignored), so a re-run of the boot path cannot duplicate tabs.
 */
export function registerPendingSource(source: PendingSource): void {
  if (sources.some((s) => s.id === source.id)) return;
  sources.push(source);
  if (source.watch && source.watch.length) subscribe(refreshPendingPanel, source.watch);
  refreshPendingPanel();
}

/** Repaint the panel from the registered sources' current state. */
export function refreshPendingPanel(): void {
  const card = document.getElementById("pending-panel");
  const body = document.getElementById("pending-panel-body");
  const tabs = document.getElementById("pending-panel-tabs");
  const status = document.getElementById("pending-panel-status");
  if (!card || !body || !tabs || !status) return;

  const live = sources.filter((s) => s.rows().length > 0);
  if (!live.length) {
    if (!card.classList.contains("hidden")) {
      card.classList.add("hidden");
      renderKey = "";
    }
    return;
  }

  if (!live.some((s) => s.id === activeId)) activeId = live[0].id;
  const active = live.find((s) => s.id === activeId)!;
  const rows = active.rows();
  card.classList.remove("hidden");
  card.classList.toggle("pending-collapsed", collapsed);
  document.getElementById("pending-panel-toggle")?.setAttribute("aria-expanded", String(!collapsed));

  // Streaming fires a render on every token update.  Rebuilding the DOM (and
  // re-creating lucide icons) only when the panel's visible content actually
  // changed is what keeps the card from flickering and stops a repaint from
  // destroying the drag source mid-drag.
  const key = [
    live.map((s) => s.id).join("\u0001"),
    activeId,
    rows.map((r) => `${r.key || ""}\u0002${r.text}\u0002${(r.actions || []).map((a) => `${a.icon}\u0003${a.tooltip}`).join("\u0003")}`).join("\u0001"),
  ].join("\u0000");
  if (key === renderKey) return;
  renderKey = key;
  paintedRows = rows;

  tabs.innerHTML = live
    .map(
      (s) =>
        `<button type="button" class="pending-tab${s.id === activeId ? " active" : ""}" data-pending-tab="${esc(s.id)}" title="${esc(s.name())}"><i data-lucide="${esc(s.icon)}" class="lucide"></i></button>`,
    )
    .join("");
  tabs.querySelectorAll<HTMLElement>("[data-pending-tab]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.dataset.pendingTab || "";
      if (id === activeId) return;
      activeId = id;
      renderKey = "";
      refreshPendingPanel();
    });
  });

  status.textContent = active.status(rows.length);
  bindCollapseToggle(card);

  const reorderable = typeof active.reorder === "function";
  const dragTitle = t("chat.dragToReorder");
  const rowIcon = active.rowIcon === undefined ? active.icon : active.rowIcon;
  body.innerHTML = rows
    .map(
      (r, i) =>
        `<div class="pending-row" data-pending-index="${i}">
        ${reorderable ? `<span class="pending-row-drag" draggable="true" data-pending-index="${i}" title="${esc(dragTitle)}"><i data-lucide="grip-vertical" class="lucide"></i></span>` : ""}
        ${rowIcon ? `<span class="pending-row-source" title="${esc(active.name())}"><i data-lucide="${esc(rowIcon)}" class="lucide"></i></span>` : ""}
        <span class="pending-row-text" title="${esc(r.tooltip || r.text)}">${esc(r.text)}</span>
        <span class="pending-row-actions">${(r.actions || [])
          .map(
            (a, ai) =>
              `<button type="button" class="pending-row-action" draggable="false" data-pending-action="${ai}" title="${esc(a.tooltip)}"><i data-lucide="${esc(a.icon)}" class="lucide"></i></button>`,
          )
          .join("")}</span>
      </div>`,
    )
    .join("");

  body.querySelectorAll<HTMLElement>("[data-pending-action]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const rowIdx = parseInt(btn.closest<HTMLElement>(".pending-row")?.dataset.pendingIndex || "0", 10);
      const action = paintedRows[rowIdx]?.actions?.[parseInt(btn.dataset.pendingAction || "0", 10)];
      if (action) action.run(rowIdx);
    });
  });

  if (reorderable) {
    dragSource = active;
    bindRowDragStarts(body);
    bindContainerDrag(body);
  } else {
    dragSource = null;
  }

  const lucide = (window as any).lucide;
  if (lucide) lucide.createIcons(card);
}

/**
 * The fold.  The toggle sits at the trailing edge of the foot strip and is part
 * of index.html, so its listener goes on once; the fold itself is pure CSS —
 * `height: 0 ↔ auto` under the app-wide `interpolate-size`, the same motion the
 * tool-call strips use.
 */
function bindCollapseToggle(card: HTMLElement): void {
  const btn = document.getElementById("pending-panel-toggle");
  if (!btn || btn.dataset.pendingBound === "1") return;
  btn.dataset.pendingBound = "1";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    collapsed = !collapsed;
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      /* storage unavailable */
    }
    card.classList.toggle("pending-collapsed", collapsed);
    btn.setAttribute("aria-expanded", String(!collapsed));
  });
}

/**
 * Drag-and-drop reorder.  Row handles are recreated on every repaint, so their
 * listeners die with the rows — but the container element is permanent, so its
 * dragover/drop bindings are installed once and read the active source from
 * module scope.  Re-binding them per repaint would stack a fresh set of
 * handlers every time the list changed and fire one reorder per handler on the
 * next drop.
 */
let dragSource: PendingSource | null = null;
let dragBody: HTMLElement | null = null;
let dragSrcIdx: number | null = null;
let dropTargetIdx: number | null = null;

function clearIndicator(body: HTMLElement): void {
  body.querySelectorAll(".pending-row").forEach((el) => el.classList.remove("drop-target"));
  dropTargetIdx = null;
}

function bindRowDragStarts(body: HTMLElement): void {
  body.querySelectorAll<HTMLElement>(".pending-row-drag").forEach((handle) => {
    handle.addEventListener("dragstart", (e) => {
      dragSrcIdx = parseInt(handle.dataset.pendingIndex || "0", 10);
      const dt = (e as DragEvent).dataTransfer;
      if (!dt) return;
      dt.setData("text/plain", String(dragSrcIdx));
      dt.effectAllowed = "move";
      // The browser needs a snapshot of the handle to render as the drag
      // preview; without this the ghost image is the whole row (buttons and
      // all), which reads as "we're dragging a card".
      const svg = handle.querySelector("svg.lucide");
      if (svg) {
        try {
          dt.setDragImage(svg, 8, 14);
        } catch {
          /* noop */
        }
      }
    });
    handle.addEventListener("dragend", () => {
      body.classList.remove("pending-dragging");
      clearIndicator(body);
      dragSrcIdx = null;
    });
  });
}

/**
 * Drop targeting is driven by the pointer's Y relative to each row, so any
 * point inside a row is a valid drop target: landing on the top half of row N
 * means "move to N-1", the bottom half "move to N".
 */
function bindContainerDrag(body: HTMLElement): void {
  if (dragBody === body) return;
  dragBody = body;

  body.addEventListener("dragover", (e) => {
    if (dragSrcIdx === null) return;
    e.preventDefault();
    const dt = (e as DragEvent).dataTransfer;
    if (dt) dt.dropEffect = "move";
    body.classList.add("pending-dragging");
    const items = Array.from(body.querySelectorAll<HTMLElement>(".pending-row"));
    const targetIdx = items.findIndex((it) => {
      const r = it.getBoundingClientRect();
      return e.clientY >= r.top && e.clientY < r.bottom;
    });
    if (targetIdx !== dropTargetIdx) {
      clearIndicator(body);
      if (targetIdx !== -1 && targetIdx !== dragSrcIdx) {
        items[targetIdx].classList.add("drop-target");
        dropTargetIdx = targetIdx;
      }
    }
  });

  body.addEventListener("dragleave", (e) => {
    // Only clear when the pointer actually left the container bounds;
    // leaving into a child row should not strip the highlight.
    if (!body.contains((e as DragEvent).relatedTarget as Node | null)) clearIndicator(body);
  });

  body.addEventListener("drop", (e) => {
    e.preventDefault();
    const src = dragSrcIdx;
    const tgt = dropTargetIdx;
    clearIndicator(body);
    body.classList.remove("pending-dragging");
    if (src !== null && tgt !== null && src !== tgt) {
      dragSource?.reorder?.(src, tgt);
      // Defer the rebuild so the drag source isn't destroyed mid-drop, which
      // can leave the renderer's drag state stuck and the prompt input
      // un-focusable afterwards.
      requestAnimationFrame(refreshPendingPanel);
    }
    dragSrcIdx = null;
  });
}

/**
 * Forget the selection and hide the card (session switch).  The next
 * `queued_submits`-style snapshot re-seeds the sources and the panel reappears
 * on the first source that has rows.
 */
export function resetPendingPanel(): void {
  activeId = "";
  renderKey = "";
  paintedRows = [];
  dragSource = null;
  dragSrcIdx = null;
  dropTargetIdx = null;
  const card = document.getElementById("pending-panel");
  if (card) card.classList.add("hidden");
  for (const id of ["pending-panel-body", "pending-panel-tabs"]) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = "";
  }
  const status = document.getElementById("pending-panel-status");
  if (status) status.textContent = "";
}

// Labels come from the locale table, so a language switch must repaint even
// though no row content changed.
onLocaleChange(() => {
  renderKey = "";
  refreshPendingPanel();
});
