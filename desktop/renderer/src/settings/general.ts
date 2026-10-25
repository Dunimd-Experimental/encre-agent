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

import { getState, setSettings, setCustomCommands, setTheme, setThemePreference, setThemeSkin, THEME_SKINS, setPermissionPolicies, subscribe, showToast, isEnabled } from "../core/state.js";
import { send } from "../core/ws.js";
import { waitForModelValidation, onWechatScanResult, refreshAllData } from "../core/stream.js";
import { setModelConfigs, setMcpServers, setSkillsList, setSubAgents } from "../core/state.js";
import type { ModelConfigMeta, MCPServerConfig, SkillInfo, ModelCatalog, McpCatalog, McpProviderEntry, ProviderEntry, ProfileData, CustomCommand, UsageStatsSessionEntry } from "../core/types.js";
import { defaultSearchFilter } from "../core/types.js";
import { Dialog } from "../ui/dialog.js";
import {
  bindTargetModelSelection,
  parseTargetModelSelection,
  readTargetModelSelection,
  renderTargetModelSelection,
  serializeTargetModelSelection,
} from "./model-selection.js";
import { t, initLocale, setLocale, getLocale, getIntlLocale, clearLocaleCache, onLocaleChange, LOCALES, type Locale } from "../features/i18n.js";
import { generalSectionsHtml, bindGeneralSections } from "../features/plugin_contributions.js";
import { applyServerCommands } from "../ui/slash-commands.js";
import { renderMarkdown } from "../chat/chat.js";
import { platformIconHtml, searchEngineIconSrc } from "../ui/icons.js";
import { formatShortcut } from "../ui/shortcut-display.js";
// Chart.js is lazy-loaded on first render of the usage panel (see
// _renderUsageSection) so it no longer blocks startup parsing.
import { showTooltipAt, hideTooltip } from "../ui/tooltip.js";
import { SEARCH_ENGINES, getDefaultSearchEngine } from "../features/browser.js";
import { searchSettingsNavItems, SEARCH_FILTER_META, SETTINGS_NAV_ITEMS } from "../features/search.js";
import { setSearchFilter } from "../core/state.js";
import type { DropdownOption } from "../ui/dropdown.js";

export function modeHintImpl(this: any, modeKey: string): string {
  return `<span class="mode-hint-icon" data-tooltip="${this.esc(t(modeKey))}">
    <i data-lucide="circle-alert" class="lucide"></i>
  </span>`;
}

