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

import { setSettings, setCustomCommands, setTheme, setThemePreference, setPermissionPolicies, subscribe, showToast, isEnabled } from "../core/state.js";
import { send } from "../core/ws.js";
import { waitForModelValidation, onWechatScanResult } from "../core/stream.js";
import { setModelConfigs, setMcpServers, setSkillsList, setSubAgents } from "../core/state.js";
import type { ModelConfigMeta, MCPServerConfig, SkillInfo, ModelCatalog, McpCatalog, McpProviderEntry, ProviderEntry, ProfileData, CustomCommand, UsageStatsSessionEntry } from "../core/types.js";
import { defaultSearchFilter } from "../core/types.js";
import { guardRender, guardAsync } from "../core/ui_boundary.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { Dialog } from "../ui/dialog.js";
import { setSurfacePanel, swapPanel as swapSurfacePanel } from "../core/scope.js";
import {
  bindTargetModelSelection,
  parseTargetModelSelection,
  readTargetModelSelection,
  renderTargetModelSelection,
  serializeTargetModelSelection,
} from "./model-selection.js";
import { t, initLocale, setLocale, getLocale, getIntlLocale, clearLocaleCache, onLocaleChange, LOCALES, type Locale } from "../features/i18n.js";
import { applyServerCommands } from "../ui/slash-commands.js";
import { hideViewAnimated, showViewAnimated } from "../ui/animate.js";
import { renderMarkdown } from "../chat/chat.js";
import { platformIconHtml, searchEngineIconSrc } from "../ui/icons.js";
import { formatShortcut } from "../ui/shortcut-display.js";
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
  // Floating session overlays must not outlive the chat view: close the
  // summary panel so it never renders above settings or leaks stale content.
  document.getElementById("summary-panel")?.classList.add("hidden");
  document.getElementById("btn-summary-panel")?.classList.remove("active");
  this.modelCreateActive = false;
  if (this.searchInput) this.searchInput.value = "";
  this.filterNavItems("");
  // No refreshAllData() here: every settings plane is filled by the unified
  // backend snapshot (boot push + pushes on config/mode/workspace changes +
  // per-mutation broadcasts), and each panel renders from the state store —
  // opening settings must never fire a 15-request refill sweep.
  this.renderAll();
  this.updateSidebarNav();
  this.switchPanel(this.currentPanel);
  // Don't highlight any nav item on initial open — only when user clicks one
  this.nav.querySelectorAll(".settings-nav-item").forEach(item => item.classList.remove("active"));
  document.getElementById("app")?.classList.add("settings-mode");
  // A close that was still fading out has to be cancelled first, otherwise
  // its finish() would land after the reopen. `.hidden` stays: settings is
  // shown purely by `#app.settings-mode`.
  showViewAnimated(document.getElementById("settings-view"), false);
  // `#settings-view` is a pre-existing element (in the HTML from
  // first paint), so `@starting-style` never fires for it.
  // Animate the enter via inline style instead of relying on
  // the cascade.
  const _sv = document.getElementById("settings-view");
  if (_sv) {
    (_sv as HTMLElement).style.opacity = "0";
    void (_sv as HTMLElement).offsetWidth;
    (_sv as HTMLElement).style.opacity = "";
  }
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
  this.showProviderCreate();
}

export function updateSidebarNavImpl(this: any): void {
  const labelMap: Record<string, string> = {
    general: t("sidebar.general"),
    flow: t("sidebar.flow"),
    usage: t("sidebar.usage"),
    shortcuts: t("sidebar.shortcuts"),
    storage: t("sidebar.storage"),
    model: t("sidebar.models"),
    gateway: t("sidebar.gateway"),
    assist: t("sidebar.assist"),
    agent: t("sidebar.agent"),
    mcp: t("sidebar.mcp"),
    library: t("sidebar.library"),
    skills: t("sidebar.skills"),
    memory: t("sidebar.memory"),
    developer: t("sidebar.developer"),
    about: t("sidebar.about"),
  };
  this.nav.querySelectorAll<HTMLElement>(".settings-nav-item").forEach((item) => {
    const panel = item.getAttribute("data-panel");
    if (panel && labelMap[panel]) {
      // The agent nav item leads with a `.sa-loader` glyph span — target the
      // label span explicitly or the glyph grid gets overwritten with text
      // (CJK then wraps one char per 4px grid row: vertical 子/智/能/体).
      const span = item.querySelector("span:not(.sa-loader)");
      if (span) span.textContent = labelMap[panel];
    }
  });
  const devBtn = document.getElementById("settings-nav-developer");
  if (devBtn) {
    devBtn.style.display = isDevModeEnabled() ? "" : "none";
  }
}

