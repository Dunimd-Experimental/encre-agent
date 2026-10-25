/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
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
 * Custom global tooltip.
 *
 * Replaces the native OS `title` tooltip for every element in the app, on
 * every platform (Windows / macOS / Linux). The native tooltip is permanently
 * suppressed by stripping the `title` attribute from ALL elements at init
 * time (storing the text in `data-encre-original-title`) and watching for
 * any new `title` attributes via MutationObserver.  Only our own floating
 * element is rendered.
 *
 * Also handles `data-i18n-title` (translation key) — looks up the current
 * locale and shows the translated text, so elements don't need a redundant
 * hardcoded `data-tooltip`.
 *
 * Styling (rounded, no fill border beyond the 1px frame) follows the design
 * system and adapts to the active theme:
 *   - background: the same colour painted behind #session-list
 *   - dark mode : 1px pure-white border, pure-white text
 *   - light mode: 1px pure-black border, pure-black text
 */

import { t } from "../features/i18n.js";

let tooltipEl: HTMLDivElement | null = null;
let currentEl: HTMLElement | null = null;
let showTimer: number | null = null;
let hideTimer: number | null = null;

const OFFSET = 8;
// Delay before the custom tooltip appears (ms).
const SHOW_DELAY = 1000;
// Native tooltip storage attribute (replaces `title` everywhere).
const TITLE_ATTR = "data-encre-original-title";
// Elements whose native tooltip we must NOT replace.
const SKIP_SELECTOR = ".window-btn, .header-window-controls";
// Marker attribute for elements carrying a structured model (see below).
const MODEL_ATTR = "data-tooltip-model";

/** One `label | value` line of a structured tooltip. */
export interface TooltipRow {
  label?: string;
  value: string;
}

/** One permission chip pinned to the bottom of a structured tooltip. */
export interface TooltipPermission {
  /** Lucide icon name, e.g. "camera". */
  icon: string;
  /** Already-translated label. */
  label: string;
  granted: boolean;
}

/**
 * Structured tooltip content. Unlike `data-tooltip` (plain text, newline
 * separated, wraps freely) this renders a fixed layout so every tab hover
 * looks identical: header, one field per line with a muted label column,
 * over-long values ellipsised, permissions always last.
 */
export interface TooltipModel {
  title: string;
  icon?: string;
  rows: TooltipRow[];
  permissions?: TooltipPermission[];
}

/**
 * Structured models live here instead of in a data attribute — a DOM
 * attribute would mean re-parsing JSON on every hover, and the model can
 * carry booleans (permission granted state) that an attribute cannot.
 */
const modelRegistry = new WeakMap<HTMLElement, TooltipModel>();

/** Attach (or clear, when `model` is null) a structured tooltip model. */
export function setTooltipModel(el: HTMLElement, model: TooltipModel | null): void {
  if (model) {
    modelRegistry.set(el, model);
    el.setAttribute(MODEL_ATTR, "1");
  } else {
    modelRegistry.delete(el);
    el.removeAttribute(MODEL_ATTR);
  }
}

function ensureTooltip(): HTMLDivElement {
  if (tooltipEl) return tooltipEl;
  const el = document.createElement("div");
  el.className = "encre-tooltip";
  el.setAttribute("role", "tooltip");
  const body = document.createElement("div");
  body.className = "encre-tooltip-body";
  el.appendChild(body);
  document.body.appendChild(el);
  tooltipEl = el;
  return el;
}

/** The content container, rebuilt on every show. */
function tipBody(tip: HTMLDivElement): HTMLDivElement {
  return tip.firstElementChild as HTMLDivElement;
}

/**
 * Only accept icon sources we can trust: remote http(s) URLs and inline
 * image data URLs. Anything else (javascript:, file:, …) is dropped.
 */
function safeIconUrl(raw: string): string {
  const url = raw.trim();
  const lower = url.toLowerCase();
  if (
    lower.startsWith("http://") ||
    lower.startsWith("https://") ||
    lower.startsWith("data:image/")
  ) {
    return url;
  }
  return "";
}

/** Small favicon / file-type image; removes itself when it 404s. */
function makeIcon(url: string, className: string): HTMLImageElement | null {
  const src = safeIconUrl(url);
  if (!src) return null;
  const img = document.createElement("img");
  img.className = className;
  img.alt = "";
  img.decoding = "async";
  img.addEventListener("error", () => img.remove());
  img.src = src;
  return img;
}