export function renderGeneralImpl(this: any): void {
  // The network-proxy sub-page (and any other inline editor on this panel)
  // owns the surface while open — re-rendering underneath would destroy the
  // mounted editor's bindings. Mirrors the guard the model/assist panels use.
  if (this._panelEditorActive("general")) return;
  const st = getState();
  const currentTheme = st.themePreference;
  const s = st.settings;
  const deviceEnabled = s.device_context_enabled === undefined || isEnabled(s.device_context_enabled);

  const currentLang = getLocale();
  this.updateSidebarNav();
  const currentLinkBehavior = (s.default_link_behavior as string) || "system";
  const currentSendMode = (s.shortcut_send_mode as string) || "enter";
  const currentStartupMode = (s.startup_session_mode as string) || "normal";
  const currentStartupBehavior = (s.startup_session_behavior as string) || "new";

  const themeOptions: DropdownOption[] = [
    { id: "system", label: t("theme.system") },
    { id: "light", label: t("theme.light") },
    { id: "dark", label: t("theme.dark") },
  ];

  const currentSkin = getState().themeSkin || "ink";
  const skinOptions: DropdownOption[] = (THEME_SKINS as readonly string[]).map(
    (id) => ({ id, label: t(`theme.skin_${id}`) }),
  );

  const langOptions: DropdownOption[] = LOCALES.map((c) => ({ id: c, label: t(`language.${c}`) }));

  const behaviorOptions: DropdownOption[] = [
    { id: "system", label: t("settings.systemBrowser") },
    { id: "in_app", label: t("settings.inApp") },
  ];

  const sendModes: DropdownOption[] = [
    { id: "enter", label: t("settings.enterSend") },
    { id: "ctrl_enter", label: t("settings.ctrlEnterSend") },
  ];

  const startupModes: DropdownOption[] = [
    { id: "normal", label: t("settings.normalMode") },
    { id: "iwork", label: t("settings.iworkMode") },
    { id: "automation", label: t("settings.automationMode") },
  ];

  const startupBehaviors: DropdownOption[] = [
    { id: "new", label: t("settings.startupNew") },
    { id: "last", label: t("settings.startupLast") },
  ];
  const currentBehavior = startupBehaviors.find(o => o.id === currentStartupBehavior) || startupBehaviors[0];
  const behaviorItems = startupBehaviors.map(o =>
    `<div class="settings-dropdown-item${o.id === currentStartupBehavior ? " selected" : ""}" data-value="${o.id}">${o.label}</div>`
  ).join("");

  this.panels.general.innerHTML = `
    <div class="settings-section-title"><i data-lucide="settings" class="lucide section-title-icon"></i> ${t("settings.basicSettings")}</div>
    <div class="settings-card">
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.theme")}</div>
          <div class="settings-item-desc">${t("settings.themeDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-theme", themeOptions, currentTheme, (v) => this.saveTheme(v))}
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("theme.skin")}</div>
          <div class="settings-item-desc">${t("theme.skinDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-skin", skinOptions, currentSkin, (v) => this.saveSkin(v))}
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.language")}</div>
          <div class="settings-item-desc">${t("settings.languageDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-lang", langOptions, currentLang, (v) => { this.saveSetting("language", v); this.renderGeneral(); })}
        </div>
      </div>
    </div>

    <div class="settings-section-title"><i data-lucide="pencil" class="lucide section-title-icon"></i> ${t("settings.preferences")}</div>
    <div class="settings-card">
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.shortcutSendMode")}</div>
          <div class="settings-item-desc">${t("settings.shortcutSendModeDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-send", sendModes, currentSendMode, (v) => { this.saveSetting("shortcut_send_mode", v); this.renderGeneral(); })}
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.startupSessionMode")}</div>
          <div class="settings-item-desc">${t("settings.startupSessionModeDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-startup", startupModes, currentStartupMode, (v) => { this.saveSetting("startup_session_mode", v); this.renderGeneral(); })}
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.startupSessionBehavior")}</div>
          <div class="settings-item-desc">${t("settings.startupSessionBehaviorDesc")}</div>
        </div>
        <div class="settings-item-control">
          <div class="settings-dropdown-wrap${currentStartupMode === "automation" ? " is-disabled" : ""}" id="dd-startup-behavior-wrap">
            <button class="settings-dropdown-trigger" id="dd-startup-behavior-trigger" type="button">
              <span>${currentBehavior.label}</span>
              <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
            </button>
            <div class="settings-dropdown" id="dd-startup-behavior-dropdown">${behaviorItems}</div>
          </div>
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.localLinkBehavior")}</div>
          <div class="settings-item-desc">${t("settings.localLinkBehaviorDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-link", behaviorOptions, currentLinkBehavior, (v) => { this.saveSetting("default_link_behavior", v); this.renderGeneral(); })}
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.subAgentAutoOpenTitle")}</div>
          <div class="settings-item-desc">${t("settings.subAgentAutoOpenDesc")}</div>
        </div>
        <div class="settings-item-control">
          <label class="toggle-switch">
            <input type="checkbox" id="sub-agent-auto-open-toggle" ${s.sub_agent_auto_open_view === undefined || isEnabled(s.sub_agent_auto_open_view) ? "checked" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.automationAutoOpenTitle")}</div>
          <div class="settings-item-desc">${t("settings.automationAutoOpenDesc")}</div>
        </div>
        <div class="settings-item-control">
          <label class="toggle-switch">
            <input type="checkbox" id="automation-auto-open-toggle" ${isEnabled(s.automation_auto_open_view) ? "checked" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
    </div>

    <!-- Device permissions — a single master switch, same shape as the
         automation auto-open row: ON collects every provider, OFF collects
         nothing.  No per-provider list is shown. -->
    <div class="settings-section-title"><i data-lucide="shield-half" class="lucide section-title-icon"></i> ${t("settings.devicePermissions")}</div>
    <div class="settings-card">
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.deviceContextEnabled")}</div>
          <div class="settings-item-desc">${t("settings.deviceContextEnabledDesc")}</div>
        </div>
        <div class="settings-item-control">
          <label class="toggle-switch">
            <input type="checkbox" id="device-context-enabled" ${deviceEnabled ? "checked" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
    </div>

    <!-- Service section -->
    <div class="settings-section-title"><i data-lucide="server" class="lucide section-title-icon"></i> ${t("settings.service")}</div>
    <div class="settings-card" id="service-settings-card">
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.autoStart")}</div>
          <div class="settings-item-desc">${t("settings.autoStartDesc")}</div>
        </div>
        <div class="settings-item-control">
          <label class="toggle-switch">
            <input type="checkbox" id="auto-start-checkbox" />
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
    </div>

    <!-- Network section: proxy mode for every outbound backend request -->
    <div class="settings-section-title"><i data-lucide="globe" class="lucide section-title-icon"></i> ${t("settings.networkSection")}</div>
    <div class="settings-card">
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.networkProxy")}</div>
          <div class="settings-item-desc">${t("settings.networkProxyDesc")}</div>
        </div>
        <div class="settings-item-control">
          <button class="btn-icon" id="open-network-proxy" data-tooltip="${t("settings.configure")}">
            <i data-lucide="arrow-up-right" class="lucide"></i>
          </button>
        </div>
      </div>
    </div>
    ${generalSectionsHtml(this)}`;

  // Bind dropdowns
  this.bindDropdown("dd-theme", (v) => this.saveTheme(v));
  this.bindDropdown("dd-skin", (v) => this.saveSkin(v));
  this.bindDropdown("dd-lang", (v) => { this.saveSetting("language", v); this.renderGeneral(); });
  this.bindDropdown("dd-send", (v) => { this.saveSetting("shortcut_send_mode", v); this.renderGeneral(); });
  this.bindDropdown("dd-startup", (v) => { this.saveSetting("startup_session_mode", v); this.renderGeneral(); });
  this.bindDropdown("dd-startup-behavior", (v) => { this.saveSetting("startup_session_behavior", v); this.renderGeneral(); });
  this.bindDropdown("dd-link", (v) => { this.saveSetting("default_link_behavior", v); this.renderGeneral(); });

  // Network proxy sub-page (inline panel editor, same as the assist cards).
  this.panels.general.querySelector("#open-network-proxy")?.addEventListener("click", () => this._openNetworkProxyEditor());

  // Plugin-contributed rows inside the General page (toggle/select/text).
  bindGeneralSections(this.panels.general, this);

  // Sub-agent auto-open toggle (default ON)
  document.getElementById("sub-agent-auto-open-toggle")?.addEventListener("change", (e) => {
    const checked = (e.target as HTMLInputElement).checked;
    this.saveSetting("sub_agent_auto_open_view", checked ? "true" : "false");
  });

  // Automation auto-open toggle (default OFF)
  document.getElementById("automation-auto-open-toggle")?.addEventListener("change", (e) => {
    const checked = (e.target as HTMLInputElement).checked;
    this.saveSetting("automation_auto_open_view", checked ? "true" : "false");
  });

  // ── Device permissions (single master switch) ────────────────────────
  // ON = collect every provider (backend whitelist reset to []), OFF =
  // collect nothing.  The provider whitelist is never narrowed from this
  // UI, so a previously stored list is reset to [] on either transition.
  const deviceMaster = document.getElementById("device-context-enabled") as HTMLInputElement | null;
  deviceMaster?.addEventListener("change", () => {
    const enabled = deviceMaster.checked;
    // `saveSetting` stringifies its value, which would turn the whitelist
    // into a string and silently fail the backend's list check — send the
    // real array directly and mirror both keys into local state.
    setSettings({ ...getState().settings, device_context_enabled: enabled ? "true" : "false", device_context_providers: [] });
    send({ type: "configure", config: { device_context_enabled: enabled ? "true" : "false", device_context_providers: [] } });
  });

  // Auto-start toggle
  const electronAPI = window.electronAPI;
  if (electronAPI) {
    (async () => {
      try {
        const autoStart = await electronAPI.getAutoStart();
        const checkbox = document.getElementById("auto-start-checkbox") as HTMLInputElement;
        if (checkbox) {
          checkbox.checked = autoStart;
          checkbox.addEventListener("change", async () => {
            const enabled = checkbox.checked;
            const result = await window.electronAPI!.setAutoStart(enabled);
            if (!result.success) {
              if (typeof showToast === "function") {
                showToast("Error", result.error || "", "error", "Settings");
              }
              checkbox.checked = !enabled;
            }
          });
        }
      } catch (err) {
        console.error("Failed to get auto-start setting:", err);
      }
    })();
  }

  if (typeof (window as any).lucide !== "undefined") {
    (window as any).lucide.createIcons({ root: this.panels.general });
  }
}

