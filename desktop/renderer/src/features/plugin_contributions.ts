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
 */

/**
 * Plugin UI contributions.
 *
 * The backend pushes every plugin's declarative UI contributions inside the
 * `plugin_ui_sync` event (`contributions` array).  Unlike `ui_providers`
 * (arbitrary iframe mounts), a contribution reuses an existing first-party
 * surface by declaring data only — the renderer keeps full control of the
 * markup, so a plugin can never break a surface's look:
 *
 *   kind "theme"        -> { css }                     injected as a <style>
 *   kind "settings"     -> declarative rows rendered either as a section in
 *                          the General page or as a dedicated nav page
 *   kind "context_menu" -> items surfaced through the shared context menu
 *   kind "search_mode"  -> extra search modes surfaced in the search view
 *
 * Values for settings rows are persisted through the normal `saveSetting`
 * path under `plugin.<plugin>.<key>`, so they ride the exact same storage
 * and sync as first-party settings.
 */

import { getState, showToast } from "../core/state.js";
import { send } from "../core/ws.js";
import type { PluginUiResult } from "../core/types.js";
import type { ContextMenuItemSpec } from "../ui/context-menu.js";
import { pluginIconHtml } from "../ui/plugin_assets.js";
import { compileSlotTemplate, registerSlotOverride, clearAllSlotOverrides } from "../core/ui_slots.js";
import { reportUiFault } from "../core/ui_boundary.js";
import { sanitizeThemeCss } from "../core/plugin_html.js";
import type { PluginContributionKind } from "../plugin_sdk/contract.js";
import { syncPluginRuntime, stopPluginRuntime, type PluginJsModule } from "../core/plugin_runtime.js";
import { renderColorPickerImpl, bindColorPickerImpl } from "../ui/color-picker.js";
import { renderDropdownImpl, bindDropdownImpl } from "../ui/dropdown.js";

/** One declarative settings row a plugin may contribute. */
export interface PluginSettingRow {
  /** Row-local id; combined with the plugin name into the storage key. */
  id: string;
  type: "toggle" | "select" | "text" | "number" | "textarea" | "color" | "button";
  label: string;
  desc?: string;
  /** Storage key suffix; defaults to the row id. */
  key?: string;
  /** Options for `select` rows. */
  options?: { id: string; label: string }[];
  /** Fallback when nothing was saved yet. */
  default?: string | boolean;
  placeholder?: string;
  /** `number` rows: optional numeric bounds / step. */
  min?: number;
  max?: number;
  step?: number;
  /** `textarea` rows: visible line count (default 3). */
  rows?: number;
  /** `button` rows: action id re-dispatched through the unified plugin
   *  channel (`plugin_ui_action`, kind "settings"); defaults to the row id. */
  action?: string;
  /** `button` rows: button caption; defaults to the row label. */
  button_label?: string;
}

/** `kind: "settings"` spec: a section in General or a dedicated page. */
export interface PluginSettingsSpec {
  page: "general" | "new";
  /** Required for `page: "new"`: stable panel id (prefixed `plugin-`). */
  page_id?: string;
  title?: string;
  icon?: string;
  rows: PluginSettingRow[];
}

/** `kind: "context_menu"` spec. */
export interface PluginContextMenuSpec {
  /** Which surface the item belongs to; empty means every list surface. */
  surface?: string;
  label: string;
  icon?: string;
  /** Render the row in the destructive (red) style. */
  danger?: boolean;
  /** Payload re-dispatched as an `encre-plugin-menu-action` DOM event. */
  action?: string;
}

/** `kind: "search_mode"` spec. */
export interface PluginSearchModeSpec {
  id: string;
  label: string;
  description?: string;
  /** Optional Lucide icon shown on the mode chip / result section. */
  icon?: string;
}

/** `kind: "theme"` spec. */
export interface PluginThemeSpec {
  css: string;
}

/**
 * `kind: "ui_override"` spec: take over one generic UI slot declared by a
 * first-party render site (see core/ui_slots.ts).  The template reuses the
 * slot's first-party CSS classes; `{{token}}` placeholders are substituted
 * with HTML-escaped context values by the renderer.
 */
