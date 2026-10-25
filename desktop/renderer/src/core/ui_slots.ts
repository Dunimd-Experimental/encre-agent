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
 * Generic UI extension points ("slots").
 *
 * Any first-party render site can expose a slot by calling
 * {@link renderSlot} with a fallback closure; a plugin may then take over
 * that fragment through the declarative `ui_override` contribution
 * (`{ slot, template }`), e.g. replacing the model-provider list row.
 *
 * Isolation guarantees:
 *   - Templates are pure data pushed over the unified `plugin_ui_sync`
 *     channel; `{{token}}` placeholders are substituted with HTML-escaped
 *     values only, so plugin markup can never inject scripts.
 *   - {@link renderSlot} guards every override: a throwing template falls
 *     back to the first-party markup, so one broken plugin can never take
 *     down the host UI.
 *   - Overrides must reuse the first-party CSS classes for the slot (that is
 *     how they keep the app's look); they are plain fragments, not apps.
 *     Plugins write the stable `ea-ui-*` aliases (see ui/ea_ui.ts), which
 *     are resolved to the first-party classes once at compile time.
 *
 * To make a new surface pluggable, wrap its existing markup in
 * `renderSlot("<slot-id>", ctx, () => existingMarkup)` — that is all.
 */

import { compilePluginHtml, escapePluginAttr } from "./plugin_html.js";
import { guardHtml } from "./ui_boundary.js";

/** HTML-escapes untrusted text (same rules as the settings views). */
export function escSlotValue(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** A compiled plugin template: ctx values -> escaped markup fragment. */
export type SlotTemplate = (ctx: Record<string, unknown>) => string;

interface SlotOverride {
  plugin: string;
  render: SlotTemplate;
  /** Higher wins; ties keep the first registration. */
  priority: number;
}

/** slot id -> overrides in registration order (last wins). */
const _overrides = new Map<string, SlotOverride[]>();

/** slot id -> dynamic overrides contributed by the plugin JS runtime. */
const _dynamic = new Map<string, { plugin: string; get: () => string; priority: number }>();

const TOKEN_RE = /\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}/g;

/**
 * Compiles a plugin-supplied template into a render function.  Only
 * `{{key}}` placeholders are substituted (HTML-escaped); everything else in
 * the template is emitted verbatim, so a plugin customizes structure and
 * classes while the renderer stays in control of escaping.
 */
export function compileSlotTemplate(template: string): SlotTemplate {
  // Token substitution cannot introduce new aliases afterwards: ctx values
  // are HTML-escaped, so no attribute boundary can be crossed by data.  The
  // final fragment is sanitized and alias-translated at render time by
  // {@link renderSlot}, so every override goes through the same pipeline.
  const source = String(template || "");
  return (ctx) => {
    let out = source;
    // Materialize the token list up front: substitution cannot introduce new
    // tokens because values are escaped (no braces survive escSlotValue).
    const tokens = source.match(TOKEN_RE) || [];
    for (const tok of tokens) {
      const key = tok.slice(2, -2).trim();
      const raw = ctx[key];
      const value =
        typeof raw === "string" || typeof raw === "number"
          ? escSlotValue(String(raw))
          : "";
      out = out.split(tok).join(value);
    }
    return out;
  };
}

/** Wrap one override fragment in its own removable, plugin-tagged host.
 *  `display:contents` keeps the fragment in the slot's layout flow while the
 *  wrapper still gives teardown and fault reporting a single element. */
function wrapSlotFragment(plugin: string, slot: string, html: string): string {
  return `<span class="ea-plugin-slot" data-ea-plugin="${escapePluginAttr(plugin)}" data-ea-slot="${escapePluginAttr(slot)}" style="display:contents">${html}</span>`;
}

/** Registers an override for `slot`; returns an undo closure. */
export function registerSlotOverride(slot: string, plugin: string, render: SlotTemplate, priority = 0): () => void {
  if (!slot || !plugin || typeof render !== "function") return () => {};
  const list = _overrides.get(slot) ?? [];
  list.push({ plugin, render, priority: Number.isFinite(priority) ? priority : 0 });
  _overrides.set(slot, list);
  return () => {
    const cur = _overrides.get(slot);
    if (!cur) return;
    const idx = cur.findIndex((o) => o.plugin === plugin && o.render === render);
    if (idx >= 0) cur.splice(idx, 1);
    if (cur.length === 0) _overrides.delete(slot);
  };
}

/** Drops every override contributed by `plugin` (hot reload / disable). */
export function clearPluginSlotOverrides(plugin: string): void {
  for (const [slot, list] of _overrides) {
    const kept = list.filter((o) => o.plugin !== plugin);
    if (kept.length === 0) _overrides.delete(slot);
    else _overrides.set(slot, kept);
  }
}

/** Removes every override from every slot (full resync). */
export function clearAllSlotOverrides(): void {
  _overrides.clear();
  _dynamic.clear();
}

/**
 * Registers (or replaces) the dynamic override for `slot` contributed by the
 * plugin JS runtime (`Encre.ui.setSlot`).  Dynamic overrides take precedence
 * over declarative templates and are cleared with the rest on resync.
 */
export function setDynamicSlotOverride(slot: string, plugin: string, get: () => string, priority = 0): void {
  if (!slot || !plugin || typeof get !== "function") return;
  const prev = _dynamic.get(slot);
  const next = Number.isFinite(priority) ? priority : 0;
  // First claim wins on a tie; a higher-priority plugin may take over.
  if (prev && (prev.plugin !== plugin && prev.priority >= next)) return;
  _dynamic.set(slot, { plugin, get, priority: next });
}

/** Drops dynamic overrides contributed by `plugin` (worker teardown). */
export function clearDynamicSlotOverrides(plugin: string): void {
  for (const [slot, entry] of _dynamic) {
    if (entry.plugin === plugin) _dynamic.delete(slot);
  }
}

/**
 * Renders `slot`.  A plugin JS runtime override wins over a declarative
 * template; both are guarded — with no override (or a throwing one) the
 * first-party `fallback` markup is used, so the host UI can never be broken
 * by a plugin.
 */
export function renderSlot(
  slot: string,
  ctx: Record<string, unknown>,
  fallback: () => string,
): string {
  const list = _overrides.get(slot) ?? [];
  let declared: SlotOverride | null = null;
  for (const ov of list) {
    if (!declared || ov.priority > declared.priority) declared = ov;
  }
  const dyn = _dynamic.get(slot);
  const useDynamic = dyn && (!declared || dyn.priority >= declared.priority);
  if (useDynamic) {
    const html = guardHtml(
      "plugin-slot",
      `${dyn.plugin}:${slot}`,
      () => {
        const raw = dyn.get();
        if (typeof raw !== "string") return "";
        return wrapSlotFragment(dyn.plugin, slot, compilePluginHtml(raw, { stripIds: true }));
      },
      "",
    );
    if (html) return html;
    // A dynamic override that threw or rendered nothing falls through to the
    // declarative override / first-party markup rather than showing a hole.
  }
  if (!declared) return fallback();
  return guardHtml(
    "plugin-slot",
    `${declared.plugin}:${slot}`,
    () => {
      const raw = declared.render(ctx);
      if (typeof raw !== "string") return fallback();
      return wrapSlotFragment(declared.plugin, slot, compilePluginHtml(raw, { stripIds: true }));
    },
    fallback(),
  );
}