/** 对话流 panel: the reply-related presentation preferences moved here from
 *  常规, plus the state-machine driving budgets for plan/spec modes.  The
 *  mode rows reuse the same map/target icon set as the Plan/Goal entries in
 *  the "+" menu, and the budgets are preset dropdowns (unlimited / capped /
 *  never) instead of free-form number inputs. */
export function renderFlowImpl(this: any): void {
  if (this._panelEditorActive("flow")) return;
  const st = getState();
  const s = st.settings;
  const currentLangPref = (s.language_preference as string) || "auto";

  const langPrefOptions: DropdownOption[] = [
    { id: "auto", label: t("language.autoFollow") },
    { id: "zh", label: t("language.zh") },
    { id: "en", label: t("language.en") },
  ];

  // Preset turn budgets for the plan/spec state machine: -1 = unlimited,
  // 0 = never auto-push, a positive preset = that many pushes at most.
  // Anything unset falls back to the unlimited entry.
  const turnOptions: DropdownOption[] = [
    { id: "-1", label: t("flow.turnsUnlimited") },
    { id: "50", label: `50 ${t("flow.turnsUnit")}` },
    { id: "25", label: `25 ${t("flow.turnsUnit")}` },
    { id: "10", label: `10 ${t("flow.turnsUnit")}` },
    { id: "5", label: `5 ${t("flow.turnsUnit")}` },
    { id: "0", label: t("flow.turnsNever") },
  ];
  const turnIds = new Set(turnOptions.map((o) => o.id));
  const normTurns = (raw: unknown): string => {
    const v = String(raw ?? "").trim();
    return turnIds.has(v) ? v : "-1";
  };

  this.panels.flow.innerHTML = `
    <div class="settings-section-title"><i data-lucide="message-circle" class="lucide section-title-icon"></i> ${t("flow.replySection")}</div>
    <div class="settings-card">
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.languagePreference")}</div>
          <div class="settings-item-desc">${t("settings.languagePreferenceDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-lang-pref", langPrefOptions, currentLangPref, () => {})}
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("settings.autoExpandTitle")}</div>
          <div class="settings-item-desc">${t("settings.autoExpandDesc")}</div>
        </div>
        <div class="settings-item-control">
          <label class="toggle-switch">
            <input type="checkbox" id="auto-expand-toggle" ${isEnabled(s.auto_expand) ? "checked" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
    </div>

    <div class="settings-section-title"><i data-lucide="target" class="lucide section-title-icon"></i> ${t("flow.modeSection")}</div>
    <div class="settings-card">
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("flow.planMaxTurns")}</div>
          <div class="settings-item-desc">${t("flow.planMaxTurnsDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-plan-turns", turnOptions, normTurns(s.plan_mode_max_turns), () => {})}
        </div>
      </div>
      <div class="settings-item-divider"></div>
      <div class="settings-item-row">
        <div class="settings-item-info">
          <div class="settings-item-title">${t("flow.specMaxTurns")}</div>
          <div class="settings-item-desc">${t("flow.specMaxTurnsDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-spec-turns", turnOptions, normTurns(s.spec_mode_max_turns), () => {})}
        </div>
      </div>
    </div>`;

  this.bindDropdown("dd-lang-pref", (v) => { this.saveSetting("language_preference", v); this.renderFlow(); });

  // Turn budgets: -1 unlimited, 0 never push, a positive preset caps the
  // pushes.  The dropdown switches in place, so only the store write is
  // needed here.
  this.bindDropdown("dd-plan-turns", (v) => this.saveSetting("plan_mode_max_turns", v));
  this.bindDropdown("dd-spec-turns", (v) => this.saveSetting("spec_mode_max_turns", v));

  // Auto-expand toggle
  this.panels.flow.querySelector("#auto-expand-toggle")?.addEventListener("change", (e) => {
    const checked = (e.target as HTMLInputElement).checked;
    this.saveSetting("auto_expand", checked ? "true" : "false");
  });

  if (typeof (window as any).lucide !== "undefined") {
    (window as any).lucide.createIcons({ root: this.panels.flow });
  }
}