export interface PluginOverrideSpec {
  slot: string;
  template: string;
  /** Higher wins; ties keep the first registered override. */
  priority?: number;
}

/**
 * `kind: "js_module"` spec: real JavaScript executed for the plugin inside a
 * sandboxed Web Worker (see core/plugin_runtime.ts).  The module reaches UI
 * slots, its own root container and the unified action channel only through
 * the injected `Encre` capability API.
 */
export interface PluginJsModuleSpec {
  code: string;
}

export type PluginContributionSpec =
  | PluginSettingsSpec
  | PluginContextMenuSpec
  | PluginSearchModeSpec
  | PluginThemeSpec
  | PluginOverrideSpec
  | PluginJsModuleSpec;

/** One wire contribution: `{ kind, plugin, spec }`. */
export interface PluginContribution {
  kind: PluginContributionKind;
  plugin: string;
  spec: PluginContributionSpec;
}

/** DOM event fired whenever the contribution set changes. */
export const CONTRIBUTIONS_CHANGED = "encre-contributions-changed";

/** Instance methods injected by the settings view (kept as an interface to avoid a circular import). */
export interface SettingsHelpers {
  saveSetting: (key: string, value: string) => void;
}

let _contribs: PluginContribution[] = [];
const _pluginPages = new Set<string>();

function settingsValue(plugin: string, row: PluginSettingRow): string {
  const key = `plugin.${plugin}.${row.key || row.id}`;
  const stored = (getState().settings as Record<string, unknown>)[key];
  if (stored !== undefined && stored !== null) return String(stored);
  return row.default === undefined ? "" : String(row.default);
}

