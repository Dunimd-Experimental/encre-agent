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
import type { PanelId } from "./settings.js";

const DEV_MODE_STORAGE_KEY = "encre-dev-mode";
const DEV_TAP_THRESHOLD = 7;
const DEV_TAP_RESET_MS = 2500;
let _devTapCount = 0;
let _devTapTimer: number = 0;

const EE_TAP_THRESHOLD = 5;
const EE_TAP_RESET_MS = 3000;
let _eeTapCount = 0;
let _eeTapTimer: number = 0;

function isDevModeEnabled(): boolean {
  try {
    return localStorage.getItem(DEV_MODE_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function setDevModeEnabled(enabled: boolean): void {
  try {
    if (enabled) localStorage.setItem(DEV_MODE_STORAGE_KEY, "1");
    else localStorage.removeItem(DEV_MODE_STORAGE_KEY);
  } catch {
    // localStorage unavailable — fail silently
  }
}

export function bindVersionTapUnlockImpl(this: any): void {
  this.panels.about.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    const row = target.closest('.about-info-row[data-key="version"]') as HTMLElement | null;
    if (!row) return;
    const ver = row.getAttribute("data-version");
    if (ver === "agent") {
      this.handleAgentTap();
    } else if (ver === "desktop") {
      this.handleDesktopTap();
    }
  });
}

export async function loadVersionsImpl(this: any): Promise<void> {
  const api = (window as any).electronAPI;
  if (api?.getAppVersions) {
    try {
      this._versions = await api.getAppVersions();
    } catch {}
  }
}

export function handleAgentTapImpl(this: any): void {
  if (isDevModeEnabled()) return;

  _devTapCount += 1;
  if (_devTapTimer) {
    clearTimeout(_devTapTimer);
  }
  _devTapTimer = window.setTimeout(() => {
    _devTapCount = 0;
  }, DEV_TAP_RESET_MS);

  if (_devTapCount >= DEV_TAP_THRESHOLD) {
    _devTapCount = 0;
    if (_devTapTimer) {
      clearTimeout(_devTapTimer);
      _devTapTimer = 0;
    }
    setDevModeEnabled(true);
    const devNav = document.getElementById("settings-nav-developer");
    if (devNav) devNav.classList.remove("hidden");
    this.updateSidebarNav();
    if (devNav && typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: devNav });
    }
  }
}

export function handleDesktopTapImpl(this: any): void {
  _eeTapCount += 1;
  if (_eeTapTimer) {
    clearTimeout(_eeTapTimer);
  }
  _eeTapTimer = window.setTimeout(() => {
    _eeTapCount = 0;
  }, EE_TAP_RESET_MS);

  if (_eeTapCount >= EE_TAP_THRESHOLD) {
    _eeTapCount = 0;
    if (_eeTapTimer) {
      clearTimeout(_eeTapTimer);
      _eeTapTimer = 0;
    }
    window.electronAPI?.openChildWindow("easter-egg", "✦ Nebula");
  }
}

export function openImpl(this: any): void {
  this.modelCreateActive = false;
  if (this.searchInput) this.searchInput.value = "";
  this.filterNavItems("");
  // Unified entry: one snapshot refresh covers every settings panel.
  refreshAllData();
  this.renderAll();
  this.updateSidebarNav();
  this.switchPanel(this.currentPanel);
  // Don't highlight any nav item on initial open — only when user clicks one
  this.nav.querySelectorAll(".settings-nav-item").forEach(item => item.classList.remove("active"));
  document.getElementById("app")?.classList.add("settings-mode");
  // Entering settings should reveal the app-level sidebar (which carries
  // the settings nav). If it was auto-collapsed by the inner sidebar or a
  // narrow viewport snap, undo that so the settings nav is immediately
  // visible — unless the user collapsed it by hand.
  (window as any).__appRestoreSidebar?.();
  const sidebarNav = document.querySelector(".sidebar-settings-nav");
  if (sidebarNav && typeof (window as any).lucide !== "undefined") {
    (window as any).lucide.createIcons({ root: sidebarNav });
  }
}

export function openModelCreateImpl(this: any): void {
  this.open();
  this.switchPanel("model");
  this.showModelCreate();
}