/** Keybind rows shared by the 快捷键 panel view and the inline editor card. */
export function shortcutsRowsHtmlImpl(this: any): string {
  const keybindsCfg = (getState().settings.keybinds as any);
  const binds: any[] = keybindsCfg?.keybinds || [];

  const categories = new Map<string, Array<{ id: string; keys: string[]; desc: string }>>();
  for (const b of binds) {
    const cat = b.category || "general";
    if (!categories.has(cat)) categories.set(cat, []);
    categories.get(cat)!.push({ id: b.id, keys: b.keys, desc: b.description || b.id });
  }

  const CAT_LABELS: Record<string, string> = {
    application: t("settings.shortcutCategoryApplication"),
    session: t("settings.shortcutCategorySession"),
    messages: t("settings.shortcutCategoryMessages"),
    input: t("settings.shortcutCategoryInput"),
    modes: t("settings.shortcutCategoryModes"),
    navigation: t("settings.shortcutCategoryNavigation"),
    search: t("settings.shortcutCategorySearch"),
    settings: t("settings.shortcutCategorySettings"),
    panels: t("settings.shortcutCategoryPanels"),
    automation: t("settings.shortcutCategoryAutomation"),
    workspace: t("settings.shortcutCategoryWorkspace"),
    notifications: t("settings.shortcutCategoryNotifications"),
    appearance: t("settings.shortcutCategoryAppearance"),
    general: t("settings.shortcutCategoryGeneral"),
  };

  let rowsHtml = "";
  for (const [cat, items] of categories) {
    rowsHtml += `<div class="shortcut-category-label">${this.esc(CAT_LABELS[cat] || cat)}</div>`;
    for (const item of items) {
      const displayKeys = item.keys && item.keys.length > 0 ? formatShortcut(item.keys[0]) : "—";
      const desc = t("shortcuts." + item.id) || item.desc;
      const isRecording = this._recordingShortcutId === item.id;
      // Live countdown label — survives panel re-renders mid-recording by
      // recomputing from the shared deadline instead of a captured value.
      const recRemain = this._recordingDeadline
        ? Math.max(1, Math.ceil((this._recordingDeadline - Date.now()) / 1000))
        : 20;
      const recUrgent = recRemain <= 5 ? " urgent" : "";
      rowsHtml += `
      <div class="settings-item-row shortcut-row" data-id="${item.id}">
        <div class="settings-item-info">
          <div class="settings-item-title">${this.esc(desc)}</div>
        </div>
        <div class="settings-item-control">
          <button class="shortcut-key-btn${isRecording ? " recording" + recUrgent : ""}" data-id="${item.id}">${isRecording ? recRemain : this.esc(displayKeys)}</button>
        </div>
      </div>`;
    }
  }

  return rowsHtml;
}

