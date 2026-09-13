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

import { getState, setSettings, setCustomCommands, setTheme, setThemePreference, setPermissionPolicies, subscribe, showToast, isEnabled } from "../core/state.js";
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
import { applyServerCommands } from "../features/slash_commands.js";
import { renderMarkdown } from "../chat/chat.js";
import { platformIconHtml, searchEngineIconSrc } from "../ui/icons.js";
import { formatShortcut } from "../features/shortcutDisplay.js";
// Chart.js is lazy-loaded on first render of the usage panel (see
// _renderUsageSection) so it no longer blocks startup parsing.
import { showTooltipAt, hideTooltip } from "../ui/tooltip.js";
import { SEARCH_ENGINES, getDefaultSearchEngine } from "../features/browser.js";
import { searchSettingsNavItems, SEARCH_FILTER_META, SETTINGS_NAV_ITEMS } from "../features/search.js";
import { setSearchFilter } from "../core/state.js";

interface DropdownOption { id: string; label: string; icon?: string }

/** Renders the shared search-filter checkboxes (used by both searches). */
export function renderSearchFilterImpl(this: any): void {
  const el = this.panels.search;
  if (!el) return;
  const filter = getState().searchFilter;
  const locale = getLocale();
  const lang = locale === "zh" ? "zh" : "en";

  const rows = SEARCH_FILTER_META.map((m) => {
    const key = m.key;
    const checked = filter[key] ? "checked" : "";
    const label = lang === "zh" ? m.zh : m.en;
    return `
      <label class="search-filter-row">
        <input type="checkbox" class="search-filter-checkbox" data-key="${key}" ${checked} />
        <span class="search-filter-label">${this.esc(label)}</span>
      </label>`;
  }).join("");

  el.innerHTML = `
    <div class="settings-panel-header">
      <h2>${this.esc(t("search.filterTitle"))}</h2>
      <p class="settings-panel-desc">${this.esc(t("search.filterDesc"))}</p>
    </div>
    <div class="settings-row" style="gap:8px;margin-bottom:12px">
      <button class="btn btn-sm" id="search-filter-all">${this.esc(t("search.filterAll"))}</button>
      <button class="btn btn-sm" id="search-filter-reset">${this.esc(t("search.filterReset"))}</button>
    </div>
    <div class="search-filter-grid">${rows}</div>
  `;

  el.querySelectorAll<HTMLInputElement>(".search-filter-checkbox").forEach((cb) => {
    cb.addEventListener("change", () => {
      const key = cb.getAttribute("data-key") as keyof typeof filter;
      const next = { ...getState().searchFilter, [key]: cb.checked };
      setSearchFilter(next);
    });
  });

  el.querySelector("#search-filter-all")?.addEventListener("click", () => {
    const all = {} as typeof filter;
    for (const m of SEARCH_FILTER_META) (all as Record<string, boolean>)[m.key] = true;
    setSearchFilter(all);
    this.renderSearchFilter();
  });
  el.querySelector("#search-filter-reset")?.addEventListener("click", () => {
    setSearchFilter(defaultSearchFilter());
    this.renderSearchFilter();
  });
}

export function renderDropdownImpl(this: any, id: string, options: DropdownOption[], currentId: string, onChange: (val: string) => void): string {
  const current = options.find(o => o.id === currentId) || options[0];
  const itemHtml = (o: DropdownOption) => `${o.icon ? `<img class="settings-dropdown-item-icon" src="${o.icon}" alt="" draggable="false" />` : ""}${o.label}`;
  const items = options.map(o =>
    `<div class="settings-dropdown-item${o.id === currentId ? " selected" : ""}" data-value="${o.id}"${o.icon ? ` data-icon="${o.icon}"` : ""}>${itemHtml(o)}</div>`
  ).join("");
  if (!current) {
    return `
      <div class="settings-dropdown-wrap" id="${id}-wrap">
        <button class="settings-dropdown-trigger" id="${id}-trigger" type="button" disabled>
          <span>—</span>
        </button>
        <div class="settings-dropdown" id="${id}-dropdown"></div>
      </div>`;
  }
  const curHtml = current.icon ? `<img class="settings-dropdown-item-icon" src="${current.icon}" alt="" draggable="false" />` : "";
  return `
    <div class="settings-dropdown-wrap" id="${id}-wrap">
      <button class="settings-dropdown-trigger" id="${id}-trigger" type="button">
        <span>${curHtml}${current.label}</span>
        <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
      </button>
      <div class="settings-dropdown" id="${id}-dropdown">${items}</div>
    </div>`;
}

export function modeHintImpl(this: any, modeKey: string): string {
  return `<span class="mode-hint-icon" data-tooltip="${this.esc(t(modeKey))}">
    <i data-lucide="circle-alert" class="lucide"></i>
  </span>`;
}

export function bindDropdownImpl(this: any, id: string, onChange: (val: string) => void): void {
  const wrap = document.getElementById(`${id}-wrap`);
  const trigger = document.getElementById(`${id}-trigger`);
  const dropdown = document.getElementById(`${id}-dropdown`);
  if (!wrap || !trigger || !dropdown) return;
  if (wrap.classList.contains("is-disabled")) return;

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = dropdown.classList.contains("open");
    // Close all other dropdowns
    document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
    if (!isOpen) dropdown.classList.add("open");
  });

  dropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      const val = (item as HTMLElement).getAttribute("data-value") || "";
      const label = (item as HTMLElement).textContent || "";
      const icon = (item as HTMLElement).getAttribute("data-icon") || "";
      const span = trigger.querySelector("span");
      if (span) {
        span.innerHTML = (icon ? `<img class="settings-dropdown-item-icon" src="${icon}" alt="" draggable="false" />` : "") + label;
      }
      dropdown.classList.remove("open");
      dropdown.querySelectorAll(".settings-dropdown-item").forEach((el) => el.classList.remove("selected"));
      (item as HTMLElement).classList.add("selected");
      onChange(val);
    });
  });
}

