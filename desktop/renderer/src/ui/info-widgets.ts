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
 * Info widgets — read-only status fragments plugins can drop into their own
 * markup.  Unlike the interactive controls these are pure HTML-string
 * factories (same shape as `contextMenuItemHtml` / `progressBar`): the caller
 * embeds the returned fragment wherever it wants and re-renders on state
 * changes.  Styling rides the theme variables only, so the fragments follow
 * the app's light/dark theme without any new CSS rules.
 *
 * Plugin-facing aliases (see ui/ea_ui.ts `EA_UI_RPC`):
 *   eaui.call("info-widget.chip", spec)  -> one inline status pill
 *   eaui.call("info-widget.card", spec)  -> one titled summary card
 */

/** Semantic tone of an info fragment; only the dot/icon takes the hue. */
export type InfoTone = "neutral" | "ok" | "warn" | "danger" | "info";

/** Tone hues (dot fills); text keeps the theme's own colors. */
const TONE_HUE: Record<InfoTone, string> = {
  neutral: "var(--text-secondary)",
  ok: "#34c77b",
  warn: "#f5a623",
  danger: "#ff5d5d",
  info: "#4f9eff",
};

function esc(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toneOf(raw: unknown): InfoTone {
  return raw === "ok" || raw === "warn" || raw === "danger" || raw === "info" ? raw : "neutral";
}

function iconHtml(icon: unknown, size: number): string {
  // Lucide glyph names are plain identifiers; escape anyway so a crafted
  // name can never break out of the attribute.
  if (typeof icon !== "string" || !icon || !/^[a-z0-9-]+$/i.test(icon)) return "";
  return `<i data-lucide="${esc(icon)}" class="lucide" style="width:${size}px;height:${size}px;flex:none"></i>`;
}

export interface InfoChipSpec {
  /** What this pill reports (e.g. "backend pings"). */
  label: string;
  /** Current value shown after the label; omitted renders label-only. */
  value?: string;
  /** Optional lucide icon name in front of the label. */
  icon?: string;
  /** Semantic tone; defaults to "neutral". */
  tone?: InfoTone;
}

/** One inline status pill: tone dot + optional icon + label + value. */
export function infoChipHtml(spec: InfoChipSpec): string {
  if (typeof spec !== "object" || spec === null) throw new Error("eaui: info chip spec must be an object");
  if (typeof spec.label !== "string" || !spec.label) throw new Error("eaui: info chip needs a 'label' string");
  if (spec.value !== undefined && typeof spec.value !== "string" && typeof spec.value !== "number" && typeof spec.value !== "boolean") {
    throw new Error("eaui: info chip 'value' must be a scalar");
  }
  const tone = toneOf(spec.tone);
  const value = spec.value === undefined || spec.value === "" ? "" : `<span style="color:var(--text-primary);font-weight:600">${esc(String(spec.value))}</span>`;
  return `<span style="display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:8px;background:var(--bg-secondary);border:1px solid var(--border-light);color:var(--text-secondary);font-size:12px;line-height:1.3;white-space:nowrap">`
    + `<span style="width:7px;height:7px;border-radius:50%;background:${TONE_HUE[tone]};flex:none"></span>`
    + iconHtml(spec.icon, 13)
    + `<span>${esc(spec.label)}</span>`
    + value
    + `</span>`;
}

export interface InfoCardSpec {
  /** Card heading. */
  title: string;
  /** Optional plain-text detail rows under the heading. */
  lines?: string[];
  /** Optional lucide icon name before the title. */
  icon?: string;
  /** Semantic tone; defaults to "neutral". */
  tone?: InfoTone;
}

/** One titled summary card: tone dot + optional icon + title + detail rows. */
export function infoCardHtml(spec: InfoCardSpec): string {
  if (typeof spec !== "object" || spec === null) throw new Error("eaui: info card spec must be an object");
  if (typeof spec.title !== "string" || !spec.title) throw new Error("eaui: info card needs a 'title' string");
  if (spec.lines !== undefined && !Array.isArray(spec.lines)) throw new Error("eaui: info card 'lines' must be an array");
  const tone = toneOf(spec.tone);
  const rows = (Array.isArray(spec.lines) ? spec.lines : [])
    .map((line) => `<div style="font-size:12px;color:var(--text-secondary);line-height:1.4">${esc(String(line))}</div>`)
    .join("");
  return `<div style="display:flex;flex-direction:column;gap:6px;padding:12px 14px;border-radius:10px;background:var(--bg-secondary);border:1px solid var(--border-light)">`
    + `<div style="display:flex;align-items:center;gap:8px;color:var(--text-primary);font-size:13px;font-weight:600">`
    + `<span style="width:7px;height:7px;border-radius:50%;background:${TONE_HUE[tone]};flex:none"></span>`
    + iconHtml(spec.icon, 14)
    + `<span>${esc(spec.title)}</span>`
    + `</div>`
    + rows
    + `</div>`;
}