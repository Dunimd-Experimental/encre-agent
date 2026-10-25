// @ts-nocheck
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

import { getState, setSettings, openRoutedLink } from "../core/state.js";
import { send } from "../core/ws.js";
import { renderSlot } from "../core/ui_slots.js";
import { t } from "../features/i18n.js";
import { platformIconHtml } from "../ui/icons.js";
import type { ModelConfigMeta, ProviderConfigMeta, ProviderEntry } from "../core/types.js";

interface AdapterFieldDef {
  key: string;
  labelKey: string;
  type: "text" | "password" | "number" | "textarea";
  placeholderKey?: string;
}

interface AdapterDef {
  id: string;
  name: string;
  desc: string;
  fields: AdapterFieldDef[];
  docs?: string;
}



export function renderModelImpl(this: any): void {
    const st = getState();
    const providers = st.providers || [];
    const catalog = st.modelCatalog;
    const providerLabel = (p: ProviderConfigMeta): string =>
      p.backend_type === "openai_compatible"
        ? (p.name?.trim() || catalog.providers.find(x => x.id === p.backend_type)?.label || p.backend_type || "?")
        : catalog.providers.find(x => x.id === p.backend_type)?.label || p.backend_type || "?";

    let rowsHtml = "";
    for (let i = 0; i < providers.length; i++) {
      const p = providers[i];
      const modelsAll = p.models || [];
      const total = modelsAll.length;
      // Master switch: when the provider is off every model under it counts as
      // unavailable (mirrors the backend, where the whole provider is skipped).
      const providerOn = p.enabled !== false;
      const available = providerOn ? modelsAll.filter((m: any) => m.enabled !== false).length : 0;
      // Generic plugin override point ("model.provider-row"): a plugin may
      // replace the row markup declaratively; the default markup below is
      // the guarded fallback (see core/ui_slots.ts).
      rowsHtml += renderSlot(
        "model.provider-row",
        { idx: i, name: providerLabel(p), total, available, providerOn, backend: p.backend_type || "" },
        () => `
        <div class="model-table-row${providerOn ? "" : " model-table-row--off"}" data-provider-idx="${i}">
          <div class="model-cell-name">
            <div class="model-name-wrap">
              <span class="model-name-text">${this.esc(providerLabel(p))}</span>
            </div>
          </div>
          <div class="model-table-cell model-cell-provider">
            <span class="model-count${providerOn ? "" : " model-count--off"}"><span class="model-count-avail">${available}</span><span class="model-count-sep">/</span><span class="model-count-total">${total}</span></span>
          </div>
          <div class="model-table-cell model-cell-actions">
            <label class="toggle-switch toggle-sm provider-master-switch" data-tooltip="${providerOn ? t("settings.enabled") : t("settings.disabled")}">
              <input type="checkbox" class="provider-enabled-toggle" data-idx="${i}" ${providerOn ? "checked" : ""} />
              <span class="toggle-slider"></span>
            </label>
            <button class="btn-icon" data-action="edit" data-idx="${i}" data-tooltip="${t("settings.open")}">
              <i data-lucide="arrow-up-right" class="lucide"></i>
            </button>
            <button class="btn-icon btn-icon--danger" data-action="delete" data-idx="${i}" data-tooltip="${t("settings.deleteProvider")}">
              <i data-lucide="trash-2" class="lucide"></i>
            </button>
          </div>
        </div>`,
      );
    }

    const tableHtml = providers.length === 0
      ? `<div class="model-empty"><i data-lucide="cpu" class="lucide"></i><span>${t("settings.noProvidersYet")}</span></div>`
      : `
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${t("settings.provider")}</div>
            <div class="model-table-cell model-cell-provider">${t("settings.providerModelsCol")}</div>
            <div class="model-table-cell model-cell-actions">${t("settings.actions")}</div>
          </div>
          ${rowsHtml}
        </div>`;

    this.panels.model.innerHTML = `
      <div class="settings-section-title">
        <i data-lucide="cpu" class="lucide section-title-icon"></i>
        ${t("settings.providerManagement")}
        <button class="btn-icon" id="btn-model-pool-settings" data-tooltip="${t("settings.modelPoolSettings")}" style="margin-inline-start:auto">
          <i data-lucide="settings" class="lucide"></i>
        </button>
      </div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${t("settings.providerManagementDesc")}</div>
          <button class="btn-add-model-top" id="btn-goto-create-provider">
            <i data-lucide="plus" class="lucide"></i>
            <span>${t("settings.addProvider")}</span>
          </button>
        </div>
        ${tableHtml}
      </div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.model });
    }
  }

export function renderGatewayImpl(this: any): void {
    const st = getState();
    const s = st.settings;
    const gs = st.gatewayStatus;
    const adapters = gs?.adapters ?? [];

    let cardsHtml = "";
    // Connector cards are plugin-provided: each adapter entry in the gateway
    // status payload carries a declarative ``ui`` spec (fields/docs/qr) from
    // its ea-connector-* plugin.  Locale keys (adapterName*/adapterDesc*)
    // still win when present; ui.name/ui.desc are the fallback for
    // third-party connectors without locale entries.
    const localeOr = (key: string, fallback: string): string => {
      const v = t(key);
      return v && v !== key ? v : fallback;
    };
    for (const stInfo of adapters) {
      const defId = stInfo.name;
      const ui = ((stInfo as any).ui ?? {}) as {
        name?: string; desc?: string; docs?: string; qr?: boolean;
        fields?: AdapterFieldDef[];
      };
      // Only plugin connectors carry a declarative card spec.  Built-in
      // infrastructure adapters (e.g. api_server) are not IM connectors and
      // stay out of this panel entirely.
      if (!ui.name && !(ui.fields && ui.fields.length)) continue;
      const fields = ui.fields ?? [];
      const adapterNameKey = `settings.adapterName${defId.charAt(0).toUpperCase() + defId.slice(1)}`;
      const displayName = localeOr(adapterNameKey, ui.name || stInfo.platform || defId);
      const enabled = !!(s[`adapter_${defId}_enabled` as keyof typeof s]);
      const statusInfo = stInfo;
      const connected = statusInfo?.connected ?? false;

      const fieldCount = fields.length;
      const allConfigured = fields.every(f => {
        const val = s[`adapter_${defId}_${f.key}` as keyof typeof s];
        return val && String(val).length > 0;
      });

      // Unified status: only ONE state shown at a time
      let statusLabel: string;
      let statusStyle: string;
      if (!enabled) {
        statusLabel = `○ ${t("settings.adapterDisabled")}`;
        statusStyle = "color:var(--text-muted)";
      } else if (!allConfigured) {
        statusLabel = `○ ${t("settings.adapterNotConfigured")}`;
        statusStyle = "color:var(--text-muted)";
      } else if (connected) {
        statusLabel = `● ${t("settings.adapterStatusConnected")}`;
        statusStyle = "color:var(--text-success)";
      } else {
        statusLabel = `● ${t("settings.adapterStatusDisconnected")}`;
        statusStyle = "color:var(--text-warning)";
      }

      // Description shows connection error, adapter description, or config hint
      const connErr = statusInfo?.error || null;
      const descFallback = ui.desc || "";
      let descHtml: string;
      if (connErr && enabled && allConfigured) {
        descHtml = `<span style="color:var(--text-danger);font-size:11px"><i data-lucide="alert-circle" style="width:11px;height:11px;display:inline-block;vertical-align:middle;margin-right:3px"></i> ${this.esc(connErr)}</span>`;
      } else if (!enabled) {
        descHtml = `<span style="color:var(--text-muted);font-size:11px">${t("settings.adapterDisabled")} · ${this.esc(localeOr(`settings.adapterDesc${defId.charAt(0).toUpperCase() + defId.slice(1)}`, descFallback))}</span>`;
      } else if (!allConfigured) {
        descHtml = `<span style="color:var(--text-muted);font-size:11px">${this.esc(localeOr(`settings.adapterDesc${defId.charAt(0).toUpperCase() + defId.slice(1)}`, descFallback))}</span>`;
      } else {
        descHtml = `<span style="color:var(--text-muted);font-size:11px">${this.esc(localeOr(`settings.adapterDesc${defId.charAt(0).toUpperCase() + defId.slice(1)}`, descFallback))}</span>`;
      }

      const iconHtml = platformIconHtml(defId, 22, "", "margin-right:10px");

      cardsHtml += `
        <div class="settings-card" style="margin-bottom:12px">
          <div class="settings-item-row" data-adapter-toggle="${defId}">
            <div class="settings-item-info">
              <div class="settings-item-title" style="display:flex;align-items:center">
                ${iconHtml}
                 ${this.esc(displayName)}
                <span style="margin-left:8px;font-size:11px;font-weight:400">${statusLabel}</span>
              </div>
              <div class="settings-item-desc">${descHtml}</div>
            </div>
            <div class="settings-item-control" style="gap:8px">
              <label class="toggle-switch" onclick="event.stopPropagation()">
                <input type="checkbox" id="adapter-enable-${defId}" ${enabled ? "checked" : ""} />
                <span class="toggle-slider"></span>
              </label>
              <button class="btn-icon" id="adapter-expand-${defId}" data-adapter-expand="${defId}" data-tooltip="${t("settings.adapterConfig")}">
                <i data-lucide="arrow-up-right" style="width:16px;height:16px"></i>
              </button>
            </div>
          </div>
        </div>`;
    }

    this.panels.gateway.innerHTML = `
      <div class="settings-section-title"><i data-lucide="cable" class="lucide section-title-icon"></i> ${t("settings.gatewayManagement")}</div>
      ${cardsHtml}`;

    // Bind event listeners
    for (const stInfo of adapters) {
      const defId = stInfo.name;
      const def = { id: defId, docs: ((stInfo as any).ui ?? {}).docs as string | undefined };
      const enableToggle = document.getElementById(`adapter-enable-${def.id}`) as HTMLInputElement;
      if (enableToggle) {
        enableToggle.addEventListener("change", () => {
          const enabled = enableToggle.checked;
          const current = { ...getState().settings, [`adapter_${def.id}_enabled`]: enabled };
          setSettings(current as any);
          send({ type: "configure", config: { [`adapter_${def.id}_enabled`]: enabled } });
          this.renderGateway();
        });
      }

      const expandBtn = document.getElementById(`adapter-expand-${def.id}`);
      if (expandBtn) {
        expandBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          this._showAdapterConfigDialog(def.id);
        });
      }

      const docsLink = document.querySelector(`[data-adapter-docs="${def.id}"]`) as HTMLAnchorElement;
      if (docsLink) {
        docsLink.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          openRoutedLink(def.docs || "");
        });
      }
    }
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.gateway });
    }
  }