export function updateSidebarNavImpl(this: any): void {
  const labelMap: Record<string, string> = {
    general: t("sidebar.general"),
    usage: t("sidebar.usage"),
    shortcuts: t("sidebar.shortcuts"),
    storage: t("sidebar.storage"),
    model: t("sidebar.models"),
    gateway: t("sidebar.gateway"),
    browser: t("sidebar.browser"),
    agent: t("sidebar.agent"),
    mcp: t("sidebar.mcp"),
    index: t("sidebar.document"),
    skills: t("sidebar.skills"),
    rules: t("sidebar.rules"),
    memory: t("sidebar.memory"),
    developer: t("sidebar.developer"),
    about: t("sidebar.about"),
  };
  this.nav.querySelectorAll<HTMLElement>(".settings-nav-item").forEach((item) => {
    const panel = item.getAttribute("data-panel");
    if (panel && labelMap[panel]) {
      const span = item.querySelector("span");
      if (span) span.textContent = labelMap[panel];
    }
  });
  const devBtn = document.getElementById("settings-nav-developer");
  if (devBtn) {
    devBtn.style.display = isDevModeEnabled() ? "" : "none";
  }
}

export function closeImpl(this: any): void {
  // Suppress the #main-content margin transition while settings-mode is
  // removed: the !important margin override (0) and the sidebar-hidden
  // negative margin differ, and without this the container visibly slides
  // its right edge on exit. Freezing the transition for one frame makes the
  // margin snap instantly, so the content surface stays put.
  const mc = document.getElementById("main-content");
  if (mc) {
    const prev = mc.style.transition;
    mc.style.transition = "none";
    document.getElementById("app")?.classList.remove("settings-mode");
    void mc.offsetWidth;
    mc.style.transition = prev;
  } else {
    document.getElementById("app")?.classList.remove("settings-mode");
  }
  delete document.documentElement.dataset.shortcutPanelActive;
  delete document.documentElement.dataset.shortcutRecording;
  (window as any).electronAPI?.setWinKeyCapture?.(false);
  this._recordingShortcutId = null;
  this._cleanupTransientOverlays();
  if (typeof (window as any).__appCleanupContentArea === "function") {
    (window as any).__appCleanupContentArea();
  }
  // Force a full chat re-render after cleanup emptied the DOM.  A plain
  // render() would hit the render-key cache, see "messages unchanged", and
  // skip fullRender -- leaving the chat blank. renderForce() resets the
  // key so the message list is repainted from current state.
  if (typeof (window as any).__chatForceRender === "function") {
    (window as any).__chatForceRender();
  } else if (typeof (window as any).__chatRender === "function") {
    (window as any).__chatRender();
  }
  // Returning from settings is a reveal, not a mode switch: the content
  // surface (with its own background) is a permanent fixture, so it must
  // appear in place immediately. Replaying the mode-switch entrance here
  // would slide the whole content area in, which reads as the container
  // itself moving — exactly what we don't want.
  (window as any).__sessionInner?.restoreSidebarVisibility?.();
}

/**
 * Best-effort teardown of every transient overlay a settings panel may
 * have created.  We only target overlays that are unconditionally safe
 * to remove (the settings view is already hidden at this point).
 */
export function _cleanupTransientOverlaysImpl(this: any): void {
  // Floating dialogs/overlays created by settings panels:
  //   - skill-detail overlay
  //   - command-create overlay
  //   - doc-name / doc-url dialog
  //   - rule-edit overlay
  //   - agent-edit overlay
  //   - memory-edit overlay
  // Each is appended to document.body with a unique class.
  const transientClasses = [
    "skill-detail-overlay",
    "command-create-overlay",
    "doc-name-dialog",
    "doc-url-dialog",
    "rule-edit-overlay",
    "agent-edit-overlay",
    "memory-edit-overlay",
  ];
  for (const cls of transientClasses) {
    document.querySelectorAll(`.${cls}`).forEach((el) => el.remove());
  }
  // Close any still-open dropdown menus so they don't pop up the
  // moment the user moves the mouse.
  document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
  // Drop inline form nodes that were inserted into #settings-content-wrap
  // by a settings panel's edit-mode flow (rare but possible).
  document.querySelectorAll("#settings-content-wrap .inline-form, #settings-content-wrap .edit-form").forEach((el) => el.remove());
}