/** Plain text (may contain newlines) plus an optional leading icon. */
function renderText(text: string, icon?: string): void {
  const tip = ensureTooltip();
  const body = tipBody(tip);
  tip.className = "encre-tooltip";
  // Clear the structured variant's inline geometry — the one-liner sizes
  // itself from content again.
  tip.style.width = "";
  tip.style.maxWidth = "";
  body.className = "encre-tooltip-body";
  body.textContent = "";
  const img = icon ? makeIcon(icon, "encre-tooltip-icon") : null;
  if (img) body.appendChild(img);
  const span = document.createElement("span");
  span.className = "encre-tooltip-text";
  span.textContent = text;
  body.appendChild(span);
  if (img) tip.classList.add("encre-tooltip--rich");
}

/**
 * JS-side truncation, one mechanism for every line of the panel.
 *
 * The CSS `text-overflow: ellipsis` chain stays in place, but it leans on
 * several rules cooperating; when any of them loses the race the line either
 * hard-clips with no glyph or smears past the panel. This measures the real
 * rendered box and binary-searches the longest prefix that fits, appending
 * the same "…" the CSS would — so title and URL always cut identically no
 * matter what the stylesheet ends up doing.
 */
function hardEllipsis(el: HTMLElement, text: string): void {
  el.textContent = text;
  // +1px tolerance for sub-pixel rounding between scrollWidth/clientWidth.
  if (el.scrollWidth <= el.clientWidth + 1) return;
  const chars = Array.from(text);
  let lo = 1;
  let hi = chars.length;
  let best = 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    el.textContent = chars.slice(0, mid).join("") + "…";
    if (el.scrollWidth <= el.clientWidth + 1) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  el.textContent = chars.slice(0, best).join("") + "…";
}

/**
 * Structured layout. Everything is flush left, values never wrap (an
 * ellipsis takes over instead) and the permission chips always close the
 * tooltip — so two tab hovers are always the same shape.
 */
function renderModel(model: TooltipModel): void {
  const tip = ensureTooltip();
  const body = tipBody(tip);
  tip.className = "encre-tooltip encre-tooltip--model";
  // Inline geometry: the fixed panel size must not depend on a stylesheet
  // rule surviving the cascade — this IS the authoritative size.
  tip.style.width = "340px";
  tip.style.maxWidth = "min(340px, calc(100vw - 16px))";
  body.className = "encre-tooltip-body encre-tooltip-body--stack";
  body.style.cssText = "display:flex;flex-direction:column;gap:6px;width:100%;min-width:0;";
  body.textContent = "";

  // ── header: icon + title ─────────────────────────────────────────
  // The measurement-critical boxes carry their layout inline: even with a
  // fully dead stylesheet every line still has a real box, one shared left
  // inset (icon 14px + gap 6px) and one shared right edge — so the JS cut
  // below lands on the same vertical line for title and URL.
  const head = document.createElement("div");
  head.className = "tt-head";
  head.style.cssText =
    "display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden;";
  const headIcon = model.icon ? makeIcon(model.icon, "tt-head-icon") : null;
  if (headIcon) {
    headIcon.style.cssText = "width:14px;height:14px;flex-shrink:0;";
    head.appendChild(headIcon);
  }
  const title = document.createElement("span");
  title.className = "tt-title";
  title.style.cssText =
    "display:block;flex:1;min-width:0;overflow:hidden;white-space:nowrap;font-weight:600;";
  head.appendChild(title);
  body.appendChild(head);
  hardEllipsis(title, model.title);

  // ── fields: one `label | value` line each, columns aligned ───────
  if (model.rows.length) {
    const grid = document.createElement("div");
    grid.className = "tt-rows";
    grid.style.cssText = "display:grid;grid-template-columns:auto minmax(0,1fr);column-gap:10px;row-gap:1px;";
    for (const row of model.rows) {
      const line = document.createElement("div");
      line.className = "tt-row";
      line.style.cssText = "display:contents;";
      if (row.label) {
        const label = document.createElement("span");
        label.className = "tt-label";
        label.style.cssText = "white-space:nowrap;opacity:0.55;";
        label.textContent = row.label;
        line.appendChild(label);
      }
      const value = document.createElement("span");
      // No label → the value owns the whole row (URLs, absolute paths) and
      // starts 20px in, exactly where the header title's text starts.
      value.className = row.label ? "tt-value" : "tt-value tt-value--full";
      value.style.cssText = row.label
        ? "display:block;min-width:0;overflow:hidden;white-space:nowrap;"
        : "display:block;grid-column:1/-1;min-width:0;overflow:hidden;white-space:nowrap;padding-inline-start:20px;";
      line.appendChild(value);
      grid.appendChild(line);
      hardEllipsis(value, row.value);
    }
    body.appendChild(grid);
  }

  // ── permissions: always the last block ───────────────────────────
  // Icon-only chips: the icon alone identifies the permission (camera,
  // location, mic…); a text caption next to every icon is noise.
  if (model.permissions && model.permissions.length) {
    const perms = document.createElement("div");
    perms.className = "tt-perms";
    for (const perm of model.permissions) {
      const chip = document.createElement("span");
      chip.className = "tt-perm" + (perm.granted ? "" : " tt-perm--denied");
      const icon = document.createElement("i");
      icon.setAttribute("data-lucide", perm.icon);
      icon.className = "lucide";
      chip.appendChild(icon);
      perms.appendChild(chip);
    }
    body.appendChild(perms);
    const lucide = (window as any).lucide;
    if (lucide && typeof lucide.createIcons === "function") {
      try { lucide.createIcons({ root: body }); } catch {}
    }
  }
}