/** 个性化 (memory) panel's 快捷键 row opens this card — the same sub-page
 *  shape as the Assist panel's rows, so keybinds are configured inside a card
 *  rather than stacked inline in the panel. */
export function openShortcutsEditorImpl(this: any): void {
  const bodyHtml = `
      <div class="settings-item-desc" style="margin: 0 0 16px; padding: 0 2px;">${t("settings.shortcutsDisabledDesc")}</div>
      <div class="settings-card" id="shortcuts-editor-card">${this.shortcutsRowsHtml()}</div>`;
  const { overlay } = this._showFormDialog(t("settings.keyboardShortcuts"), bodyHtml, true);
  const footerEl = overlay.querySelector(".dialog-footer") as HTMLElement | null;
  if (footerEl) footerEl.style.display = "none";
  // The shell ships the arrow with an inline `display:none`; this card has no
  // footer, so the arrow IS its exit and must always be up.
  const panelBack = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
  if (panelBack) panelBack.style.display = "";
  this._keepPanelEditorClean(overlay);

  if (typeof (window as any).lucide !== "undefined") {
    (window as any).lucide.createIcons({ root: overlay });
  }

  const card = overlay.querySelector<HTMLElement>("#shortcuts-editor-card");
  if (!card) return;
  this._shortcutsEditorCard = card;
  this.bindShortcutsCard(card);
}

/** Repaint an open 快捷键 card in place so its recording labels stay live. */
export function refreshOpenShortcutsEditorImpl(this: any): void {
  const card = this._shortcutsEditorCard as HTMLElement | null | undefined;
  if (!card) return;
  if (!card.isConnected) {
    this._shortcutsEditorCard = null;
    return;
  }
  card.innerHTML = this.shortcutsRowsHtml();
  this.bindShortcutsCard(card);
}

export function renderShortcutsImpl(this: any): void {
  this.panels.shortcuts.innerHTML = `
    <div class="settings-section-title"><i data-lucide="keyboard" class="lucide section-title-icon"></i> ${t("settings.keyboardShortcuts")}</div>
    <div class="settings-item-desc" style="margin: -8px 0 16px; padding: 0 2px;">${t("settings.shortcutsDisabledDesc")}</div>
    <div class="settings-card" id="shortcuts-card">${this.shortcutsRowsHtml()}</div>`;

  if (typeof (window as any).lucide !== "undefined") {
    (window as any).lucide.createIcons({ root: this.panels.shortcuts });
  }

  const card = this.panels.shortcuts.querySelector<HTMLElement>("#shortcuts-card");
  if (card) this.bindShortcutsCard(card);

  // An open 快捷键 card repaints in place so its countdown stays live.
  this.refreshOpenShortcutsEditor();
}