export function focusSearchImpl(this: any): void {
  if (!document.getElementById("app")?.classList.contains("settings-mode")) {
    this.open();
  }
  setTimeout(() => this.searchInput?.focus(), 50);
}

export function onSearchInputImpl(this: any): void {
  if (!this.searchInput) return;
  clearTimeout(this.searchTimer);
  const q = this.searchInput.value.trim().toLowerCase();
  this.searchTimer = window.setTimeout(() => {
    this.filterNavItems(q);
  }, 100);
}

export function filterNavItemsImpl(this: any, q: string): void {
  const matchedPanels = new Set(
    q ? searchSettingsNavItems(q).map((n) => n.panel) : SETTINGS_NAV_ITEMS.map((n) => n.panel),
  );
  const lower = q.toLowerCase();
  let firstMatch: HTMLElement | null = null;
  let firstPanelId: string | null = null;

  this.nav.querySelectorAll<HTMLElement>(".settings-nav-item").forEach((item) => {
    const panel = item.getAttribute("data-panel") as string;
    let matches = matchedPanels.has(panel);
    // Never reveal the developer nav item unless dev mode is on.
    if (panel === "developer" && !isDevModeEnabled()) matches = false;
    item.style.display = matches ? "" : "none";

    if (matches && !firstMatch) {
      firstMatch = item;
      firstPanelId = panel;
    }
  });

  if (lower && firstMatch && firstPanelId) {
    if (firstPanelId !== this.currentPanel) {
      this.switchPanel(firstPanelId as PanelId);
    }
    setTimeout(() => this.highlightInPanel(firstPanelId as PanelId, lower), 150);
  }

  this._updateDividerVisibility();
}

/** Hide dividers that separate groups where all items are hidden by search. */
export function _updateDividerVisibilityImpl(this: any): void {
  const children = this.nav.children;
  let lastVisibleIdx = -1;
  for (let i = 0; i < children.length; i++) {
    const child = children[i] as HTMLElement;
    if (child.classList.contains("settings-nav-item")) {
      if (child.style.display !== "none") {
        lastVisibleIdx = i;
      }
    } else if (child.classList.contains("settings-nav-divider")) {
      let hasVisibleAfter = false;
      for (let j = i + 1; j < children.length; j++) {
        const next = children[j] as HTMLElement;
        if (next.classList.contains("settings-nav-divider")) break;
        if (next.classList.contains("settings-nav-item") && next.style.display !== "none") {
          hasVisibleAfter = true;
          break;
        }
      }
      const hasVisibleBefore = lastVisibleIdx >= 0;
      child.style.display = hasVisibleBefore && hasVisibleAfter ? "" : "none";
    }
  }
}

export function highlightInPanelImpl(this: any, panelId: PanelId, query: string): void {
  const panel = this.panels[panelId];
  if (!panel) return;

  this.clearHighlights();

  const walker = document.createTreeWalker(panel, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      if (!node.textContent || !node.textContent.toLowerCase().includes(query)) {
        return NodeFilter.FILTER_REJECT;
      }
      let parent = node.parentElement;
      while (parent && parent !== panel) {
        const style = window.getComputedStyle(parent);
        if (style.display === "none" || style.visibility === "hidden") return NodeFilter.FILTER_REJECT;
        parent = parent.parentElement;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });

  const firstNode = walker.nextNode();
  if (!firstNode) return;

  let target: HTMLElement | null = firstNode.parentElement;
  while (target && !this.isHighlightable(target, panel)) {
    target = target.parentElement;
  }
  if (!target) return;

  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.add("settings-search-flash");
  setTimeout(() => target?.classList.remove("settings-search-flash"), 1600);
}

export function isHighlightableImpl(el: HTMLElement, panel: HTMLElement): boolean {
  if (el === panel) return false;
  const tag = el.tagName.toLowerCase();
  if (tag === "span" || tag === "p" || tag === "div" || tag === "h3" || tag === "h4" ||
      tag === "label" || tag === "button" || tag === "pre" || tag === "code" ||
      tag === "td" || tag === "th") {
    return true;
  }
  return false;
}

export function clearHighlightsImpl(): void {
  document.querySelectorAll(".settings-search-flash").forEach((el) => {
    el.classList.remove("settings-search-flash");
  });
}