function resolvePaintedBackground(start: HTMLElement | null): string {
  let node: HTMLElement | null = start;
  while (node) {
    const bg = getComputedStyle(node).backgroundColor;
    if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") return bg;
    node = node.parentElement;
  }
  return "var(--bg-secondary)";
}

function syncBackground(): void {
  const el = ensureTooltip();
  el.style.background = resolvePaintedBackground(
    document.getElementById("session-list"),
  );
}

function position(el: HTMLElement): void {
  const tip = tooltipEl!;
  const rect = el.getBoundingClientRect();
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;

  let left = rect.left + rect.width / 2 - tw / 2;
  let top = rect.bottom + OFFSET;

  if (top + th > window.innerHeight - 4) {
    top = rect.top - th - OFFSET;
  }

  left = Math.max(4, Math.min(left, window.innerWidth - tw - 4));
  top = Math.max(4, Math.min(top, window.innerHeight - th - 4));

  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function positionAt(x: number, y: number): void {
  const tip = tooltipEl!;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;

  let left = x - tw / 2;
  let top = y + OFFSET;

  if (top + th > window.innerHeight - 4) {
    top = y - th - OFFSET;
  }

  left = Math.max(4, Math.min(left, window.innerWidth - tw - 4));
  top = Math.max(4, Math.min(top, window.innerHeight - th - 4));

  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function show(el: HTMLElement, info: { text: string; icon?: string; model?: TooltipModel }): void {
  const tip = ensureTooltip();
  if (info.model) renderModel(info.model);
  else renderText(info.text, info.icon);
  if (currentEl === el && tip.classList.contains("encre-tooltip--visible")) {
    position(el);
    return;
  }
  syncBackground();
  position(el);
  tip.classList.add("encre-tooltip--visible");
}

function restoreAndHide(): void {
  if (showTimer) {
    clearTimeout(showTimer);
    showTimer = null;
  }
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
  if (currentEl) {
    // Never restore `title` — that would let the native OS tooltip
    // reappear.  The text stays in `data-encre-original-title` if
    // that was the original source.
    delete currentEl.dataset.encreTooltip;
    delete currentEl.dataset.encreTooltipSrc;
    currentEl = null;
  }
  if (tooltipEl) tooltipEl.classList.remove("encre-tooltip--visible");
}

function getTooltipText(
  el: HTMLElement,
): { attr: string; text: string; icon?: string; model?: TooltipModel } | null {
  // Optional leading icon (favicon, file-type icon) shown beside the text.
  const icon = el.dataset.tooltipIcon || undefined;
  // 0. Structured model (tabs) — richest, so it wins over everything.
  const model = modelRegistry.get(el);
  if (model) {
    return { attr: MODEL_ATTR, text: model.title, icon: model.icon, model };
  }
  // 1. Check data-i18n-title (translation key) — dynamic i18n, overrides everything.
  const i18nKey = el.getAttribute("data-i18n-title");
  if (i18nKey) {
    return { attr: "data-i18n-title", text: t(i18nKey).trim(), icon };
  }
  // 2. Check custom tooltip (data-tooltip). Newlines are honoured (pre-wrap).
  if (el.dataset.tooltip != null && el.dataset.tooltip !== "") {
    return { attr: "data-tooltip", text: el.dataset.tooltip.trim(), icon };
  }
  // 3. Check stored native tooltip (data-encre-original-title).
  const stored = el.getAttribute(TITLE_ATTR);
  if (stored) {
    return { attr: TITLE_ATTR, text: stored.trim(), icon };
  }
  return null;
}

function activate(el: HTMLElement): void {
  const info = getTooltipText(el);
  if (!info) return;
  if (!info.model && !info.text) return;
  currentEl = el;
  if (showTimer) clearTimeout(showTimer);
  showTimer = window.setTimeout(() => {
    showTimer = null;
    if (currentEl === el) show(el, info!);
  }, SHOW_DELAY);
}

function handleOver(e: Event): void {
  const target = e.target as HTMLElement | null;
  if (!target) return;
  const el = target.closest<HTMLElement>(
    `[data-tooltip], [data-i18n-title], [${MODEL_ATTR}], [${TITLE_ATTR}]`,
  );
  if (!el || el.closest(SKIP_SELECTOR)) return;
  // Same element already active — nothing to do.
  if (currentEl === el) return;
  // Different element — cancel pending timers, hide tooltip, reactivate.
  if (showTimer) clearTimeout(showTimer);
  if (hideTimer) clearTimeout(hideTimer);
  showTimer = null;
  hideTimer = null;
  if (tooltipEl) tooltipEl.classList.remove("encre-tooltip--visible");
  currentEl = null;
  activate(el);
}

function handleOut(e: Event): void {
  if (!currentEl) return;
  const related = (e as MouseEvent).relatedTarget as Node | null;
  if (related && currentEl.contains(related)) return;
  restoreAndHide();
}

function handleFocus(e: FocusEvent): void {
  if (currentEl) return;
  const target = e.target as HTMLElement | null;
  if (!target) return;
  const el = target.closest<HTMLElement>(
    `[data-tooltip], [data-i18n-title], [${MODEL_ATTR}], [${TITLE_ATTR}]`,
  );
  if (!el || el.closest(SKIP_SELECTOR)) return;
  activate(el);
}

function handleBlur(e: FocusEvent): void {
  if (!currentEl) return;
  const related = e.relatedTarget as Node | null;
  if (related && currentEl.contains(related)) return;
  restoreAndHide();
}

/**
 * Strip `title` from `el`, storing the original value in
 * `data-encre-original-title` so the custom tooltip can still use it.
 */
function stripTitle(el: Element): void {
  const existing = el.getAttribute(TITLE_ATTR);
  const t = el.getAttribute("title");
  if (t && !existing) {
    el.setAttribute(TITLE_ATTR, t);
  }
  el.removeAttribute("title");
}

export function initTooltip(): void {
  ensureTooltip();
  syncBackground();

  // ─────────────────────────────────────────────────────────────────
  // 1. Strip `title` from every existing element so the native OS
  //    tooltip can never fire on any platform.
  // ─────────────────────────────────────────────────────────────────
  document.querySelectorAll("[title]").forEach(stripTitle);

  // ─────────────────────────────────────────────────────────────────
  // 2. Watch for dynamically added `title` attributes and strip them
  //    immediately, before the browser has a chance to render the
  //    native tooltip.
  // ─────────────────────────────────────────────────────────────────
  const titleObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "attributes" && mutation.attributeName === "title") {
        stripTitle(mutation.target as Element);
      }
    }
  });
  titleObserver.observe(document.body, {
    subtree: true,
    attributes: true,
    attributeFilter: ["title"],
  });

  // ─────────────────────────────────────────────────────────────────
  // 3. Capture-phase handlers — also strip `title` on every mouseover
  //    as a safety net in case the MutationObserver misses something
  //    (e.g. a title set by a third-party script).
  // ─────────────────────────────────────────────────────────────────
  document.addEventListener(
    "mouseover",
    (e) => {
      const target = e.target as HTMLElement | null;
      if (target?.hasAttribute("title")) stripTitle(target);
    },
    true,
  );

  document.addEventListener("mouseover", handleOver, true);
  document.addEventListener("mouseout", handleOut, true);
  document.addEventListener("focusin", handleFocus, true);
  document.addEventListener("focusout", handleBlur, true);

  window.addEventListener(
    "scroll",
    () => {
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = window.setTimeout(restoreAndHide, 0);
    },
    true,
  );
  window.addEventListener("blur", restoreAndHide);
  document.addEventListener("pointerdown", restoreAndHide, true);

  const themeObserver = new MutationObserver(() => syncBackground());
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
}

/**
 * Show the custom tooltip at an arbitrary viewport position with custom text.
 * Used by canvas-based widgets (e.g. the Chart.js usage chart) that have no
 * single DOM element to attach `data-tooltip` to. Bypasses the hover delay.
 */
export function showTooltipAt(text: string, x: number, y: number): void {
  if (!text) return;
  const tip = ensureTooltip();
  if (showTimer) {
    clearTimeout(showTimer);
    showTimer = null;
  }
  // Detach from any element-driven tooltip so we don't fight the scheduler.
  if (currentEl) {
    delete currentEl.dataset.encreTooltip;
    delete currentEl.dataset.encreTooltipSrc;
    currentEl = null;
  }
  renderText(text);
  syncBackground();
  positionAt(x, y);
  tip.classList.add("encre-tooltip--visible");
}

/** Hide a tooltip previously shown via {@link showTooltipAt}. */
export function hideTooltip(): void {
  if (tooltipEl) tooltipEl.classList.remove("encre-tooltip--visible");
}