/** Wire every key button in `card` to the recorder. */
export function bindShortcutsCardImpl(this: any, card: HTMLElement): void {
  card.querySelectorAll<HTMLButtonElement>(".shortcut-key-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.getAttribute("data-id");
      if (!id) return;

      // Fully tear down a recording already in progress — whether the user
      // clicks the same button (cancel) or a different one (switch target).
      // The old button's label must be restored here: the countdown left it
      // showing a frozen number, which read as two buttons "recording" at once.
      const wasRecording = this._recordingShortcutId;
      if (wasRecording) {
        document.removeEventListener("keydown", this._captureHandler as any, true);
        this._captureHandler = null;
        this._stopShortcutCountdown();
        this._recordingShortcutId = null;
        delete document.documentElement.dataset.shortcutRecording;
        (window as any).electronAPI?.setWinKeyCapture?.(false);
        const prevBtn = card.querySelector<HTMLButtonElement>(`.shortcut-key-btn[data-id="${wasRecording}"]`);
        if (prevBtn) {
          const prev = ((getState().settings.keybinds as any)?.keybinds || []).find((b: any) => b.id === wasRecording);
          prevBtn.textContent = prev?.keys && prev.keys.length ? formatShortcut(prev.keys[0]) : "—";
          prevBtn.classList.remove("recording", "urgent");
        }
      }

      // Clicking the already-recording button just cancels.
      if (wasRecording === id) {
        this.renderShortcuts();
        return;
      }

      this._recordingShortcutId = id;
      document.documentElement.dataset.shortcutRecording = "true";
      // Let the Win / Cmd key reach the recorder as a modifier instead of
      // being swallowed by the OS (Start menu on Windows).
      (window as any).electronAPI?.setWinKeyCapture?.(true);
      const allBtns = card.querySelectorAll<HTMLButtonElement>(".shortcut-key-btn");
      allBtns.forEach((b) => b.classList.remove("recording", "urgent"));
      btn.classList.add("recording");

      const stopRecording = () => {
        this._recordingShortcutId = null;
        this._stopShortcutCountdown();
        delete document.documentElement.dataset.shortcutRecording;
        (window as any).electronAPI?.setWinKeyCapture?.(false);
      };

      // 20-second recording window. The button ticks down the remaining
      // seconds; hitting zero cancels the recording whatever the user is
      // doing — no modifier held, no key pressed, doesn't matter.
      const deadline = Date.now() + 20_000;
      const tick = () => {
        const remain = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
        if (remain <= 0) {
          stopRecording();
          document.removeEventListener("keydown", this._captureHandler as any, true);
          this._captureHandler = null;
          this.renderShortcuts();
          return;
        }
        btn.textContent = String(remain);
        // Last 5s: flip to the danger tint (styles.css .recording.urgent).
        btn.classList.toggle("urgent", remain <= 5);
      };
      this._recordingDeadline = deadline;
      btn.textContent = "20";
      this._recordingTimer = setInterval(tick, 250);

      this._captureHandler = async (ev: KeyboardEvent) => {
        ev.preventDefault();
        ev.stopPropagation();

        if (ev.repeat) return;

        // Escape cancels recording — it must NOT be captured as a shortcut,
        // and it must NOT be eaten by the global keydown dispatcher either.
        if (ev.key === "Escape") {
          stopRecording();
          this.renderShortcuts();
          document.removeEventListener("keydown", this._captureHandler!, true);
          return;
        }

        // Ignore modifier-only key presses
        if (ev.key === "Control" || ev.key === "Meta" || ev.key === "Alt" || ev.key === "Shift") {
          return;
        }

        const parts: string[] = [];
        if (ev.ctrlKey || ev.metaKey) parts.push("ctrlcmd");
        if (ev.altKey) parts.push("alt");
        if (ev.shiftKey) parts.push("shift");

        const key = ev.key;
        const mappedKey = key === "Escape" ? "escape"
          : key === " " ? "space"
          : key === "," ? ","
          : key === "." ? "."
          : key === "`" ? "`"
          : key === "=" ? "="
          : key === "-" ? "-"
          : key === "[" ? "["
          : key === "]" ? "]"
          : key === ";" ? ";"
          : key === "'" ? "'"
          : key === "\\" ? "\\"
          : key === "/" ? "/"
          : key.toLowerCase();
        parts.push(mappedKey);
        const pattern = parts.join("+");

        stopRecording();

        const cfg = (getState().settings.keybinds as any);
        const allBinds: any[] = cfg?.keybinds ? [...cfg.keybinds] : [];
        const target = allBinds.find((b: any) => b.id === id);

        // Conflict detection — check if another shortcut already uses this key combo
        if (target) {
          const conflict = allBinds.find((b: any) => b.id !== id && b.keys && b.keys.includes(pattern));
          if (conflict) {
            const conflictName = t(`shortcuts.${conflict.id}` as any) || conflict.description || conflict.id;
            const msg = t("settings.shortcutConflict").replace("{0}", conflictName);
            const ok = await Dialog.confirm(t("settings.shortcutConflictTitle"), msg, { scope: "panel" });
            if (!ok) {
              this.renderShortcuts();
              document.removeEventListener("keydown", this._captureHandler!, true);
              return;
            }
            // Remove the conflicting key from the other shortcut
            conflict.keys = conflict.keys.filter((k: string) => k !== pattern);
          }

          const existingIdx = target.keys.indexOf(pattern);
          if (existingIdx >= 0) target.keys.splice(existingIdx, 1);
          if (target.keys.length === 0) target.keys = [pattern];
          else target.keys[0] = pattern;
        }

        const updated = { ...cfg, keybinds: allBinds };
        setSettings({ ...getState().settings, keybinds: updated });
        send({ type: "configure", config: { keybinds: updated } });
        // localStorage fallback — survive backend crypto failure across restarts
        try { localStorage.setItem("encre_keybinds", JSON.stringify(updated)); } catch { /* ignore */ }
        this.renderShortcuts();

        document.removeEventListener("keydown", this._captureHandler!, true);
      };

      document.addEventListener("keydown", this._captureHandler, true);
    });
  });
}

export function saveSettingImpl(this: any, key: string, value: string): void {
  const current = { ...getState().settings, [key]: value };
  setSettings(current);
  if (key === "language") {
    const locale = value as Locale;
    if ((LOCALES as readonly string[]).includes(locale)) {
      setLocale(locale);
      this.updateSidebarNav();
    }
  }
  send({ type: "configure", config: { [key]: value } });
}

export function saveThemeImpl(this: any, value: string): void {
  const current = { ...getState().settings, theme: value };
  setSettings(current);
  setThemePreference(value as "system" | "dark" | "light");
  // "system" resolves light: the main process pins nativeTheme.themeSource
  // to light, so the OS is never actually dark while this pref is active.
  setTheme(value === "dark" ? "dark" : "light");
  localStorage.setItem("encre-theme", value);
  send({ type: "configure", config: { theme: value } });
  this.renderGeneral();
}