export function renderGeneralImpl(this: any): void {
  const st = getState();
  const currentTheme = st.themePreference;
  const s = st.settings;

  const currentLang = getLocale();
  this.updateSidebarNav();
  const currentLinkBehavior = (s.default_link_behavior as string) || "system";
  const currentSendMode = (s.shortcut_send_mode as string) || "enter";
  const currentStartupMode = (s.startup_session_mode as string) || "normal";
  const currentStartupBehavior = (s.startup_session_behavior as string) || "new";
  const currentLangPref = (s.language_preference as string) || "auto";

  const themeOptions: DropdownOption[] = [
    { id: "system", label: t("theme.system") },
    { id: "light", label: t("theme.light") },
    { id: "dark", label: t("theme.dark") },
  ];

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

  const langPrefOptions: DropdownOption[] = [
    { id: "auto", label: t("language.autoFollow") },
    { id: "zh", label: t("language.zh") },
    { id: "en", label: t("language.en") },
  ];

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
          <div class="settings-item-title">${t("settings.languagePreference")}</div>
          <div class="settings-item-desc">${t("settings.languagePreferenceDesc")}</div>
        </div>
        <div class="settings-item-control">
          ${this.renderDropdown("dd-lang-pref", langPrefOptions, currentLangPref, (v) => { this.saveSetting("language_preference", v); this.renderGeneral(); })}
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
    </div>`;

  // Bind dropdowns
  this.bindDropdown("dd-theme", (v) => this.saveTheme(v));
  this.bindDropdown("dd-lang", (v) => { this.saveSetting("language", v); this.renderGeneral(); });
  this.bindDropdown("dd-send", (v) => { this.saveSetting("shortcut_send_mode", v); this.renderGeneral(); });
  this.bindDropdown("dd-startup", (v) => { this.saveSetting("startup_session_mode", v); this.renderGeneral(); });
  this.bindDropdown("dd-startup-behavior", (v) => { this.saveSetting("startup_session_behavior", v); this.renderGeneral(); });
  this.bindDropdown("dd-link", (v) => { this.saveSetting("default_link_behavior", v); this.renderGeneral(); });
  this.bindDropdown("dd-lang-pref", (v) => { this.saveSetting("language_preference", v); this.renderGeneral(); });

  // Auto-expand toggle
  document.getElementById("auto-expand-toggle")?.addEventListener("change", (e) => {
    const checked = (e.target as HTMLInputElement).checked;
    this.saveSetting("auto_expand", checked ? "true" : "false");
  });

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

export function renderShortcutsImpl(this: any): void {
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
      rowsHtml += `
      <div class="settings-item-row shortcut-row" data-id="${item.id}">
        <div class="settings-item-info">
          <div class="settings-item-title">${this.esc(desc)}</div>
        </div>
        <div class="settings-item-control">
          <button class="shortcut-key-btn${isRecording ? " recording" : ""}" data-id="${item.id}">${isRecording ? "..." : this.esc(displayKeys)}</button>
        </div>
      </div>`;
    }
  }

  this.panels.shortcuts.innerHTML = `
    <div class="settings-section-title"><i data-lucide="keyboard" class="lucide section-title-icon"></i> ${t("settings.keyboardShortcuts")}</div>
    <div class="settings-item-desc" style="margin: -8px 0 16px; padding: 0 2px;">${t("settings.shortcutsDisabledDesc")}</div>
    <div class="settings-card" id="shortcuts-card">${rowsHtml}</div>`;

  if (typeof (window as any).lucide !== "undefined") {
    (window as any).lucide.createIcons({ root: this.panels.shortcuts });
  }

  // Bind key capture
  const card = document.getElementById("shortcuts-card");
  if (!card) return;

  card.querySelectorAll<HTMLButtonElement>(".shortcut-key-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.getAttribute("data-id");
      if (!id) return;

      // Cancel current recording and start new one
      if (this._recordingShortcutId) {
        document.removeEventListener("keydown", this._captureHandler as any, true);
      }

      this._recordingShortcutId = id;
      document.documentElement.dataset.shortcutRecording = "true";
      const allBtns = card.querySelectorAll<HTMLButtonElement>(".shortcut-key-btn");
      allBtns.forEach((b) => b.classList.remove("recording"));
      btn.classList.add("recording");
      btn.textContent = "...";

      const stopRecording = () => {
        this._recordingShortcutId = null;
        delete document.documentElement.dataset.shortcutRecording;
      };

      this._captureHandler = async (ev: KeyboardEvent) => {
        ev.preventDefault();
        ev.stopPropagation();

        if (ev.repeat) return;

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
            const ok = await Dialog.confirm(t("settings.shortcutConflictTitle"), msg, { container: document.getElementById("settings-view") ?? undefined });
            if (!ok) {
this.renderShortcuts();
    this.renderStorage();
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
  if (value === "dark") setTheme("dark");
  else if (value === "light") setTheme("light");
      else setTheme("light");
  localStorage.setItem("encre-theme", value);
  send({ type: "configure", config: { theme: value } });
  this.renderGeneral();
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