export function refreshCurrentPanelImpl(this: any): void {
  this.switchPanel(this.currentPanel);
}

export function switchPanelImpl(this: any, id: PanelId): void {
  this.currentPanel = id;
  this.modelCreateActive = false;
  delete document.documentElement.dataset.shortcutPanelActive;
  (window as any).electronAPI?.setWinKeyCapture?.(false);

  this.nav.querySelectorAll(".settings-nav-item").forEach((item) => {
    item.classList.toggle("active", item.getAttribute("data-panel") === id);
  });

  Object.entries(this.panels).forEach(([key, el]) => {
    el.classList.toggle("active", false);
  });

  if (id === "skills") {
    console.log("[DEBUG switchPanel] skills panel, skillsList:", getState().skillsList.length, "items");
    this.panels.skills.classList.add("active");
    this.renderSkills();
  } else if (id === "model") {
    this.panels.model.classList.add("active");
    this.renderModel();
  } else if (id === "gateway") {
    this.panels.gateway.classList.add("active");
    this.renderGateway();
  } else if (id === "browser") {
    this.panels.browser.classList.add("active");
    this.renderBrowser();
  } else if (id === "mcp") {
    this.panels.mcp.classList.add("active");
    this.renderMcpList();
  } else if (id === "agent") {
    this.panels.agent.classList.add("active");
    this.renderAgent();
  } else if (id === "memory") {
    this.panels.memory.classList.add("active");
    this.renderMemory();
  } else if (id === "usage") {
    this.panels.usage.classList.add("active");
    void this._renderUsageSection();
  } else if (id === "shortcuts") {
    this.panels.shortcuts.classList.add("active");
    this.renderShortcuts();
    document.documentElement.dataset.shortcutPanelActive = "true";
    (window as any).electronAPI?.setWinKeyCapture?.(true);
  } else if (id === "storage") {
    this.panels.storage.classList.add("active");
    this.renderStorage();
  } else if (id === "index") {
    console.log("[DEBUG switchPanel] index panel, docsList:", getState().docsList.length, "items");
    this.panels.index.classList.add("active");
    this.renderIndex();
  } else if (id === "rules") {
    this.panels.rules.classList.add("active");
    this.renderRules();
  } else if (id === "permissions") {
    this.panels.permissions.classList.add("active");
    this.renderPermissions();
  } else if (id === "developer") {
    this.panels.developer.classList.add("active");
    this.renderDeveloper();
  } else if (id === "search") {
    this.panels.search.classList.add("active");
    this.renderSearchFilter();
  } else {
    this.panels[id].classList.add("active");
  }
}

export function showSkillDetailImpl(this: any, skillName: string, isEdit = false): void {
  console.log("[DEBUG showSkillDetail] called with:", skillName, "isEdit:", isEdit);
  this._renderSkillDetailDialog(skillName, isEdit);
}

export function showSkillListImpl(this: any): void {
  this.panels.skills.classList.add("active");
  this.renderSkills();
}

export function showMcpCreateImpl(this: any, editIdx?: number): void {
  this._renderMcpImportDialog(editIdx);
}

export function showMcpListImpl(this: any): void {
  this.panels.mcp.classList.add("active");
  this.renderMcpList();
}

export function showAgentCreateImpl(this: any, existing?: import("../core/types.js").SubAgentConfig): void {
  this._renderAgentCreateDialog(existing);
}

export function showAgentListImpl(this: any): void {
  this.panels.agent.classList.add("active");
  this.renderAgent();
}

export function showModelCreateImpl(this: any): void {
  this.modelCreateActive = true;
  this._renderModelCreateDialog();
}

export function showModelEditImpl(this: any, idx: number): void {
  this.modelCreateActive = true;
  this._renderModelCreateDialog(idx);
}

export function renderAllImpl(this: any): void {
  this.renderGeneral();
  void this._renderUsageSection();
  this.renderShortcuts();
  this.renderModel();
  this.renderGateway();
  this.renderBrowser();
  this.renderIndex();
  this.renderSkills();
  this.renderRules();
  this.renderPermissions();
  this.renderMcp();
  this.renderAgent();
  this.renderAbout();
  this.renderSearchFilter();
  if (isDevModeEnabled()) this.renderDeveloper();
}