/**
 * Persists and applies the colour skin.  The palette swap is a pure CSS
 * variable change ([data-skin] selects themes/<skin>.css); it applies
 * synchronously so the settings row and the palette can never disagree.
 */
export function saveSkinImpl(this: any, value: string): void {
  setThemeSkin(value);
  const current = { ...getState().settings, theme_skin: value };
  setSettings(current);
  localStorage.setItem("encre-theme-skin", value);
  send({ type: "configure", config: { theme_skin: value } });
  // Mirror the skin onto the tray popup (main process caches it for cold
  // starts; the popup repaints via the tray-data push).
  try {
    window.electronAPI?.traySkinUpdate?.(value);
  } catch {
    // Tray bridge unavailable (e.g. non-Electron host): tray keeps last skin.
  }
  this.renderGeneral();
}

/**
 * 系统网络代理 sub-page — an inline panel card (inline-editor host, general is
 * in INLINE_EDITOR_PANELS) that behaves like the model provider form.
 *
 * The footer (Cancel / Save) only exists for the custom-proxy draft: picking
 * "manual" turns the card into a draft that Save persists in one `configure`
 * and Cancel (or the shell's discard prompt when leaving settings) throws
 * away.  The other two modes have nothing to type, so each selection applies
 * at once (apply-at-once) and the footer stays hidden.
 *
 * Modes:
 *   system — inherit whatever the OS / launcher environment exposes (the
 *            backend snapshots env at boot and probes getproxies());
 *   manual — the fields below build the tunnel URL; host and port are required
 *            because a manual proxy without them cannot be dialled;
 *   direct — every managed proxy env var is cleared.
 */
