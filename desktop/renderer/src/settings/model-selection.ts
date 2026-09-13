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
 * Reusable "target model selection" widget (frontend half).
 *
 * A target-model selection is a list of model ids chosen to restrict which
 * configured models may serve a given feature (e.g. a gateway adapter's
 * allowed models in Settings -> Gateway).  This module is the UI + value
 * handling counterpart of ``encre.model_selection`` on the backend:
 *
 * * :func:`renderTargetModelSelection` -- build the selection card HTML
 *   (a toggle switch that reveals a multi-select list of enabled models).
 * * :func:`bindTargetModelSelection` -- wire up the toggle + click handlers
 *   after the HTML is inserted into the DOM.
 * * :func:`readTargetModelSelection` -- collect the currently selected
 *   model ids from the DOM.
 * * :func:`parseTargetModelSelection` / :func:`serializeTargetModelSelection`
 *   -- convert between the JSON string stored in settings (``adapter_<id>_models``)
 *   and the ``string[]`` in memory.
 *
 * The module is framework-agnostic and has no dependency on the settings
 * page: pass in the configured models, the initially selected ids and a
 * translation function, and it renders / binds / reads itself.
 */

import type { ModelConfigMeta } from "../core/types.js";

export interface TargetModelSelectionOptions {
  /** Unique id suffix for DOM ids (e.g. a gateway adapter id). */
  id: string;
  /** All configured models; enabled ones are shown as selectable items. */
  models: ModelConfigMeta[];
  /** Model ids selected when the widget renders. */
  selected: string[];
  /**
   * Explicitly force the enable toggle on/off.  When omitted the toggle
   * state is derived from ``selected`` (on when at least one model is
   * selected).  Use this to persist an "enabled but nothing picked yet"
   * state (e.g. per-workspace model restriction).
   */
  checked?: boolean;
  /**
   * Global master-switch off: disables the toggle and greys out the card.
   * Clicks are ignored; the UI reflects that the pool is disabled globally.
   */
  disabled?: boolean;
  /** i18n lookup; defaults to the settings-page keys. */
  t: (key: string) => string;
  /** Optional i18n keys overriding the defaults. */
  titleKey?: string;
  hintKey?: string;
  emptyKey?: string;
}

function escapeHtml(str: string): string {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Parse a stored selection (JSON string) into a string[]; never throws. */
export function parseTargetModelSelection(raw: string | undefined | null): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

/** Serialize a selection into the JSON string persisted in settings. */
export function serializeTargetModelSelection(ids: string[]): string {
  return JSON.stringify(ids);
}

/** Build the selection card HTML. Insert the result, then call bindTargetModelSelection. */
export function renderTargetModelSelection(opts: TargetModelSelectionOptions): string {
  const { id } = opts;
  const t = opts.t;
  const titleKey = opts.titleKey ?? "settings.adapterTargetModel";
  const hintKey = opts.hintKey ?? "settings.adapterTargetModelHint";
  const emptyKey = opts.emptyKey ?? "settings.noModelConfigured";

  const title = t(titleKey);
  const hint = t(hintKey);
  const disabled = !!opts.disabled;
  const enabledModels = (opts.models ?? []).filter(m => m.enabled !== false);
  const selected = opts.selected ?? [];
  const on = opts.checked !== undefined ? !!opts.checked : selected.length > 0;

  const listHtml = enabledModels.length === 0
    ? `<span style="color:var(--text-muted);font-size:13px">${t(emptyKey)}</span>`
    : enabledModels.map(m => {
        const isSel = selected.includes(m.model_id);
        return `<div class="auto-push-gw-item${isSel ? " selected" : ""} ${disabled ? "disabled" : ""}" data-model-id="${escapeHtml(m.model_id)}">
              <span class="auto-push-gw-name">${escapeHtml(m.name || m.model_id)}</span>
              <span class="auto-push-gw-dot" title="${escapeHtml(m.model_id)}"></span>
              <span class="auto-push-gw-check"></span>
            </div>`;
      }).join("");

  const disabledAttr = disabled ? "disabled" : "";
  const disabledClass = disabled ? " model-selection-card--disabled" : "";

  return `
      <div class="settings-card model-selection-card${disabledClass}" style="overflow:hidden">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">
              <span>${title}</span>
            </div>
            <div class="settings-item-desc">${disabled ? t("settings.modelPoolGlobalDisabled") : hint}</div>
          </div>
          <div class="settings-item-control">
            <label class="toggle-switch" title="${title}">
              <input type="checkbox" id="adapter-model-toggle-${id}" ${on ? "checked" : ""} ${disabledAttr} />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
        <div id="adapter-models-row-${id}" class="model-selection-row${on && !disabled ? " model-selection-row--open" : ""}">
          <div class="model-selection-row-inner">
            <div class="auto-push-gateways">
              <div class="auto-push-gateways-label">${title}</div>
              <div id="dlg-models-${id}" class="model-selection-list">${listHtml}</div>
            </div>
          </div>
        </div>
      </div>`;
}

/** Bind the toggle switch and model item clicks inside the dialog.
 *  Skips binding when the parent card has class model-selection-card--disabled
 *  (global pool master switch is off). */
export function bindTargetModelSelection(
  root: HTMLElement,
  id: string,
  onChange?: (selected: string[]) => void,
): void {
  const toggle = root.querySelector<HTMLInputElement>(`#adapter-model-toggle-${id}`);
  if (!toggle) return;
  const card = toggle.closest<HTMLElement>(".settings-card.model-selection-card");
  const disabled = card ? card.classList.contains("model-selection-card--disabled") : false;
  const container = root.querySelector<HTMLElement>(`#dlg-models-${id}`);
  const row = root.querySelector<HTMLElement>(`#adapter-models-row-${id}`);
  const emit = (): void => {
    if (onChange) onChange(readTargetModelSelection(root, id));
  };
  if (disabled) return;
  if (row) {
    toggle.addEventListener("change", () => {
      row.classList.toggle("model-selection-row--open", toggle.checked);
      emit();
    });
  }
  if (!container) return;
  container.querySelectorAll(".auto-push-gw-item").forEach(item => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      item.classList.toggle("selected", !item.classList.contains("selected"));
      emit();
    });
  });
}

/** Read the currently selected model ids from the DOM. */
export function readTargetModelSelection(root: HTMLElement, id: string): string[] {
  const container = root.querySelector<HTMLElement>(`#dlg-models-${id}`);
  if (!container) return [];
  return Array.from(container.querySelectorAll<HTMLElement>(".auto-push-gw-item.selected"))
    .map(item => item.getAttribute("data-model-id")!)
    .filter(Boolean);
}

/** Read whether the enable toggle is currently switched on. */
export function readTargetModelSelectionEnabled(root: HTMLElement, id: string): boolean {
  const toggle = root.querySelector<HTMLInputElement>(`#adapter-model-toggle-${id}`);
  return toggle ? toggle.checked : false;
}