function esc(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Theme injection ─────────────────────────────────────────────────────────

function applyThemeStyles(next: PluginContribution[]): void {
  document.querySelectorAll("style[data-plugin-theme]").forEach((el) => el.remove());
  for (const c of next) {
    if (c.kind !== "theme") continue;
    const spec = c.spec as PluginThemeSpec;
    const css = sanitizeThemeCss(spec?.css);
    if (!css) continue;
    const style = document.createElement("style");
    style.setAttribute("data-plugin-theme", c.plugin);
    style.textContent = css;
    document.head.appendChild(style);
  }
}

// ── Settings rows rendering (shared by General section and dedicated pages) ─

/** Renders one declarative row with the same markup as first-party settings. */
function renderRow(plugin: string, row: PluginSettingRow, helpers: SettingsHelpers): string {
  const key = `plugin.${plugin}.${row.key || row.id}`;
  const value = settingsValue(plugin, row);
  const info = `<div class="settings-item-info">
      <div class="settings-item-title">${esc(row.label)}</div>
      ${row.desc ? `<div class="settings-item-desc">${esc(row.desc)}</div>` : ""}
    </div>`;
  let control = "";
  if (row.type === "toggle") {
    const checked = value === "true" || value === "1";
    control = `<label class="toggle-switch">
        <input type="checkbox" data-plugin-setting="${esc(key)}" ${checked ? "checked" : ""} />
        <span class="toggle-slider"></span>
      </label>`;
  } else if (row.type === "select" && row.options?.length) {
    const did = `dd-plugin-${esc(plugin)}-${esc(row.key || row.id)}`;
    // The unified dropdown markup; the storage key rides the wrap so bindRows
    // can wire it (same pattern as the color rows).
    control = renderDropdownImpl.call(null, did, row.options, value, () => {}, { "plugin-dropdown-key": key });
  } else if (row.type === "number") {
    const bounds = [
      row.min !== undefined ? `min="${row.min}"` : "",
      row.max !== undefined ? `max="${row.max}"` : "",
      row.step !== undefined ? `step="${row.step}"` : "",
    ].filter(Boolean).join(" ");
    control = `<input type="number" class="settings-text-input" data-plugin-setting="${esc(key)}" value="${esc(value)}" ${bounds} placeholder="${esc(row.placeholder || "")}" />`;
  } else if (row.type === "textarea") {
    control = `<textarea class="settings-text-input settings-textarea" rows="${row.rows ?? 3}" data-plugin-setting="${esc(key)}" placeholder="${esc(row.placeholder || "")}">${esc(value)}</textarea>`;
  } else if (row.type === "color") {
    const cid = `cp-plugin-${esc(plugin)}-${esc(row.key || row.id)}`;
    // The unified color picker markup; the storage key rides the wrap so
    // bindRows can wire it (same pattern as the dropdown rows).
    control = renderColorPickerImpl.call(null, cid, value || "#4f9eff", () => {}, { "plugin-color-key": key });
  } else if (row.type === "button") {
    control = `<button type="button" class="btn btn-sm" data-plugin-action="${esc(row.action || row.id)}" data-plugin-name="${esc(plugin)}">${esc(row.button_label || row.label)}</button>`;
  } else {
    control = `<input type="text" class="settings-text-input" data-plugin-setting="${esc(key)}" value="${esc(value)}" placeholder="${esc(row.placeholder || "")}" />`;
  }
  return `<div class="settings-item-row">${info}<div class="settings-item-control">${control}</div></div>`;
}

/** Binds change/dropdown events for rows rendered by {@link renderRowsHtml}. */
function bindRows(root: HTMLElement, helpers: SettingsHelpers): void {
  root.querySelectorAll<HTMLElement>("[data-plugin-setting]").forEach((el) => {
    const key = el.getAttribute("data-plugin-setting") || "";
    try {
      if (el instanceof HTMLInputElement && el.type === "checkbox") {
        el.addEventListener("change", () => helpers.saveSetting(key, el.checked ? "true" : "false"));
      } else {
        el.addEventListener("change", () => helpers.saveSetting(key, (el as HTMLInputElement | HTMLTextAreaElement).value));
      }
    } catch (e) {
      reportUiFault("plugin-settings", key || "row", e);
    }
  });
  // Button rows: clicks ride the unified plugin action channel.
  root.querySelectorAll<HTMLButtonElement>("button[data-plugin-action]").forEach((btn) => {
    try {
      btn.addEventListener("click", () => {
        sendPluginUiAction("settings", btn.dataset.pluginName || "", btn.dataset.pluginAction || "run", {});
      });
    } catch (e) {
      reportUiFault("plugin-settings", btn.dataset.pluginName || "action", e);
    }
  });
  // Dropdown rows: the unified picker wrap carries the storage key.
  root.querySelectorAll<HTMLElement>("[data-plugin-dropdown-key]").forEach((wrap) => {
    const key = wrap.getAttribute("data-plugin-dropdown-key") || "";
    const id = wrap.id;
    if (!key || !id) return;
    try {
      bindDropdownImpl(id.replace(/-wrap$/, ""), (v) => helpers.saveSetting(key, v));
    } catch (e) {
      reportUiFault("plugin-settings", key, e);
    }
  });
  // Color rows: the unified picker wrap carries the storage key.
  root.querySelectorAll<HTMLElement>("[data-plugin-color-key]").forEach((wrap) => {
    const key = wrap.getAttribute("data-plugin-color-key") || "";
    const id = wrap.id;
    if (!key || !id) return;
    try {
      bindColorPickerImpl(id.replace(/-wrap$/, ""), (v) => helpers.saveSetting(key, v));
    } catch (e) {
      reportUiFault("plugin-settings", key, e);
    }
  });
}

/** Renders all rows of one plugin section, followed by dividers. */
export function renderRowsHtml(plugin: string, rows: PluginSettingRow[], helpers: SettingsHelpers): string {
  const rendered = (rows || []).map((row) => {
    try {
      return renderRow(plugin, row, helpers);
    } catch (e) {
      reportUiFault("plugin-settings", `${plugin}:${row?.id || "row"}`, e);
      return "";
    }
  }).filter(Boolean);
  return `<div class="ea-plugin-settings-rows" data-ea-plugin="${esc(plugin)}">${rendered.join('\n<div class="settings-item-divider"></div>\n')}</div>`;
}

// ── Dedicated pages (page: "new") ───────────────────────────────────────────

function ensurePluginPage(c: PluginContribution, helpers: SettingsHelpers): void {
  const spec = c.spec as PluginSettingsSpec;
  const rawId = (spec.page_id || c.plugin).replace(/[^a-z0-9_-]/gi, "");
  const panelId = `plugin-${rawId}`;
  const nav = document.querySelector(".sidebar-settings-items");
  let panel = document.getElementById(`panel-${panelId}`);
  if (!nav) return;
  if (!panel) {
    const item = document.createElement("button");
    item.className = "settings-nav-item";
    item.setAttribute("data-panel", panelId);
    item.dataset.eaPlugin = c.plugin;
    item.innerHTML = `${pluginIconHtml(spec.icon, c.plugin)}<span>${esc(spec.title || c.plugin)}</span>`;
    nav.appendChild(item);
    const anchor = document.getElementById("panel-general");
    panel = document.createElement("div");
    panel.className = "settings-panel";
    panel.id = `panel-${panelId}`;
    panel.dataset.eaPlugin = c.plugin;
    anchor?.parentElement?.appendChild(panel);
  }
  _pluginPages.add(c.plugin);
  const card = document.createElement("div");
  card.className = "settings-card";
  card.dataset.eaPlugin = c.plugin;
  try {
    card.innerHTML = renderRowsHtml(c.plugin, spec.rows, helpers);
  } catch (e) {
    reportUiFault("plugin-settings", `${c.plugin}:page`, e);
    card.innerHTML = `<div class="settings-item-row"><div class="settings-item-info"><div class="settings-item-title">${esc(spec.title || c.plugin)}</div><div class="settings-item-desc">This plugin's settings could not be rendered.</div></div></div>`;
  }
  panel.innerHTML = `<div class="settings-section-title">${pluginIconHtml(spec.icon, c.plugin, { extraClass: "section-title-icon" })} ${esc(spec.title || c.plugin)}</div>`;
  panel.appendChild(card);
  bindRows(card, helpers);
}

/**
 * Mounts every `page: "new"` settings contribution as a dedicated nav entry +
 * panel, reusing the settings view's own dropdown components.  Idempotent per
 * plugin; called by the settings view after construction and again whenever
 * the contribution set changes.
 */
export function mountPluginSettingPages(helpers: SettingsHelpers): void {
  for (const c of _contribs) {
    if (c.kind !== "settings") continue;
    const spec = c.spec as PluginSettingsSpec;
    if (spec.page !== "new" || !spec.rows?.length) continue;
    try {
      ensurePluginPage(c, helpers);
    } catch (e) {
      // One broken contribution must not block the settings view.
      reportUiFault("plugin-settings", c.plugin, e);
    }
  }
}

/** Remove dedicated settings pages whose plugin no longer contributes one. */
function prunePluginPages(next: PluginContribution[]): void {
  const live = new Set<string>();
  for (const c of next) {
    if (c.kind !== "settings") continue;
    const spec = c.spec as PluginSettingsSpec;
    if (spec.page === "new" && spec.rows?.length) live.add(c.plugin);
  }
  for (const plugin of Array.from(_pluginPages)) {
    if (live.has(plugin)) continue;
    document.querySelectorAll<HTMLElement>(`.settings-nav-item[data-ea-plugin="${CSS.escape(plugin)}"]`).forEach((el) => el.remove());
    document.querySelectorAll<HTMLElement>(`.settings-panel[data-ea-plugin="${CSS.escape(plugin)}"]`).forEach((el) => el.remove());
    _pluginPages.delete(plugin);
  }
}

/** Renders `page: "general"` sections; returns their HTML (for the General page). */
export function generalSectionsHtml(helpers: SettingsHelpers): string {
  const parts: string[] = [];
  for (const c of _contribs) {
    if (c.kind !== "settings") continue;
    const spec = c.spec as PluginSettingsSpec;
    if (spec.page !== "general" || !spec.rows?.length) continue;
    // One malformed plugin section is omitted; the rest of General renders.
    try {
      parts.push(
        `<div class="settings-section-title">${pluginIconHtml(spec.icon, c.plugin, { extraClass: "section-title-icon" })} ${esc(spec.title || c.plugin)}</div>` +
        `<div class="settings-card" data-plugin-settings-card="${esc(c.plugin)}" data-ea-plugin="${esc(c.plugin)}">${renderRowsHtml(c.plugin, spec.rows, helpers)}</div>`,
      );
    } catch (e) {
      reportUiFault("plugin-settings", c.plugin, e);
    }
  }
  return parts.join("\n");
}

/** Binds the plugin sections previously injected into the General page. */
export function bindGeneralSections(container: HTMLElement, helpers: SettingsHelpers): void {
  container.querySelectorAll<HTMLElement>("[data-plugin-settings-card]").forEach((card) => bindRows(card, helpers));
}

// ── Context menu / search mode queries ─────────────────────────────────────

/** Context-menu items contributed for `surface` (empty = all surfaces). */
export function pluginContextMenuItems(surface: string): { plugin: string; label: string; icon?: string; danger?: boolean; action?: string }[] {
  const out: { plugin: string; label: string; icon?: string; danger?: boolean; action?: string }[] = [];
  for (const c of _contribs) {
    if (c.kind !== "context_menu") continue;
    const spec = c.spec as PluginContextMenuSpec;
    if (spec.surface && spec.surface !== surface) continue;
    out.push({ plugin: c.plugin, label: spec.label, icon: spec.icon, danger: spec.danger, action: spec.action });
  }
  return out;
}

/**
 * THE single splice point for plugin context-menu items.  Every surface that
 * shows a context menu must append the result of this helper (nothing else may
 * hand-build plugin rows), so contributed items are defined exactly once:
 * escaping, icon fallback, divider placement and the unified
 * `plugin_ui_action` round-trip all live here.
 *
 * `payload` carries the surface-local context (target ids etc.) re-dispatched
 * to the plugin's backend handler together with the surface name.
 */
export function pluginMenuSpecs(surface: string, payload: Record<string, unknown> = {}): ContextMenuItemSpec[] {
  const items = pluginContextMenuItems(surface);
  if (items.length === 0) return [];
  return [
    { divider: true },
    ...items.map((item) => ({
      id: `ctx-plugin-${item.plugin}-${item.action || "run"}`,
      icon: item.icon,
      label: esc(item.label),
      danger: item.danger === true,
      onClick: () => sendPluginUiAction("context_menu", item.plugin, item.action || "run", { surface, ...payload }),
    })),
  ];
}

/** Search modes contributed by plugins. */
export function pluginSearchModes(): { plugin: string; id: string; label: string; description?: string; icon?: string }[] {
  const out: { plugin: string; id: string; label: string; description?: string; icon?: string }[] = [];
  for (const c of _contribs) {
    if (c.kind !== "search_mode") continue;
    const spec = c.spec as PluginSearchModeSpec;
    out.push({ plugin: c.plugin, id: spec.id, label: spec.label, description: spec.description, icon: spec.icon });
  }
  return out;
}

// ── Action round-trip (unified client↔server plugin messaging) ─────────────

/**
 * Sends a plugin UI action to the backend through the unified client
 * message channel.  The backend dispatches it to the plugin's registered
 * WS handler (`encre.ws_handlers.register("plugin_ui_action", ...)`), so
 * contributed UI is fully operational — not merely visual.
 */
export function sendPluginUiAction(kind: string, plugin: string, action: string, payload?: Record<string, unknown>): void {
  try {
    send({ type: "plugin_ui_action", kind, plugin, action, payload });
  } catch {
    // Not connected — the action is dropped silently; UI must not break.
  }
  // Local surfaces (iframe bridges) can also observe the interaction.
  window.dispatchEvent(new CustomEvent("encre-plugin-ui-action", { detail: { kind, plugin, action, payload } }));
}

/** Handler for `plugin_ui_result` events pushed by the backend. */
export type PluginUiResultHandler = (result: PluginUiResult) => void;

// Settings-button feedback: every successful plugin_ui_result carrying a
// `data.message` surfaces as a toast, so action rows (probe.ping etc.)
// visibly round-trip instead of appearing dead.  Installed once, lazily.
let _resultToastsBound = false;
function bindPluginResultToasts(): void {
  if (_resultToastsBound) return;
  _resultToastsBound = true;
  onPluginUiResult((result) => {
    if (!result.ok) return;
    const data = result.data as Record<string, unknown> | undefined;
    const msg = data?.message;
    if (typeof msg === "string" && msg) showToast(result.plugin, msg);
  });
}

const _uiResultHandlers = new Set<PluginUiResultHandler>();

if (typeof window !== "undefined") {
  window.addEventListener("encre-plugin-ui-result", (event) => {
    const detail = (event as CustomEvent).detail as PluginUiResult;
    if (!detail) return;
    for (const handler of _uiResultHandlers) {
      try {
        handler(detail);
      } catch (e) {
        // A faulty handler must not break the dispatch loop.
        reportUiFault("plugin-ui-result", detail.plugin || "handler", e);
      }
    }
  });
}

/** Subscribes to backend results for plugin UI actions; returns unsubscribe. */
export function onPluginUiResult(handler: PluginUiResultHandler): () => void {
  _uiResultHandlers.add(handler);
  return () => {
    _uiResultHandlers.delete(handler);
  };
}

// ── UI slot overrides ───────────────────────────────────────────────────────

/**
 * Re-registers every `ui_override` contribution.  Called on each full resync;
 * the registry is cleared first, so a plugin reload can never leave stale
 * templates behind.  A malformed contribution is skipped (and logged via
 * console) — never fatal for the sync itself.
 */
function applySlotOverrides(next: PluginContribution[]): void {
  clearAllSlotOverrides();
  for (const c of next) {
    if (c.kind !== "ui_override") continue;
    const spec = c.spec as PluginOverrideSpec;
    if (!spec || typeof spec.slot !== "string" || !spec.slot || typeof spec.template !== "string" || !spec.template) continue;
    if (spec.template.length > 65536) continue;
    try {
      registerSlotOverride(
        spec.slot,
        c.plugin,
        compileSlotTemplate(spec.template),
        Number.isFinite(Number(spec.priority)) ? Number(spec.priority) : 0,
      );
    } catch {
      // One broken override must not block the rest of the sync.
    }
  }
}

// ── Sync entry points ───────────────────────────────────────────────────────

/**
 * Replaces the contribution set. Themes are re-applied immediately; every
 * other kind is re-read on demand by its surface, which is notified through
 * the {@link CONTRIBUTIONS_CHANGED} DOM event.
 */
export function syncPluginContributions(list: PluginContribution[]): void {
  const next = (list || []).filter(
    (c) => c && typeof c.plugin === "string" && c.plugin && typeof c.kind === "string" && c.spec && typeof c.spec === "object",
  );
  _contribs = next;
  bindPluginResultToasts();
  try {
    prunePluginPages(next);
  } catch (e) {
    reportUiFault("plugin-settings", "prune-pages", e);
  }
  // Every step is individually guarded: a malformed contribution set (or a
  // throwing subsystem) can never break the sync pipeline or the surfaces
  // that listen on CONTRIBUTIONS_CHANGED.
  try {
    applyThemeStyles(next);
  } catch {
    document.querySelectorAll("style[data-plugin-theme]").forEach((el) => el.remove());
  }
  try {
    applySlotOverrides(next);
  } catch {
    clearAllSlotOverrides();
  }
  // Hand `js_module` contributions to the sandboxed worker runtime; it
  // rebuilds workers itself when the module set changed.
  try {
    const modules: PluginJsModule[] = [];
    for (const c of next) {
      if (c.kind !== "js_module") continue;
      const code = (c.spec as { code?: unknown }).code;
      if (typeof code === "string" && code) modules.push({ plugin: c.plugin, code });
    }
    syncPluginRuntime(modules);
  } catch {
    stopPluginRuntime();
  }
  try {
    window.dispatchEvent(new CustomEvent(CONTRIBUTIONS_CHANGED));
  } catch {
    // Listener errors are their own; dispatch itself cannot throw, but stay safe.
  }
}

/** Clears every contribution and removes injected theme styles (hot reload). */
export function resetPluginContributions(): void {
  _contribs = [];
  try {
    prunePluginPages([]);
    document.querySelectorAll("style[data-plugin-theme]").forEach((el) => el.remove());
    clearAllSlotOverrides();
    stopPluginRuntime();
  } catch {
    // Teardown is best-effort; the contribution set is already empty.
  }
  window.dispatchEvent(new CustomEvent(CONTRIBUTIONS_CHANGED));
}

/** Current contribution count (diagnostics). */
export function pluginContributionCount(): number {
  return _contribs.length;
}