export function openNetworkProxyEditorImpl(this: any): void {
  const s = getState().settings;
  const str = (key: string, fallback: string): string => {
    const v = s[key];
    return typeof v === "string" ? v : fallback;
  };
  const mode = str("network_proxy_mode", "system");
  const scheme = str("network_proxy_scheme", "http");
  const host = str("network_proxy_host", "");
  const username = str("network_proxy_username", "");
  const password = str("network_proxy_password", "");
  const bypass = str("network_proxy_bypass", "");
  let port = Number(s.network_proxy_port ?? 0);
  if (!Number.isFinite(port) || port < 0) port = 0;

  const modeOptions: DropdownOption[] = [
    { id: "system", label: t("settings.proxyModeSystem") },
    { id: "manual", label: t("settings.proxyModeManual") },
    { id: "direct", label: t("settings.proxyModeDirect") },
  ];
  const schemeOptions: DropdownOption[] = [
    { id: "http", label: "HTTP" },
    { id: "https", label: "HTTPS" },
    { id: "socks5", label: "SOCKS5" },
  ];

  const manualHidden = mode !== "manual" ? "display:none" : "";
  // Same field idiom as the model provider form: one `.model-form-row` per
  // field, label on top (`.model-form-label`), control filling the row, and
  // the hint for a non-obvious field parked behind a `?` (`model-form-help-btn`)
  // instead of a second line of body text under the label.
  const help = (hintKey: string): string => `
        <button type="button" class="model-form-help-btn" data-tooltip="${this.esc(t(hintKey))}" tabindex="-1">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
            <path d="M12 17h.01"/>
          </svg>
        </button>`;
  const row = (
    forId: string,
    labelKey: string,
    controlHtml: string,
    opts: { hintKey?: string; required?: boolean } = {},
  ): string => `
    <div class="model-form-row">
      <label class="model-form-label${opts.hintKey ? " model-form-label--inline" : ""}" for="${forId}">${opts.required ? '<span class="model-form-required">*</span>' : ""}${t(labelKey)}${opts.hintKey ? help(opts.hintKey) : ""}</label>
      ${controlHtml}
    </div>`;
  const dropdownRow = (id: string, labelKey: string, options: DropdownOption[], current: string): string =>
    row(id, labelKey, `<div class="model-form-dropdown-row">${this.renderDropdown(id, options, current, () => {})}</div>`);

  const bodyHtml = `
    ${dropdownRow("dd-network-proxy-mode", "settings.proxyMode", modeOptions, mode)}
    <div id="network-manual-fields" style="${manualHidden}">
      ${dropdownRow("dd-network-proxy-scheme", "settings.proxyScheme", schemeOptions, scheme)}
      ${row(
        "network-proxy-host",
        "settings.proxyHost",
        `<input type="text" id="network-proxy-host" class="model-form-input" placeholder="127.0.0.1" value="${this.esc(host)}" autocomplete="off" spellcheck="false" />`,
        { required: true },
      )}
      ${row(
        "network-proxy-port",
        "settings.proxyPort",
        `<input type="number" id="network-proxy-port" class="model-form-input" value="${port || 7890}" min="1" max="65535" step="1" />`,
        { required: true },
      )}
      ${row(
        "network-proxy-username",
        "settings.proxyUsername",
        `<input type="text" id="network-proxy-username" class="model-form-input" placeholder="user" value="${this.esc(username)}" autocomplete="off" spellcheck="false" />`,
        { hintKey: "settings.proxyUsernameDesc" },
      )}
      ${row(
        "network-proxy-password",
        "settings.proxyPassword",
        `<div class="model-form-input-wrap">
          <input type="password" id="network-proxy-password" class="model-form-input model-form-input--reveal" placeholder="password" value="${this.esc(password)}" autocomplete="off" spellcheck="false" />
          <button class="model-form-eye-btn" type="button" data-eye-target="network-proxy-password" title="${t("settings.revealSecret")}"><i data-lucide="eye"></i></button>
        </div>`,
      )}
      ${row(
        "network-proxy-bypass",
        "settings.proxyBypass",
        `<input type="text" id="network-proxy-bypass" class="model-form-input" placeholder="localhost,127.0.0.1" value="${this.esc(bypass)}" autocomplete="off" spellcheck="false" />`,
        { hintKey: "settings.proxyBypassDesc" },
      )}
    </div>`;

  const { overlay, close } = this._showFormDialog(t("settings.networkProxyTitle"), bodyHtml, true);
  const okBtn = overlay.querySelector<HTMLButtonElement>("#dialog-form-ok");
  const footer = overlay.querySelector<HTMLElement>(".dialog-footer");
  if (okBtn) okBtn.textContent = t("common.save");
  this._bindEyeToggles(overlay);

  let draftMode = mode;
  let draftScheme = scheme;
  const readText = (id: string): string =>
    (overlay.querySelector<HTMLInputElement>(`#${id}`)?.value ?? "").trim();

  // Shared persist routine: validates the manual fields, then writes the whole
  // card in one `configure` (the backend rebuilds the managed proxy env from
  // the complete key set), mirrored into local state as usual.  Returns false
  // when the manual draft is incomplete so the caller can keep it open.
  const persist = (): boolean => {
    const host = readText("network-proxy-host");
    const port = Number(readText("network-proxy-port") || 0);
    // A manual proxy cannot be dialled without an address, and a port outside
    // 1-65535 is not a port: both are required, so refuse to save until they
    // are there (system/direct ignore them).
    if (draftMode === "manual" && (!host || !(port >= 1 && port <= 65535))) {
      showToast(t("common.pleaseFillRequired"), "", "error", "Settings");
      return false;
    }
    const config = {
      network_proxy_mode: draftMode,
      network_proxy_scheme: draftScheme,
      network_proxy_host: host,
      network_proxy_port: String(port || 0),
      network_proxy_username: readText("network-proxy-username"),
      network_proxy_password: overlay.querySelector<HTMLInputElement>("#network-proxy-password")?.value ?? "",
      network_proxy_bypass: readText("network-proxy-bypass"),
    };
    setSettings({ ...getState().settings, ...config });
    send({ type: "configure", config });
    return true;
  };

  // Cancel / Save are only meaningful for the custom-proxy draft.  System and
  // direct have nothing to type: hide the footer and let every selection
  // apply at once.
  const syncFooter = (): void => {
    if (footer) footer.style.display = draftMode === "manual" ? "" : "none";
  };
  syncFooter();

  // The mode picker reveals/hides the manual card; manual keeps the draft
  // flow, the other two modes persist immediately.
  this.bindDropdown("dd-network-proxy-mode", (v: string) => {
    draftMode = v;
    const manual = overlay.querySelector("#network-manual-fields") as HTMLElement | null;
    if (manual) manual.style.display = v === "manual" ? "" : "none";
    syncFooter();
    if (v !== "manual") persist();
  });
  this.bindDropdown("dd-network-proxy-scheme", (v: string) => { draftScheme = v; });
  // Port: clamp in place on commit (blur/steppers) so the field never shows a
  // value the save would reject.  No backend call — this is a draft.
  overlay.querySelector<HTMLInputElement>("#network-proxy-port")?.addEventListener("change", (e) => {
    const input = e.target as HTMLInputElement;
    const n = Math.max(0, Math.min(65535, Math.floor(Number(input.value) || 0)));
    input.value = String(n);
  });

  // Nothing is written while the manual draft is open: Save persists the whole
  // card in one go and Cancel (or the shell's discard prompt when leaving
  // settings) throws the draft away.  The back arrow stays hidden — on a main
  // editor page it would only duplicate Cancel, same as the model provider form.
  okBtn?.addEventListener("click", () => {
    if (!persist()) return;
    close();
  });
}

export function escImpl(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function _maxTokensDefaultImpl(backendType: string, context?: number): number {
  const catalog = getState().modelCatalog;
  const defOutput = catalog.default_output_tokens[backendType] || 8192;
  if (context && context > 0) {
    return Math.min(context, defOutput);
  }
  return defOutput;
}