export async function closeImpl(this: any): Promise<void> {
  // A panel may still hold an unsaved inline editor draft.  Leaving settings
  // destroys it, so ask first — same contract as the code editor's unsaved-tab
  // prompt.  Cancelling keeps the settings view (and the draft) exactly as-is.
  if (typeof this._confirmDiscardEditors === "function" && !(await this._confirmDiscardEditors())) {
    return;
  }
  // Suppress the #main-content margin transition while settings-mode is
  // removed: the !important margin override (0) and the sidebar-hidden
  // negative margin differ, and without this the container visibly slides
  // its right edge on exit. Freezing the transition for one frame makes the
  // margin snap instantly, so the content surface stays put.
  const mc = document.getElementById("main-content");
  // The panel leaves through the shared exit motion instead of vanishing:
  // it is lifted out of the flow so the chat can take the slot on this very
  // same frame, and dissolves above it (see `.view-exit` in styles.css).
  const settingsView = document.getElementById("settings-view");
  if (mc) {
    const prev = mc.style.transition;
    mc.style.transition = "none";
    document.getElementById("app")?.classList.remove("settings-mode");
    void mc.offsetWidth;
    mc.style.transition = prev;
  } else {
    document.getElementById("app")?.classList.remove("settings-mode");
  }
  hideViewAnimated(settingsView);
  // Leaving settings: shelf everything that belongs to the panel the user was
  // on (an unanswered unbind confirm, a form draft) so it reappears when
  // settings is reopened on that same panel — instead of floating above the
  // chat, which is what happened while these were merely DOM children of
  // #main-content.
  swapSurfacePanel(this.currentPanel as string, "");
  setSurfacePanel(null);
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
  // Bring back the unsent draft of the active session: cleanupContentArea()
  // wiped the composer (after saving it into the draft map), and unlike
  // mode switches the settings-return path has no session_ready event that
  // would trigger restoreDraft() on its own.
  (window as any).__app?.restoreDraft?.();
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
  // Close any still-open dropdown menus so they don't pop up the
  // moment the user moves the mouse.
  document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
  // Panel-owned surfaces (inline editors, full-screen forms, pending confirms)
  // are NOT swept here any more: the scope host shelved them when settings
  // closed, and a draft editor leaves through its own exit motion rather than
  // being yanked out of #main-content.
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
  // Cancel any active shortcut recording before switching panels — the
  // capture handler is document-level and would otherwise keep recording
  // from the wrong panel.
  if (this._recordingShortcutId) {
    if (this._captureHandler) {
      document.removeEventListener("keydown", this._captureHandler, true);
    }
    this._stopShortcutCountdown();
    this._recordingShortcutId = null;
    delete document.documentElement.dataset.shortcutRecording;
    this.renderShortcuts();
  }

  // Every floating surface that belongs to a settings panel — an unanswered
  // unbind confirm, an inline editor draft, a full-screen form — is shelved
  // and restored by the scope host, exactly like session surfaces follow their
  // conversation. Must run BEFORE currentPanel is overwritten: it doubles as
  // the outgoing panel key. Re-entering the same panel passes "" so the
  // restore path still runs.
  const prevPanel = (this.currentPanel as string) || "";
  setSurfacePanel(id);
  swapSurfacePanel(prevPanel === id ? "" : prevPanel, id);
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
    this.panels.skills.classList.add("active");
    this.renderSkills();
  } else if (id === "flow") {
    this.panels.flow.classList.add("active");
    this.renderFlow();
  } else if (id === "model") {
    this.panels.model.classList.add("active");
    this.renderModel();
  } else if (id === "gateway") {
    this.panels.gateway.classList.add("active");
    this.renderGateway();
  } else if (id === "assist") {
    // Assist Panels: one landing page lists every auxiliary surface
    // (Terminal / Explorer / Review / Browser) as storage-page "Data Center"
    // controls; the top-right arrow drills into its editor card. The render
    // is skipped while a card is open so the draft survives panel hops.
    this.panels.assist.classList.add("active");
    this.renderAssistPanel();
  } else if (id === "mcp") {
    this.panels.mcp.classList.add("active");
    this.renderMcpList();
  } else if (id === "agent") {
    this.panels.agent.classList.add("active");
    this.renderAgent();
  } else if (id === "memory") {
    // 记忆: the relationship graph owns the whole panel; a node opens its
    // reader as a sub-page card.
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
  } else if (id === "library") {
    this.panels.library.classList.add("active");
    this.renderLibrary();
  } else if (id === "developer") {
    this.panels.developer.classList.add("active");
    this.renderDeveloper();
  } else if (id === "plugins") {
    this.panels.plugins.classList.add("active");
    this.renderPlugins();
  } else {
    // Plugin-contributed panels are not part of the constructor-time
    // `panels` snapshot; resolve them from the DOM by id.
    const el = this.panels[id as PanelId] || document.getElementById(`panel-${id}`);
    el?.classList.add("active");
  }
}

export function showSkillDetailImpl(this: any, skillName: string, isEdit = false): void {
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

export function showProviderCreateImpl(this: any): void {
  this.modelCreateActive = true;
  this._renderProviderDialog();
}

export function showProviderEditImpl(this: any, idx: number): void {
  this.modelCreateActive = true;
  this._renderProviderDialog(idx);
}

export function renderAllImpl(this: any): void {
  // Per-panel boundaries AND per-panel scheduler keys: a broken settings
  // panel keeps its old DOM, never prevents the remaining panels from
  // rendering, and repeated calls in one frame coalesce like every other
  // surface.
  const run = (id: string, fn: () => void) =>
    scheduleRender(`settings:${id}`, () => guardRender("settings-panel", id, fn), 60);
  run("general", () => this.renderGeneral());
  run("flow", () => this.renderFlow());
  scheduleRender("settings:usage", () => {
    void guardAsync("settings-panel", "usage", () => this._renderUsageSection());
  }, 60);
  run("shortcuts", () => this.renderShortcuts());
  run("model", () => this.renderModel());
  run("gateway", () => this.renderGateway());
  run("assist", () => this.renderAssistPanel());
  run("library", () => this.renderLibrary());
  run("skills", () => this.renderSkills());
  run("memory", () => this.renderMemory());
  run("mcp", () => this.renderMcp());
  run("agent", () => this.renderAgent());
  run("about", () => this.renderAbout());
  if (isDevModeEnabled()) run("developer", () => this.renderDeveloper());
}
