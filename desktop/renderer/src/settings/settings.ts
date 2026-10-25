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

import { getState, setSettings, setCustomCommands, setTheme, setThemePreference, subscribe, showToast, isEnabled, openRoutedLink, setLibraryContent } from "../core/state.js";
import { createMemoryGraph } from "../components/MemoryGraph.js";
import { send } from "../core/ws.js";
import { waitForModelValidation, onWechatScanResult, refreshAllData } from "../core/stream.js";
import { setModelConfigs, setMcpServers, setProviders, setSkillsList, setSubAgents } from "../core/state.js";
import { guardRender } from "../core/ui_boundary.js";
import { runScheduledRenderNow } from "../core/render_scheduler.js";
import type { ModelConfigMeta, MCPServerConfig, SkillInfo, ModelCatalog, McpCatalog, McpProviderEntry, ProviderEntry, ProfileData, CustomCommand, UsageStatsData, UsageStatsSessionEntry } from "../core/types.js";
import { defaultSearchFilter } from "../core/types.js";
import { Dialog } from "../ui/dialog.js";
import { registerSurface, unregisterSurface } from "../core/scope.js";
import {
  bindTargetModelSelection,
  parseTargetModelSelection,
  readTargetModelSelection,
  renderTargetModelSelection,
  serializeTargetModelSelection,
} from "./model-selection.js";
import { t, initLocale, setLocale, getLocale, getIntlLocale, clearLocaleCache, onLocaleChange, LOCALES, type Locale } from "../features/i18n.js";
import { removeAnimated } from "../ui/animate.js";
import { EALoader } from "../features/ealoader.js";
import { applyServerCommands } from "../ui/slash-commands.js";
import { renderMarkdown } from "../chat/chat.js";
import { handleMarkdownContentClick } from "../chat/chat_markdown.js";
import { platformIconHtml, searchEngineIconSrc } from "../ui/icons.js";
import { formatShortcut } from "../ui/shortcut-display.js";
import { initSettingsDomEvents } from "./events_bind.js";
import { renderModelImpl, renderGatewayImpl } from "./model-panels.js";
import { modeHintImpl, openNetworkProxyEditorImpl, renderGeneralImpl, renderFlowImpl, renderShortcutsImpl, shortcutsRowsHtmlImpl, bindShortcutsCardImpl, openShortcutsEditorImpl, refreshOpenShortcutsEditorImpl, saveSettingImpl, saveThemeImpl, saveSkinImpl, escImpl, _maxTokensDefaultImpl } from "./general.js";
import { renderDropdownImpl, bindDropdownImpl, type DropdownOption } from "../ui/dropdown.js";
import { PERMISSION_MODES, permissionModeGroup, permissionModeLabel, permissionModeIcon, guardPermissionMode, dialogPermissionConfirm } from "../core/permission_mode.js";
import { parseAssistTiles, defaultHomeTiles, layoutHomeTiles, ASSIST_TILE_ICONS, DEFAULT_TILE_IDS, type AssistTileCfg } from "../session/session_inner.js";
import { mountPluginSettingPages, CONTRIBUTIONS_CHANGED } from "../features/plugin_contributions.js";
import { renderPluginPanelImpl } from "./plugin-panels.js";
import { bindVersionTapUnlockImpl, loadVersionsImpl, handleAgentTapImpl, handleDesktopTapImpl, openImpl, openModelCreateImpl, updateSidebarNavImpl, closeImpl, _cleanupTransientOverlaysImpl, focusSearchImpl, onSearchInputImpl, filterNavItemsImpl, _updateDividerVisibilityImpl, highlightInPanelImpl, isHighlightableImpl, clearHighlightsImpl, refreshCurrentPanelImpl, switchPanelImpl, showSkillDetailImpl, showSkillListImpl, showMcpCreateImpl, showMcpListImpl, showAgentCreateImpl, showAgentListImpl, showProviderCreateImpl, showProviderEditImpl, renderAllImpl } from "./lifecycle.js";
// Chart.js is lazy-loaded on first render of the usage panel (see
// _renderUsageSection) so it no longer blocks startup parsing.
import { showTooltipAt, hideTooltip } from "../ui/tooltip.js";
import { attachInputExpand } from "../ui/input-expand.js";
import { TransitionHelper } from "../ui/transition-helper.js";
import { SEARCH_ENGINES, getDefaultSearchEngine } from "../features/browser.js";
import { searchSettingsNavItems, SEARCH_FILTER_META, SETTINGS_NAV_ITEMS } from "../features/search.js";
import { setSearchFilter } from "../core/state.js";

/** Context-window presets (union of officially documented provider windows). */
const CTX_PRESETS: Array<{ v: number; label: string }> = [
  { v: 0, label: "Auto" },
  { v: 8192, label: "8K" },
  { v: 16384, label: "16K" },
  { v: 32768, label: "32K" },
  { v: 65536, label: "64K" },
  { v: 131072, label: "128K" },
  { v: 200000, label: "200K" },
  { v: 262144, label: "256K" },
  { v: 400000, label: "400K" },
  { v: 524288, label: "512K" },
  { v: 1048576, label: "1M" },
  { v: 2097152, label: "2M" },
  { v: 10000000, label: "10M" },
];

/** Wraps an async operation with a loading spinner on the button. */
export async function withLoading<T>(btn: HTMLButtonElement | null, fn: () => Promise<T>): Promise<T> {
  // Live-saving editors call save routines without a button — nothing to
  // decorate, just run the work.
  if (!btn) return fn();
  const origText = btn.textContent || "";
  const origDisabled = btn.disabled;
  btn.innerHTML = `<span class="btn-loading-icon"><i data-lucide="loader" style="width:14px;height:14px"></i></span> ${origText}`;
  btn.classList.add("btn-loading");
  btn.disabled = true;
  if (typeof (window as any).lucide !== "undefined") {
    (window as any).lucide.createIcons({ root: btn });
  }
  try {
    return await fn();
  } finally {
    btn.innerHTML = origText;
    btn.classList.remove("btn-loading");
    btn.disabled = origDisabled;
  }
}

initLocale();

const APP_VERSION = "0.5.0";

/** Frozen 3×3 pixel chevron — mirrors the chat sub-agent glyph
 *  (chat_item_render.ts `.sa-loader` rendered at `data-running="false"`:
 *  settled frame is a right-pointing chevron through cells 2/4/8).  Settings
 *  must match the conversation UI, which already swapped `sparkles` for this. */
const SUB_AGENT_GLYPH =
  '<span class="sa-loader" data-running="false"><i></i><i></i><i class="sa-chevron"></i><i></i><i class="sa-chevron"></i><i></i><i></i><i></i><i class="sa-chevron"></i></span>';

export type PanelId = "general" | "flow" | "usage" | "shortcuts" | "storage" | "model" | "gateway" | "library" | "skills" | "plugins" | "mcp" | "agent" | "about" | "developer" | "memory" | "assist";

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

const PERMISSION_OPTIONS = ["default", "auto", "bypass", "custom"];

// Validation outcomes are cached so a slow or failing endpoint never forces a
// full re-run: the next save only re-probes the models that failed, timed out
// or whose configuration changed.  The cache key is the identity of the probe
// inputs (backend, endpoint, credentials, headers, model ID and display name);
// any change to those re-validates the row.
const VALIDATION_CACHE_TTL_MS = 30 * 60 * 1000;
let _validationCache: Map<string, { result: unknown; at: number }> = new Map();

function _validationSignature(backend: string, baseUrl: string, apiKey: string, headers: Record<string, string>, modelId: string, name: string): string {
  return JSON.stringify([backend, baseUrl, apiKey, Object.keys(headers).sort().map((k) => [k, headers[k]]), modelId, name]);
}

function _validationCacheKey(signature: string, multimodal: boolean, probeReasoning: boolean, tokensDefault: number): string {
  // "v2" busts persisted stamps computed under the old probe semantics
  // (unknown-veto and the blanket DeepSeek text-only declaration): every
  // saved row re-probes once against the fixed pipeline.
  return JSON.stringify(["v2", signature, multimodal, probeReasoning, tokensDefault]);
}

function _cachePut(key: string, result: unknown): void {
  _validationCache.set(key, { result, at: Date.now() });
}

function _cacheGet(key: string): unknown | null {
  const hit = _validationCache.get(key);
  if (!hit) return null;
  if (Date.now() - hit.at > VALIDATION_CACHE_TTL_MS) {
    _validationCache.delete(key);
    return null;
  }
  return hit.result;
}

// The in-memory cache dies with the renderer, so an app restart -- or an edit
// days later -- re-probed EVERY model of a provider even when only one row
// was new.  This digest is the persistent counterpart: it is saved with the
// model (ModelConfig.validated_key) and recomputed on every save.  A row
// whose digest matches already passed validation for exactly this config, so
// the probe is skipped and its stored results are reused; any change to
// credentials, endpoint, headers, model id, name or capability toggles moves
// the digest and re-validates just that row.  The API key feeds the digest
// but is never stored in it.
function _validationStamp(cacheKey: string, apiKey: string): string {
  const input = `${cacheKey}\u0000${apiKey}`;
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < input.length; i++) {
    const ch = input.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
}

/** Per-model detail state edited inside the model sub-page.  Every field is
 *  scoped to ONE model row: same provider, different models may differ. */
type ProvModelDetail = {
  context_window: number;
  level: string;
  multimodal: boolean;
  thinking: boolean;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  /** Digest of the validation probe this row passed (empty = unvalidated). */
  validated_key?: string;
};

function _defaultModelDetail(): ProvModelDetail {
  return { context_window: 0, level: "", multimodal: false, thinking: true };
}

function _buildValidatedModel(r: { model_id: string; name: string; enabled: boolean; detail: ProvModelDetail }, result: any, tokensDefault: number): import("../core/types.js").ProviderModelMeta {
  // Capability outcomes: only a DEFINITIVE probe negative keeps the
  // capability off for that model.  An inconclusive probe ("unknown" -- 400
  // quirks, 429, WAF, timeout) must never veto the user's explicit toggle:
  // relays often reject the tiny probe image yet accept real ones, and the
  // probe's own contract says unknown is not a permanent negative.
  // Capabilities and sampling come from the per-model sub-page, never from a
  // provider-level switch.
  const multiOk = r.detail.multimodal && result?.multimodal_support !== "unsupported";
  const reasoningOk = result?.capabilities?.reasoning !== "unsupported";
  return {
    name: r.name || r.model_id,
    model_id: r.model_id,
    // Per-turn output budget: provider default (the per-model pill
    // is the context window, not max_tokens).
    max_tokens: tokensDefault,
    ...(r.detail.context_window > 0 ? { context_window: r.detail.context_window } : {}),
    enabled: r.enabled,
    multimodal: multiOk,
    multimodal_support: result?.multimodal_support,
    reasoning_levels: Array.isArray(result?.reasoning_levels) ? result.reasoning_levels : [],
    capabilities: result?.capabilities || {},
    thinking_config: r.detail.thinking
      ? (reasoningOk
          ? { type: "enabled" as const, enabled: true, level: r.detail.level, selectable: true }
          : { type: "enabled" as const, enabled: true, level: "", selectable: false })
      : { type: "enabled" as const, enabled: true, level: "", selectable: false },
    ...(r.detail.temperature !== undefined ? { temperature: r.detail.temperature } : {}),
    ...(r.detail.top_p !== undefined ? { top_p: r.detail.top_p } : {}),
    ...(r.detail.top_k !== undefined ? { top_k: Math.round(r.detail.top_k) } : {}),
    ...(r.detail.validated_key ? { validated_key: r.detail.validated_key } : {}),
  };
}

/** The four auxiliary session surfaces the Assist panel launches into. */
type AssistKind = "terminal" | "editor" | "review" | "browser";

/** xterm's default word separators.  A named constant because the value
 *  contains both a double quote and a backtick, which is exactly the kind of
 *  literal that breaks when it is written inline inside a template string. */
const DEFAULT_TERM_WORD_SEPARATORS = "  ()[]{}\",`";


export class Settings {
  private nav: HTMLElement;
  private currentPanel: PanelId = "general";
  private panels: Record<PanelId, HTMLElement>;
  private searchInput: HTMLInputElement;
  private searchTimer: number = 0;
  private _versions: { desktop: string; agent: string } | null = null;
  private _usageBarChart: import("chart.js").Chart | null = null;
  private _usageBarChartData: {
    labels: string[];
    datasets: any[];
  } | null = null;
  private _usageGroupBy: "model" | "channel" = "model";
  // Reentrancy guard for the async usage render.  Because _renderUsageSection
  // awaits `import("chart.js")`, concurrent triggers (refresh callback + tab
  // switch + autorun on usageStats change) could otherwise stack multiple
  // Chart.js instances onto the canvas that innerHTML just replaced — that is
  // what produced the perpetual "刷刷刷" flicker.  Overlapping triggers are
  // coalesced into a single re-run via _usageRenderPending.
  private _usageRendering: boolean = false;
  private _usageRenderPending: boolean = false;
  // Render-dedup: state replaces the usageStats object on every push, so
  // reference equality is an exact "did the data change" test.  Panel
  // switches re-enter _renderUsageSection constantly; without this check
  // every visit blinked the loading shimmer over identical data and looked
  // like the panel was being re-fetched.
  private _lastUsageStatsRendered: UsageStatsData | null | undefined = undefined;
  private _lastUsageMutedRendered: string | null = null;
  private _lastUsageGroupByRendered: "model" | "channel" | null = null;
  private _lastUsageLocaleRendered: Locale | null = null;
  private _recordingShortcutId: string | null = null;
  private _captureHandler: ((ev: KeyboardEvent) => void) | null = null;
  private _recordingTimer: ReturnType<typeof setInterval> | null = null;
  private _recordingDeadline: number | null = null;
  /** The `.settings-card` inside an open 快捷键 editor card (个性化 → 快捷键),
   *  kept so an in-place repaint during recording stays live. */
  private _shortcutsEditorCard: HTMLElement | null = null;

  /** Clear the 20s recording countdown timer. Safe to call repeatedly. */
  private _stopShortcutCountdown(): void {
    if (this._recordingTimer !== null) {
      clearInterval(this._recordingTimer);
      this._recordingTimer = null;
    }
    this._recordingDeadline = null;
  }

  constructor() {
    this.nav = document.querySelector(".sidebar-settings-items")!;
    this.panels = {
      general: document.getElementById("panel-general")!,
      flow: document.getElementById("panel-flow")!,
      usage: document.getElementById("panel-usage")!,
      shortcuts: document.getElementById("panel-shortcuts")!,
      storage: document.getElementById("panel-storage")!,
      model: document.getElementById("panel-model")!,
      gateway: document.getElementById("panel-gateway")!,
      library: document.getElementById("panel-library")!,
      skills: document.getElementById("panel-skills")!,
      plugins: document.getElementById("panel-plugins")!,
      assist: document.getElementById("panel-assist")!,
      mcp: document.getElementById("panel-mcp")!,
      agent: document.getElementById("panel-agent")!,
      about: document.getElementById("panel-about")!,
      developer: document.getElementById("panel-developer")!,
      memory: document.getElementById("panel-memory")!,
    };

    this.searchInput = document.querySelector(".sidebar-settings-search-input") as HTMLInputElement;
    this.searchInput?.addEventListener("input", () => this.onSearchInput());

    document.getElementById("btn-settings-back")?.addEventListener("click", () => this.close());

    this.bindVersionTapUnlock();
    this.loadVersions();

    this.nav.addEventListener("click", (e) => {
      const target = (e.target as HTMLElement).closest(".settings-nav-item") as HTMLElement | null;
      if (!target) return;
      const panel = target.getAttribute("data-panel") as PanelId;
      if (panel) this.switchPanel(panel);
    });

    // Plugin-contributed pages and rows reuse the view's own dropdown and
    // saveSetting machinery, so they look and persist exactly like the
    // first-party settings.  Re-mounted whenever the contribution set changes
    // (plugin_ui_sync / hot reload).
    const pluginHelpers = {
      saveSetting: (key: string, value: string) => this.saveSetting(key, value),
    };
    mountPluginSettingPages(pluginHelpers);
    window.addEventListener(CONTRIBUTIONS_CHANGED, () => mountPluginSettingPages(pluginHelpers));

    initSettingsDomEvents.call(this);
  }

  private bindVersionTapUnlock(): void { bindVersionTapUnlockImpl.call(this); }
  private async loadVersions(): Promise<void> { return loadVersionsImpl.call(this); }
  private handleAgentTap(): void { handleAgentTapImpl.call(this); }
  private handleDesktopTap(): void { handleDesktopTapImpl.call(this); }
  open(): void { openImpl.call(this); }
  public openModelCreate(): void { openModelCreateImpl.call(this); }
  /** Open settings on the memory panel, enter its editor card and show the
   *  requested memory on the reader sub-page. Used by the summary panel's
   *  memory reference rows. */
  public openMemoryDetail(path: string): void {
    this.open();
    this.switchPanel("memory");
    this._openMemoryEditor(path);
  }
  private updateSidebarNav(): void { updateSidebarNavImpl.call(this); }
  close(): void { void closeImpl.call(this); }
  private _cleanupTransientOverlays(): void { _cleanupTransientOverlaysImpl.call(this); }
  focusSearch(): void { focusSearchImpl.call(this); }
  private onSearchInput(): void { onSearchInputImpl.call(this); }
  private filterNavItems(q: string): void { filterNavItemsImpl.call(this, q); }
  private _updateDividerVisibility(): void { _updateDividerVisibilityImpl.call(this); }
  private highlightInPanel(panelId: PanelId, query: string): void { highlightInPanelImpl.call(this, panelId, query); }
  private isHighlightable(el: HTMLElement, panel: HTMLElement): boolean { return isHighlightableImpl(el, panel); }
  private clearHighlights(): void { clearHighlightsImpl(); }

  private modelCreateActive = false;

  private refreshCurrentPanel(): void { refreshCurrentPanelImpl.call(this); }
  switchPanel(id: PanelId): void { switchPanelImpl.call(this, id); }
  private showSkillDetail(skillName: string, isEdit = false): void { showSkillDetailImpl.call(this, skillName, isEdit); }
  private showSkillList(): void { showSkillListImpl.call(this); }
  private showMcpCreate(editIdx?: number): void { showMcpCreateImpl.call(this, editIdx); }
  private showMcpList(): void { showMcpListImpl.call(this); }
  private showAgentCreate(existing?: import("../core/types.js").SubAgentConfig): void { showAgentCreateImpl.call(this, existing); }
  private showAgentList(): void { showAgentListImpl.call(this); }
  private showProviderCreate(): void { showProviderCreateImpl.call(this); }
  private showProviderEdit(idx: number): void { showProviderEditImpl.call(this, idx); }
  private showModelPoolSettings(idx: number): void { this._renderModelPoolSettingsDialog(idx); }
  renderAll(): void { renderAllImpl.call(this); }

  renderPlugins(): void { renderPluginPanelImpl.call(this); }
  private renderDropdown(id: string, options: DropdownOption[], currentId: string, onChange: (val: string) => void): string { return renderDropdownImpl.call(this, id, options, currentId, onChange); }
  private modeHint(modeKey: string): string { return modeHintImpl.call(this, modeKey); }
  private bindDropdown(id: string, onChange: (val: string) => void): void { bindDropdownImpl.call(this, id, onChange); }

  private renderGeneral(): void { renderGeneralImpl.call(this); }

  private renderFlow(): void { renderFlowImpl.call(this); }

  /** 系统网络代理子页 — opens as an inline panel editor so it survives
   *  panel hops exactly like the assist cards. */
  private _openNetworkProxyEditor(): void { openNetworkProxyEditorImpl.call(this); }

  private renderShortcuts(): void { renderShortcutsImpl.call(this); }

  /** The 个性化 panel's 快捷键 row opens the keybinds as an inline editor card
   *  (same shape as the Assist panel's rows). */
  private openShortcutsEditor(): void { openShortcutsEditorImpl.call(this); }
  private shortcutsRowsHtml(): string { return shortcutsRowsHtmlImpl.call(this); }
  private bindShortcutsCard(card: HTMLElement): void { bindShortcutsCardImpl.call(this, card); }
  private refreshOpenShortcutsEditor(): void { refreshOpenShortcutsEditorImpl.call(this); }

  private saveSetting(key: string, value: string): void { saveSettingImpl.call(this, key, value); }

  private saveTheme(value: string): void { saveThemeImpl.call(this, value); }
  private saveSkin(value: string): void { saveSkinImpl.call(this, value); }

  private esc(s: string): string { return escImpl(s); }

  private _maxTokensDefault(backendType: string, context?: number): number { return _maxTokensDefaultImpl(backendType, context); }

  // Every panel-list render below bails out while that panel holds an inline
  // editor draft: re-painting the list would wipe the form the user is filling
  // in (and lose the caret) on every state push.  The panel repaints itself
  // once the editor closes (`_renderPanelBody`).
  private renderModel(): void {
    if (this._panelEditorActive("model")) return;
    renderModelImpl.call(this);
  }

  private renderGateway(): void {
    if (this._panelEditorActive("gateway")) return;
    renderGatewayImpl.call(this);
  }

  /** Open a modal dialog (model-config style) for any adapter. */
  private _showAdapterConfigDialog(defId: string): void {
    // Connector cards are plugin-provided now: the declarative ui spec rides
    // the gateway status payload (GatewayAdapterSpec.ui on the backend), so
    // there is no renderer-side per-adapter table anymore.
    const ui = ((getState().gatewayStatus?.adapters ?? []).find(a => a.name === defId)?.ui ?? {}) as {
      name?: string; desc?: string; docs?: string; qr?: boolean;
      fields?: { key: string; labelKey: string; type: "text" | "password" | "number" | "textarea"; placeholderKey?: string }[];
    };
    const def = { id: defId, name: ui.name || defId, desc: ui.desc || "", docs: ui.docs, fields: ui.fields || [] };
    const st = getState();
    const s = st.settings;
    const tFn = t;

    // Build bodyHtml from fields
    let bodyHtml = "";
    const isQrAdapter = ui.qr === true;
    // Live adapter state is the source of truth for QR adapters. WeChat
    // credentials live in backend runtime keys, so the local settings mirror
    // may not carry an app_id — only QQ requires the persisted one.
    const qrConnected = isQrAdapter
      && ((st.gatewayStatus?.adapters ?? []).find(a => a.name === defId)?.connected ?? false)
      && (defId === "weixin" || defId === "wecom"
        || !!(s[`adapter_${defId}_app_id` as keyof typeof s] as string));
    if (isQrAdapter) {
      const appIdRaw = (s[`adapter_${defId}_app_id` as keyof typeof s] as string) ?? "";
      // WeChat persists as account_id on the backend (config sync key);
      // WeCom persists as bot_id / secret.
      const appId = defId === "weixin"
        ? (appIdRaw || (s["adapter_weixin_account_id" as keyof typeof s] as string) || "")
        : defId === "wecom"
          ? ((s["adapter_wecom_bot_id" as keyof typeof s] as string) ?? "")
          : appIdRaw;
      const secretKey = defId === "weixin" ? "token" : defId === "wecom" ? "secret" : "client_secret";
      const secret = (s[`adapter_${defId}_${secretKey}` as keyof typeof s] as string) ?? "";
      // api_url (frontend field key) vs base_url (backend sync key).
      const apiUrl = defId === "weixin"
        ? ((s["adapter_weixin_api_url" as keyof typeof s] as string)
          || (s["adapter_weixin_base_url" as keyof typeof s] as string) || "")
        : "";

      if (qrConnected) {
        const secretLabel = defId === "weixin"
          ? tFn("settings.fieldToken")
          : defId === "wecom" ? tFn("settings.fieldSecret") : tFn("settings.fieldClientSecret");
        const appIdLabel = defId === "wecom" ? tFn("settings.fieldWecomBotId") : tFn("settings.fieldAppId");
        const apiUrlRow = defId === "weixin" ? `
          <div class="model-form-row" style="user-select:none;-webkit-user-select:none">
            <label class="model-form-label">${tFn("settings.fieldWeixinApiUrl")}</label>
            <input type="text" class="model-form-input" value="${this.esc(apiUrl)}" readonly style="user-select:none;-webkit-user-select:none" />
          </div>` : "";
        // No required marks here: credentials come from the QR bind flow,
        // nothing is manually filled, and every field is read-only.
        bodyHtml = `
          <div class="model-form-row" style="user-select:none;-webkit-user-select:none">
            <label class="model-form-label">${appIdLabel}</label>
            <input type="text" class="model-form-input" value="${this.esc(appId)}" readonly style="user-select:none;-webkit-user-select:none" />
          </div>
          <div class="model-form-row" style="user-select:none;-webkit-user-select:none">
            <label class="model-form-label">${secretLabel}</label>
            <div class="model-form-input-wrap" style="width:100%">
              <input type="password" id="dlg-weixin-token-input" class="model-form-input model-form-input--reveal" value="${this.esc(secret)}" readonly style="user-select:none;-webkit-user-select:none" />
              <button class="model-form-eye-btn" id="dlg-weixin-eye-btn" type="button" data-eye-target="dlg-weixin-token-input" title="显示">
                <i data-lucide="eye"></i>
              </button>
            </div>
          </div>${apiUrlRow}`;
      } else {
        // Not connected — show QR scan area.
        // `flex: 1 1 auto` makes the area consume the leftover height of the
        // (flex-column) dialog body, so `justify-content: center` actually has
        // surplus space to center in; `min-height` is only the floor before
        // the image paints. Without this the area collapses to its content
        // height and the QR image visually sits in the top third.
        bodyHtml = `<div id="dlg-weixin-qr-area" style="display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;flex:1 1 auto;min-height:240px">
          <div style="position:relative;display:inline-block;margin:0 auto">
            <img id="dlg-weixin-qr-img" draggable="false" style="display:none;width:240px;height:240px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.12);user-select:none;-webkit-user-drag:none" />
            <div id="dlg-weixin-qr-success" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,0.35);border-radius:12px;align-items:center;justify-content:center">
              <div class="qr-overlay-icon" style="width:56px;height:56px;border-radius:50%;background:#07C160;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 24 24" width="32" height="32" style="color:#fff;display:block"><path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
              </div>
            </div>
            <div id="dlg-weixin-qr-refresh" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,0.35);border-radius:12px;align-items:center;justify-content:center;cursor:pointer">
              <div class="qr-overlay-icon" style="width:56px;height:56px;border-radius:50%;background:#9aa0a6;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 24 24" width="32" height="32" style="color:#fff;display:block"><path fill="currentColor" d="M17.65 6.35A7.96 7.96 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
              </div>
            </div>
          </div>
          <div id="dlg-weixin-qr-status" style="padding:8px 0 0;font-size:14px;color:var(--text-muted)">${tFn("settings.wechatScanning")}</div>
          <div id="dlg-weixin-qr-countdown" style="display:none;padding:4px 0 0;font-size:13px;color:var(--text-muted)"></div>
        </div>`;
      }
    } else {
      for (const f of def.fields) {
        const val = (s[`adapter_${defId}_${f.key}` as keyof typeof s] as string) ?? "";
        const ph = f.placeholderKey ? tFn("settings." + f.placeholderKey) : "";
        const phAttr = ph ? ` placeholder="${this.esc(ph)}"` : "";
        if (f.type === "textarea") {
          bodyHtml += `
                <div class="model-form-row">
                    <label class="model-form-label" for="dlg-${defId}-${f.key}"><span class="model-form-required">*</span>${tFn("settings." + f.labelKey)}</label>
                    <div class="input-wrapper input-wrapper--dialog"><textarea id="dlg-${defId}-${f.key}" class="setting-rich-text" style="min-height:80px" spellcheck="false"${phAttr}>${this.esc(val)}</textarea></div>
                </div>`;
        } else if (f.type === "password") {
          bodyHtml += `
                <div class="model-form-row">
                    <label class="model-form-label" for="dlg-${defId}-${f.key}"><span class="model-form-required">*</span>${tFn("settings." + f.labelKey)}</label>
                    <div class="model-form-input-wrap">
                        <input type="password" id="dlg-${defId}-${f.key}" class="model-form-input model-form-input--reveal" value="${this.esc(val)}" spellcheck="false" autocomplete="off"${phAttr} />
                        <button class="model-form-eye-btn" type="button" data-eye-target="dlg-${defId}-${f.key}" title="显示"><i data-lucide="eye"></i></button>
                    </div>
                </div>`;
        } else {
          bodyHtml += `
                <div class="model-form-row">
                    <label class="model-form-label" for="dlg-${defId}-${f.key}"><span class="model-form-required">*</span>${tFn("settings." + f.labelKey)}</label>
                    <input type="${f.type}" id="dlg-${defId}-${f.key}" class="model-form-input" value="${this.esc(val)}" spellcheck="false"${phAttr} />
                </div>`;
        }
      }
    }

    // ── Model selection section (reuse push-gateway card style) ──
    // QR-login adapters (WeChat, QQ) must not show the model pool until the
    // account is actually connected, otherwise it shows before the QR is
    // even fetched.
    if (!isQrAdapter || qrConnected) {
      const models = st.modelConfigs || [];
      const selectedModels = parseTargetModelSelection(s[`adapter_${defId}_models` as keyof typeof s] as string);
      bodyHtml += renderTargetModelSelection({ id: defId, models, selected: selectedModels, t: tFn, disabled: st.settings.model_pool_enabled === false });
    }

    const titleKey = `settings.adapterName${defId.charAt(0).toUpperCase() + defId.slice(1)}`;
    const title = `Connector : ${tFn(titleKey)}`;
    // Route through the shared form-dialog entry: inside the gateway panel
    // the dialog docks into the same panel-editor floating card the model
    // editing dialogs use (centered over the content area); other panels
    // fall back to the full-screen modal.
    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    // Bind reveal-eye toggles for every password field in this dialog.
    this._bindEyeToggles(overlay);
    // Bind model selection click events
    bindTargetModelSelection(overlay, defId);

    // Inject docs link into title bar. QR-scan adapters (WeChat, QQ) skip it:
    // they need no manual configuration, so there is nothing to document.
    if (def.docs && !isQrAdapter) {
      const titleBar = overlay.querySelector(".toast-title") as HTMLElement;
      if (titleBar) {
        titleBar.style.display = "flex";
        titleBar.style.justifyContent = "space-between";
        titleBar.style.alignItems = "center";
        titleBar.innerHTML = '<span>' + this.esc(title) + '</span><a href="#" class="model-get-apikey-link" id="dlg-' + defId + '-docs" style="font-size:12px">' + tFn("settings.viewDocs") + '</a>';
        const docsLink = titleBar.querySelector('#dlg-' + defId + '-docs') as HTMLAnchorElement;
        if (docsLink) {
          docsLink.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            openRoutedLink(def.docs || "");
          });
        }
      }
    }

    if (isQrAdapter) {
      if (qrConnected) {
        // Connected: turn OK button into "解绑" (danger), add eye toggle
        okBtn.textContent = tFn("settings.wechatUnbind");
        okBtn.className = "btn";
        const unbindKeys: Record<string, any> = defId === "weixin"
          ? {
              adapter_weixin_enabled: false, adapter_weixin_app_id: "",
              adapter_weixin_token: "", adapter_weixin_api_url: "",
              adapter_weixin_account_id: "", adapter_weixin_base_url: "",
            }
          : defId === "wecom"
            ? {
                adapter_wecom_enabled: false, adapter_wecom_bot_id: "",
                adapter_wecom_secret: "",
              }
            : {
                adapter_qqbot_enabled: false, adapter_qqbot_app_id: "",
                adapter_qqbot_client_secret: "",
              };
        const unbindFn = () => {
          const platformName = tFn(`settings.adapterName${defId.charAt(0).toUpperCase() + defId.slice(1)}`);
          Dialog.confirm(tFn("settings.wechatUnbind"), tFn("settings.wechatUnbindConfirm").replace("{name}", platformName)).then((confirmed) => {
            if (confirmed) {
              send({ type: "configure", config: unbindKeys });
              setSettings({ ...getState().settings, ...unbindKeys } as any);
              this.renderGateway();
              close();
            }
          });
        };
        okBtn.addEventListener("click", unbindFn);

        // Eye toggle for token field
        this._bindEyeToggles(overlay);
      } else {
        // Not connected: QR scan mode — hide OK button
        okBtn.style.display = "none";

        // Mark the dialog open so the scan-result handler accepts events,
        // and keep the close callback so the handler can dismiss it.
        (this as any)._wechatDialogOpen = true;
        (this as any)._qrAdapterId = defId;
        (this as any)._qrResultReceived = false;
        (this as any)._closeWeixinDialog = close;

        // Start QR scan when dialog opens
        send({ type: "wechat_scan", adapter_id: defId });

        // QR refresh overlay click
        const refreshEl = overlay.querySelector("#dlg-weixin-qr-refresh") as HTMLElement;
        if (refreshEl) {
          refreshEl.addEventListener("click", () => {
            refreshEl.style.display = "none";
            (this as any)._qrResultReceived = false;
            const statusEl = document.getElementById("dlg-weixin-qr-status");
            if (statusEl) { statusEl.textContent = tFn("settings.wechatScanning"); statusEl.style.display = "block"; }
            const img = document.getElementById("dlg-weixin-qr-img") as HTMLImageElement | null;
            if (img) { img.style.display = "none"; img.src = ""; }
            send({ type: "wechat_scan", adapter_id: defId });
          });
        }

        // Override cancel to clean up
        const cancelBtn = overlay.querySelector("#dialog-form-cancel") as HTMLButtonElement;
        if (cancelBtn) {
          cancelBtn.addEventListener("click", () => {
            (this as any)._wechatDialogOpen = false;
            if ((this as any)._qrCountdown) clearInterval((this as any)._qrCountdown);
            this.renderGateway();
            close();
          });
        }
      }
    } else {
      okBtn.textContent = tFn("settings.adapterSave");

      // Save button
      okBtn.addEventListener("click", () => {
        const current = { ...getState().settings };
        const config: Record<string, any> = {};
        for (const f of def.fields) {
          const input = document.getElementById(`dlg-${defId}-${f.key}`) as HTMLInputElement | HTMLTextAreaElement;
          const val = input?.value ?? "";
          (current as any)[`adapter_${defId}_${f.key}`] = val;
          config[`adapter_${defId}_${f.key}`] = val;
        }
        const enableInput = document.getElementById(`adapter-enable-${defId}`) as HTMLInputElement;
        if (enableInput) {
          config[`adapter_${defId}_enabled`] = enableInput.checked;
        }
        // Read model selection
        const selectedModels = readTargetModelSelection(overlay, defId);
        const modelsJson = serializeTargetModelSelection(selectedModels);
        (current as any)[`adapter_${defId}_models`] = modelsJson;
        config[`adapter_${defId}_models`] = modelsJson;
        setSettings(current as any);
        send({ type: "configure", config });
        this.renderGateway();
        close();
      });
    }

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }
  }

  private _getCatalog(): ModelCatalog {
    return getState().modelCatalog;
  }

  private _findProvider(providerId: string): ProviderEntry | undefined {
    return this._getCatalog().providers.find(p => p.id === providerId);
  }

  private _providerLabel(backendType: string): string {
    return this._findProvider(backendType)?.label || backendType || "?";
  }

  /** Display name of a configured provider: custom providers use their
   * user-defined name, built-in ones their official catalog label. */
  private _providerDisplayName(p?: { backend_type: string; name?: string }): string {
    if (!p) return "?";
    if (p.backend_type === "openai_compatible") return p.name?.trim() || this._providerLabel(p.backend_type);
    return this._providerLabel(p.backend_type);
  }

  private _renderProviderDialog(editIdx?: number): void {
    const isEdit = editIdx !== undefined;
    const providers = getState().providers || [];
    const existing = isEdit ? providers[editIdx!] : undefined;
    const title = isEdit
      ? `Provider : ${this.esc(this._providerDisplayName(existing))}`
      : "Provider";
    const catalog = this._getCatalog();

    const catalogProviders = [...catalog.providers].sort((a, b) => a.label.localeCompare(b.label));
    const providerOptions: DropdownOption[] = catalogProviders.map(p => ({ id: p.id, label: p.label }));
    const initialBackend = existing?.backend_type || (catalogProviders.length > 0 ? catalogProviders[0].id : "");
    const catalogProvider = this._findProvider(initialBackend);
    const tokensDefault = this._maxTokensDefault(initialBackend);

    // Model rows: model ID + display name + detail entry (opens the
    // per-model sub-page) + delete + enable.  Context window, capabilities
    // and sampling all live in the sub-page and are stored PER MODEL --
    // same provider, different models, independently recorded configs.
    const legacySampling = {
      temperature: existing?.temperature,
      top_p: existing?.top_p,
      top_k: existing?.top_k,
    };
    const detailFromMeta = (m?: import("../core/types.js").ProviderModelMeta): ProvModelDetail => ({
      context_window: m?.context_window || 0,
      level: m?.thinking_config?.level || "",
      multimodal: m?.multimodal ?? false,
      thinking: m ? m.thinking_config?.selectable !== false : true,
      // Legacy provider-level sampling (old configs) seeds each model so
      // the per-model values survive the migration on first save.
      temperature: m?.temperature ?? legacySampling.temperature,
      top_p: m?.top_p ?? legacySampling.top_p,
      top_k: m?.top_k ?? legacySampling.top_k,
      validated_key: m?.validated_key || "",
    });
    const initialDetails: ProvModelDetail[] = [];
    const makeModelRow = (m?: import("../core/types.js").ProviderModelMeta): string => {
      initialDetails.push(m ? detailFromMeta(m) : _defaultModelDetail());
      return `
      <div class="prov-model-row">
        <div class="prov-model-main">
          <input type="text" class="model-form-input prov-m-id" placeholder="${t("settings.modelId")}" value="${m ? this.esc(m.model_id) : ""}" />
          <input type="text" class="model-form-input prov-m-name" placeholder="${t("settings.displayName")}" value="${m ? this.esc(m.name) : ""}" />
          <button type="button" class="btn-icon prov-model-detail" data-tooltip="${t("settings.modelDetail")}">
            <i data-lucide="settings-2" class="lucide"></i>
          </button>
          <button type="button" class="btn-icon btn-icon--danger prov-model-remove" data-tooltip="${t("settings.remove")}">
            <i data-lucide="trash-2" class="lucide"></i>
          </button>
          <label class="prov-model-enable" data-tooltip="${t("settings.enabled")}">
            <span class="toggle-switch toggle-sm">
              <input type="checkbox" class="prov-m-enabled" ${m?.enabled !== false ? "checked" : ""} />
              <span class="toggle-slider"></span>
            </span>
          </label>
        </div>
      </div>`;
    };

    const initialRows = (existing?.models || []).map(m => makeModelRow(m)).join("") || makeModelRow();

    const headerRowHtml = (k = "", v = ""): string => `
      <div class="mcp-env-row prov-header-row">
        <input type="text" class="model-form-input prov-h-key" placeholder="${t("settings.mcpEnvKey")}" value="${this.esc(k)}" />
        <input type="text" class="model-form-input prov-h-value" placeholder="${t("settings.mcpEnvValue")}" value="${this.esc(v)}" />
        <button type="button" class="btn-icon mcp-env-remove" data-tooltip="${t("settings.remove")}">
          <i data-lucide="trash-2" class="lucide"></i>
        </button>
      </div>`;
    const initialHeaderRows = Object.entries(existing?.headers || {}).map(([k, v]) => headerRowHtml(k, v)).join("");

    // Main view + per-model sub-page live in the SAME dialog container
    // (workspace-settings style): opening a model's detail page swaps the
    // visible view instead of stacking another dialog.
    const bodyHtml = `
      <div id="prov-main-view">
      <div class="model-form-row">
        <label class="model-form-label" for="prov-backend"><span class="model-form-required">*</span>${t("settings.providerSelect")}</label>
        <div class="model-form-dropdown-row">
          ${this.renderDropdown("prov-backend", providerOptions, initialBackend, () => {})}
          <input type="hidden" id="prov-backend-hidden" value="${this.esc(initialBackend)}" />
        </div>
      </div>
      <div class="model-form-row" id="prov-name-row" style="${initialBackend === "openai_compatible" ? "" : "display:none"}">
        <label class="model-form-label" for="prov-name"><span class="model-form-required">*</span>${t("settings.providerName")}</label>
        <input type="text" id="prov-name" class="model-form-input" placeholder="${t("settings.providerNamePlaceholder")}" value="${existing?.name ? this.esc(existing.name) : ""}" autocomplete="off" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label" for="prov-apikey">
          <span class="model-form-required" id="prov-key-required">*</span>${t("settings.apiKey")}
          ${catalogProvider?.docs ? `<a href="${this.esc(catalogProvider.docs)}" class="model-get-apikey-link">${t("settings.getApiKey")}</a>` : ""}
        </label>
        <div class="model-form-input-wrap">
          <input type="password" id="prov-apikey" class="model-form-input model-form-input--reveal" placeholder="${t("settings.enterApiKey")}" value="${existing ? this.esc(existing.api_key) : ""}" autocomplete="off" />
          <button class="model-form-eye-btn" type="button" data-eye-target="prov-apikey" title="显示"><i data-lucide="eye"></i></button>
        </div>
      </div>
      <div class="model-form-row">
        <label class="model-form-label model-form-label--inline" for="prov-url">
          <span class="model-form-required">*</span>${t("settings.baseUrl")}
          <button type="button" class="model-form-help-btn" data-tooltip="${t("settings.baseUrlHint")}" tabindex="-1">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
              <path d="M12 17h.01"/>
            </svg>
          </button>
        </label>
        <input type="text" id="prov-url" class="model-form-input" placeholder="${t("settings.baseUrlPlaceholder")}" value="${existing ? this.esc(existing.base_url) : (catalogProvider ? this.esc(catalogProvider.base_url) : "")}" />
      </div>
      <div class="model-form-row" style="flex-direction:column;align-items:stretch">
        <div class="prov-sec-row">
          <span class="prov-sec-label"><span class="model-form-required">*</span>${t("settings.providerModelConfig")}</span>
          <button type="button" class="btn-icon" id="prov-add-model" data-tooltip="${t("settings.addModel")}">
            <i data-lucide="plus" class="lucide"></i>
          </button>
        </div>
        <div id="prov-model-list">${initialRows}</div>
      </div>
      <div class="model-form-row" style="flex-direction:column;align-items:stretch">
        <div class="prov-sec-row">
          <span class="prov-sec-label">${t("settings.customHeaders")}</span>
          <button type="button" class="btn-icon" id="prov-add-header" data-tooltip="${t("settings.addHeader")}">
            <i data-lucide="plus" class="lucide"></i>
          </button>
        </div>
        <div id="prov-header-list">${initialHeaderRows}</div>
      </div>
      </div>
      <div id="prov-model-view" style="display:none"></div>`;

    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    // Two save paths: "Save & Test" (default confirm) runs the live endpoint
    // probes before persisting; "Save Only" persists without probing.  Both
    // share one save routine below.
    okBtn.textContent = t("settings.saveAndTest");
    const footerEl = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    const saveOnlyBtn = document.createElement("button");
    saveOnlyBtn.type = "button";
    saveOnlyBtn.className = "btn";
    saveOnlyBtn.id = "dialog-form-save-only";
    saveOnlyBtn.textContent = t("settings.saveOnly");
    if (footerEl) footerEl.insertBefore(saveOnlyBtn, okBtn);
    this._bindEyeToggles(overlay);

    // API keys never contain whitespace: sanitize pastes so a key copied
    // with trailing spaces / newlines lands clean instead of poisoning the
    // form with invisible characters.
    const apiKeyInput = overlay.querySelector("#prov-apikey") as HTMLInputElement | null;
    apiKeyInput?.addEventListener("paste", (e) => {
      const text = e.clipboardData?.getData("text") ?? "";
      const cleaned = text.replace(/\s+/g, "");
      if (!cleaned) {
        e.preventDefault();
        return;
      }
      if (text === cleaned) return;
      e.preventDefault();
      const el = e.currentTarget as HTMLInputElement;
      const start = el.selectionStart ?? el.value.length;
      const end = el.selectionEnd ?? el.value.length;
      el.value = el.value.slice(0, start) + cleaned + el.value.slice(end);
      const caret = start + cleaned.length;
      el.setSelectionRange(caret, caret);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });

    // Seed each initial row with its per-model detail state (from the saved
    // meta, or defaults for a brand-new row).  Detail state rides on the row
    // element, so deleting a row drops its config with it.
    const rowDetail = (row: Element): ProvModelDetail => {
      if (!(row as any)._detail) (row as any)._detail = _defaultModelDetail();
      return (row as any)._detail as ProvModelDetail;
    };
    overlay.querySelectorAll(".prov-model-row").forEach((row, i) => {
      (row as any)._detail = initialDetails[i] ?? _defaultModelDetail();
    });

    // ── Per-model sub-page (workspace-settings style) ───────────────────
    // Same dialog container, swapped views: the model list hides while the
    // detail page (context window / capabilities / sampling) shows.  Every
    // field is scoped to ONE model -- never shared across the provider.
    const mainView = overlay.querySelector("#prov-main-view") as HTMLElement;
    const modelView = overlay.querySelector("#prov-model-view") as HTMLElement;
    const titleEl = (overlay.querySelector(".panel-editor-title-text") as HTMLElement | null)
      || (overlay.querySelector(".toast-title") as HTMLElement | null);
    const mainTitle = titleEl?.textContent || "";
    const panelBack = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
    // The footer (Cancel / Confirm) belongs to the MAIN page only: both
    // buttons always operate on the WHOLE provider, never on a single model
    // sub-page.  The sub-page navigates back via the top-bar arrow instead.
    // `footerEl` is declared above (Save-Only button insertion).
    if (panelBack) panelBack.style.display = "none";
    let inDetail = false;
    let curDetail: ProvModelDetail | null = null;

    const goBackToMain = () => {
      modelView.innerHTML = "";
      modelView.style.display = "none";
      mainView.style.display = "";
      if (titleEl) titleEl.textContent = mainTitle;
      if (panelBack) panelBack.style.display = "none";
      if (footerEl) footerEl.style.display = "";
      curDetail = null;
      inDetail = false;
    };

    // Sampling range checks run on the way OUT of the sub-page so a typo
    // cannot poison the model config.
    const _detailNum = (root: HTMLElement, id: string): number | undefined => {
      const raw = (root.querySelector(id) as HTMLInputElement)?.value?.trim() ?? "";
      if (raw === "") return undefined;
      const n = Number(raw);
      return Number.isFinite(n) ? n : undefined;
    };
    const _applyDetail = (detail: ProvModelDetail): boolean => {
      const temp = _detailNum(modelView, "#prov-m-temperature");
      const topP = _detailNum(modelView, "#prov-m-top-p");
      const topK = _detailNum(modelView, "#prov-m-top-k");
      if (temp !== undefined && (temp < 0 || temp > 2)) {
        showToast(t("settings.invalidSampling", { field: t("settings.temperature") }), "", "error", "Settings");
        return false;
      }
      if (topP !== undefined && (topP < 0 || topP > 1)) {
        showToast(t("settings.invalidSampling", { field: t("settings.topP") }), "", "error", "Settings");
        return false;
      }
      if (topK !== undefined && topK < 0) {
        showToast(t("settings.invalidSampling", { field: t("settings.topK") }), "", "error", "Settings");
        return false;
      }
      detail.temperature = temp;
      detail.top_p = topP;
      detail.top_k = topK !== undefined ? Math.round(topK) : undefined;
      return true;
    };

    let detailSeq = 0;
    const openModelDetail = (row: HTMLElement) => {
      const detail = rowDetail(row);
      const modelId = (row.querySelector(".prov-m-id") as HTMLInputElement)?.value?.trim() || "";
      const modelName = (row.querySelector(".prov-m-name") as HTMLInputElement)?.value?.trim() || modelId;
      const cw = detail.context_window || 0;
      const known = CTX_PRESETS.some(p => p.v === cw);
      const ctxOptions: DropdownOption[] = (known ? CTX_PRESETS : [{ v: cw, label: `${Math.round(cw / 1000)}K` }, ...CTX_PRESETS])
        .map(p => ({ id: String(p.v), label: p.label }));
      const ctxId = `prov-m-ctx-${detailSeq++}`;
      modelView.innerHTML = `
        <div class="settings-card" style="margin-bottom:12px">
          <div class="settings-item-row">
            <div class="settings-item-info">
              <div class="settings-item-title">${t("settings.contextWindow")}</div>
              <div class="settings-item-desc">${t("settings.contextWindowHint")}</div>
            </div>
            <div class="settings-item-control">
              <div class="model-form-dropdown-row">
                ${this.renderDropdown(ctxId, ctxOptions, String(cw), () => {})}
              </div>
            </div>
          </div>
        </div>
        <div class="model-form-thinking-card" style="margin-bottom:12px">
          <div class="model-form-thinking-info">
            <div class="model-form-thinking-title">${t("settings.multimodalToggle")}</div>
            <div class="model-form-thinking-desc">${t("settings.multimodalHint")}</div>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="prov-m-multimodal" ${detail.multimodal ? "checked" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="model-form-thinking-card" style="margin-bottom:12px">
          <div class="model-form-thinking-info">
            <div class="model-form-thinking-title">${t("settings.enableThinkingLevel")}</div>
            <div class="model-form-thinking-desc">${t("settings.enableThinkingLevelHint")}</div>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="prov-m-thinking" ${detail.thinking ? "checked" : ""} />
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="model-form-row">
          <label class="model-form-label model-form-label--inline" for="prov-m-temperature">${t("settings.temperature")}
            <button type="button" class="model-form-help-btn" data-tooltip="${t("settings.temperatureHint")}" tabindex="-1">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <path d="M12 17h.01"/>
              </svg>
            </button>
          </label>
          <input type="number" id="prov-m-temperature" class="model-form-input" min="0" max="2" step="0.1"
            placeholder="${t("settings.temperaturePlaceholder")}"
            value="${detail.temperature !== undefined ? detail.temperature : ""}" />
        </div>
        <div class="model-form-row">
          <label class="model-form-label model-form-label--inline" for="prov-m-top-p">${t("settings.topP")}
            <button type="button" class="model-form-help-btn" data-tooltip="${t("settings.topPHint")}" tabindex="-1">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <path d="M12 17h.01"/>
              </svg>
            </button>
          </label>
          <input type="number" id="prov-m-top-p" class="model-form-input" min="0" max="1" step="0.05"
            placeholder="${t("settings.topPPlaceholder")}"
            value="${detail.top_p !== undefined ? detail.top_p : ""}" />
        </div>
        <div class="model-form-row">
          <label class="model-form-label model-form-label--inline" for="prov-m-top-k">${t("settings.topK")}
            <button type="button" class="model-form-help-btn" data-tooltip="${t("settings.topKHint")}" tabindex="-1">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <path d="M12 17h.01"/>
              </svg>
            </button>
          </label>
          <input type="number" id="prov-m-top-k" class="model-form-input" min="0" step="1"
            placeholder="${t("settings.topKPlaceholder")}"
            value="${detail.top_k !== undefined ? detail.top_k : ""}" />
        </div>`;
      // Every control writes straight into the row's detail state, so the
      // sub-page stays a pure editor over one model's configuration.
      this.bindDropdown(ctxId, (val) => {
        detail.context_window = parseInt(val, 10) || 0;
      });
      modelView.querySelector("#prov-m-multimodal")?.addEventListener("change", (e) => {
        detail.multimodal = (e.target as HTMLInputElement).checked;
      });
      modelView.querySelector("#prov-m-thinking")?.addEventListener("change", (e) => {
        detail.thinking = (e.target as HTMLInputElement).checked;
      });
      // Sampling inputs write through IMMEDIATELY (same pattern as the ctx
      // dropdown and the toggles above): no matter which path leaves the
      // sub-page, the typed value is already on the model's detail state.
      // The exit hook (_applyDetail) only re-reads for range validation.
      const bindSampling = (id: string, key: "temperature" | "top_p" | "top_k") => {
        modelView.querySelector(`#${id}`)?.addEventListener("input", () => {
          const n = _detailNum(modelView, `#${id}`);
          detail[key] = key === "top_k" && n !== undefined ? Math.round(n) : n;
        });
      };
      bindSampling("prov-m-temperature", "temperature");
      bindSampling("prov-m-top-p", "top_p");
      bindSampling("prov-m-top-k", "top_k");
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: modelView });
      }
      mainView.style.display = "none";
      modelView.style.display = "";
      if (titleEl) titleEl.textContent = modelName ? `Model : ${modelName}` : "Model";
      if (panelBack) panelBack.style.display = "";
      // Hide the footer on the sub-page: Cancel/Confirm are whole-provider
      // operations and live on the main page only (see goBackToMain).
      if (footerEl) footerEl.style.display = "none";
      curDetail = detail;
      inDetail = true;
    };

    // Capture-phase routing while the sub-page is open: the top-bar back
    // arrow means "back to the model list" (syncing the sub-page inputs into
    // the row's detail state first) and must NOT trigger the editor-level
    // close handler that shares the same button.
    overlay.addEventListener("click", (e) => {
      if (!inDetail) return;
      const target = e.target as HTMLElement;
      const back = target.closest(".panel-editor-back");
      if (back) {
        e.stopPropagation();
        e.preventDefault();
        if (curDetail && _applyDetail(curDetail)) goBackToMain();
      }
    }, true);

    const _wireApiKeyLink = () => {
      const apiKeyLink = overlay.querySelector(".model-get-apikey-link") as HTMLAnchorElement | null;
      if (!apiKeyLink) return;
      const p = this._findProvider((overlay.querySelector("#prov-backend-hidden") as HTMLInputElement)?.value);
      apiKeyLink.href = p?.docs || "#";
      apiKeyLink.style.display = p?.docs ? "" : "none";
      apiKeyLink.onclick = (e) => {
        e.preventDefault();
        const url = apiKeyLink.href;
        if (!url || url === "#") return;
        openRoutedLink(url);
      };
    };
    _wireApiKeyLink();

    this.bindDropdown("prov-backend", (v) => {
      const hidden = overlay.querySelector("#prov-backend-hidden") as HTMLInputElement;
      if (hidden) hidden.value = v;
      _wireApiKeyLink();
      // Custom providers (OpenAI-Compatible) reveal the user-defined name
      // field; built-in ones hide it (their identity is the catalog label).
      const nameRow = overlay.querySelector("#prov-name-row") as HTMLElement | null;
      if (nameRow) nameRow.style.display = v === "openai_compatible" ? "" : "none";
      // Switching providers prefills the official endpoint, but a manually
      // edited URL always wins (same rule as the MCP dialog).
      const urlEl = overlay.querySelector("#prov-url") as HTMLInputElement;
      const p = this._findProvider(v);
      if (urlEl && !urlEl.dataset.userModified) urlEl.value = p?.base_url || "";
    });
    overlay.querySelector("#prov-url")?.addEventListener("input", () => {
      const el = overlay.querySelector("#prov-url") as HTMLInputElement;
      if (el) el.dataset.userModified = "true";
    });

    const modelList = overlay.querySelector("#prov-model-list") as HTMLElement | null;
    overlay.querySelector("#prov-add-model")?.addEventListener("click", () => {
      if (!modelList) return;
      const tmp = document.createElement("div");
      tmp.innerHTML = makeModelRow();
      const row = tmp.firstElementChild as HTMLElement | null;
      if (!row) return;
      modelList.appendChild(row);
      // Brand-new rows start with default per-model detail state.
      (row as any)._detail = _defaultModelDetail();
      row.querySelector<HTMLInputElement>(".prov-m-id")?.focus();
      // Newly inserted nodes need their lucide icons created, otherwise the
      // remove button renders as an empty shell.
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: row });
      }
    });
    modelList?.addEventListener("click", (e) => {
      const det = (e.target as HTMLElement).closest(".prov-model-detail");
      if (det) {
        const row = det.closest(".prov-model-row") as HTMLElement | null;
        if (row) openModelDetail(row);
        return;
      }
      const del = (e.target as HTMLElement).closest(".prov-model-remove");
      if (del) { (del.closest(".prov-model-row") as HTMLElement | null)?.remove(); }
    });

    const headerList = overlay.querySelector("#prov-header-list") as HTMLElement | null;
    overlay.querySelector("#prov-add-header")?.addEventListener("click", () => {
      if (!headerList) return;
      const tmp = document.createElement("div");
      tmp.innerHTML = headerRowHtml();
      const row = tmp.firstElementChild as HTMLElement | null;
      if (!row) return;
      headerList.appendChild(row);
      row.querySelector<HTMLInputElement>(".prov-h-key")?.focus();
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: row });
      }
    });
    headerList?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest(".mcp-env-remove");
      if (btn) (btn.closest(".prov-header-row") as HTMLElement | null)?.remove();
    });

    // One save routine, two entry points: "Save & Test" probes every model
    // against the live endpoint first; "Save Only" persists immediately and
    // keeps each model's last known capabilities.  The footer's confirm button
    // is Save & Test; the extra Save Only button sits beside it.
    const saveProvider = async (runTests: boolean, btn: HTMLButtonElement) => {
      try {
      // Inside the model sub-page the footer is hidden, so this guard is a
      // safety net only: apply the sub-page inputs and go back to the list.
      if (inDetail && curDetail) {
        if (_applyDetail(curDetail)) goBackToMain();
        return;
      }
      const backend = (overlay.querySelector("#prov-backend-hidden") as HTMLInputElement)?.value || initialBackend;
      const apiKey = (overlay.querySelector("#prov-apikey") as HTMLInputElement)?.value.trim();
      const baseUrl = (overlay.querySelector("#prov-url") as HTMLInputElement)?.value.trim();
      const catalogEntry = this._findProvider(backend);
      // Local / IAM-style providers do not take a static API key.
      const needsKey = catalogEntry ? !["none", "aws_iam"].includes(catalogEntry.auth) : true;
      if (needsKey && !apiKey) {
        showToast(t("common.pleaseFillRequired"), t("settings.enterApiKey"), "error", "Settings");
        return;
      }
      // Base URL is mandatory for every provider that talks HTTP; only the
      // local transformers backend (no endpoint) and Bedrock (region-based)
      // are exempt.
      const needsUrl = backend !== "local" && !(catalogEntry && catalogEntry.auth === "aws_iam");
      if (needsUrl && !baseUrl) {
        showToast(t("common.pleaseFillRequired"), t("settings.baseUrlPlaceholder"), "error", "Settings");
        return;
      }

      // Provider names share ONE namespace: built-in providers are identified
      // by their official catalog label (each can be configured once), custom
      // ones by the user-defined name.  A custom provider may not reuse any
      // existing name -- e.g. "DeepSeek" is refused while the built-in
      // DeepSeek provider is configured.
      const providerName = backend === "openai_compatible"
        ? ((overlay.querySelector("#prov-name") as HTMLInputElement)?.value.trim() || "")
        : "";
      if (backend === "openai_compatible" && !providerName) {
        showToast(t("common.pleaseFillRequired"), t("settings.providerNamePlaceholder"), "error", "Settings");
        return;
      }
      const resolvedName = backend === "openai_compatible"
        ? providerName
        : this._providerLabel(backend);
      const duplicate = (getState().providers || []).some((p, i) => {
        if (isEdit && i === editIdx) return false;
        const other = p.backend_type === "openai_compatible"
          ? (p.name || "").trim()
          : this._providerLabel(p.backend_type);
        return !!other && other.toLowerCase() === resolvedName.toLowerCase();
      });
      if (duplicate) {
        showToast(t("settings.providerNameDuplicate", { name: resolvedName }), "", "error", "Settings");
        return;
      }

      // Collect model rows; every row needs at least a model ID.  Capabilities
      // and sampling come from each row's per-model detail state (sub-page).
      const rows: Array<{ model_id: string; name: string; enabled: boolean; detail: ProvModelDetail }> = [];
      let rowMissing = false;
      overlay.querySelectorAll(".prov-model-row").forEach((row) => {
        const modelId = (row.querySelector(".prov-m-id") as HTMLInputElement)?.value?.trim() || "";
        if (!modelId) { rowMissing = true; return; }
        rows.push({
          model_id: modelId,
          name: (row.querySelector(".prov-m-name") as HTMLInputElement)?.value?.trim() || "",
          enabled: (row.querySelector(".prov-m-enabled") as HTMLInputElement | null)?.checked !== false,
          detail: rowDetail(row),
        });
      });
      if (rowMissing) {
        showToast(t("common.pleaseFillRequired"), "", "error", "Settings");
        return;
      }
      // At least one model must be configured (the section is required).
      if (rows.length === 0) {
        showToast(t("common.pleaseFillRequired"), "", "error", "Settings");
        return;
      }

      // Collect custom headers.  A row without a key is dropped -- an empty
      // key cannot address anything, so it is not written to the config.  An
      // empty VALUE is kept as-is: a header that exists but carries no body
      // is a valid, meaningful entry.
      const headers: Record<string, string> = {};
      headerList?.querySelectorAll(".prov-header-row").forEach((row) => {
        const k = (row.querySelector(".prov-h-key") as HTMLInputElement)?.value?.trim();
        const v = (row.querySelector(".prov-h-value") as HTMLInputElement)?.value || "";
        if (k) headers[k] = v;
      });

      const prevActiveId = getState().modelConfigs[getState().activeModelIndex]?.model_id || "";
      const validatedModels: import("../core/types.js").ProviderModelMeta[] = [];
      const failures: string[] = [];
      // Last known probe results per model id: "Save Only" falls back to them
      // (then to unknown) so a model keeps its verified capabilities without
      // re-probing the endpoint.
      const prevMetaById = new Map<string, import("../core/types.js").ProviderModelMeta>();
      (existing?.models || []).forEach((m) => { if (m.model_id) prevMetaById.set(m.model_id, m); });
      // Correlation ids for probe responses: model ids repeat across
      // providers, so every in-flight validate_model gets its own unique
      // request_id and the response is routed by it, not by model_id.
      let validationSeq = 0;
      if (runTests) {
        // Live validation: every model is verified against the real endpoint
        // (connectivity + multimodal + reasoning-level probes).  Rows whose
        // validation digest already matches -- in the short-lived cache, or
        // persisted with the saved model -- are reused as-is, so adding one
        // model to a validated provider re-probes exactly that one row.
        // The remaining rows are all dispatched up front and awaited in
        // parallel so a slow endpoint no longer serialises the whole save.
        // A single failure never aborts the batch: it is reported at the end,
        // its key is left out of the cache, and its stamp is cleared so the
        // next attempt retries only that row.
        await withLoading(btn, async () => {
          const pending: { row: typeof rows[number]; cacheKey: string; promise: Promise<any> }[] = [];
          for (const r of rows) {
            const signature = _validationSignature(backend, baseUrl, apiKey || "", headers, r.model_id, r.name);
            // Capability toggles are per-model now, so they participate in
            // the cache key alongside the row identity.
            const cacheKey = _validationCacheKey(signature, r.detail.multimodal, r.detail.thinking, tokensDefault);
            const stamp = _validationStamp(cacheKey, apiKey || "");
            const cached = _cacheGet(cacheKey);
            if (cached !== null) {
              r.detail.validated_key = stamp;
              validatedModels.push(_buildValidatedModel(r, cached, tokensDefault));
              continue;
            }
            const prev = prevMetaById.get(r.model_id);
            if (prev && prev.validated_key === stamp) {
              // Validated for exactly this config in a previous session --
              // reuse the stored probe results, no network call.
              r.detail.validated_key = stamp;
              validatedModels.push(_buildValidatedModel(r, {
                multimodal_support: prev.multimodal_support,
                reasoning_levels: prev.reasoning_levels,
                capabilities: prev.capabilities,
              }, tokensDefault));
              continue;
            }
            const requestId = `${r.model_id}#${++validationSeq}-${Date.now()}`;
            send({
              type: "validate_model", backend_type: backend, api_key: apiKey || "",
              base_url: baseUrl, model_id: r.model_id, max_tokens: tokensDefault,
              name: r.name, multimodal: r.detail.multimodal,
              probe_reasoning: r.detail.thinking,
              headers: Object.keys(headers).length > 0 ? headers : undefined,
              // Sampling overrides ride along so the probe hits the relay
              // with the exact config the model will run with.
              temperature: r.detail.temperature, top_p: r.detail.top_p, top_k: r.detail.top_k,
              request_id: requestId,
            } as any);
            pending.push({ row: r, cacheKey, promise: waitForModelValidation(r.model_id, requestId) });
          }
          // Await every outstanding probe concurrently; results are re-sorted
          // back into row order so provider.models keeps its UI order.
          const outcomes = await Promise.all(
            pending.map(async ({ row, cacheKey, promise }) => {
              try {
                const result = await promise;
                _cachePut(cacheKey, result);
                return { row, cacheKey, result, error: "" };
              } catch (e: any) {
                return { row, cacheKey, result: null, error: `${row.model_id}: ${t("common.connectionFailed")}${e ? `: ${e}` : ""}` };
              }
            })
          );
          for (const o of outcomes) {
            if (o.result) {
              o.row.detail.validated_key = _validationStamp(o.cacheKey, apiKey || "");
              validatedModels.push(_buildValidatedModel(o.row, o.result, tokensDefault));
            } else {
              o.row.detail.validated_key = "";
              failures.push(o.error);
            }
          }
        });
        if (failures.length > 0) {
          showToast(failures.join("\n"), "", "error", "Settings");
          return;
        }
      } else {
        // Save without probing: prefer the in-memory validation cache, then
        // the previously saved meta, then "unknown" for brand-new rows.  A
        // row keeps its validation stamp only while the digest still matches
        // -- changed credentials/endpoint/toggles clear it so the next
        // Save & Test re-probes that row.
        await withLoading(btn, async () => {
          for (const r of rows) {
            const signature = _validationSignature(backend, baseUrl, apiKey || "", headers, r.model_id, r.name);
            const cacheKey = _validationCacheKey(signature, r.detail.multimodal, r.detail.thinking, tokensDefault);
            const stamp = _validationStamp(cacheKey, apiKey || "");
            const cached = _cacheGet(cacheKey);
            const prev = prevMetaById.get(r.model_id);
            if (cached !== null) {
              r.detail.validated_key = stamp;
              validatedModels.push(_buildValidatedModel(r, cached, tokensDefault));
              continue;
            }
            if (prev && prev.validated_key === stamp) {
              r.detail.validated_key = stamp;
            } else {
              r.detail.validated_key = "";
            }
            validatedModels.push(_buildValidatedModel(r, {
              multimodal_support: prev?.multimodal_support ?? "unknown",
              reasoning_levels: Array.isArray(prev?.reasoning_levels) ? prev.reasoning_levels : [],
              capabilities: prev?.capabilities || {},
            }, tokensDefault));
          }
        });
      }

      // Sampling overrides live on each MODEL now (sub-page) -- the provider
      // keeps only credentials + headers, like before.
      const newProvider: import("../core/types.js").ProviderConfigMeta = {
        backend_type: backend,
        ...(providerName ? { name: providerName } : {}),
        api_key: apiKey || "", base_url: baseUrl || "",
        ...(Object.keys(headers).length > 0 ? { headers } : {}),
        // Preserve the provider's master switch across an edit: saving the
        // provider must never silently switch a disabled provider back on.
        enabled: existing ? existing.enabled !== false : true,
        models: validatedModels,
      };
      const current = [...(getState().providers || [])];
      if (isEdit && editIdx! < current.length) current[editIdx!] = newProvider;
      else current.push(newProvider);

      // Keep the active flat index pointing at the same model when possible;
      // otherwise fall back to the saved provider's first model.
      let offset = 0;
      for (let i = 0; i < (isEdit ? editIdx! : current.length - 1); i++) offset += current[i]?.models?.length || 0;
      let activeIdx = -1;
      if (prevActiveId) {
        let n = 0;
        for (const p of current) {
          const j = (p.models || []).findIndex(m => m.model_id === prevActiveId);
          if (j >= 0) { activeIdx = n + j; break; }
          n += p.models?.length || 0;
        }
      }
      if (activeIdx < 0) activeIdx = validatedModels.length > 0 ? offset : 0;

      setProviders(current);
      send({ type: "update_providers", providers: current, active_model_index: activeIdx });
      this.renderModel();
      close();
      } catch (err) {
        // A throw anywhere above (unexpected saved metadata, DOM drift, a
        // backend reply that never arrived) used to reject the click handler
        // silently: the dialog just sat there and nothing was saved.  Surface
        // it so 仅保存 can never fail without telling the user why.
        console.error("[saveProvider]", err);
        showToast(t("settings.error"), String((err as Error)?.message || err || ""), "error", "Settings");
      }
    };

    okBtn.addEventListener("click", () => { void saveProvider(true, okBtn); });
    saveOnlyBtn.addEventListener("click", () => { void saveProvider(false, saveOnlyBtn); });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }
  }

  private _renderModelPoolSettingsDialog(_idx: number): void {
    const st = getState();
    const tFn = t;
    // model_pool_enabled is stored as settings field; undefined/null → ON (backwards compat)
    const poolEnabled = st.settings.model_pool_enabled !== false;
    const current = st.settings.model_pool_fallback_enabled === true;
    // Cycling strategy (ONE global policy inherited by every pool scope).
    // Numbers follow: -1 (default) = unlimited, 0 = immediate, N = N units.
    const secOf = (raw: unknown): number => {
      if (raw === undefined || raw === null || raw === "") return -1;
      const n = Number(raw);
      if (!Number.isFinite(n)) return -1;
      return n < 0 ? -1 : Math.round(n / 1000);
    };
    const intOf = (raw: unknown): number => {
      if (raw === undefined || raw === null || raw === "") return -1;
      const n = Number(raw);
      if (!Number.isFinite(n)) return -1;
      const v = Math.trunc(n);
      return v < -1 ? -1 : v;
    };
    const cooldownSec = secOf(st.settings.model_pool_cooldown_ms);
    const recycleSec = secOf(st.settings.model_pool_recycle_wait_ms);
    const maxFallbacks = intOf(st.settings.model_pool_max_fallbacks);
    // Cycle order list: one row per configured provider entry (enabled or
    // not — a disabled one keeps its slot and dims; the cycle itself skips
    // it), each row labelled with its provider.
    // Pool members are PROVIDER-QUALIFIED entries: "<provider><US><model_id>".
    // Two providers declaring the same model_id are SEPARATE members (the
    // backend resolves candidates by the same key), so every flat entry gets
    // its own row and nothing is deduped.  Legacy persisted entries without
    // the separator are matched to the first provider declaring that id.
    const POOL_KEY_SEP = "\u001f";
    const savedOrderRaw = (st.settings as Record<string, unknown>).model_pool_cycle_order;
    const savedOrder: string[] = Array.isArray(savedOrderRaw)
      ? (savedOrderRaw as string[]).filter((x) => typeof x === "string" && x)
      : [];
    const catalogLabel = (backendId: string): string =>
      (getState().modelCatalog?.providers?.find((c: { id: string }) => c.id === backendId)?.label) || backendId;
    type PoolEntry = { key: string; id: string; name: string; provider: string; enabled: boolean };
    let entries: PoolEntry[] = [];
    let entryByKey = new Map<string, PoolEntry>();
    let firstKeyById = new Map<string, string>();
    let defaultOrder: string[] = [];
    const rebuildEntries = (): void => {
      entries = [];
      for (const p of getState().providers || []) {
        const qualified = p.backend_type === "openai_compatible";
        const pkey = qualified ? (p.name?.trim() || p.backend_type) : p.backend_type;
        const label = qualified ? (p.name?.trim() || catalogLabel(p.backend_type)) : catalogLabel(p.backend_type);
        for (const m of p.models || []) {
          const entry: PoolEntry = {
            key: `${pkey}${POOL_KEY_SEP}${m.model_id}`,
            id: m.model_id,
            name: (m as { name?: string }).name || m.model_id,
            provider: label,
            enabled: m.enabled !== false && p.enabled !== false,
          };
          entries.push(entry);
          if (!entryByKey.has(entry.key)) entryByKey.set(entry.key, entry);
          if (!firstKeyById.has(entry.id)) firstKeyById.set(entry.id, entry.key);
        }
      }
      defaultOrder = entries.map((e) => e.key);
    };
    rebuildEntries();
    // Resolve a persisted key to a CURRENT key: qualified keys pass through
    // when the member still exists; separator-less keys are legacy bare ids.
    const resolveKey = (key: string): string | undefined => {
      if (entryByKey.has(key)) return key;
      if (!key.includes(POOL_KEY_SEP)) return firstKeyById.get(key);
      return undefined;
    };
    // The drag list shows the EFFECTIVE order: saved custom order resolved to
    // surviving members first, then every configured member missing from it
    // (the same merge rule the backend applies when resolving candidates).
    const mergeOrder = (saved: string[]): string[] => {
      const out: string[] = [];
      for (const key of saved) {
        const k = resolveKey(key);
        if (k && !out.includes(k)) out.push(k);
      }
      for (const key of defaultOrder) if (!out.includes(key)) out.push(key);
      return out;
    };
    const initialOrder: string[] = mergeOrder(savedOrder);

    const cycleRowHtml = (key: string, i: number): string => {
      const entry = entryByKey.get(key);
      const disabled = entry ? !entry.enabled : false;
      const name = entry?.name || key;
      const provider = entry?.provider || "";
      return `
        <div class="pool-cycle-row${disabled ? " pool-cycle-row--disabled" : ""}" data-pool-key="${this.esc(key)}">
          <span class="pool-cycle-drag" draggable="true">
            <i data-lucide="grip-vertical" class="lucide"></i>
          </span>
          <span class="pool-cycle-name">${this.esc(name)}</span>
          <span class="pool-cycle-provider">${this.esc(provider)}</span>
          <span class="pool-cycle-pos">${i + 1}</span>
        </div>`;
    };

    // BOTH views are rendered ONCE up front and never rebuilt: the strategy
    // fields stay mounted (hidden) while the main view shows, so the dirty
    // check is accurate across view swaps and a save always carries every
    // field's current value.
    const bodyHtml = `
      <div id="pool-main-view">
      <div class="model-form-thinking-card" style="margin-bottom:12px">
        <div class="model-form-thinking-info">
          <div class="model-form-thinking-title">${tFn("settings.modelPoolEnableTitle")}</div>
          <div class="model-form-thinking-desc">${tFn("settings.modelPoolEnableDesc")}</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="model-pool-enabled-toggle" ${poolEnabled ? "checked" : ""} />
          <span class="toggle-slider"></span>
        </label>
      </div>
      <div class="settings-card" style="margin-bottom:12px" id="pool-strategy-entry">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${tFn("settings.modelPoolFallbackTitle")}</div>
            <div class="settings-item-desc">${tFn("settings.modelPoolFallbackDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="pool-strategy-open" data-tooltip="${tFn("settings.modelPoolSettings")}">
              <i data-lucide="arrow-up-right" style="width:16px;height:16px"></i>
            </button>
          </div>
        </div>
      </div>
      </div>
      <div id="pool-strategy-view" style="display:none">
      <div class="model-form-thinking-card" style="margin-bottom:12px">
        <div class="model-form-thinking-info">
          <div class="model-form-thinking-title model-form-thinking-title--with-help">
            ${tFn("settings.modelPoolFallbackTitle")}
            <button type="button" class="model-form-help-btn" data-tooltip="${tFn("settings.modelPoolFallbackCacheHint")}" tabindex="-1">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <path d="M12 17h.01"/>
              </svg>
            </button>
          </div>
          <div class="model-form-thinking-desc">${tFn("settings.modelPoolFallbackDesc")}</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="model-pool-fallback-toggle" ${current ? "checked" : ""} />
          <span class="toggle-slider"></span>
        </label>
      </div>
      <div id="pool-strategy-fields" style="${current ? "" : "display:none"}">
      <div class="model-form-row" style="flex-direction:column;align-items:stretch;margin-bottom:12px">
        <div class="prov-sec-row">
          <span class="prov-sec-label">
            ${tFn("settings.modelPoolCycleOrder")}
            <button type="button" class="model-form-help-btn" data-tooltip="${tFn("settings.modelPoolCycleOrderHint")}" tabindex="-1">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <path d="M12 17h.01"/>
              </svg>
            </button>
          </span>
          <button type="button" class="btn-icon" id="pool-order-reset" data-tooltip="${tFn("settings.modelPoolOrderReset")}">
            <i data-lucide="rotate-ccw" class="lucide"></i>
          </button>
        </div>
        <div id="pool-cycle-list" class="pool-cycle-list">${initialOrder.map(cycleRowHtml).join("")}</div>
      </div>
      <div class="model-form-row" style="flex-direction:column;align-items:stretch;margin-bottom:12px">
        <label class="model-form-label model-form-label--inline" for="model-pool-cooldown">
          ${tFn("settings.modelPoolCooldown")}
          <button type="button" class="model-form-help-btn" data-tooltip="${tFn("settings.modelPoolCooldownHint")}" tabindex="-1">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
              <path d="M12 17h.01"/>
            </svg>
          </button>
        </label>
        <input type="number" id="model-pool-cooldown" class="model-form-input" min="-1" step="1"
          placeholder="-1" value="${cooldownSec}" />
      </div>
      <div class="model-form-row" style="flex-direction:column;align-items:stretch;margin-bottom:12px">
        <label class="model-form-label model-form-label--inline" for="model-pool-recycle">
          ${tFn("settings.modelPoolRecycleWait")}
          <button type="button" class="model-form-help-btn" data-tooltip="${tFn("settings.modelPoolRecycleWaitHint")}" tabindex="-1">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
              <path d="M12 17h.01"/>
            </svg>
          </button>
        </label>
        <input type="number" id="model-pool-recycle" class="model-form-input" min="-1" step="1"
          placeholder="-1" value="${recycleSec}" />
      </div>
      <div class="model-form-row" style="flex-direction:column;align-items:stretch">
        <label class="model-form-label model-form-label--inline" for="model-pool-max-fallbacks">
          ${tFn("settings.modelPoolMaxFallbacks")}
          <button type="button" class="model-form-help-btn" data-tooltip="${tFn("settings.modelPoolMaxFallbacksHint")}" tabindex="-1">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
              <path d="M12 17h.01"/>
            </svg>
          </button>
        </label>
        <input type="number" id="model-pool-max-fallbacks" class="model-form-input" min="-1" step="1"
          placeholder="-1" value="${maxFallbacks}" />
      </div>
      </div>
      </div>`;

    const { overlay, close } = this._showFormDialog(
      tFn("settings.modelPoolSettings"),
      bodyHtml,
      false,
    );
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = tFn("common.save");

    const mainView = overlay.querySelector("#pool-main-view") as HTMLElement;
    const strategyView = overlay.querySelector("#pool-strategy-view") as HTMLElement;
    const titleEl = (overlay.querySelector(".panel-editor-title-text") as HTMLElement | null)
      || (overlay.querySelector(".toast-title") as HTMLElement | null);
    const mainTitle = titleEl?.textContent || "";
    const panelBack = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
    // Back arrow ALWAYS visible: on the strategy sub-page it means "back to
    // the toggles" (capture-intercepted); on the main view it is the way OUT
    // of the editor (the generic requestClose: prompt-if-dirty, then close).
    // The template ships it with display:none, so it must be revealed here —
    // not only when the sub-page opens — or the first open has no exit.
    if (panelBack) panelBack.style.display = "";
    // The footer lives ONLY on the sub-page: the main view's 模型池 toggle
    // applies on flip, and 取消/保存 own the strategy form.
    const footerEl = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    if (footerEl) footerEl.style.display = "none";

    // ONE save routine, ONE entry point (the footer 保存 button): every value
    // is read straight from the form DOM — both views' fields are always
    // mounted, so a save carries the whole form no matter which view is open.
    const applyPoolSettings = () => {
      const poolToggle = overlay.querySelector<HTMLInputElement>("#model-pool-enabled-toggle");
      const fbToggle = overlay.querySelector<HTMLInputElement>("#model-pool-fallback-toggle");
      const order = Array.from(overlay.querySelectorAll<HTMLElement>("#pool-cycle-list .pool-cycle-row"))
        .map((r) => r.dataset.poolKey || "")
        .filter(Boolean);
      const num = (sel: string): number => {
        const raw = (overlay.querySelector(sel) as HTMLInputElement | null)?.value?.trim() ?? "";
        if (raw === "") return -1;
        const n = Number(raw);
        if (!Number.isFinite(n)) return -1;
        const v = Math.trunc(n);
        return v < -1 ? -1 : v;
      };
      const _cd = num("#model-pool-cooldown");
      const _rw = num("#model-pool-recycle");
      const payload: Record<string, unknown> = {
        model_pool_enabled: poolToggle ? poolToggle.checked : poolEnabled,
        model_pool_fallback_enabled: fbToggle ? fbToggle.checked : current,
        model_pool_cycle_order: order,
        model_pool_cooldown_ms: _cd < 0 ? -1 : _cd * 1000,
        model_pool_recycle_wait_ms: _rw < 0 ? -1 : _rw * 1000,
        model_pool_max_fallbacks: num("#model-pool-max-fallbacks"),
      };
      setSettings({ ...getState().settings, ...payload });
      send({ type: "configure", config: payload });
      this.renderModel();
    };

    // ── Strategy sub-page (view swap only — the DOM is static) ──────────
    let inStrategy = false;
    // Edit-session snapshot: captured from the APPLIED state (st.settings)
    // when the sub-page opens; 取消 restores it, 保存 re-captures it.
    let snapshot: { fallback: boolean; cooldown: number; recycle: number; max: number; order: string[] } | null = null;
    const captureSnapshot = () => {
      const s = getState().settings as Record<string, unknown>;
      const savedRaw = s.model_pool_cycle_order;
      const saved: string[] = Array.isArray(savedRaw)
        ? (savedRaw as string[]).filter((x) => typeof x === "string" && x)
        : [];
      snapshot = {
        fallback: s.model_pool_fallback_enabled === true,
        cooldown: secOf(s.model_pool_cooldown_ms),
        recycle: secOf(s.model_pool_recycle_wait_ms),
        max: intOf(s.model_pool_max_fallbacks),
        order: mergeOrder(saved),
      };
    };
    // Single rebuild path for the drag list (initial render, snapshot
    // restore, the reset button and live refresh all go through here).
    const renderCycleList = (order: string[]): void => {
      const list = overlay.querySelector("#pool-cycle-list") as HTMLElement | null;
      if (!list) return;
      list.innerHTML = order.map(cycleRowHtml).join("");
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: list });
      }
    };
    // Re-snapshot the shell's dirty baseline (the _serializePanelEditor
    // comparison): call it whenever the form lands in a state that is
    // already persisted — after Save, after the main-view toggle flip,
    // after a snapshot restore — so exiting the editor never nags about
    // changes that are in fact saved.
    const rebaseline = (): void => {
      this._editorBaselines.set(overlay, this._serializePanelEditor(overlay));
    };
    const restoreSnapshot = () => {
      if (!snapshot) return;
      const fb = overlay.querySelector<HTMLInputElement>("#model-pool-fallback-toggle");
      if (fb) fb.checked = snapshot.fallback;
      const setNum = (sel: string, v: number) => {
        const el = overlay.querySelector<HTMLInputElement>(sel);
        if (el) el.value = String(v);
      };
      setNum("#model-pool-cooldown", snapshot.cooldown);
      setNum("#model-pool-recycle", snapshot.recycle);
      setNum("#model-pool-max-fallbacks", snapshot.max);
      renderCycleList(snapshot.order);
      syncFields();
      // The restore puts the form back to the SAVED state; the baseline
      // must follow it or a later exit still nags about discarded edits.
      rebaseline();
    };
    const showFooter = (show: boolean) => {
      if (footerEl) footerEl.style.display = show ? "" : "none";
    };
    const goBackToMain = () => {
      strategyView.style.display = "none";
      mainView.style.display = "";
      if (titleEl) titleEl.textContent = mainTitle;
      showFooter(false);
      inStrategy = false;
    };
    const renumberCycleRows = () => {
      overlay.querySelectorAll<HTMLElement>("#pool-cycle-list .pool-cycle-row").forEach((r, i) => {
        const pos = r.querySelector(".pool-cycle-pos") as HTMLElement | null;
        if (pos) pos.textContent = String(i + 1);
      });
    };
    const openStrategy = () => {
      mainView.style.display = "none";
      strategyView.style.display = "";
      if (titleEl) titleEl.textContent = tFn("settings.modelPoolFallbackTitle");
      if (panelBack) panelBack.style.display = "";
      captureSnapshot();
      syncFields();
      showFooter(true);
      inStrategy = true;
    };
    // The enable toggle lives HERE (sub-page): flipping it shows/hides the
    // strategy fields below (values apply on 保存, not on flip).
    const syncFields = () => {
      const fb = overlay.querySelector<HTMLInputElement>("#model-pool-fallback-toggle");
      const fields = overlay.querySelector("#pool-strategy-fields") as HTMLElement | null;
      if (fields && fb) fields.style.display = fb.checked ? "" : "none";
    };
    overlay.querySelector("#model-pool-fallback-toggle")?.addEventListener("change", syncFields);
    // Drag-to-reorder (same HTML5 pattern as the pending panel: grip
    // handles start the drag; half-row drop targeting moves the row).  The
    // reorder only moves DOM rows — nothing is sent until 保存.
    let dragIdx: number | null = null;
    const listEl = overlay.querySelector("#pool-cycle-list") as HTMLElement;
    listEl.addEventListener("dragstart", (e) => {
      const handle = (e.target as HTMLElement).closest(".pool-cycle-drag") as HTMLElement | null;
      if (!handle) return;
      dragIdx = Array.from(listEl.querySelectorAll<HTMLElement>(".pool-cycle-row"))
        .indexOf(handle.closest(".pool-cycle-row") as HTMLElement);
      const dt = (e as DragEvent).dataTransfer;
      if (!dt) return;
      dt.setData("text/plain", String(dragIdx));
      dt.effectAllowed = "move";
    });
    listEl.addEventListener("dragover", (e) => {
      if (dragIdx === null) return;
      e.preventDefault();
      const dt = (e as DragEvent).dataTransfer;
      if (dt) dt.dropEffect = "move";
      const rows = Array.from(listEl.querySelectorAll<HTMLElement>(".pool-cycle-row"));
      const target = rows.findIndex((r) => {
        const b = r.getBoundingClientRect();
        return e.clientY >= b.top && e.clientY < b.bottom;
      });
      rows.forEach((r) => r.classList.remove("drop-target"));
      if (target >= 0 && target !== dragIdx) rows[target].classList.add("drop-target");
    });
    listEl.addEventListener("drop", (e) => {
      if (dragIdx === null) return;
      e.preventDefault();
      const rows = Array.from(listEl.querySelectorAll<HTMLElement>(".pool-cycle-row"));
      let target = rows.findIndex((r) => {
        const b = r.getBoundingClientRect();
        return e.clientY >= b.top && e.clientY < b.bottom;
      });
      if (target < 0) target = rows.length - 1;
      const from = dragIdx;
      dragIdx = null;
      rows.forEach((r) => r.classList.remove("drop-target"));
      if (target === from) return;
      const srcRow = rows[from];
      if (from < target) rows[target].after(srcRow);
      else rows[target].before(srcRow);
      renumberCycleRows();
    });
    listEl.addEventListener("dragend", () => {
      dragIdx = null;
      listEl.querySelectorAll(".pool-cycle-row").forEach((r) => r.classList.remove("drop-target"));
    });
    overlay.querySelector("#pool-order-reset")?.addEventListener("click", () => {
      renderCycleList(defaultOrder);
    });
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }

    // ONLY the corner arrow enters the strategy sub-page (the gateway
    // adapter-card pattern: expand button, not the row).  The classic modal
    // fallback has no back arrow to return from the sub-page, so the entry
    // is not offered there at all.
    if (!overlay.classList.contains("panel-editor")) {
      overlay.querySelector("#pool-strategy-entry")?.remove();
    }
    overlay.querySelector("#pool-strategy-open")?.addEventListener("click", (e) => {
      e.stopPropagation();
      openStrategy();
    });
    // The main view's 模型池 toggle applies on flip (single key) — it is the
    // only control on the main view, and there is no footer there.
    overlay.querySelector("#model-pool-enabled-toggle")?.addEventListener("change", (e) => {
      const on = (e.target as HTMLInputElement).checked;
      setSettings({ ...getState().settings, model_pool_enabled: on });
      send({ type: "configure", config: { model_pool_enabled: on } });
      this.renderModel();
      rebaseline();
    });
    // Sub-page edit-session helpers: `subpageValues` reads the form as it
    // stands, `subpageDirty` compares it against the entry snapshot, and
    // `discardStrategyEdits` is the ONE exit that never persists — it asks
    // before throwing edits away (取消 and ← both route through it), then
    // restores the snapshot.  Nothing on these paths writes to the backend.
    const subpageValues = () => {
      const fb = overlay.querySelector<HTMLInputElement>("#model-pool-fallback-toggle");
      const num = (sel: string): number => {
        const raw = (overlay.querySelector(sel) as HTMLInputElement | null)?.value?.trim() ?? "";
        if (raw === "") return -1;
        const n = Number(raw);
        if (!Number.isFinite(n)) return -1;
        const v = Math.trunc(n);
        return v < -1 ? -1 : v;
      };
      const order = Array.from(overlay.querySelectorAll<HTMLElement>("#pool-cycle-list .pool-cycle-row"))
        .map((r) => r.dataset.poolKey || "")
        .filter(Boolean);
      return {
        fallback: fb ? fb.checked : current,
        cooldown: num("#model-pool-cooldown"),
        recycle: num("#model-pool-recycle"),
        max: num("#model-pool-max-fallbacks"),
        order,
      };
    };
    const subpageDirty = (): boolean => {
      if (!snapshot) return false;
      const v = subpageValues();
      return v.fallback !== snapshot.fallback
        || v.cooldown !== snapshot.cooldown
        || v.recycle !== snapshot.recycle
        || v.max !== snapshot.max
        || v.order.join("\u0000") !== snapshot.order.join("\u0000");
    };
    const discardStrategyEdits = async (): Promise<void> => {
      if (subpageDirty()) {
        const ok = await Dialog.confirm(
          t("sessionInner.closeUnsavedTitle"),
          t("sessionInner.closeUnsavedMessage", { name: tFn("settings.modelPoolFallbackTitle") }),
          { primary: t("sessionInner.discardChanges") },
        );
        if (!ok) return;
      }
      restoreSnapshot();
      goBackToMain();
    };
    // Capture-phase routing on the sub-page: 取消 and ← both discard (with a
    // prompt when the form was touched) and must never trigger the
    // editor-level close that shares the same controls, nor write anything.
    // On the main view the generic requestClose (prompt-if-dirty) runs
    // untouched.
    overlay.addEventListener("click", (e) => {
      if (!inStrategy) return;
      const target = e.target as HTMLElement;
      if (target.closest("#dialog-form-cancel")) {
        e.stopPropagation();
        e.preventDefault();
        void discardStrategyEdits();
        return;
      }
      const back = target.closest(".panel-editor-back");
      if (!back) return;
      e.stopPropagation();
      e.preventDefault();
      void discardStrategyEdits();
    }, true);
    // 保存 commits the strategy form and lands back on the main view.
    okBtn.addEventListener("click", () => {
      if (!inStrategy) return;
      applyPoolSettings();
      captureSnapshot();
      // Everything just written IS persisted — align the shell's dirty
      // baseline with it so closing the editor right after saving does not
      // prompt "unsaved changes".
      rebaseline();
      goBackToMain();
    });

    // Live-refresh the cycle list while the editor is open: models added,
    // removed or reordered in a provider dialog are pushed into providers /
    // modelConfigs and the list follows automatically (no manual close/reopen
    // needed).  The current DOM order is preserved for surviving members,
    // newly added ones are appended, removed ones drop out, and the rebuild
    // only happens when the member set actually changed — an in-progress
    // drag or the row numbers are never clobbered by no-op pushes.
    const memberKey = (r: HTMLElement): string => r.dataset.poolKey || "";
    let lastMemberSet = Array.from(overlay.querySelectorAll<HTMLElement>("#pool-cycle-list .pool-cycle-row"))
      .map(memberKey)
      .join("\u0000");
    const unsubscribeModels = subscribe(() => {
      if (!overlay.isConnected) { unsubscribeModels(); return; }
      rebuildEntries();
      const set = defaultOrder.join("\u0000");
      if (set === lastMemberSet) return;
      lastMemberSet = set;
      const rows = Array.from(overlay.querySelectorAll<HTMLElement>("#pool-cycle-list .pool-cycle-row")).map(memberKey);
      renderCycleList(mergeOrder(rows));
      renumberCycleRows();
    }, ["modelConfigs", "providers"]);
  }

  // ── Knowledge library (space → folder → typed leaves) ─────────────

  /** Library UI state: current selection, expanded branch ids, save timer. */
  private _libSelectedId = "";
  private _libExpanded: Set<string> | null = null;
  private _libSaveTimer: ReturnType<typeof setTimeout> | null = null;
  /** Markdown leaf working buffer + view mode for the edit/preview toggle. */
  private _libMdText = "";
  private _libMdMode: "preview" | "edit" = "preview";

  /** Lucide icon per library node type. */
  private _libIcon(type: string): string {
    switch (type) {
      case "space": return "book-marked";
      case "folder": return "folder";
      case "doc": return "file-text";
      case "sheet": return "table";
      case "clip": return "globe";
      case "md": return "file-code";
      case "attachment": return "paperclip";
      default: return "file";
    }
  }

  /** Display name fallback chain: name → default space → untitled. */
  private _libDisplayName(n: import("../core/types.js").LibraryNode): string {
    if (n.name) return n.name;
    if (n.type === "space") return t("settings.libDefaultSpace");
    return t("settings.libUntitled");
  }

  private _libFormatSize(size: number | undefined): string {
    const s = size || 0;
    if (s > 1024 * 1024) return (s / 1024 / 1024).toFixed(1) + " MB";
    if (s > 1024) return (s / 1024).toFixed(1) + " KB";
    return s + " B";
  }

  /** Group tree nodes by parent id, each list sorted by order. */
  private _libChildrenMap(nodes: import("../core/types.js").LibraryNode[]): Map<string, import("../core/types.js").LibraryNode[]> {
    const map = new Map<string, import("../core/types.js").LibraryNode[]>();
    for (const n of nodes) {
      const key = n.parent_id || "";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(n);
    }
    for (const list of map.values()) {
      list.sort((a, b) => a.order - b.order || a.created_at - b.created_at);
    }
    return map;
  }

  /** The parent a "new node" action should target, based on the selection. */
  private _libTargetParentId(): string {
    const st = getState();
    const sel = st.libraryTree.find((n) => n.id === this._libSelectedId);
    if (!sel) return "";
    if (sel.type === "space" || sel.type === "folder") return sel.id;
    return sel.parent_id || "";
  }

  private renderLibrary(): void {
    if (this._panelEditorActive("library")) return;
    const st = getState();
    const tFn = t;
    const nodes = st.libraryTree;

    // Drop a selection whose node no longer exists (deleted elsewhere).
    if (this._libSelectedId && !nodes.some((n) => n.id === this._libSelectedId)) {
      this._libSelectedId = "";
    }
    // First paint: auto-expand every space so the tree is visible at once.
    if (this._libExpanded === null) {
      this._libExpanded = new Set(nodes.filter((n) => n.type === "space").map((n) => n.id));
    }

    const children = this._libChildrenMap(nodes);
    const sel = nodes.find((n) => n.id === this._libSelectedId) || null;
    const newItems = this._libNewItems(tFn);

    const html = `
      <div class="settings-section-title"><i data-lucide="book-marked" class="lucide section-title-icon"></i> ${tFn("settings.library")}</div>
      <div class="settings-card library-shell">
        <aside class="library-sidebar">
          <div class="library-side-head">
            <div class="model-manage-desc">${tFn("settings.libraryTreeDesc")}</div>
            <div class="settings-dropdown-wrap doc-add-dropdown" id="lib-add-dropdown-wrap">
              <button class="btn-add-model-top" id="lib-add-trigger" type="button">
                <i data-lucide="plus" class="lucide" style="width:14px;height:14px"></i>
                <span>${tFn("settings.libNew")}</span>
                <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
              </button>
              <div class="settings-dropdown" id="lib-add-dropdown">
                ${newItems.map((it) => `
                <div class="settings-dropdown-item" data-action="${it.action}">
                  <i data-lucide="${it.icon}" class="lucide" style="width:14px;height:14px"></i>
                  <span>${it.label}</span>
                </div>`).join("")}
              </div>
            </div>
          </div>
          <div class="library-tree" id="library-tree">${this._renderLibTreeHtml(children, "", 0)}</div>
        </aside>
        <section class="library-content">
          <div class="library-crumb-row">
            <div class="library-crumbs" id="library-crumbs">${this._renderLibCrumbs(sel)}</div>
            <div class="library-crumb-actions" id="library-crumb-actions">${this._renderLibCrumbActions(sel, tFn)}</div>
          </div>
          <div class="library-view" id="library-view">${this._renderLibContent(sel, children)}</div>
        </section>
      </div>`;

    this.panels.library.innerHTML = html;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.library });
    }
    this._bindLibraryInteractions();
  }

  /** The "New" dropdown entries, one per library node type. */
  private _libNewItems(tFn: any): Array<{ action: string; icon: string; label: string }> {
    return [
      { action: "space", icon: "book-marked", label: tFn("settings.libSpace") },
      { action: "folder", icon: "folder", label: tFn("settings.libFolder") },
      { action: "doc", icon: "file-text", label: tFn("settings.libDoc") },
      { action: "sheet", icon: "table", label: tFn("settings.libSheet") },
      { action: "md", icon: "file-code", label: tFn("settings.libMd") },
      { action: "clip", icon: "globe", label: tFn("settings.libClip") },
      { action: "attachment", icon: "paperclip", label: tFn("settings.libAttachment") },
    ];
  }

  private _renderLibTreeHtml(children: Map<string, import("../core/types.js").LibraryNode[]>, parentId: string, depth: number): string {
    const kids = children.get(parentId) || [];
    if (kids.length === 0 && depth === 0) {
      return `<div class="model-empty"><i data-lucide="book-marked" class="lucide"></i><span>${t("settings.noDocuments")}</span></div>`;
    }
    let html = "";
    for (const n of kids) {
      const isBranch = n.type === "space" || n.type === "folder";
      const expanded = this._libExpanded!.has(n.id);
      const loading = n.status === "loading";
      html += `
        <div class="lib-tree-item${this._libSelectedId === n.id ? " selected" : ""}${isBranch ? " branch" : ""}" data-node-id="${n.id}" draggable="true" style="--lib-depth:${depth}">
          <span class="lib-twist" data-action="lib-toggle">${isBranch ? `<i data-lucide="${expanded ? "chevron-down" : "chevron-right"}" class="lucide"></i>` : ""}</span>
          <i data-lucide="${this._libIcon(n.type)}" class="lucide lib-type-icon"></i>
          <span class="lib-tree-name">${this._escapeHtml(this._libDisplayName(n))}</span>
          ${loading ? `<span class="doc-loading-spinner"></span>` : ""}
        </div>`;
      if (isBranch && expanded) {
        html += this._renderLibTreeHtml(children, n.id, depth + 1);
      }
    }
    return html;
  }

  /** Breadcrumb chain from the library root down to the selection. */
  private _renderLibCrumbs(sel: import("../core/types.js").LibraryNode | null): string {
    const st = getState();
    const chain: import("../core/types.js").LibraryNode[] = [];
    let cur = sel;
    while (cur) {
      chain.unshift(cur);
      cur = st.libraryTree.find((n) => n.id === cur!.parent_id) || null;
    }
    let html = `<span class="lib-crumb${sel ? "" : " current"}" data-node-id="">${t("settings.library")}</span>`;
    for (const n of chain) {
      html += `<i data-lucide="chevron-right" class="lucide lib-crumb-sep"></i><span class="lib-crumb${this._libSelectedId === n.id ? " current" : ""}" data-node-id="${n.id}"><i data-lucide="${this._libIcon(n.type)}" class="lucide" style="width:12px;height:12px"></i>${this._escapeHtml(this._libDisplayName(n))}</span>`;
    }
    return html;
  }

  private _renderLibCrumbActions(sel: import("../core/types.js").LibraryNode | null, tFn: any): string {
    if (!sel) return "";
    return `
      <button class="btn-icon" data-action="lib-rename" data-tooltip="${tFn("settings.libRename")}"><i data-lucide="pencil" class="lucide"></i></button>
      <button class="btn-icon btn-icon--danger" data-action="lib-delete" data-tooltip="${tFn("settings.delete")}"><i data-lucide="trash-2" class="lucide"></i></button>`;
  }

  /** Right pane body for the selected node (or the empty state). */
  private _renderLibContent(sel: import("../core/types.js").LibraryNode | null, children: Map<string, import("../core/types.js").LibraryNode[]>): string {
    if (!sel) {
      return `<div class="model-empty"><i data-lucide="book-marked" class="lucide"></i><span>${t("settings.libEmpty")}</span></div>`;
    }
    if (sel.type === "space" || sel.type === "folder") {
      const kids = children.get(sel.id) || [];
      if (kids.length === 0) {
        return `<div class="model-empty"><i data-lucide="${this._libIcon(sel.type)}" class="lucide"></i><span>${t("settings.libSpaceEmpty")}</span></div>`;
      }
      const rows = kids.map((n) => `
        <div class="lib-pane-item" data-node-id="${n.id}">
          <i data-lucide="${this._libIcon(n.type)}" class="lucide lib-type-icon"></i>
          <span class="lib-tree-name">${this._escapeHtml(this._libDisplayName(n))}</span>
          ${n.status === "loading" ? `<span class="doc-loading-spinner"></span>` : `<span class="skill-desc-text">${this._libFormatSize(n.size)}</span>`}
        </div>`).join("");
      return `<div class="lib-pane-list">${rows}</div>`;
    }

    // Leaf: content is only rendered once loaded (library_content arrives async).
    const st = getState();
    const loaded = st.libraryContent && st.libraryContent.id === sel.id ? st.libraryContent.content : null;
    if (sel.status === "loading" || loaded === null) {
      return `<div class="model-empty"><span class="doc-loading-spinner"></span><span>${t("common.loading")}</span></div>`;
    }
    const content = loaded;

    if (sel.type === "doc") {
      return `
        <div class="lib-editor-head">
          <div class="lib-doc-toolbar">
            <button class="btn-icon" data-cmd="bold" data-tooltip="${t("settings.fmtBold")}"><i data-lucide="bold" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="italic" data-tooltip="${t("settings.fmtItalic")}"><i data-lucide="italic" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="underline" data-tooltip="${t("settings.fmtUnderline")}"><i data-lucide="underline" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="formatBlock:&lt;h1&gt;" data-tooltip="${t("settings.fmtH1")}"><i data-lucide="heading-1" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="formatBlock:&lt;h2&gt;" data-tooltip="${t("settings.fmtH2")}"><i data-lucide="heading-2" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="insertUnorderedList" data-tooltip="${t("settings.fmtUl")}"><i data-lucide="list" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="insertOrderedList" data-tooltip="${t("settings.fmtOl")}"><i data-lucide="list-ordered" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="formatBlock:&lt;blockquote&gt;" data-tooltip="${t("settings.fmtQuote")}"><i data-lucide="quote" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="formatBlock:&lt;pre&gt;" data-tooltip="${t("settings.fmtCode")}"><i data-lucide="code" class="lucide"></i></button>
            <button class="btn-icon" data-cmd="removeFormat" data-tooltip="${t("settings.fmtClear")}"><i data-lucide="eraser" class="lucide"></i></button>
          </div>
          <span class="lib-save-state" id="lib-save-state"></span>
        </div>
        <div class="lib-doc-body" id="lib-doc-body" contenteditable="true">${content}</div>`;
    }

    if (sel.type === "md") {
      const previewActive = this._libMdMode === "preview";
      const body = previewActive
        ? `<div class="msg-text skill-preview-content">${renderMarkdown(content)}</div>`
        : `<textarea class="lib-md-editor" id="lib-md-editor" spellcheck="false">${this._escapeHtml(content)}</textarea>`;
      return `
        <div class="lib-editor-head">
          <div class="lib-md-toggle">
            <button class="btn-icon${previewActive ? " active" : ""}" data-md-mode="preview" data-tooltip="${t("settings.libPreview")}"><i data-lucide="eye" class="lucide"></i></button>
            <button class="btn-icon${previewActive ? "" : " active"}" data-md-mode="edit" data-tooltip="${t("settings.libEdit")}"><i data-lucide="pencil" class="lucide"></i></button>
          </div>
          <span class="lib-save-state" id="lib-save-state"></span>
        </div>
        <div class="lib-md-wrap" id="lib-md-wrap">${body}</div>`;
    }

    if (sel.type === "sheet") {
      let rows: string[][] = [];
      try {
        rows = (JSON.parse(content).rows || []) as string[][];
      } catch {
        rows = [["", "", ""]];
      }
      const body = rows.map((row) => `
        <tr>${row.map((cell) => `<td contenteditable="plaintext-only">${this._escapeHtml(cell)}</td>`).join("")}</tr>`).join("");
      return `
        <div class="lib-editor-head">
          <div class="lib-sheet-actions">
            <button class="btn-add-model-top" data-action="lib-add-row"><i data-lucide="plus" class="lucide" style="width:14px;height:14px"></i><span>${t("settings.libAddRow")}</span></button>
            <button class="btn-add-model-top" data-action="lib-add-col"><i data-lucide="plus" class="lucide" style="width:14px;height:14px"></i><span>${t("settings.libAddCol")}</span></button>
          </div>
          <span class="lib-save-state" id="lib-save-state"></span>
        </div>
        <div class="lib-sheet-wrap"><table class="lib-sheet-grid" id="lib-sheet-grid"><tbody>${body}</tbody></table></div>`;
    }

    if (sel.type === "clip") {
      const src = sel.original_url || "";
      return `
        <div class="lib-source-row">
          <i data-lucide="globe" class="lucide" style="width:14px;height:14px"></i>
          <span class="lib-source-link" data-url="${this._escapeHtml(src)}">${this._escapeHtml(src)}</span>
        </div>
        <div class="msg-text skill-preview-content">${renderMarkdown(content)}</div>`;
    }

    // attachment
    return `
      <div class="lib-source-row">
        <i data-lucide="paperclip" class="lucide" style="width:14px;height:14px"></i>
        <span class="skill-desc-text">${this._escapeHtml(sel.original_path || "")} · ${this._libFormatSize(sel.size)}</span>
      </div>
      ${content ? `<pre class="lib-att-preview">${this._escapeHtml(content)}</pre>` : `<div class="model-empty"><span>${t("settings.libBinaryFile")}</span></div>`}`;
  }

  /** Re-render only the right pane (crumbs + content) for the selection. */
  private _renderLibPane(): void {
    const st = getState();
    const sel = st.libraryTree.find((n) => n.id === this._libSelectedId) || null;
    const crumbs = document.getElementById("library-crumbs");
    if (crumbs) {
      crumbs.innerHTML = this._renderLibCrumbs(sel);
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: crumbs });
      }
    }
    const actions = document.getElementById("library-crumb-actions");
    if (actions) {
      actions.innerHTML = this._renderLibCrumbActions(sel, t);
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: actions });
      }
    }
    const view = document.getElementById("library-view");
    if (view) {
      view.innerHTML = this._renderLibContent(sel, this._libChildrenMap(st.libraryTree));
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: view });
      }
      this._libMdText = st.libraryContent && st.libraryContent.id === this._libSelectedId ? st.libraryContent.content : "";
      this._libMdMode = "preview";
    }
  }

  /** Select a tree node: highlight + refresh pane, load leaf content. */
  private _libSelect(nodeId: string): void {
    this._libSelectedId = nodeId;
    document.querySelectorAll("#library-tree .lib-tree-item").forEach((el) => {
      el.classList.toggle("selected", el.getAttribute("data-node-id") === nodeId);
    });
    const st = getState();
    const node = st.libraryTree.find((n) => n.id === nodeId) || null;
    if (node && node.type !== "space" && node.type !== "folder") {
      // Invalidate cached content so the pane shows loading until fresh data.
      setLibraryContent(null);
      this._renderLibPane();
      send({ type: "library_get_content", id: nodeId } as any);
      return;
    }
    this._renderLibPane();
  }

  /** Debounced save of leaf content back to the backend. */
  private _libScheduleSave(nodeId: string, content: string): void {
    if (this._libSaveTimer) clearTimeout(this._libSaveTimer);
    const stateEl = document.getElementById("lib-save-state");
    if (stateEl) stateEl.textContent = t("settings.libSaving");
    this._libSaveTimer = setTimeout(() => {
      this._libSaveTimer = null;
      send({ type: "library_save_content", id: nodeId, content } as any);
      const el = document.getElementById("lib-save-state");
      if (el) el.textContent = t("settings.libSaved");
    }, 900);
  }

  /** Collect the current sheet grid from the DOM as a row matrix. */
  private _libCollectSheet(): string[][] {
    const grid = document.getElementById("lib-sheet-grid");
    if (!grid) return [];
    const rows: string[][] = [];
    grid.querySelectorAll("tr").forEach((tr) => {
      const row: string[] = [];
      tr.querySelectorAll("td").forEach((td) => row.push((td as HTMLElement).textContent || ""));
      rows.push(row);
    });
    return rows;
  }

  /**
   * Bind the library panel's delegated events.
   *
   * All listeners attach to the persistent containers (#library-tree,
   * #library-crumbs, #library-crumb-actions, #library-view), so partial
   * refreshes that only swap innerHTML never need re-binding.
   */
  private _bindLibraryInteractions(): void {
    const tree = document.getElementById("library-tree");
    if (!tree) return;

    // New-node dropdown (direct bind: these elements are recreated per render).
    const trigger = document.getElementById("lib-add-trigger");
    const dropdown = document.getElementById("lib-add-dropdown");
    trigger?.addEventListener("click", (e) => {
      e.stopPropagation();
      dropdown?.classList.toggle("open");
    });
    dropdown?.addEventListener("click", (e) => {
      const item = (e.target as HTMLElement).closest(".settings-dropdown-item") as HTMLElement | null;
      if (!item) return;
      dropdown.classList.remove("open");
      this._libHandleNew(item.getAttribute("data-action") || "");
    });

    // Tree: select / toggle / drag-move (all delegated on the container).
    tree.addEventListener("click", (e) => {
      const item = (e.target as HTMLElement).closest(".lib-tree-item") as HTMLElement | null;
      if (!item) return;
      const id = item.getAttribute("data-node-id") || "";
      const twist = (e.target as HTMLElement).closest('[data-action="lib-toggle"]');
      if (twist) {
        if (this._libExpanded!.has(id)) this._libExpanded!.delete(id);
        else this._libExpanded!.add(id);
        this.refreshLibraryTree();
        return;
      }
      this._libSelect(id);
    });
    tree.addEventListener("dragstart", (e) => {
      const item = (e.target as HTMLElement).closest(".lib-tree-item") as HTMLElement | null;
      if (!item) return;
      (e as DragEvent).dataTransfer?.setData("text/plain", `lib-node:${item.getAttribute("data-node-id") || ""}`);
    });
    tree.addEventListener("dragover", (e) => {
      const item = (e.target as HTMLElement).closest(".lib-tree-item") as HTMLElement | null;
      if (!item) return;
      const st = getState();
      const node = st.libraryTree.find((n) => n.id === item.getAttribute("data-node-id"));
      if (!node || (node.type !== "space" && node.type !== "folder")) return;
      e.preventDefault();
      item.classList.add("lib-drop-hover");
    });
    tree.addEventListener("dragleave", (e) => {
      const item = (e.target as HTMLElement).closest(".lib-tree-item");
      item?.classList.remove("lib-drop-hover");
    });
    tree.addEventListener("drop", (e) => {
      const item = (e.target as HTMLElement).closest(".lib-tree-item") as HTMLElement | null;
      if (!item) return;
      item.classList.remove("lib-drop-hover");
      const raw = (e as DragEvent).dataTransfer?.getData("text/plain") || "";
      if (!raw.startsWith("lib-node:")) return;
      const draggedId = raw.slice("lib-node:".length);
      const targetId = item.getAttribute("data-node-id") || "";
      if (draggedId === targetId) return;
      send({ type: "library_move_node", id: draggedId, parent_id: targetId, index: 9999 } as any);
    });

    // Breadcrumbs navigate up the chain.
    document.getElementById("library-crumbs")?.addEventListener("click", (e) => {
      const crumb = (e.target as HTMLElement).closest(".lib-crumb") as HTMLElement | null;
      if (!crumb) return;
      this._libSelect(crumb.getAttribute("data-node-id") || "");
    });

    // Rename / delete for the current selection.
    document.getElementById("library-crumb-actions")?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest("[data-action]") as HTMLElement | null;
      if (!btn) return;
      const action = btn.getAttribute("data-action");
      const st = getState();
      const sel = st.libraryTree.find((n) => n.id === this._libSelectedId);
      if (!sel) return;
      if (action === "lib-rename") {
        this._showDocNameDialog(this._libDisplayName(sel), (name: string) => {
          send({ type: "library_rename_node", id: sel.id, name } as any);
        }, t);
      } else if (action === "lib-delete") {
        Dialog.confirm(t("settings.deleteDoc"), t("settings.libConfirmDelete", { name: this._libDisplayName(sel) }), { scope: "panel" }).then((ok) => {
          if (ok) send({ type: "library_delete_node", id: sel.id } as any);
        });
      }
    });

    // Pane interactions: navigation, editors, toolbar, sheet ops.
    const view = document.getElementById("library-view");
    if (!view) return;
    view.addEventListener("mousedown", (e) => {
      // Keep the text selection alive when clicking a format button.
      if ((e.target as HTMLElement).closest("[data-cmd]")) e.preventDefault();
    });
    view.addEventListener("click", (e) => {
      const el = e.target as HTMLElement;
      const cmdBtn = el.closest("[data-cmd]") as HTMLElement | null;
      if (cmdBtn) {
        const raw = cmdBtn.getAttribute("data-cmd") || "";
        const sep = raw.indexOf(":");
        const cmd = sep === -1 ? raw : raw.slice(0, sep);
        const val = sep === -1 ? undefined : raw.slice(sep + 1).replace("&lt;", "<").replace("&gt;", ">");
        document.execCommand(cmd, false, val);
        const body = document.getElementById("lib-doc-body");
        if (body && this._libSelectedId) this._libScheduleSave(this._libSelectedId, body.innerHTML);
        return;
      }
      const mdBtn = el.closest("[data-md-mode]") as HTMLElement | null;
      if (mdBtn) {
        const editor = document.getElementById("lib-md-editor") as HTMLTextAreaElement | null;
        if (editor) this._libMdText = editor.value;
        this._libMdMode = (mdBtn.getAttribute("data-md-mode") as "preview" | "edit") || "preview";
        this._renderLibPane();
        return;
      }
      const paneItem = el.closest(".lib-pane-item") as HTMLElement | null;
      if (paneItem) {
        this._libSelect(paneItem.getAttribute("data-node-id") || "");
        return;
      }
      const sourceLink = el.closest(".lib-source-link") as HTMLElement | null;
      if (sourceLink) {
        openRoutedLink(sourceLink.getAttribute("data-url") || "");
        return;
      }
      const actionBtn = el.closest("[data-action]") as HTMLElement | null;
      if (actionBtn && actionBtn.getAttribute("data-action") === "lib-add-row") {
        const grid = document.getElementById("lib-sheet-grid");
        if (grid) {
          const cols = grid.querySelector("tr")?.querySelectorAll("td").length || 3;
          const tr = document.createElement("tr");
          for (let i = 0; i < cols; i++) {
            const td = document.createElement("td");
            td.setAttribute("contenteditable", "plaintext-only");
            tr.appendChild(td);
          }
          grid.querySelector("tbody")?.appendChild(tr);
          if (this._libSelectedId) this._libScheduleSave(this._libSelectedId, JSON.stringify({ rows: this._libCollectSheet() }));
        }
      } else if (actionBtn && actionBtn.getAttribute("data-action") === "lib-add-col") {
        const grid = document.getElementById("lib-sheet-grid");
        if (grid) {
          grid.querySelectorAll("tr").forEach((tr) => {
            const td = document.createElement("td");
            td.setAttribute("contenteditable", "plaintext-only");
            tr.appendChild(td);
          });
          if (this._libSelectedId) this._libScheduleSave(this._libSelectedId, JSON.stringify({ rows: this._libCollectSheet() }));
        }
      }
    });
    view.addEventListener("input", (e) => {
      const el = e.target as HTMLElement;
      if (!this._libSelectedId) return;
      if (el.id === "lib-doc-body") {
        this._libScheduleSave(this._libSelectedId, el.innerHTML);
      } else if (el.id === "lib-md-editor") {
        this._libMdText = (el as HTMLTextAreaElement).value;
        this._libScheduleSave(this._libSelectedId, this._libMdText);
      } else if (el.closest(".lib-sheet-grid")) {
        this._libScheduleSave(this._libSelectedId, JSON.stringify({ rows: this._libCollectSheet() }));
      }
    });
  }

  /** Dispatch a "New" dropdown action for the target parent. */
  private _libHandleNew(action: string): void {
    if (!action) return;
    const parentId = this._libTargetParentId();
    if (action === "space") {
      this._showDocNameDialog("", (name: string) => {
        send({ type: "library_create_node", parent_id: "", node_type: "space", name } as any);
      }, t);
      return;
    }
    if (action === "folder" || action === "doc" || action === "sheet") {
      this._showDocNameDialog("", (name: string) => {
        send({ type: "library_create_node", parent_id: parentId, node_type: action, name } as any);
      }, t);
      return;
    }
    if (action === "md" || action === "attachment") {
      const input = document.createElement("input");
      input.type = "file";
      if (action === "md") {
        input.accept = ".md,.markdown,.txt";
      }
      input.addEventListener("change", () => {
        const file = input.files?.[0];
        if (!file) return;
        const path = (window as any).webUtils?.getPathForFile?.(file) || (file as any).path || "";
        if (!path) return;
        this._showDocNameDialog(file.name, (name: string) => {
          send({ type: "library_add_file", parent_id: parentId, name, file_path: path } as any);
        }, t);
      });
      input.click();
      return;
    }
    if (action === "clip") {
      this._showDocUrlDialog(parentId);
    }
  }

  /** Public: the backend pushed a new library tree. Refresh sidebar + crumbs
   *  only — the content editor must never be rebuilt while the user types. */
  public refreshLibraryTree(): void {
    if (!this.panels.library || !document.getElementById("library-tree")) {
      this.renderLibrary();
      return;
    }
    const st = getState();
    if (this._libSelectedId && !st.libraryTree.some((n) => n.id === this._libSelectedId)) {
      this._libSelectedId = "";
    }
    const children = this._libChildrenMap(st.libraryTree);
    const treeEl = document.getElementById("library-tree");
    if (treeEl) {
      treeEl.innerHTML = this._renderLibTreeHtml(children, "", 0);
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: treeEl });
      }
    }
    // Selection highlight after the tree re-render.
    document.querySelectorAll("#library-tree .lib-tree-item").forEach((el) => {
      el.classList.toggle("selected", el.getAttribute("data-node-id") === this._libSelectedId);
    });
    const sel = st.libraryTree.find((n) => n.id === this._libSelectedId) || null;
    const crumbs = document.getElementById("library-crumbs");
    if (crumbs) {
      crumbs.innerHTML = this._renderLibCrumbs(sel);
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: crumbs });
      }
    }
    const actions = document.getElementById("library-crumb-actions");
    if (actions) {
      actions.innerHTML = this._renderLibCrumbActions(sel, t);
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: actions });
      }
    }
    // A pending leaf that just turned ready may need its pane spinner cleared.
    if (sel && sel.type !== "space" && sel.type !== "folder") {
      const view = document.getElementById("library-view");
      if (view && view.querySelector(".doc-loading-spinner") && sel.status === "ready") {
        send({ type: "library_get_content", id: sel.id } as any);
      }
    }
  }

  /** Public: the requested leaf content arrived. Fill the pane if still wanted. */
  public refreshLibraryContent(): void {
    const st = getState();
    if (!st.libraryContent || st.libraryContent.id !== this._libSelectedId) return;
    const view = document.getElementById("library-view");
    if (!view) return;
    this._renderLibPane();
  }


  private _showDocNameDialog(defaultName: string, onConfirm: (name: string) => void, tFn: any): void {
    // Routed through the shared form entry so this dialog inherits the panel
    // embedding (isolated draft + unsaved-changes guard) and the standard
    // Cancel / Confirm footer, instead of hand-rolling its own overlay.
    const bodyHtml = `
      <div class="input-wrapper input-wrapper--dialog">
        <input id="dialog-doc-name" value="${this._escapeHtml(defaultName)}" placeholder="${tFn("settings.docNamePlaceholder")}" style="width:100%;border:none;outline:none;background:transparent;color:var(--text-primary);font-family:inherit;font-size:13px">
      </div>`;
    const { overlay, close } = this._showFormDialog(tFn("settings.docName"), bodyHtml, false);

    const input = overlay.querySelector("#dialog-doc-name") as HTMLInputElement;
    input?.focus();
    input?.select();

    // Cancel is already wired by the shared shell.
    overlay.querySelector("#dialog-form-ok")?.addEventListener("click", () => {
      onConfirm(input?.value?.trim() || defaultName);
      close();
    });
  }

  private _showDocUrlDialog(parentId: string): void {
    const bodyHtml = `
      <input class="toast-input" id="dialog-doc-name-input" placeholder="${t("settings.docNamePlaceholder")}">
      <input class="toast-input" id="dialog-doc-url-input" placeholder="${t("settings.docUrlPlaceholder")}" style="margin-top:8px">`;
    const { overlay, close } = this._showFormDialog(t("settings.addDocFromUrl"), bodyHtml, false);

    const nameInput = overlay.querySelector("#dialog-doc-name-input") as HTMLInputElement;
    const urlInput = overlay.querySelector("#dialog-doc-url-input") as HTMLInputElement;
    urlInput?.focus();

    overlay.querySelector("#dialog-form-ok")?.addEventListener("click", () => {
      const name = nameInput?.value?.trim() || "";
      const url = urlInput?.value?.trim() || "";
      if (!url) return;
      send({ type: "library_add_clip", parent_id: parentId, name, url } as any);
      close();
    });
  }
  private _escapeHtml(str: string): string {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  /** Settings panels whose create/edit forms are rendered as a floating card
   *  over the settings content area (`.panel-editor` on `#main-content`, the
   *  same mount pattern as `.encre-dialog-overlay`) instead of a full-screen
   *  modal, so the settings sidebar stays usable and every panel keeps its own
   *  isolated draft. */
  private static readonly INLINE_EDITOR_PANELS = new Set<string>([
    "general", "model", "agent", "mcp", "skills", "gateway", "library", "memory",
    "assist",
  ]);
  /** Fingerprint captured when a panel editor mounts.  Keyed by the editor
   *  node (WeakMap) so a torn-down draft's snapshot is reclaimed with it. */
  private _editorBaselines = new WeakMap<HTMLElement, string>();

  /** The panel that should host an inline editor right now, or null when a
   *  modal is still the right container (settings closed, or a panel that is
   *  not a form host). */
  private _inlineEditorHost(): { id: string; el: HTMLElement } | null {
    if (!document.getElementById("app")?.classList.contains("settings-mode")) return null;
    const id = this.currentPanel as string;
    if (!Settings.INLINE_EDITOR_PANELS.has(id)) return null;
    const el = (this.panels as unknown as Record<string, HTMLElement>)[id];
    return el ? { id, el } : null;
  }

  /** True while `panelId` holds an editor: its list render is skipped so the
   *  draft (and the caret) survives a hop to another panel and back. */
  private _panelEditorActive(panelId: string): boolean {
    return !!this._editorHost()?.querySelector(`:scope > .panel-editor[data-panel="${panelId}"]`);
  }

  /** The stable, non-scrolling, positioned element the inline editors mount
   *  on — same approach as `Dialog` appending an `.encre-dialog-overlay` to
   *  its container, so the backdrop never inherits anchors/transforms from
   *  panel content. */
  private _editorHost(): HTMLElement | null {
    return document.getElementById("main-content");
  }

  /** Order-stable fingerprint of every editable control inside an editor.
   *  `querySelectorAll` walks in document order, and adding or removing a row
   *  changes the entry count, so structural edits read as dirty too. */
  private _serializePanelEditor(root: HTMLElement): string {
    const parts: string[] = [];
    root.querySelectorAll<HTMLElement>(
      "input, textarea, [contenteditable], .settings-dropdown-wrap"
    ).forEach((el) => {
      if (el.classList.contains("settings-dropdown-wrap")) {
        // Custom dropdown: the live selection lives on the item, not the trigger.
        const sel = el.querySelector(".settings-dropdown-item.selected");
        parts.push(`dd:${sel?.getAttribute("data-value") ?? ""}`);
      } else if (el.tagName === "TEXTAREA") {
        parts.push(`v:${(el as HTMLTextAreaElement).value}`);
      } else if (el.tagName === "INPUT") {
        const inp = el as HTMLInputElement;
        parts.push(inp.type === "checkbox" || inp.type === "radio" ? `cb:${inp.checked}` : `v:${inp.value}`);
      } else {
        parts.push(`v:${el.textContent ?? ""}`);
      }
    });
    return parts.join("\u0000");
  }

  /** Assist-style editors persist every control the moment it changes (the
   *  shared `saveSetting` path), so the shell's dirty check must never fire on
   *  them: re-snapshot the baseline after each edit and the back arrow closes
   *  straight away instead of asking about changes that are already saved. */
  private _keepPanelEditorClean(editor: HTMLElement): void {
    const rebaseline = () => {
      this._editorBaselines.set(editor, this._serializePanelEditor(editor));
    };
    editor.addEventListener("change", rebaseline);
    editor.addEventListener("input", rebaseline);
    rebaseline();
  }

  /** Every panel that currently owns an inline editor. */
  private _openPanelEditors(): Array<{ id: string; panel: HTMLElement; editor: HTMLElement }> {
    const out: Array<{ id: string; panel: HTMLElement; editor: HTMLElement }> = [];
    const panels = this.panels as unknown as Record<string, HTMLElement>;
    this._editorHost()?.querySelectorAll<HTMLElement>(":scope > .panel-editor").forEach((editor) => {
      const id = editor.dataset.panel || "";
      if (id && panels[id]) out.push({ id, panel: panels[id], editor });
    });
    return out;
  }

  /** Re-paint a panel's list/landing content once its editor is gone. */
  private _renderPanelBody(panelId: string): void {
    // One failing settings panel keeps its previous DOM and must not abort
    // the panel switch or any other surface. Immediate mode of the single
    // render scheduler, so settings paints share the same path.
    runScheduledRenderNow(`settings:${panelId}`, () => guardRender("settings-panel", panelId, () => {
      switch (panelId) {
        case "model": this.renderModel(); break;
        case "skills": this.renderSkills(); break;
        case "gateway": this.renderGateway(); break;
        case "mcp": this.renderMcpList(); break;
        case "agent": this.renderAgent(); break;
        case "memory": this.renderMemory(); break;
        case "assist": this.renderAssistPanel(); break;
        case "library": this.renderLibrary(); break;
      }
    }));
  }

  /** Unmount an inline editor and give its panel the list back.  The editor
   *  fades out as ONE unit (backdrop + card + controls share the same exit
   *  animation), so the card never snaps away while its controls linger. */
  private _disposePanelEditor(editor: HTMLElement, repaint: boolean): void {
    unregisterSurface(editor);
    removeAnimated(editor, () => {
      this._editorBaselines.delete(editor);
      // The memory editor owns live singletons (graph control, detail loader)
      // outside the editor node — tear them down with the card so a stale
      // handle is never fed updates or blocks resubscription.
      if (editor.dataset.panel === "memory") {
        this._memoryGraphCtl?.destroy();
        this._memoryGraphCtl = null;
        this._destroyMemoryDetailLoader();
      }
      if (!repaint) return;
      const id = editor.dataset.panel || "";
      if (id && (this.panels as unknown as Record<string, HTMLElement>)[id]) this._renderPanelBody(id);
    }, "closing");
  }

  /** Leaving the settings view: if a panel still holds a modified draft, ask
   *  before discarding it.  `false` means the user chose to stay. */
  private async _confirmDiscardEditors(): Promise<boolean> {
    const open = this._openPanelEditors();
    if (open.length === 0) return true;
    const dirty = open.filter(({ editor }) => {
      const baseline = this._editorBaselines.get(editor);
      return baseline !== undefined && this._serializePanelEditor(editor) !== baseline;
    });
    if (dirty.length > 0) {
      const name = dirty[0].editor.querySelector(".panel-editor-title-text")?.textContent || "";
      const ok = await Dialog.confirm(
        t("sessionInner.closeUnsavedTitle"),
        t("sessionInner.closeUnsavedMessage", { name }),
        { primary: t("sessionInner.discardChanges") },
      );
      if (!ok) return false;
    }
    open.forEach(({ editor }) => this._disposePanelEditor(editor, false));
    return true;
  }

  /** Entry point for every settings create/edit form.
   *
   *  Inside settings the form becomes a floating card anchored to the owning
   *  panel (`_showPanelEditor`), so the sidebar stays clickable and each panel
   *  keeps an isolated draft.  Anywhere else — or for panels that are not form
   *  hosts — it stays the classic full-screen modal.
   *
   *  Both flavours return `overlay`, which is what keeps every dialog body
   *  untouched: they all resolve their controls via `overlay.querySelector`. */
  private _showFormDialog(title: string, bodyHtml: string, wide: boolean = false): {
    overlay: HTMLElement;
    close: (repaint?: boolean) => void;
    isDirty: () => boolean;
  } {
    const host = this._inlineEditorHost();
    if (host) return this._showPanelEditor(host.id, host.el, title, bodyHtml, wide);
    return this._showModalFormDialog(title, bodyHtml, wide);
  }

  /** Full-screen flavour — the original behaviour, unchanged. */
  private _showModalFormDialog(title: string, bodyHtml: string, wide: boolean = false): {
    overlay: HTMLElement;
    close: (repaint?: boolean) => void;
    isDirty: () => boolean;
  } {
    const overlay = document.createElement("div");
    overlay.className = "toast-overlay";
    overlay.innerHTML = `
      <div class="toast-dialog ${wide ? 'dialog-wide' : ''}">
        <div class="toast-title">${this._escapeHtml(title)}</div>
        <div class="dialog-body" style="display:flex;flex-direction:column;width:100%;flex:1 1 auto;min-height:0;box-sizing:border-box">${bodyHtml}</div>
        <div class="dialog-footer">
          <button class="btn" id="dialog-form-cancel">${t("common.cancel")}</button>
          <button class="btn btn--primary" id="dialog-form-ok">${t("common.confirm")}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    // Same wiring as the in-panel flavour: any `.input-wrapper--dialog` holding
    // a multi-line textarea gets the composer's expand toggle (top-right,
    // hover-revealed).  New dialogs built on this entry get it for free.
    attachInputExpand(overlay);

    overlay.querySelectorAll("textarea").forEach((ta) => {
      let raf = 0;
      const autoResize = () => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          ta.style.height = "auto";
          ta.style.height = Math.min(ta.scrollHeight, 300) + "px";
        });
      };
      ta.addEventListener("input", autoResize);
      autoResize();
    });

    // Auto-resize contenteditable prompt-inputs in dialogs
    overlay.querySelectorAll(".prompt-input[contenteditable]").forEach((el) => {
      const ph = el.parentElement?.querySelector(".prompt-placeholder") as HTMLElement;
      let raf = 0;
      const update = () => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          (el as HTMLElement).style.height = "auto";
          (el as HTMLElement).style.height = Math.min((el as HTMLElement).scrollHeight, 300) + "px";
          if (ph) {
            ph.classList.toggle("hidden", (el.textContent || "").trim().length > 0);
          }
        });
      };
      el.addEventListener("input", update);
      update();
    });

    // A settings form that has to use the full-screen flavour still belongs to
    // the panel that opened it: the host shelves it while another panel is up,
    // instead of leaving it floating over the chat once settings closes.
    registerSurface(overlay, {
      scope: "panel",
      panelKey: this.currentPanel as string,
      onClose: () => overlay.remove(),
    });
    const close = () => {
      unregisterSurface(overlay);
      removeAnimated(overlay);
    };
    overlay.querySelector("#dialog-form-cancel")?.addEventListener("click", close);
    // No "click outside to dismiss" -- the form has unsaved edits and the
    // user has to deliberately press Cancel to discard them.  Also avoids
    // accidental dismissal when the user clicks on dropdown menus that
    // temporarily extend beyond the dialog bounds.

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }

    return { overlay, close, isDirty: () => false };
  }

  /** In-panel flavour of {@link _showFormDialog}.
   *
   *  The form is a floating card over the settings content area: exactly like
   *  `Dialog` mounting an `.encre-dialog-overlay`, the dimmed, blurred
   *  backdrop is appended DIRECTLY to a stable positioned container
   *  (`#main-content`) — never inside panel content, where animations and
   *  transforms would steal the anchoring.  The settings sidebar keeps
   *  working, and the draft is owned by one panel alone (keyed by
   *  `data-panel`): hopping to another panel merely shelves it
   *  (`.panel-editor-off`), and coming back re-shows it exactly as it was
   *  left.  Only an explicit close (Back / Cancel) or leaving settings can
   *  throw the draft away, and both ask first when it was modified.
   *
   *  The card reuses the `.toast-dialog` markup shell purely as a form hook
   *  (`.model-form-*` rules, expand toggle, footer buttons); the floating-card
   *  chrome (backdrop, background / border / radius / shadow) comes from the
   *  `.panel-editor` CSS, mirroring `.encre-dialog-overlay`. */
  private _showPanelEditor(panelId: string, panel: HTMLElement, title: string, bodyHtml: string, wide: boolean): {
    overlay: HTMLElement;
    close: () => void;
    isDirty: () => boolean;
  } {
    const host = this._editorHost() ?? panel;
    // One editor per panel: mounting a second replaces the first, so a panel
    // can never show two competing drafts.
    host.querySelectorAll(`:scope > .panel-editor[data-panel="${panelId}"]`).forEach((el) => el.remove());

    const editor = document.createElement("div");
    // Same mount pattern as `.encre-dialog-overlay`: a direct child of a
    // stable positioned container, so `inset: 0` covers the ENTIRE settings
    // content area regardless of what the underlying panel is doing.
    editor.className = "panel-editor";
    editor.dataset.panel = panelId;
    // The top-bar back arrow ships hidden: on a main editor page it duplicates
    // the footer Cancel/OK buttons. Sub-pages (e.g. the per-model detail view)
    // reveal it explicitly — there it means "back to the parent list".
    editor.innerHTML = `
      <div class="toast-dialog ${wide ? 'dialog-wide' : ''} panel-editor-dialog encre-dialog-card">
        <div class="toast-title panel-editor-title">
          <button type="button" class="btn-icon btn-icon--sm panel-editor-back" style="display:none" data-tooltip="${this._escapeHtml(t("sidebar.back"))}">
            <i data-lucide="arrow-left" class="lucide"></i>
          </button>
          <span class="panel-editor-title-text">${this._escapeHtml(title)}</span>
        </div>
        <div class="dialog-body" style="display:flex;flex-direction:column;width:100%;flex:1 1 auto;min-height:0;box-sizing:border-box">${bodyHtml}</div>
        <div class="dialog-footer">
          <button class="btn" id="dialog-form-cancel">${t("common.cancel")}</button>
          <button class="btn btn--primary" id="dialog-form-ok">${t("common.confirm")}</button>
        </div>
      </div>`;
    host.appendChild(editor);
    // Only the current panel's editor is visible; every other panel's draft
    // stays mounted but shelved via `panel-editor-off` (display:none — the
    // subtree leaves rendering entirely, so no control can outlive the shell).
    // Re-showing replays the enter animations from zero: backdrop, card and
    // controls appear as ONE synchronized surface.  Draft state survives —
    // the DOM nodes are only taken out of rendering, not destroyed.
    //
    // The editor declares itself to the scope host as PANEL-owned and
    // self-shelving: switching panels is the host's job, so no hand-written
    // sweep of `#main-content` is left to remember this node.
    registerSurface(editor, {
      scope: "panel",
      panelKey: panelId,
      mode: "self",
      onHide: () => editor.classList.add("panel-editor-off"),
      onShow: () => editor.classList.remove("panel-editor-off"),
      onClose: () => this._disposePanelEditor(editor, true),
    });

    // Same wiring as the modal flavour, so every dialog built on this entry
    // (expand toggle, auto-grow, lucide icons) behaves identically.
    attachInputExpand(editor);

    editor.querySelectorAll("textarea").forEach((ta) => {
      let raf = 0;
      const autoResize = () => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          ta.style.height = "auto";
          ta.style.height = Math.min(ta.scrollHeight, 300) + "px";
        });
      };
      ta.addEventListener("input", autoResize);
      autoResize();
    });

    // Auto-resize contenteditable prompt-inputs in editors
    editor.querySelectorAll(".prompt-input[contenteditable]").forEach((el) => {
      const ph = el.parentElement?.querySelector(".prompt-placeholder") as HTMLElement;
      let raf = 0;
      const update = () => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          (el as HTMLElement).style.height = "auto";
          (el as HTMLElement).style.height = Math.min((el as HTMLElement).scrollHeight, 300) + "px";
          if (ph) {
            ph.classList.toggle("hidden", (el.textContent || "").trim().length > 0);
          }
        });
      };
      el.addEventListener("input", update);
      update();
    });

    this._editorBaselines.set(editor, this._serializePanelEditor(editor));
    const isDirty = () => this._serializePanelEditor(editor) !== this._editorBaselines.get(editor);

    // `repaint` defaults to true (form dialogs repaint their panel list on
    // close); view-only dialogs (e.g. the memory detail reader) pass false --
    // repainting would rebuild the panel and reset the graph view, which
    // reads exactly like "the page refreshed itself".
    const close = (repaint = true) => this._disposePanelEditor(editor, repaint);
    // Back / Cancel discard the draft, so they ask first when it was touched.
    // Save handlers call `close()` directly and stay prompt-free.
    const requestClose = async () => {
      if (isDirty()) {
        const ok = await Dialog.confirm(
          t("sessionInner.closeUnsavedTitle"),
          t("sessionInner.closeUnsavedMessage", { name: title }),
          { primary: t("sessionInner.discardChanges") },
        );
        if (!ok) return;
      }
      close();
    };
    editor.querySelector("#dialog-form-cancel")?.addEventListener("click", () => { void requestClose(); });
    editor.querySelector(".panel-editor-back")?.addEventListener("click", () => { void requestClose(); });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: editor });
    }

    return { overlay: editor, close, isDirty };
  }

  private _chooseImportMode(): Promise<"overwrite" | "replace" | "skip" | null> {
    // Reuse the standard form dialog + the project's settings-dropdown widget
    // so the picker matches the rest of the app.  The destructive "replace"
    // option still triggers a second, high-priority confirmation.
    return new Promise((done) => {
      const modeOptions: DropdownOption[] = [
        { id: "overwrite", label: t("settings.storageImportModeOverwrite") },
        { id: "replace", label: t("settings.storageImportModeReplace") },
        { id: "skip", label: t("settings.storageImportModeSkip") },
      ];
      const bodyHtml = `
        <div class="model-form-row" style="flex-direction:column;align-items:stretch">
          <label class="model-form-label">${this.esc(t("settings.storageImportModeDesc"))}</label>
          <div class="model-form-dropdown-row">
            ${this.renderDropdown("import-mode", modeOptions, "overwrite", () => {})}
          </div>
          <div class="model-form-hint" id="import-mode-desc">${this.esc(t("settings.storageImportModeOverwriteDesc"))}</div>
        </div>`;

      const { overlay } = this._showFormDialog(t("settings.storageImportTitle"), bodyHtml, false);

      const modeDescriptions: Record<string, string> = {
        overwrite: t("settings.storageImportModeOverwriteDesc"),
        replace: t("settings.storageImportModeReplaceDesc"),
        skip: t("settings.storageImportModeSkipDesc"),
      };
      let selected: "overwrite" | "replace" | "skip" = "overwrite";
      this.bindDropdown("import-mode", (val) => {
        selected = (val as "overwrite" | "replace" | "skip") || "overwrite";
        const desc = overlay.querySelector("#import-mode-desc") as HTMLElement;
        if (desc) desc.textContent = modeDescriptions[selected] || "";
      });

      const finish = (v: "overwrite" | "replace" | "skip" | null) => {
        removeAnimated(overlay, () => done(v));
      };

      const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
      okBtn.addEventListener("click", async () => {
        if (selected === "replace") {
          const sure = await Dialog.confirm(
            t("settings.storageImportReplaceTitle"),
            t("settings.storageImportReplaceConfirm"),
            { priority: "high", scope: "app" },
          );
          if (!sure) return;
        }
        finish(selected);
      });
      overlay.querySelector("#dialog-form-cancel")?.addEventListener("click", () => finish(null));
    });
  }

  private renderSkills(): void {
    if (this._panelEditorActive("skills")) return;
    const st = getState();
    const skills: SkillInfo[] = st.skillsList;
    const enabled = new Set(st.enabledSkills);

    let rowsHtml = "";
    for (const sk of skills) {
      const isOn = enabled.has(sk.name);
      const isUser = sk.source === "user" || sk.source === "project";
      const canDelete = sk.source !== "bundled" && sk.source !== "managed";
      rowsHtml += `
        <div class="model-table-row" data-skill="${this.esc(sk.name)}">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">${this.esc(sk.name)}</span>
          </div>
          <div class="model-table-cell model-cell-provider">
            <span class="skill-desc-text"${sk.description ? ` data-tooltip="${this.esc(sk.description)}"` : ""}>${this.esc(sk.description)}</span>
          </div>
          <div class="model-table-cell model-cell-actions">
            <button class="btn-icon" data-action="view-skill" data-name="${this.esc(sk.name)}" data-tooltip="${t("settings.view")}">
              <i data-lucide="arrow-up-right" class="lucide"></i>
            </button>
            ${isUser ? `<button class="btn-icon" data-action="edit-skill" data-name="${this.esc(sk.name)}" data-tooltip="${t("settings.edit")}">
              <i data-lucide="pencil" class="lucide"></i>
            </button>` : ""}
            ${canDelete ? `<button class="btn-icon btn-icon--danger" data-action="delete-skill" data-name="${this.esc(sk.name)}" data-tooltip="${t("settings.delete")}">
              <i data-lucide="trash-2" class="lucide"></i>
            </button>` : ""}
            <label class="toggle-switch toggle-sm">
              <input type="checkbox" class="skill-toggle" data-skill="${this.esc(sk.name)}" ${isOn ? "checked" : ""} />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>`;
    }

    const skillsCardHtml = skills.length === 0
      ? `<div class="model-empty"><i data-lucide="wand-2" class="lucide"></i><span>${t("settings.noSkills")}</span></div>`
      : `
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${t("settings.skillName")}</div>
            <div class="model-table-cell model-cell-provider">${t("settings.skillDescription")}</div>
            <div class="model-table-cell model-cell-actions">${t("settings.actions")}</div>
          </div>
          ${rowsHtml}
        </div>`;

    const commandsHtml = this.renderCommandsHTML();

    this.panels.skills.innerHTML = `
      <div class="settings-section-title"><i data-lucide="wand-2" class="lucide section-title-icon"></i> ${t("settings.skillsManagement")}</div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${t("settings.skillsInstructions")}</div>
          <button class="btn-add-model-top" id="btn-install-skill">
            <i data-lucide="plus" class="lucide"></i>
            <span>${t("settings.addSkill")}</span>
          </button>
        </div>
        ${skillsCardHtml}
      </div>
      <div class="settings-section-title"><i data-lucide="terminal" class="lucide section-title-icon"></i> ${t("settings.slashCommands")}</div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${t("settings.commandsDesc")}</div>
          <button class="btn-add-model-top" id="btn-add-command">
            <i data-lucide="plus" class="lucide"></i>
            <span>${t("settings.addCommand")}</span>
          </button>
        </div>
        ${commandsHtml}
      </div>
      <div id="skills-rules-host"></div>`;

    // The global-rules table renders inline, in the same section-heading +
    // framed-card shape as the two sections above — no sub-page hop.
    this._rulesEditorHost = this.panels.skills.querySelector("#skills-rules-host") as HTMLElement | null;
    this.renderRules();

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.skills });
    }
  }

  private renderCommandsHTML(): string {
    const st = getState();
    const cmds = st.customCommands;

    if (cmds.length === 0) {
      return `<div class="model-empty"><i data-lucide="terminal" class="lucide"></i><span>${t("settings.noCommands")}</span></div>`;
    }

    let rowsHtml = "";
    for (const cmd of cmds) {
      rowsHtml += `
        <div class="model-table-row" data-command="${this.esc(cmd.name)}">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">/${this.esc(cmd.name)}</span>
          </div>
          <div class="model-table-cell model-cell-provider">
            <span class="skill-desc-text">${this.esc(cmd.title)}</span>
          </div>
          <div class="model-table-cell model-cell-actions">
            <button class="btn-icon" data-action="edit-command" data-name="${this.esc(cmd.name)}" data-tooltip="${t("settings.edit")}">
              <i data-lucide="pencil" class="lucide"></i>
            </button>
            <button class="btn-icon btn-icon--danger" data-action="delete-command" data-name="${this.esc(cmd.name)}" data-tooltip="${t("settings.delete")}">
              <i data-lucide="trash-2" class="lucide"></i>
            </button>
          </div>
        </div>`;
    }

    return `
      <div class="model-table">
        <div class="model-table-header">
          <div class="model-table-cell model-cell-name">${t("settings.commandName")}</div>
          <div class="model-table-cell model-cell-provider">${t("settings.commandTitle")}</div>
          <div class="model-table-cell model-cell-actions">${t("settings.actions")}</div>
        </div>
        ${rowsHtml}
      </div>`;
  }

  private showCommandCreate(editName?: string): void {
    this._renderCommandCreateDialog(editName);
  }

  private _renderCommandCreateDialog(editName?: string): void {
    const st = getState();
    const existing = editName ? st.customCommands.find(c => c.name === editName) : null;
    const isEdit = !!existing;

    const bodyHtml = `
      <div class="model-form-row">
        <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.commandName")}</label>
        <input type="text" id="cmd-name" class="model-form-input" value="${this.esc(existing?.name || "")}" placeholder="${t("settings.commandNamePlaceholder")}" ${isEdit ? "readonly" : ""} />
      </div>
      <div class="model-form-row">
        <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.commandTitle")}</label>
        <input type="text" id="cmd-title" class="model-form-input" value="${this.esc(existing?.title || "")}" placeholder="${t("settings.commandTitlePlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.commandDescription")}</label>
        <input type="text" id="cmd-description" class="model-form-input" value="${this.esc(existing?.description || "")}" placeholder="${t("settings.commandDescriptionPlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.commandPrompt")}</label>
        <div class="input-wrapper input-wrapper--dialog">
          <textarea id="cmd-prompt" class="setting-rich-text" placeholder="${t("settings.commandPromptPlaceholder")}" style="min-height:80px">${this.esc(existing?.prompt || "")}</textarea>
        </div>
      </div>`;

    const title = isEdit ? `Command : ${this.esc(existing?.name || "")}` : "Command";
    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);

    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = isEdit ? t("settings.saveChanges") : t("settings.createCommand");
    okBtn.addEventListener("click", () => {
      const name = (document.getElementById("cmd-name") as HTMLInputElement)?.value?.trim();
      if (!name) {
        Dialog.alert(t("settings.error"), t("settings.commandNamePlaceholder"));
        return;
      }
      if (!/^[a-zA-Z0-9][a-zA-Z0-9-]*$/.test(name)) {
        Dialog.alert(t("settings.error"), t("settings.invalidCommandName"));
        return;
      }

      const newCmd: CustomCommand = {
        name,
        title: (document.getElementById("cmd-title") as HTMLInputElement)?.value?.trim() || name,
        description: (document.getElementById("cmd-description") as HTMLInputElement)?.value?.trim() || "",
        icon: "wand-2",
        prompt: (document.getElementById("cmd-prompt") as HTMLTextAreaElement)?.value?.trim() || undefined,
      };

      if (isEdit) {
        const updated = st.customCommands.map(c => c.name === editName ? newCmd : c);
        this.saveCustomCommands(updated);
      } else {
        if (st.customCommands.some(c => c.name === name)) {
          Dialog.alert(t("settings.error"), t("settings.invalidCommandName"));
          return;
        }
        const updated = [...st.customCommands, newCmd];
        this.saveCustomCommands(updated);
      }
      close();
      this.renderSkills();
    });
  }

  private saveCustomCommands(commands: CustomCommand[]): void {
    setCustomCommands(commands);
    send({
      type: "configure",
      config: { custom_slash_commands: commands },
    } as any);
    applyServerCommands(commands);
    window.dispatchEvent(new CustomEvent("slash-commands-updated"));
  }

  private removeCustomCommand(name: string): void {
    const st = getState();
    const updated = st.customCommands.filter(c => c.name !== name);
    this.saveCustomCommands(updated);
    this.renderSkills();
  }

  private _renderSkillDetailDialog(skillName: string, isEdit = false): void {
    const st = getState();
    const sk = st.skillsList.find((s) => s.name === skillName);
    if (!sk) return;
    const isUser = sk.source === "user";
    const canEdit = isUser && isEdit;
    const content = sk.body || "";

    let frontmatter = `---\nname: ${sk.name}\ndescription: ${sk.description}`;
    if (sk.aliases && sk.aliases.length > 0) frontmatter += `\naliases: ${sk.aliases.join(", ")}`;
    if (sk.license) frontmatter += `\nlicense: ${sk.license}`;
    if (sk.compatibility) frontmatter += `\ncompatibility: ${sk.compatibility}`;
    if (sk.argument_hint) frontmatter += `\nargument_hint: ${sk.argument_hint}`;
    if (sk.allowed_tools && sk.allowed_tools.length > 0) frontmatter += `\nallowed_tools: ${sk.allowed_tools.join(", ")}`;
    if (sk.when_to_use) frontmatter += `\nwhen_to_use: ${sk.when_to_use}`;
    if (sk.context) frontmatter += `\ncontext: ${sk.context}`;
    if (sk.model) frontmatter += `\nmodel: ${sk.model}`;
    frontmatter += `\nuser_invocable: ${sk.user_invocable ? "true" : "false"}`;
    frontmatter += `\ndisable_model_invocation: ${sk.disable_model_invocation ? "true" : "false"}`;
    frontmatter += `\nsource: ${sk.source}`;
    if (sk.metadata && Object.keys(sk.metadata).length > 0) {
      frontmatter += `\nmetadata:`;
      for (const [k, v] of Object.entries(sk.metadata)) {
        frontmatter += `\n  ${k}: ${v}`;
      }
    }
    frontmatter += `\n---`;

    const fullContent = `${frontmatter}\n\n${content}`;

    let extraFields = "";
    if (sk.license) extraFields += `<div class="model-form-row"><label class="model-form-label">${t("settings.license")}</label><input type="text" class="model-form-input" value="${this.esc(sk.license)}" readonly /></div>`;
    if (sk.compatibility) extraFields += `<div class="model-form-row"><label class="model-form-label">${t("settings.compatibility")}</label><input type="text" class="model-form-input" value="${this.esc(sk.compatibility)}" readonly /></div>`;
    if (sk.argument_hint) extraFields += `<div class="model-form-row"><label class="model-form-label">${t("settings.argumentHint")}</label><input type="text" class="model-form-input" value="${this.esc(sk.argument_hint)}" readonly /></div>`;
    if (sk.when_to_use) extraFields += `<div class="model-form-row"><label class="model-form-label">${t("settings.whenToUse")}</label><input type="text" class="model-form-input" value="${this.esc(sk.when_to_use)}" readonly /></div>`;

    const bodyHtml = `
      ${canEdit ? `<div class="model-form-row">
        <label class="model-form-label">${t("settings.skillName")}</label>
        <input type="text" class="model-form-input" value="${this.esc(sk.name)}" readonly />
      </div>` : ""}
      ${extraFields}
      ${canEdit
        ? `<div class="model-form-row">
            <label class="model-form-label">${t("settings.skillContent")}</label>
            <div class="input-wrapper input-wrapper--dialog"><textarea id="skill-content-editor" class="setting-rich-text" style="min-height:300px">${this.esc(fullContent)}</textarea></div>
          </div>`
       : `<div class="msg-text skill-preview-content">${renderMarkdown(content)}</div>`}`;

    const title = `Skill : ${sk.name}`;
    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);

    if (canEdit) {
      const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
      okBtn.textContent = t("settings.saveChanges");
      okBtn.addEventListener("click", () => {
        const newContent = (overlay.querySelector("#skill-content-editor") as HTMLTextAreaElement)?.value || "";
        send({ type: "update_skill", name: sk.name, content: newContent });
        close();
      });
    } else {
      // Read-only preview, same chrome as the memory detail dialog: the
      // markdown fills the whole dialog body (flex column, internal scroll,
      // no "skill content" label), the cancel button disappears and the OK
      // button becomes Close.
      const dialog = overlay.querySelector(".toast-dialog") as HTMLElement | null;
      if (dialog) dialog.classList.add("skill-preview-dialog");
      const cancelBtn = overlay.querySelector("#dialog-form-cancel") as HTMLElement | null;
      if (cancelBtn) cancelBtn.style.display = "none";
      const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
      okBtn.textContent = t("header.close");
      okBtn.addEventListener("click", () => close());
    }

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }
  }

  private async installSkill(): Promise<void> {
    if (!window.electronAPI) return;
    try {
      const paths = await window.electronAPI.pickFiles();
      if (paths.length === 0) return;
      for (const fp of paths) {
        const name = fp.split(/[/\\]/).pop() ?? fp;
        const isZip = name.toLowerCase().endsWith(".zip");
        const isMd = name.toLowerCase().endsWith(".md");
        if (!isMd && !isZip) {
          showToast(t("settings.skillFileError"), "", "error", "Settings");
          continue;
        }
        if (isZip) {
          // For zip files, send the file path so the backend reads the binary zip
          const skillName = name.replace(/\.zip$/i, "");
          send({ type: "install_skill", name: skillName, content: "", file_path: fp });
        } else {
          const fileResult = await window.electronAPI.readFile(fp);
          const skillName = name.replace(/\.md$/i, "");
          send({ type: "install_skill", name: skillName, content: fileResult.content, file_path: "" });
        }
      }
    } catch (e: any) {
      showToast(t("settings.failedInstallSkill"), e.message || String(e), "error", "Settings");
    }
  }

  private renderMcpList(): void {
    if (this._panelEditorActive("mcp")) return;
    const st = getState();
    const servers: MCPServerConfig[] = st.mcpServers || [];

    let rowsHtml = "";
    for (let i = 0; i < servers.length; i++) {
      const srv = servers[i];
      const isHttp = srv.type === "http";
      const transportTag = isHttp ? t("settings.transportTagHttp") : t("settings.transportTagStdio");
      const isDisabled = srv.disabled === true;
      const sourceLabel = srv.source === "model" ? "model" : "user";
      rowsHtml += `
        <div class="model-table-row" data-mcp-idx="${i}">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">${this.esc(srv.name)}</span>
          </div>
          <div class="model-table-cell model-cell-provider">
            <span class="mcp-transport-tag">${transportTag}</span>
          </div>
          <div class="model-table-cell model-cell-actions">
            <button class="btn-icon" data-action="edit-mcp" data-idx="${i}" data-tooltip="${t("settings.edit")}">
              <i data-lucide="pencil" class="lucide"></i>
            </button>
            <button class="btn-icon btn-icon--danger" data-action="delete-mcp" data-idx="${i}" data-tooltip="${t("settings.removeServer")}">
              <i data-lucide="trash-2" class="lucide"></i>
            </button>
            <label class="toggle-switch toggle-sm">
              <input type="checkbox" class="mcp-enable-toggle" data-idx="${i}" ${isDisabled ? "" : "checked"} />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>`;
    }

    const tableHtml = servers.length === 0
      ? `<div class="model-empty"><i data-lucide="server" class="lucide"></i><span>${t("settings.noMcpServers")}</span></div>`
      : `
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${t("settings.serverName")}</div>
            <div class="model-table-cell model-cell-provider">${t("settings.type")}</div>
            <div class="model-table-cell model-cell-actions">${t("settings.actions")}</div>
          </div>
          ${rowsHtml}
        </div>`;

    this.panels.mcp.innerHTML = `
      <div class="settings-section-title"><i data-lucide="server" class="lucide section-title-icon"></i> ${t("settings.mcpServers")}</div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${t("settings.mcpInstructions")}</div>
          <button class="btn-add-model-top" id="btn-goto-create-mcp">
            <i data-lucide="plus" class="lucide"></i>
            <span>${t("settings.addMcpServer")}</span>
          </button>
        </div>
        ${tableHtml}
      </div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.mcp });
    }
  }

  // ── MCP JSON import helpers ───────────────────────────────────────────

  /** Serialize a single ``MCPServerConfig`` back to standard JSON string. */
  private _serverConfigToJson(srv: MCPServerConfig): string {
    const obj: Record<string, any> = {};
    if (srv.type === "http") {
      obj.url = srv.url || "";
      obj.timeout = srv.timeout ?? 60;
      if (srv.headers && Object.keys(srv.headers).length > 0) obj.headers = srv.headers;
    } else {
      obj.command = srv.command || "";
      if (srv.args && srv.args.length > 0) obj.args = srv.args;
      if (srv.cwd) obj.cwd = srv.cwd;
    }
    obj.type = srv.type;
    if (srv.env && Object.keys(srv.env).length > 0) obj.env = srv.env;
    if (srv.disabled) obj.disabled = true;
    const wrapper: Record<string, any> = {};
    wrapper[srv.name] = obj;
    return JSON.stringify(wrapper, null, 2);
  }

  /** Try to detect whether *obj* is a map of MCP server configs (as opposed to some arbitrary object). */
  private _isMcpServerMap(obj: Record<string, unknown>): boolean {
    return Object.values(obj).some((v) =>
      v && typeof v === "object" && !Array.isArray(v) &&
      (typeof (v as any).command === "string" || typeof (v as any).url === "string")
    );
  }

  /** Normalize a single raw config entry into an ``MCPServerConfig``. */
  private _normalizeMcpServerConfig(name: string, cfg: any): MCPServerConfig {
    const type: "stdio" | "http" = (cfg.type === "http" || cfg.transport === "http") ? "http" : "stdio";
    const result: MCPServerConfig = { name, type };
    if (type === "http") {
      result.url = cfg.url || "";
      result.timeout = cfg.timeout ?? cfg.http_timeout ?? 60;
      result.headers = cfg.headers || {};
    } else {
      result.command = cfg.command || "";
      result.args = Array.isArray(cfg.args) ? cfg.args : [];
      result.cwd = cfg.cwd || "";
    }
    if (cfg.env && typeof cfg.env === "object" && !Array.isArray(cfg.env)) {
      result.env = cfg.env;
    }
    if (cfg.disabled) result.disabled = true;
    return result;
  }

  /** Parse standard MCP JSON into ``MCPServerConfig[]``. */
  private _parseMcpJsonToServers(parsed: any): MCPServerConfig[] {
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];

    let map: Record<string, unknown> | null = null;

    // Format 1: { mcpServers: { name: { ... } } } — the official Claude Code format
    if (parsed.mcpServers && typeof parsed.mcpServers === "object" && !Array.isArray(parsed.mcpServers)) {
      map = parsed.mcpServers;
    }
    // Format 2: { name: { command: "...", ... } } — a single server config object
    else if (parsed.command || parsed.url) {
      return [this._normalizeMcpServerConfig("mcp-server", parsed)];
    }
    // Format 3: { name: { ... }, name2: { ... } } — bare server map
    else if (this._isMcpServerMap(parsed)) {
      map = parsed;
    }

    if (!map) return [];
    return Object.entries(map).map(([name, cfg]) =>
      this._normalizeMcpServerConfig(name, cfg)
    );
  }

  // ── MCP JSON Import Dialog ────────────────────────────────────────────

  private _renderMcpImportDialog(editIdx?: number): void {
    const isEdit = editIdx !== undefined;
    const servers = getState().mcpServers || [];
    const existing = isEdit ? servers[editIdx!] : undefined;
    const title = isEdit ? `MCP Server : ${this.esc(existing?.name || "")}` : "MCP Server";

    const initialName = existing?.name || "";
    const initialType: "stdio" | "http" = existing?.type === "http" ? "http" : "stdio";
    const initialCommand = existing?.command || "";
    const initialArgs = (existing?.args || []).join(" ");
    const initialUrl = existing?.url || "";
    const initialEnv = existing?.env || {};

    const envRowHtml = (k: string, v: string): string => `
      <div class="mcp-env-row">
        <input type="text" class="model-form-input mcp-env-key" placeholder="${t("settings.mcpEnvKey")}" value="${this.esc(k)}" />
        <input type="text" class="model-form-input mcp-env-value" placeholder="${t("settings.mcpEnvValue")}" value="${this.esc(v)}" />
        <button type="button" class="btn-icon mcp-env-remove" data-tooltip="${t("settings.remove")}">
          <i data-lucide="trash-2" class="lucide"></i>
        </button>
      </div>`;
    const envRowsHtml = (obj: Record<string, string>): string =>
      Object.entries(obj).map(([k, v]) => envRowHtml(k, v)).join("");

    const transportOptions: DropdownOption[] = [
      { id: "stdio", label: t("settings.transportTagStdio") },
      { id: "http", label: t("settings.transportTagHttp") },
    ];

    const bodyHtml = `
      <div class="model-form-row">
        <label class="model-form-label" for="mcp-name"><span class="model-form-required">*</span>${t("settings.serverName")}</label>
        <input type="text" id="mcp-name" class="model-form-input" value="${this.esc(initialName)}" placeholder="${t("settings.mcpNamePlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label" for="mcp-transport">${t("settings.mcpTransport")}</label>
        <div class="model-form-dropdown-row">
          ${this.renderDropdown("mcp-transport", transportOptions, initialType, () => {})}
        </div>
      </div>
      <div id="mcp-stdio-fields" class="mcp-transport-fields" ${initialType === "http" ? 'style="display:none"' : ""}>
        <div class="model-form-row">
          <label class="model-form-label" for="mcp-command"><span class="model-form-required">*</span>${t("settings.mcpCommand")}</label>
          <input type="text" id="mcp-command" class="model-form-input" value="${this.esc(initialCommand)}" placeholder="${t("settings.mcpCommandPlaceholder")}" />
        </div>
        <div class="model-form-row">
          <label class="model-form-label" for="mcp-args">${t("settings.mcpArgs")}</label>
          <input type="text" id="mcp-args" class="model-form-input" value="${this.esc(initialArgs)}" placeholder="${t("settings.mcpArgsPlaceholder")}" />
        </div>
      </div>
      <div id="mcp-http-fields" class="mcp-transport-fields" ${initialType === "stdio" ? 'style="display:none"' : ""}>
        <div class="model-form-row">
          <label class="model-form-label" for="mcp-url"><span class="model-form-required">*</span>${t("settings.mcpUrl")}</label>
          <input type="text" id="mcp-url" class="model-form-input" value="${this.esc(initialUrl)}" placeholder="${t("settings.mcpUrlPlaceholder")}" />
        </div>
      </div>
      <div class="model-form-row" style="flex-direction:column;align-items:stretch">
        <div class="mcp-env-label-row">
          <label class="model-form-label">${t("settings.mcpEnvVars")}</label>
          <button type="button" class="btn-icon" id="mcp-add-env" data-tooltip="${t("settings.mcpAddEnv")}">
            <i data-lucide="plus" class="lucide"></i>
          </button>
        </div>
        <div id="mcp-env-list">${envRowsHtml(initialEnv)}</div>
      </div>`;

    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = isEdit ? t("common.save") : t("settings.addMcpServer");

    let currentType: "stdio" | "http" = initialType;
    this.bindDropdown("mcp-transport", (val) => {
      currentType = (val as "stdio" | "http") || initialType;
      const stdioEl = overlay.querySelector("#mcp-stdio-fields") as HTMLElement | null;
      const httpEl = overlay.querySelector("#mcp-http-fields") as HTMLElement | null;
      if (stdioEl) stdioEl.style.display = currentType === "http" ? "none" : "";
      if (httpEl) httpEl.style.display = currentType === "http" ? "" : "none";
    });

    const envList = overlay.querySelector("#mcp-env-list") as HTMLElement | null;
    overlay.querySelector("#mcp-add-env")?.addEventListener("click", () => {
      if (!envList) return;
      const row = document.createElement("div");
      row.className = "mcp-env-row";
      row.innerHTML = `
        <input type="text" class="model-form-input mcp-env-key" placeholder="${t("settings.mcpEnvKey")}" />
        <input type="text" class="model-form-input mcp-env-value" placeholder="${t("settings.mcpEnvValue")}" />
        <button type="button" class="btn-icon mcp-env-remove" data-tooltip="${t("settings.remove")}">
          <i data-lucide="trash-2" class="lucide"></i>
        </button>`;
      envList.appendChild(row);
      row.querySelector<HTMLInputElement>(".mcp-env-key")?.focus();
      // Newly inserted nodes need their lucide icons created, otherwise the
      // remove button renders as an empty shell.
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: row });
      }
    });
    envList?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest(".mcp-env-remove");
      if (btn) (btn.closest(".mcp-env-row") as HTMLElement | null)?.remove();
    });

    okBtn.addEventListener("click", () => {
      const name = (overlay.querySelector("#mcp-name") as HTMLInputElement)?.value?.trim();
      if (!name) {
        showToast(t("settings.mcpNameRequired"), "", "error", "Settings");
        return;
      }

      const newServer: MCPServerConfig = { name, type: currentType };
      if (currentType === "http") {
        const url = (overlay.querySelector("#mcp-url") as HTMLInputElement)?.value?.trim() || "";
        if (!url) {
          showToast(t("settings.mcpUrlRequired"), "", "error", "Settings");
          return;
        }
        newServer.url = url;
        newServer.timeout = 60;
        if (existing?.headers) newServer.headers = existing.headers;
      } else {
        const command = (overlay.querySelector("#mcp-command") as HTMLInputElement)?.value?.trim() || "";
        if (!command) {
          showToast(t("settings.mcpCommandRequired"), "", "error", "Settings");
          return;
        }
        newServer.command = command;
        const argsRaw = (overlay.querySelector("#mcp-args") as HTMLInputElement)?.value?.trim() || "";
        newServer.args = argsRaw ? argsRaw.split(/\s+/) : [];
        if (existing?.cwd) newServer.cwd = existing.cwd;
      }

      // Same rule as the provider headers: a row without a key is dropped
      // (an unnamed variable cannot be addressed), an empty VALUE is kept
      // because a variable that exists but is unset is meaningful.
      const env: Record<string, string> = {};
      envList?.querySelectorAll(".mcp-env-row").forEach((row) => {
        const k = (row.querySelector(".mcp-env-key") as HTMLInputElement)?.value?.trim();
        const v = (row.querySelector(".mcp-env-value") as HTMLInputElement)?.value || "";
        if (k) env[k] = v;
      });
      if (Object.keys(env).length > 0) newServer.env = env;

      const current = [...(getState().mcpServers || [])];
      if (isEdit && editIdx! < current.length) {
        if (current[editIdx!].source) newServer.source = current[editIdx!].source;
        if (current[editIdx!].disabled) newServer.disabled = true;
        current[editIdx!] = newServer;
      } else {
        newServer.source = "user";
        current.push(newServer);
      }
      send({ type: "update_mcp", mcp_servers: current });
      close();
    });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }
  }

  /** Host element of the open 规则 editor card (see :meth:`renderRules`). */
  private _rulesEditorHost: HTMLElement | null = null;

  /** The 个性化 panel's 规则 row opens the global rules list as an inline
   *  editor card (same shape as the Assist panel's rows).  Re-runnable on its
   *  own: the rules subscription calls it without touching the card shell. */
  private renderRules(): void {
    const host = this._rulesEditorHost && this._rulesEditorHost.isConnected
      ? this._rulesEditorHost
      : null;
    if (!host) return;

    const st = getState();
    const rules = st.globalRules;

    let rowsHtml = "";
    for (const r of rules) {
      rowsHtml += `
        <div class="model-table-row" data-rule="${this._escapeHtml(r.name)}">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">${this._escapeHtml(r.name)}.md</span>
          </div>
          <div class="model-table-cell model-cell-provider">
            ${new Date(r.modified * 1000).toLocaleString()}
          </div>
          <div class="model-table-cell model-cell-actions">
            <button class="btn-icon" data-action="edit-rule" data-name="${this._escapeHtml(r.name)}" data-tooltip="${t("settings.edit")}">
              <i data-lucide="pencil" class="lucide"></i>
            </button>
            <button class="btn-icon btn-icon--danger" data-action="delete-rule" data-name="${this._escapeHtml(r.name)}" data-tooltip="${t("settings.delete")}">
              <i data-lucide="trash-2" class="lucide"></i>
            </button>
          </div>
        </div>`;
    }

    const tableHtml = rules.length === 0
      ? `<div class="model-empty"><i data-lucide="list-checks" class="lucide"></i><span>${t("settings.noGlobalRules")}</span></div>`
      : `
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${t("settings.ruleSource")}</div>
            <div class="model-table-cell model-cell-provider">${t("settings.memoryTime")}</div>
            <div class="model-table-cell model-cell-actions">${t("settings.actions")}</div>
          </div>
          ${rowsHtml}
        </div>`;

    host.innerHTML = `
      <div class="settings-section-title"><i data-lucide="list-checks" class="lucide section-title-icon"></i> ${t("settings.globalRules")}</div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${t("settings.globalRulesDesc")}</div>
          <button class="btn-add-model-top" id="btn-add-global-rule">
            <i data-lucide="plus" class="lucide"></i>
            <span>${t("settings.addGlobalRule")}</span>
          </button>
        </div>
        ${tableHtml}
      </div>`;

    // Bind edit
    host.querySelectorAll("[data-action='edit-rule']").forEach((btn) => {
      btn.addEventListener("click", () => {
        const name = (btn as HTMLElement).getAttribute("data-name") || "";
        send({ type: "get_global_rule_content", name });
      });
    });

    // Bind delete
    host.querySelectorAll("[data-action='delete-rule']").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = (btn as HTMLElement).getAttribute("data-name") || "";
        if (await Dialog.confirm(t("common.confirmDeleteTitle"), t("common.confirmDelete", { name: `${name}.md` }))) {
          send({ type: "delete_global_rule", name });
        }
      });
    });

    host.querySelector("#btn-add-global-rule")?.addEventListener("click", () => {
      this._showRuleFormDialog("", "", false);
    });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: host });
    }
    // The unified push already delivers global_rules_list on connect and
    // after every rule save/delete — nothing to fetch here.
  }

  /** Open the global rules list as an inline editor card — the same shell the
   *  Assist panel and the 记忆 / 快捷键 rows use: footer dropped, the top-bar
   *  back arrow is the only exit, and the shell's dirty check stays quiet
   *  because every action in here (add / edit / delete) saves through the
   *  backend immediately. */
  private _openRulesEditor(): void {
    const { overlay } = this._showFormDialog(t("settings.globalRules"), `<div id="rules-editor-host"></div>`, true);

    const footerEl = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    if (footerEl) footerEl.style.display = "none";
    const panelBack = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
    if (panelBack) panelBack.style.display = "";

    this._rulesEditorHost = overlay.querySelector("#rules-editor-host") as HTMLElement | null;
    this.renderRules();
    this._keepPanelEditorClean(overlay);
  }

  private _showRuleFormDialog(name: string, content: string, isEdit: boolean): void {
    const title = isEdit ? `Rules : ${this.esc(name)}` : "Rules";
    const bodyHtml = `
      <div class="model-form-row">
        <label class="model-form-label" for="rule-form-name">
          <span class="model-form-required">*</span>${t("settings.ruleSource")}
        </label>
        <input type="text" id="rule-form-name" class="model-form-input" placeholder="rule-name" value="${this._escapeHtml(name)}" ${isEdit ? "readonly" : ""} />
      </div>
      <div class="model-form-row">
        <label class="model-form-label" for="rule-form-content">${t("settings.ruleContent")}</label>
        <div class="input-wrapper input-wrapper--dialog">
          <textarea id="rule-form-content" class="setting-rich-text" placeholder="${t("settings.ruleContent")}..." style="min-height:300px">${this._escapeHtml(content)}</textarea>
        </div>
      </div>`;

    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = isEdit ? t("settings.saveChanges") : t("settings.addGlobalRule");

    okBtn.addEventListener("click", () => {
      const inputName = (overlay.querySelector("#rule-form-name") as HTMLInputElement).value.trim();
      const inputContent = (overlay.querySelector("#rule-form-content") as HTMLTextAreaElement).value.trim();
      if (!inputName) return;
      send({ type: "save_global_rule", name: inputName, content: inputContent });
      close();
    });
  }

  private renderMcp(): void {
    this.renderMcpList();
  }

  private renderAgent(): void {
    if (this._panelEditorActive("agent")) return;
    const agents = getState().subAgents || [];

    const tableContent = agents.length === 0
      ? `<div class="model-empty">${SUB_AGENT_GLYPH}<span>${t("settings.noSubAgentsYet")}</span></div>`
      : `
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${t("settings.subAgent")}</div>
            <div class="model-table-cell model-cell-provider">${t("settings.description")}</div>
            <div class="model-table-cell model-cell-actions">${t("settings.actions")}</div>
          </div>
          ${agents.map((a, i) => {
              const sourceLabel = a.source === "model" ? "model" : "user";
              return `
            <div class="model-table-row">
              <div class="model-table-cell model-cell-name">
                <span class="model-name-text">${this.esc(a.name)}</span>
              </div>
              <div class="model-table-cell model-cell-provider">
                <span class="skill-desc-text">${this.esc(a.description || "-")}</span>
              </div>
              <div class="model-table-cell model-cell-actions">
                <button class="btn-icon" data-action="edit" data-index="${i}" data-tooltip="${t("settings.edit")}">
                  <i data-lucide="pencil" class="lucide icon-sm"></i>
                </button>
                <button class="btn-icon btn-icon--danger" data-action="delete" data-index="${i}" data-tooltip="${t("settings.delete")}">
                  <i data-lucide="trash-2" class="lucide icon-sm"></i>
                </button>
              </div>
            </div>`}).join("")}
        </div>`;

    this.panels.agent.innerHTML = `
      <div class="settings-section-title">${SUB_AGENT_GLYPH} ${t("settings.agentManagement")}</div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${t("settings.agentInstructions")}</div>
          <button class="btn-add-model-top" id="btn-create-agent">
            <i data-lucide="plus" class="lucide"></i>
            <span>${t("settings.addSubAgent")}</span>
          </button>
        </div>
        ${tableContent}
      </div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.agent });
    }
  }

  private _renderAgentCreateDialog(existing?: import("../core/types.js").SubAgentConfig): void {
    const isEdit = !!existing;

    // Execution model + permission mode: the SAME dropdown pair the market's
    // "add plugin" agent form shows — enabled models only (ids are flat
    // modelConfigs indexes), the permission picker mirrors the four chat modes.
    const allModels = getState().modelConfigs || [];
    const enabledModels = allModels.filter((m) => m.enabled !== false);
    const modelOptions: DropdownOption[] = enabledModels.map((m) => ({
      id: String(allModels.indexOf(m)),
      label: m.name,
    }));
    const initModelIndex = (() => {
      const idx = existing?.model_index ?? getState().activeModelIndex;
      if (allModels[idx] && allModels[idx].enabled !== false) return idx;
      const first = allModels.findIndex((m) => m.enabled !== false);
      return first >= 0 ? first : idx;
    })();

    const bodyHtml = `
      <div class="model-form-row">
        <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.subAgentName")}</label>
        <input type="text" id="agent-name" class="model-form-input" value="${this.esc(existing?.name || "")}" placeholder="${t("settings.subAgentNamePlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.description")}</label>
        <input type="text" id="agent-description" class="model-form-input" value="${this.esc(existing?.description || "")}" placeholder="${t("settings.subAgentDescPlaceholder")}" />
      </div>
      <div class="model-form-row auto-model-perm-row">
        <div class="auto-model-perm-col">
          <label class="model-form-label">${t("automation.modelLabel")}</label>
          <div class="model-form-dropdown-row">
            ${renderDropdownImpl("agent-model-dd", modelOptions, String(initModelIndex), () => {})}
          </div>
        </div>
        <div class="auto-model-perm-col">
          <label class="model-form-label">${t("input.permissionMode")}</label>
          <div class="model-form-dropdown-row">
            <div class="settings-dropdown-wrap" id="agent-perm-wrap">
              <button class="settings-dropdown-trigger" id="agent-perm-trigger" type="button">
                <span class="auto-perm-mode-icon" id="agent-perm-mode-icon"></span>
                <span class="auto-perm-mode-label"></span>
                <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
              </button>
              <div class="settings-dropdown" id="agent-perm-dropdown"></div>
            </div>
          </div>
        </div>
      </div>
      <div class="model-form-row">
        <label class="model-form-label"><span class="model-form-required">*</span>${t("settings.systemPrompt")}</label>
        <textarea id="agent-system-prompt" class="setting-rich-text" placeholder="${t("settings.subAgentSystemPromptPlaceholder")}" style="min-height:80px">${this.esc(existing?.system_prompt || "")}</textarea>
      </div>`;

    const title = isEdit ? t("agents.subAgentEdit", { name: this.esc(existing?.name || "") }) : t("agents.subAgent");
    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);

    let selectedModelIndex = initModelIndex;
    bindDropdownImpl("agent-model-dd", (val) => {
      const idx = parseInt(val);
      if (!Number.isNaN(idx)) selectedModelIndex = idx;
    });

    // Permission mode: the automation dialog's selector, verbatim — shared
    // catalogue + icons (core/permission_mode.ts), group-normalised value,
    // guarded "bypass" switch with the danger confirmation.
    const permTrigger = overlay.querySelector("#agent-perm-trigger") as HTMLElement;
    const permDropdown = overlay.querySelector("#agent-perm-dropdown") as HTMLElement;
    let selectedPermission = permissionModeGroup(existing?.permission_mode || "default");
    const permLabelEl = permTrigger.querySelector(".auto-perm-mode-label") as HTMLElement;
    const permIconEl = permTrigger.querySelector(".auto-perm-mode-icon") as HTMLElement;
    const paintPermTrigger = () => {
      permLabelEl.textContent = permissionModeLabel(selectedPermission);
      permIconEl.innerHTML = `<i data-lucide="${permissionModeIcon(selectedPermission)}" class="lucide"></i>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: permIconEl });
      }
    };
    paintPermTrigger();
    const buildPermOptions = () => {
      permDropdown.innerHTML = PERMISSION_MODES.map((m) =>
        `<div class="settings-dropdown-item${m.id === selectedPermission ? " selected" : ""}" data-perm-mode="${m.id}"><i data-lucide="${m.icon}" class="lucide"></i><span>${this.esc(t(m.labelKey))}</span></div>`
      ).join("");
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: permDropdown });
      }
      permDropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          void (async () => {
            // Shared guarded path: "bypass" requires the danger confirmation,
            // rejection keeps the previous selection.
            const applied = await guardPermissionMode(
              (item as HTMLElement).getAttribute("data-perm-mode") || "default",
              dialogPermissionConfirm,
            );
            permDropdown.classList.remove("open");
            if (applied === null) return;
            selectedPermission = applied;
            paintPermTrigger();
          })();
        });
      });
    };
    permTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = permDropdown.classList.contains("open");
      if (isOpen) { permDropdown.classList.remove("open"); return; }
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      buildPermOptions();
      permDropdown.classList.add("open");
    });

    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = isEdit ? t("settings.saveChanges") : t("settings.addSubAgent");
    okBtn.addEventListener("click", () => {
      const name = (document.getElementById("agent-name") as HTMLInputElement)?.value?.trim();
      if (!name) {
        Dialog.alert(t("settings.subAgentNameRequired"), "");
        return;
      }

      const agents = getState().subAgents || [];
      const description = (document.getElementById("agent-description") as HTMLInputElement)?.value?.trim() || "";
      const systemPrompt = (document.getElementById("agent-system-prompt") as HTMLTextAreaElement)?.value || "";
      if (!systemPrompt.trim()) {
        Dialog.alert(t("settings.subAgentPromptRequired"), "");
        return;
      }

      const newAgent = {
        name, description, system_prompt: systemPrompt,
        model_index: selectedModelIndex, permission_mode: selectedPermission,
      };

      if (isEdit && existing) {
        const idx = agents.findIndex(a => a.name === existing.name);
        if (idx >= 0) {
          agents[idx] = newAgent;
        }
      } else {
        agents.push(newAgent);
      }

      send({ type: "update_sub_agents", agents: agents });
      close();
    });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }
  }

  private _memoryGraphCtl: import("../components/MemoryGraph.js").MemoryGraphHandle | null = null;
  private _memoryDetailLoader: EALoader | null = null;

  /** The 记忆 panel: the relationship graph fills the panel below its own
   *  section heading.  Selecting a content-bearing node opens the markdown
   *  reader as a sub-page card. */
  private renderMemory(): void {
    this.panels.memory.innerHTML = `
      <div class="settings-section-title"><i data-lucide="brain" class="lucide section-title-icon"></i> ${t("settings.memory")}</div>
      <div id="mem-graph-view" style="position:relative;height:calc(100vh - 250px);min-height:360px">
        <div id="memory-graph-host" style="position:absolute;inset:0"></div>
      </div>`;

    const host = this.panels.memory.querySelector("#memory-graph-host") as HTMLElement | null;
    if (!host) return;
    const graph = getState().memoryGraph || { nodes: [], edges: [] };
    this._memoryGraphCtl?.destroy();
    this._memoryGraphCtl = createMemoryGraph(host, graph, {
      // Dragged node positions persist across panel switches.
      storageKey: "encre.memoryGraphLayout",
      // Entity hubs are pure index nodes (no markdown behind them); only
      // content-bearing node types open the reader.
      onSelect: (node) => {
        if (node.type === "entity") return;
        this._openMemoryDetail(`node:${node.id}`, node.title);
      },
    });
  }

  /** The memory reader as a sub-page card (记忆 panel → node → reader).  One
   *  exit only: the top-bar back arrow.  Content arrival is the id-based
   *  memoryDetail subscription in events_bind. */
  private _openMemoryDetail(path: string, nodeTitle?: string): void {
    const { overlay } = this._showFormDialog(
      nodeTitle ? `Memory : ${nodeTitle}` : "Memory",
      `<div id="memory-detail-content" class="msg-text"></div>`,
      true,
    );
    const dialog = overlay.querySelector(".toast-dialog") as HTMLElement | null;
    dialog?.classList.add("memory-detail-dialog");
    const footerEl = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    if (footerEl) footerEl.style.display = "none";
    const panelBack = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
    if (panelBack) panelBack.style.display = "";

    const contentEl = overlay.querySelector("#memory-detail-content") as HTMLElement | null;
    if (contentEl) {
      contentEl.addEventListener("click", (e) => {
        handleMarkdownContentClick(contentEl, e as MouseEvent, {
          onOpenExternal: (href) => openRoutedLink(href),
        });
      });
      this._showMemoryDetailLoading(contentEl);
    }
    send({ type: "get_memory_detail", path });
  }

  /** Open the memory panel's inline editor card: main view = relationship
   *  graph, detail sub-page = the memory's markdown reader.  Same
   *  workspace-settings style container as the provider dialog — one card,
   *  swapped views, per-panel isolation (`data-panel="memory"`: the draft
   *  survives a hop to another panel and comes back exactly as left). */
  private _openMemoryEditor(initialPath?: string, initialTitle?: string): void {
    // The graph fills the card edge to edge -- no framing settings-card box:
    // the canvas IS the memory surface, chrome only blurs that read.  The
    // host is absolutely positioned inside a flex-grow wrapper so its height
    // can never collapse with the card's height chain (a collapsed host made
    // the SVG letterbox into the top half of the card).
    const bodyHtml = `
      <div id="mem-graph-view" style="position:relative;flex:1 1 auto;min-height:0">
        <div id="memory-graph-host" style="position:absolute;inset:0"></div>
      </div>
      <div id="mem-detail-view" style="display:none;flex-direction:column;flex:1 1 auto;min-height:0">
        <div id="memory-detail-content" class="msg-text"></div>
      </div>`;
    const { overlay, close } = this._showFormDialog("Memory", bodyHtml, true);

    const dialog = overlay.querySelector(".toast-dialog") as HTMLElement | null;
    // Reuse the reader body rules (flex column, side-only padding, no footer
    // separator): the card's one scroll track belongs to the open view.
    dialog?.classList.add("memory-detail-dialog");
    // This card has exactly ONE exit — the top-bar back arrow — on BOTH views.
    // The Cancel/Confirm row is dropped entirely: nothing here is saved, and a
    // second exit button would only compete with the arrow.
    const footerEl = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    if (footerEl) footerEl.style.display = "none";

    const graphView = overlay.querySelector("#mem-graph-view") as HTMLElement;
    const detailView = overlay.querySelector("#mem-detail-view") as HTMLElement;
    const titleEl = overlay.querySelector(".panel-editor-title-text") as HTMLElement | null;
    const panelBack = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
    // The shell ships the arrow with an inline `display:none` (main editor
    // pages normally exit through the footer).  Reveal it: on the graph view
    // the shell's own wiring closes the card, and the capture listener below
    // re-routes it to "back to the graph" while the reader is open.
    if (panelBack) panelBack.style.display = "";
    let inDetail = false;

    const showGraphView = () => {
      this._destroyMemoryDetailLoader();
      detailView.style.display = "none";
      detailView.innerHTML = "";
      graphView.style.display = "";
      if (titleEl) titleEl.textContent = "Memory";
      if (panelBack) panelBack.style.display = "";
      inDetail = false;
    };

    // The reader fills the body exactly like the old detail dialog: bare
    // markdown, branded loader while the frame is in flight.  Content arrival
    // is the memoryDetail subscription in events_bind (id-based).
    const showDetailView = (path: string, nodeTitle?: string) => {
      graphView.style.display = "none";
      detailView.style.display = "flex";
      detailView.innerHTML = `<div id="memory-detail-content" class="msg-text"></div>`;
      const contentEl = detailView.querySelector("#memory-detail-content") as HTMLElement;
      contentEl.addEventListener("click", (e) => {
        handleMarkdownContentClick(contentEl, e as MouseEvent, {
          onOpenExternal: (href) => openRoutedLink(href),
        });
      });
      this._showMemoryDetailLoading(contentEl);
      if (titleEl) titleEl.textContent = nodeTitle ? `Memory : ${nodeTitle}` : "Memory";
      if (panelBack) panelBack.style.display = "";
      inDetail = true;
      send({ type: "get_memory_detail", path });
    };

    // Capture-phase routing while the reader is open: the top-bar arrow means
    // "back to the graph" and must NOT trigger the editor-level close wired
    // on the same button.  On the graph view the arrow keeps its editor-close
    // meaning.  Same two-level pattern as the provider model sub-page.
    overlay.addEventListener("click", (e) => {
      if (!inDetail) return;
      if ((e.target as HTMLElement).closest(".panel-editor-back")) {
        e.stopPropagation();
        e.preventDefault();
        showGraphView();
      }
    }, true);

    const host = overlay.querySelector("#memory-graph-host") as HTMLElement | null;
    if (host) {
      const graph = getState().memoryGraph || { nodes: [], edges: [] };
      this._memoryGraphCtl?.destroy();
      this._memoryGraphCtl = createMemoryGraph(host, graph, {
        // Dragged node positions persist across card close/reopen.
        storageKey: "encre.memoryGraphLayout",
        // Entity hubs are pure index nodes (no markdown behind them); only
        // content-bearing node types open the detail sub-page.
        onSelect: (node) => {
          if (node.type === "entity") return;
          showDetailView(`node:${node.id}`, node.title);
        },
      });
    } else {
      // Modal fallback (settings closed mid-hop): nothing to paint.
      close();
      return;
    }

    if (initialPath) showDetailView(initialPath, initialTitle);
  }

  /** Renders the unified EALoader as the memory detail loading state. */
  private _showMemoryDetailLoading(contentEl: HTMLElement): void {
    this._destroyMemoryDetailLoader();
    contentEl.innerHTML = "";
    // Reserve breathing room so the loader is vertically centred inside the
    // auto-height dialog body, not glued to the title bar.
    contentEl.style.minHeight = "220px";
    this._memoryDetailLoader = EALoader.mountCentered(contentEl, "96px");
  }

  /** Tears down the memory detail loader (observer + DOM), if any. */
  private _destroyMemoryDetailLoader(): void {
    this._memoryDetailLoader?.destroy();
    this._memoryDetailLoader = null;
    const el = document.getElementById("memory-detail-content");
    if (el) el.style.minHeight = "";
  }

  private async _renderUsageSection(): Promise<void> {
    // Reentrancy guard: _renderUsageSection awaits `import("chart.js")`, so
    // overlapping triggers (refresh callback + tab switch + autorun on
    // usageStats change) are coalesced into a single re-run.
    if (this._usageRendering) {
      this._usageRenderPending = true;
      return;
    }
    this._usageRendering = true;
    try {
      const el = this.panels.usage;
      if (!el) return;
      const stats = getState().usageStats;
      const muted = getComputedStyle(document.documentElement).getPropertyValue("--text-muted").trim() || "#666";
      const locale = getLocale();
      const localeChanged = locale !== this._lastUsageLocaleRendered;

      // Nothing visibly changed (same data push, same theme tone, same
      // grouping, same locale) and the panel already carries its rendered
      // state — skip entirely so a mere panel switch never replays the
      // loading shimmer.
      const renderedMarker = stats
        ? el.querySelector("#usage-metrics")
        : el.querySelector("#usage-empty");
      if (
        renderedMarker &&
        stats === this._lastUsageStatsRendered &&
        muted === this._lastUsageMutedRendered &&
        this._usageGroupBy === this._lastUsageGroupByRendered &&
        locale === this._lastUsageLocaleRendered
      ) {
        return;
      }

      // A locale switch rebuilds the whole section from scratch: translated
      // strings AND locale-formatted dates (metric labels, heatmap month
      // captions, chart day labels) are baked into the DOM/SVG at shell
      // build time, so an in-place text patch would miss the heatmap and
      // the chart.  Wipe the panel once — the empty-state guard and
      // needsShell below then rebuild everything in the new language.
      if (localeChanged && (el.querySelector("#usage-empty") || el.querySelector("#usage-metrics"))) {
        el.innerHTML = "";
      }

      if (!stats || stats.total_sessions === 0) {
        // No data — render the empty state (only when not already shown, so a
        // refresh that returns no data doesn't flicker the whole panel).
        if (!el.querySelector("#usage-empty")) {
          el.innerHTML = `
            <div class="settings-section-title"><i data-lucide="chart-column" class="lucide section-title-icon"></i> ${t("settings.usageStats")}</div>
            <div class="usage-empty" id="usage-empty">
              <div class="usage-empty-icon"><i data-lucide="chart-column" class="lucide"></i></div>
              <div class="usage-empty-text">${t("settings.noUsageData")}</div>
              <button class="btn-secondary" id="btn-refresh-usage-empty">${t("settings.refresh")}</button>
            </div>`;
          document.getElementById("btn-refresh-usage-empty")?.addEventListener("click", () => {
            refreshAllData();
          });
          if (typeof (window as any).lucide !== "undefined") {
            (window as any).lucide.createIcons({ root: el });
          }
        }
        this._lastUsageStatsRendered = stats;
        this._lastUsageMutedRendered = muted;
        this._lastUsageGroupByRendered = this._usageGroupBy;
        this._lastUsageLocaleRendered = locale;
        return;
      }

      const groupBy = this._usageGroupBy;
      const model = this._computeUsageChartModel(stats.sessions || [], groupBy);

      // Build the static shell (title + icon + metrics container + activity
      // heatmap + chart box) ONLY when it isn't already present.  A refresh /
      // autorun / re-entry that finds an existing shell skips this and only
      // updates the concrete data below — so the section title, its icon and
      // the activity heatmap never re-render on refresh.
      const needsShell = !el.querySelector("#usage-metrics");

      if (needsShell) {
        const heatmap = this._renderUsageHeatmap(stats.sessions || [], muted);
        const unifiedChart = this._renderUnifiedDailyBarChart(model, groupBy, muted);
        el.innerHTML = `
          <div class="settings-section-title">
            <i data-lucide="chart-column" class="lucide section-title-icon"></i>
            ${t("settings.usageStats")}
            <button class="btn-icon" id="btn-refresh-usage-chart" data-tooltip="${t("settings.refresh")}" style="margin-left:auto">
              <i data-lucide="refresh-cw" class="lucide"></i>
            </button>
          </div>

          <div class="usage-metrics" id="usage-metrics">
            ${this._metricHtml("usage-metric-sessions", stats.total_sessions, "settings.totalSessions")}
            <div class="usage-metric-vr"></div>
            ${this._metricHtml("usage-metric-tokens", this._formatNumber(stats.total_tokens), "settings.totalTokens")}
            <div class="usage-metric-vr"></div>
            ${this._metricHtml("usage-metric-input", this._formatNumber(stats.total_input_tokens), "settings.totalInputTokens")}
            <div class="usage-metric-vr"></div>
            ${this._metricHtml("usage-metric-output", this._formatNumber(stats.total_output_tokens), "settings.totalOutputTokens")}
            <div class="usage-metric-vr"></div>
            ${this._metricHtml("usage-metric-tools", stats.total_tool_calls, "settings.totalToolCalls")}
          </div>

          ${heatmap}

          ${unifiedChart}`;

        document.getElementById("btn-refresh-usage-chart")?.addEventListener("click", () => {
          refreshAllData();
        });
        el.querySelectorAll(".usage-groupby-seg .seg-item").forEach((btn) => {
          btn.addEventListener("click", () => {
            const mode = (btn as HTMLElement).getAttribute("data-groupby") as "model" | "channel";
            if (mode) this._applyUsageGrouping(mode);
          });
        });
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: el });
        }
      }

      // ---- in-place DATA update (runs on every render / refresh) ----
      const metricsEl = el.querySelector("#usage-metrics") as HTMLElement | null;
      const chartWrap = el.querySelector("#usage-chart-wrap") as HTMLElement | null;
      const canvas = el.querySelector("#usage-bar-chart") as HTMLCanvasElement | null;

      // Show the skeleton shimmer on the data regions while we recompute.
      metricsEl?.classList.add("is-loading");
      chartWrap?.classList.remove("is-ready");

      // Metric numbers update synchronously.
      this._setMetricValue(el, "usage-metric-sessions", String(stats.total_sessions));
      this._setMetricValue(el, "usage-metric-tokens", this._formatNumber(stats.total_tokens));
      this._setMetricValue(el, "usage-metric-input", this._formatNumber(stats.total_input_tokens));
      this._setMetricValue(el, "usage-metric-output", this._formatNumber(stats.total_output_tokens));
      this._setMetricValue(el, "usage-metric-tools", String(stats.total_tool_calls));

      // Legend + title + segment highlight follow the grouping silently.
      const legendEl = el.querySelector(".usage-legend") as HTMLElement | null;
      if (legendEl) legendEl.innerHTML = model.legendHtml;
      const titleEl = el.querySelector(".usage-chart-title-text") as HTMLElement | null;
      if (titleEl) {
        titleEl.textContent = t(groupBy === "model" ? "settings.dailyUsageByModel" : "settings.dailyUsageByChannel");
      }
      el.querySelectorAll(".usage-groupby-seg .seg-item").forEach((b) => {
        const isActive = (b as HTMLElement).getAttribute("data-groupby") === groupBy;
        b.classList.toggle("active", isActive);
        (b as HTMLElement).setAttribute("aria-selected", String(isActive));
      });

      // Lazy-load Chart.js (cached after first resolve).
      const { Chart, registerables } = await import("chart.js");
      Chart.register(...registerables);

      if (needsShell) {
        // Fresh canvas — build a new instance.
        if (this._usageBarChart) {
          this._usageBarChart.destroy();
          this._usageBarChart = null;
        }
        if (canvas && this._usageBarChartData) {
          const data = this._usageBarChartData;
          this._usageBarChart = new Chart(canvas, {
            type: "bar",
            data: { labels: data.labels, datasets: data.datasets },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              interaction: { mode: "index", intersect: false },
              // The shimmer placeholder already conveys "loading"; disable the
              // chart's own entry animation so switching/refreshing doesn't
              // re-flicker the bars.
              animation: false,
              plugins: {
                legend: { display: false },
                // Native Chart.js tooltip disabled in favour of our global custom
                // tooltip (see tooltip.ts) which matches the app's design system.
                tooltip: { enabled: false },
              },
              onHover: (event: any, activeElements: any[]) => {
                const d = this._usageBarChartData;
                if (!d || !activeElements || activeElements.length === 0) {
                  hideTooltip();
                  return;
                }
                const idx = activeElements[0].index;
                const day = d.labels[idx];
                const lines = [`${day}`];
                let total = 0;
                for (const ds of d.datasets) {
                  const v = (ds.data as any[])[idx] || 0;
                  if (v > 0) {
                    lines.push(`${ds.label}: ${this._formatNumber(v)}`);
                    total += v;
                  }
                }
                lines.push(`${t("settings.chartTotal")}: ${this._formatNumber(total)}`);
                const rect = canvas!.getBoundingClientRect();
                const x = rect.left + (event?.x ?? rect.width / 2);
                const y = rect.top + (event?.y ?? 0);
                showTooltipAt(lines.join("\n"), x, y);
              },
              scales: {
                x: {
                  stacked: true,
                  grid: { color: "rgba(224,226,242,0.08)" },
                  ticks: { color: muted, font: { size: 10 } },
                },
                y: {
                  stacked: true,
                  beginAtZero: true,
                  grid: { color: "rgba(224,226,242,0.08)" },
                  ticks: {
                    color: muted,
                    font: { size: 10 },
                    callback: (v: any) => this._formatNumber(Number(v)),
                  },
                },
              },
            },
          });
          canvas.addEventListener("mouseleave", () => hideTooltip());
        }
      } else if (this._usageBarChart) {
        // Same canvas — update in place, no recreate, no flicker.
        this._usageBarChart.data.labels = this._usageBarChartData!.labels as any;
        this._usageBarChart.data.datasets = this._usageBarChartData!.datasets as any;
        this._usageBarChart.update();
      }

      // Reveal the data: drop the shimmer.
      metricsEl?.classList.remove("is-loading");
      requestAnimationFrame(() => chartWrap?.classList.add("is-ready"));
      this._lastUsageStatsRendered = stats;
      this._lastUsageMutedRendered = muted;
      this._lastUsageGroupByRendered = this._usageGroupBy;
      this._lastUsageLocaleRendered = locale;
    } finally {
      this._usageRendering = false;
      if (this._usageRenderPending) {
        this._usageRenderPending = false;
        void this._renderUsageSection();
      }
    }
  }

  /** Build one metric cell (value + label) with a hidden shimmer bar that is
   *  revealed only while the metrics row is in its loading state. */
  private _metricHtml(id: string, value: string | number, labelKey: string): string {
    return `
          <div class="usage-metric">
            <span class="usage-metric-value" id="${id}">${value}</span>
            <span class="usage-metric-shimmer" aria-hidden="true"></span>
            <span class="usage-metric-label">${t(labelKey)}</span>
          </div>`;
  }

  /** Update a metric cell's numeric value in place (no DOM rebuild). */
  private _setMetricValue(root: HTMLElement, id: string, value: string): void {
    const node = root.querySelector("#" + id) as HTMLElement | null;
    if (node) node.textContent = value;
  }

  /** Pure data computation for the unified daily bar chart.  Shared by the
   *  full panel render and the lightweight in-place re-grouping so the
   *  (somewhat heavy) aggregation is defined exactly once. */
  private _computeUsageChartModel(
    sessions: UsageStatsSessionEntry[],
    groupBy: "model" | "channel",
  ): {
    days: string[];
    groupOrder: string[];
    cellMap: Record<string, Record<string, { tokens: number; count: number; turns: number; tools: number }>>;
    groupColor: Record<string, string>;
    groupTotals: Record<string, { tokens: number; status?: string }>;
    truncated: boolean;
    labels: string[];
    datasets: any[];
    legendHtml: string;
  } {
    // Collect every day that has any session, sorted ascending.
    const daySet = new Set<string>();
    for (const s of sessions) {
      const ts = s.last_active || s.first_active;
      const dk = this._formatDayKey(ts);
      if (dk) daySet.add(dk);
    }
    const allDays = Array.from(daySet).sort();
    // Cap the bar chart to the most recent 60 days so bars stay readable
    // with many models.  Older days are still counted in metrics and
    // shown in the heatmap; we just don't draw them in the daily chart.
    const BAR_CHART_DAY_LIMIT = 60;
    const days = allDays.slice(-BAR_CHART_DAY_LIMIT);

    // Collect every group (model or channel) that appears, sort by total tokens descending
    const groupTotals: Record<string, { tokens: number; status?: string }> = {};
    for (const s of sessions) {
      const g = groupBy === "model" ? (s.model || "(unknown model)") : this._channelDisplayName(s.channel || "normal");
      if (!groupTotals[g]) groupTotals[g] = { tokens: 0, status: s.model_status };
      groupTotals[g].tokens += s.total_tokens || 0;
    }
    const groupOrder = Object.keys(groupTotals).sort(
      (a, b) => (groupTotals[b].tokens - groupTotals[a].tokens) || a.localeCompare(b)
    );
    const groupColor: Record<string, string> = {};
    groupOrder.forEach((g, i) => {
      groupColor[g] = Settings.CHART_COLORS[i % Settings.CHART_COLORS.length];
    });

    // Build (day, group) → { tokens, count, turns, tools } matrix.
    const cellMap: Record<string, Record<string, { tokens: number; count: number; turns: number; tools: number }>> = {};
    for (const s of sessions) {
      const ts = s.last_active || s.first_active;
      const dk = this._formatDayKey(ts);
      if (!dk) continue;
      const g = groupBy === "model" ? (s.model || "(unknown model)") : this._channelDisplayName(s.channel || "normal");
      if (!cellMap[dk]) cellMap[dk] = {};
      if (!cellMap[dk][g]) cellMap[dk][g] = { tokens: 0, count: 0, turns: 0, tools: 0 };
      const cell = cellMap[dk][g];
      cell.tokens += s.total_tokens || 0;
      cell.count += 1;
      cell.turns += s.turns || 0;
      cell.tools += s.tool_calls || 0;
    }

    const truncated = allDays.length > days.length;
    const labels = days.map((d) => this._shortDay(d));
    const datasets = groupOrder.map((g) => ({
      label: g,
      data: days.map((d) => cellMap[d]?.[g]?.tokens || 0),
      backgroundColor: groupColor[g],
      borderColor: groupColor[g],
      borderWidth: 0,
      borderRadius: 2,
      maxBarThickness: 28,
      stack: "usage",
    }));
    this._usageBarChartData = { labels, datasets };

    // Legend: top groups by tokens, one compact `dot + name` item each.
    // Usage and deleted/unknown status live in the hover tooltip
    // (`data-tooltip`, newlines honoured by the global tooltip) instead of
    // inline tags after the name.  The global tooltip handler is delegated,
    // so dynamically inserted items need no re-init.
    const LEGEND_MAX = 8;
    const legendEntries = groupOrder.filter((g) => (groupTotals[g]?.tokens || 0) > 0);
    const legendHtml =
      legendEntries
        .slice(0, LEGEND_MAX)
        .map((g) => {
          const color = groupColor[g];
          const total = groupTotals[g]?.tokens || 0;
          const status = groupTotals[g]?.status;
          const tipLines = [g, `${t("settings.usageTipTokens")}: ${this._formatNumber(total)}`];
          if (groupBy === "model" && status === "deleted") tipLines.push(t("settings.deletedModel"));
          else if (groupBy === "model" && status === "unknown") tipLines.push(t("settings.unknownModel"));
          return `
          <div class="usage-legend-item" data-tooltip="${this.esc(tipLines.join("\n"))}">
            <span class="usage-model-dot" style="background:${color}"></span>
            <span class="usage-legend-name">${this.esc(g)}</span>
          </div>`;
        })
        .join("") +
      (legendEntries.length > LEGEND_MAX
        ? `<div class="usage-legend-item usage-legend-item--more" data-tooltip="${this.esc(legendEntries.slice(LEGEND_MAX).join("\n"))}">+${legendEntries.length - LEGEND_MAX}</div>`
        : "");

    return { days, groupOrder, cellMap, groupColor, groupTotals, truncated, labels, datasets, legendHtml };
  }

  /** Switch the daily chart grouping (按模型 / 按模式) WITHOUT rebuilding the
   *  whole panel.  Reuses the existing Chart.js instance and only swaps the
   *  datasets + legend, then fades the shimmer out.  This is what removes the
   *  "switching triggers the entire panel to re-render" flicker. */
  private _applyUsageGrouping(mode: "model" | "channel"): void {
    if (mode === this._usageGroupBy) return;
    const el = this.panels.usage;
    if (!el) return;
    const stats = getState().usageStats;
    if (!stats || stats.total_sessions === 0) return;

    this._usageGroupBy = mode;
    // Update the segment highlight immediately for responsiveness.
    el.querySelectorAll(".usage-groupby-seg .seg-item").forEach((b) => {
      const isActive = (b as HTMLElement).getAttribute("data-groupby") === mode;
      b.classList.toggle("active", isActive);
      (b as HTMLElement).setAttribute("aria-selected", String(isActive));
    });

    // If a full render is currently in flight, let it finish and coalesce.
    if (this._usageRendering) {
      this._usageRenderPending = true;
      return;
    }
    this._usageRendering = true;
    try {
      const wrap = el.querySelector("#usage-chart-wrap") as HTMLElement | null;
      const chartBox = el.querySelector(".usage-chart-box") as HTMLElement | null;
      const legendEl = el.querySelector(".usage-legend") as HTMLElement | null;
      const titleEl = el.querySelector(".usage-chart-title-text") as HTMLElement | null;

      // Cross-fade (按模型 ⇄ 按模式) instead of a loading flicker: fade the
      // chart, its title and the legend out (the shimmer stays hidden because
      // .is-ready is never removed), swap the data while invisible, then fade
      // the new state back in — a dissolve like the app's mode-switch motion.
      const fade = TransitionHelper.step("--duration-fast", 120);
      wrap?.classList.add("is-switching");
      chartBox?.classList.add("is-switching");

      window.setTimeout(() => {
        const liveMode = this._usageGroupBy;
        const model = this._computeUsageChartModel(stats.sessions || [], liveMode);
        if (this._usageBarChart) {
          this._usageBarChart.data.labels = model.labels as any;
          this._usageBarChart.data.datasets = model.datasets as any;
          this._usageBarChart.update();
        }
        if (legendEl) legendEl.innerHTML = model.legendHtml;
        if (titleEl) {
          titleEl.textContent = t(liveMode === "model" ? "settings.dailyUsageByModel" : "settings.dailyUsageByChannel");
        }
        wrap?.classList.remove("is-switching");
        chartBox?.classList.remove("is-switching");
        // The DOM now reflects this grouping — keep the render-dedup marker
        // in sync so a later panel re-entry with unchanged data can skip.
        this._lastUsageGroupByRendered = liveMode;
      }, fade);
    } finally {
      this._usageRendering = false;
      if (this._usageRenderPending) {
        this._usageRenderPending = false;
        void this._renderUsageSection();
      }
    }
  }

  /** Map channel identifier to a display label. */
  private _channelDisplayName(channel: string): string {
    const labels: Record<string, string> = {
      normal: t("settings.modeNormal"),
      iwork: "iWork",
      iclaw: "iClaw",
      sub_agent: t("settings.modeAutomation"),
      qqbot: "QQ Bot",
      feishu: "飞书",
      telegram: "Telegram",
      discord: "Discord",
      dingtalk: "钉钉",
      weixin: "微信",
      wecom: "企业微信",
      slack: "Slack",
      signal: "Signal",
      matrix: "Matrix",
      whatsapp: "WhatsApp",
      email: "Email",
      homeassistant: "Home Assistant",
      sms: "SMS",
      webhook: "Webhook",
      bluebubbles: "BlueBubbles",
      api_server: "API Server",
      google_chat: "Google Chat",
      irc: "IRC",
      line: "Line",
      msgraph: "MS Graph",
      raft: "Raft",
      simplex: "Simplex",
      teams: "Teams",
      ntfy: "Ntfy",
      photon: "Photon",
    };
    // Normalise to lowercase so "QQBot" and "qqbot" resolve to the same label.
    return labels[channel.toLowerCase()] || channel;
  }

  /** Bind reveal-eye toggles for every password field inside *root*.
   *  Each button with `data-eye-target="some-input-id"` toggles the
   *  referred input between `password` / `text` type on click.
   *  For the WeChat token field, revealing the token also activates a
   *  secure anti-copy mode that blocks all keyboard shortcuts and
   *  clipboard operations until the dialog is dismissed. */
  private _bindEyeToggles(root: HTMLElement): void {
    root.querySelectorAll<HTMLButtonElement>(".model-form-eye-btn").forEach((btn) => {
      const targetId = btn.getAttribute("data-eye-target");
      if (!targetId) return;
      const input = root.querySelector<HTMLInputElement>(`#${CSS.escape(targetId)}`);
      if (!input) return;
      const isWeixinToken = input.id === "dlg-weixin-token-input";
      let visible = false;
      let secureCleanup: (() => void) | null = null;
      btn.addEventListener("click", () => {
        visible = !visible;
        input.type = visible ? "text" : "password";
        const iconEl = btn.querySelector("i");
        if (iconEl) iconEl.setAttribute("data-lucide", visible ? "eye-off" : "eye");
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: btn });
        }
        // WeChat token reveal → lock down clipboard & shortcuts
        if (visible && isWeixinToken && !secureCleanup) {
          secureCleanup = this._enableSecureMode();
          // Auto-release when the dialog is removed from the DOM.
          const observer = new MutationObserver(() => {
            if (!root.isConnected) {
              if (secureCleanup) secureCleanup();
              secureCleanup = null;
              observer.disconnect();
            }
          });
          observer.observe(document.body, { childList: true, subtree: true });
        }
      });
    });
  }

  /** Activate anti-copy / anti-shortcut lockdown for the entire document.
   *  Blocks every keyboard event and clipboard operation.
   *  Returns a cleanup function to restore normal behaviour. */
  private _enableSecureMode(): () => void {
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
    };
    const onClip = (e: ClipboardEvent) => {
      e.preventDefault();
    };
    const onCtx = (e: MouseEvent) => {
      e.preventDefault();
    };
    const onSelect = (e: Event) => {
      e.preventDefault();
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("copy", onClip, true);
    document.addEventListener("cut", onClip, true);
    document.addEventListener("contextmenu", onCtx, true);
    document.addEventListener("selectstart", onSelect, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("copy", onClip, true);
      document.removeEventListener("cut", onClip, true);
      document.removeEventListener("contextmenu", onCtx, true);
      document.removeEventListener("selectstart", onSelect, true);
    };
  }

  /** YYYY-MM-DD key for grouping sessions by calendar day (local time). */
  private _formatDayKey(timestamp: number): string {
    if (!timestamp || timestamp <= 0) return "";
    const d = new Date(timestamp * 1000);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  /**
   * Render one unified bar chart covering all models.  X axis = days,
   * Y axis = tokens per day.  Each day's bar is a single stacked column
   * where every model contributes one colored segment (fixed color from
   * :data:`Settings.CHART_COLORS`).  Stacked layout means all models
   * share the same bar per day, distinguished only by color.  A legend
   * below the chart maps every model name to its color and shows a
   * "(deleted)" / "(unknown)" tag for historical-only models.
   */
  private _renderUnifiedDailyBarChart(
    model: {
      days: string[];
      groupOrder: string[];
      truncated: boolean;
      legendHtml: string;
    },
    groupBy: "model" | "channel" = "model",
    muted: string = "",
  ): string {
    if (model.days.length === 0 || model.groupOrder.length === 0) return "";

    const titleKey = groupBy === "model" ? "settings.dailyUsageByModel" : "settings.dailyUsageByChannel";

    return `
      <div class="usage-chart-box">
        <div class="usage-chart-title">
          <i data-lucide="bar-chart-3" class="lucide"></i>
          <span class="usage-chart-title-text">${t(titleKey)}</span>
          <div class="seg usage-groupby-seg">
            <button class="seg-item${groupBy === "model" ? " active" : ""}" data-groupby="model" aria-selected="${groupBy === "model" ? "true" : "false"}">${t("settings.usageGroupByModel")}</button>
            <button class="seg-item${groupBy === "channel" ? " active" : ""}" data-groupby="channel" aria-selected="${groupBy === "channel" ? "true" : "false"}">${t("settings.usageGroupByMode")}</button>
          </div>
        </div>
        <div class="usage-heatmap-stats">
          ${model.truncated ? `<div class="usage-heatmap-stat"><span>${t("settings.olderDaysHidden")}</span></div>` : ""}
        </div>
        <div class="usage-chart-canvas-wrap" id="usage-chart-wrap">
          <canvas id="usage-bar-chart"></canvas>
          <div class="usage-chart-shimmer" aria-hidden="true">
            <span class="usage-shimmer-bar bar-1"></span>
            <span class="usage-shimmer-bar bar-2"></span>
            <span class="usage-shimmer-bar bar-3"></span>
            <span class="usage-shimmer-bar bar-4"></span>
          </div>
        </div>
        <div class="usage-legend">${model.legendHtml}</div>
      </div>`;
  }

  /** "2026-06-22" -> "Jun 22" for compact X-axis labels. */
  private _shortDay(iso: string): string {
    if (!iso) return "";
    const parts = iso.split("-");
    if (parts.length !== 3) return iso;
    const d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(getIntlLocale(), { month: "short", day: "numeric" });
  }

  /**
    * Render a daily activity heatmap.  Each day is one column with 6
    * rows, one per 4-hour block of the day (00:00 at the top, 20:00 at
    * the bottom) × N day columns (one per calendar day, newest on the
    * right).  Cell color encodes total tokens consumed during that
    * 4-hour block of that day, summed across every model.
   *
   * Color scale: empty = light/neutral, more activity = deeper brand
   * green.  The user wanted "less = white, more = green", which means
   * an empty cell is NOT a dark block -- it's the panel background
   * with just a thin border, so the chart reads as mostly white with
   * green dots marking activity.
   *
   * Layout is intentionally compact: small cells, small fonts, and
   * the SVG total width grows with the day count so the container
   * scrolls horizontally for long histories.
   */
   private _renderUsageHeatmap(sessions: UsageStatsSessionEntry[], muted: string): string {
    if (!sessions || sessions.length === 0) return "";
    const loc = getIntlLocale();

    // Aggregate sessions by (day, 4-hour block).  Each day is one column
    // with BLOCKS rows; every row spans 4 hours of the day so the chart
    // stays compact (6 rows instead of 24 tiny hourly boxes).
    const BLOCK_HOURS = 4;
    const BLOCKS = Math.floor(24 / BLOCK_HOURS); // 6 rows
    const cellTokens: Record<string, number> = {};
    for (const s of sessions) {
      const ts = s.last_active || s.first_active;
      if (!ts) continue;
      const d = new Date(ts * 1000);
      if (isNaN(d.getTime())) continue;
      const dayKey = this._formatDayKey(ts);
      if (!dayKey) continue;
      const block = Math.floor(d.getHours() / BLOCK_HOURS);
      cellTokens[`${dayKey}|${block}`] = (cellTokens[`${dayKey}|${block}`] || 0) + (s.total_tokens || 0);
    }

    // Determine the date range.  Always show at least MIN_DAYS of
    // calendar so a user with one session doesn't see a single bar.
    const MIN_DAYS = 30;    // minimum visible days (~1 month)
    const MAX_DAYS = 60;    // cap at the recent ~2 months so it stays readable
    const activeTs = sessions.map(s => (s.last_active || s.first_active) * 1000).filter(t => t);
    const latestMs = Math.max(...activeTs);
    const earliestMs = Math.min(...activeTs);
    const latestDate = new Date(latestMs);
    const earliestDate = new Date(earliestMs);
    let startDate = new Date(earliestDate);
    startDate.setHours(0, 0, 0, 0);
    const endDate = new Date(latestDate);
    endDate.setHours(0, 0, 0, 0);
    const totalDays = Math.round((endDate.getTime() - startDate.getTime()) / 86400000) + 1;
    let numDays = totalDays;
    if (numDays < MIN_DAYS) {
      // Pad backwards so we always show at least MIN_DAYS columns.
      const newStart = new Date(endDate);
      newStart.setDate(newStart.getDate() - (MIN_DAYS - 1));
      startDate = newStart;
      numDays = MIN_DAYS;
    } else if (numDays > MAX_DAYS) {
      // Crop to the most recent MAX_DAYS so the chart stays readable.
      const newStart = new Date(endDate);
      newStart.setDate(newStart.getDate() - (MAX_DAYS - 1));
      startDate = newStart;
      numDays = MAX_DAYS;
    }

    // Max value for color scaling.
    let maxCell = 0;
    for (const k of Object.keys(cellTokens)) {
      if (cellTokens[k] > maxCell) maxCell = cellTokens[k];
    }
    if (maxCell <= 0) {
      // No usage at all -- still render the grid so the user sees the shape.
      maxCell = 1;
    }

    // Layout -- square cells that fill the panel width.  With only
    // BLOCKS (6) rows the chart stays short; each cell spans 4 hours.
    // The chart is capped at the recent 3 months (this month + the
    // previous two) and scales to fit the panel (no horizontal scroll),
    // so the cells spread out to fill the available width.
    // Bigger square cells with a UNIFORM gap in all four directions
    // (left/right/top/bottom).  The day columns are no longer stretched to
    // fill the panel width -- that stretch made the horizontal gap far
    // larger than the vertical one and produced tiny cells with big empty
    // space.  Now every gap equals cellGap so the grid reads as a clean
    // checkerboard; the scroll container already has overflow-x:auto for
    // long histories.
    const cellW = 12, cellH = 12, cellGap = 4;
    const cellRadius = 2;
    const PL = 6, PR = 6, PT = 14, PB = 20;
    const stride = cellW + cellGap; // uniform horizontal gap == cellGap
    const W = PL + numDays * stride - cellGap + PR;
    const H = PT + BLOCKS * (cellH + cellGap) - cellGap + PB;

    // Today marker -- highlight the column for today.
    const todayKey = this._formatDayKey(Math.floor(Date.now() / 1000));

    // Color ramp: 5 stops.  Empty cell is the panel background
    // (almost transparent over the chart box), so empty reads as
    // "white" / "no color".  Activity is brand green at increasing
    // alpha over a near-white base, so the chart visually inverts to
    // "less = white, more = green" as the user asked.
    //
    // The brand color is var(--bg-bg-brand, #32f08c); the base is
    // var(--bg-bg-base-secondary, #222427) for dark and #ffffff for
    // light.  We pick the base from the active theme.
    const baseBg = "var(--usage-hm-base, #ffffff)";
    const emptyColor = "var(--usage-hm-empty, #f3f4f6)";
    const ramp = [
      emptyColor,
      `color-mix(in srgb, var(--bg-bg-brand, #32f08c) 22%, ${baseBg})`,
      `color-mix(in srgb, var(--bg-bg-brand, #32f08c) 45%, ${baseBg})`,
      `color-mix(in srgb, var(--bg-bg-brand, #32f08c) 70%, ${baseBg})`,
      `color-mix(in srgb, var(--bg-bg-brand, #32f08c) 92%, ${baseBg})`,
    ];
    const pickColor = (v: number): string => {
      if (v <= 0) return ramp[0];
      const t = v / maxCell;
      let idx = 1;
      if (t > 0.75) idx = 4;
      else if (t > 0.5) idx = 3;
      else if (t > 0.25) idx = 2;
      return ramp[idx];
    };

    // No left-axis hour labels (removed per user request); cells start
    // at the panel edge.

    // Day labels along the bottom: ONLY month names (e.g. "7月"),
    // shown once at the first column of each month.  No day numbers, no
    // weekly ticks -- the user explicitly wants month-only axis labels.
    // The first label is left-anchored and the last is right-anchored so
    // the text never spills outside the SVG viewBox (which would clip it);
    // a single month is centered.
    // Labels live in the bottom padding band between the last cell row
    // (cellsBottom) and the SVG bottom edge (H).  Center them vertically
    // in that band via dominant-baseline so they never clip at the
    // viewBox bottom (a hand-computed baseline used to overflow H by 1px).
    const cellsBottom = PT + BLOCKS * (cellH + cellGap) - cellGap;
    const labelY = (cellsBottom + H) / 2;
    const labelInfos: { x: number; label: string }[] = [];
    let lastMonth = -1;
    for (let d = 0; d < numDays; d++) {
      const colDate = new Date(startDate);
      colDate.setDate(colDate.getDate() + d);
      const m = colDate.getMonth();
      if (m !== lastMonth) {
        lastMonth = m;
        const x = PL + d * stride + cellW / 2;
        const label = colDate.toLocaleDateString(loc, { month: "short" });
        labelInfos.push({ x, label });
      }
    }
    const dayLabels = labelInfos.map((info, i) => {
      let anchor: string;
      let tx: number;
      if (labelInfos.length === 1) {
        anchor = "middle";
        tx = W / 2;
      } else if (i === 0) {
        anchor = "start";
        tx = info.x;
      } else if (i === labelInfos.length - 1) {
        anchor = "end";
        tx = info.x;
      } else {
        anchor = "middle";
        tx = info.x;
      }
      return `<text x="${tx.toFixed(1)}" y="${labelY.toFixed(1)}" ` +
        `text-anchor="${anchor}" dominant-baseline="central" ` +
        `fill="var(--text-text-secondary, #9599a6)" ` +
        `style="font-size:10px;font-weight:var(--font-weight-medium,500);font-family:var(--font-family-default);">` +
        `${info.label}</text>`;
    });

    // Cells: one per (day, 4-hour block).  Only render cells inside the
    // data range; padding cells stay empty (drawn with the empty color).
    // The native SVG <title> is replaced by data-tooltip so our global
    // custom tooltip (tooltip.ts) renders the hover hint instead.
    const cells: string[] = [];
    for (let d = 0; d < numDays; d++) {
      const colDate = new Date(startDate);
      colDate.setDate(colDate.getDate() + d);
      const dayKey = this._formatDayKey(Math.floor(colDate.getTime() / 1000));
      const isToday = dayKey === todayKey;
      for (let b = 0; b < BLOCKS; b++) {
        const v = cellTokens[`${dayKey}|${b}`] || 0;
        const x = PL + d * stride;
        const y = PT + b * (cellH + cellGap);
        const fill = pickColor(v);
        const startH = b * BLOCK_HOURS;
        const endH = startH + BLOCK_HOURS;
        const tooltip = v > 0
          ? `${dayKey} ${String(startH).padStart(2, "0")}:00–${String(endH).padStart(2, "0")}:00\n${this._formatNumber(v)} tokens`
          : `${dayKey} ${String(startH).padStart(2, "0")}:00–${String(endH).padStart(2, "0")}:00\n${t("settings.usageNoActivity")}`;
        // Empty cells get a hairline border so the grid is visible
        // even on a fully-empty day.  Today's column gets a slightly
        // thicker green ring on the topmost block (b=0) to anchor the
        // eye.
        const stroke = isToday && b === 0
          ? "var(--bg-bg-brand, #32f08c)"
          : "var(--usage-hm-border, rgba(15, 23, 42, 0.08))";
        const strokeW = isToday && b === 0 ? 1 : 0.75;
        cells.push(
          `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${cellW}" height="${cellH}" ` +
          `rx="${cellRadius}" ry="${cellRadius}" fill="${fill}" ` +
          `stroke="${stroke}" stroke-width="${strokeW}" class="usage-hm-cell" ` +
          `data-day="${this.esc(dayKey)}" data-block="${b}" ` +
          `data-tooltip="${this.esc(tooltip)}"></rect>`
        );
      }
    }

    // Legend row at the bottom -- "Less" ... ramp swatches ... "More".
    const legendCellSize = 12;
    const legendGap = 3;
    const legendW = ramp.length * legendCellSize + (ramp.length - 1) * legendGap;
    const legendRects = ramp.map((c, i) =>
      `<rect x="${i * (legendCellSize + legendGap)}" y="0" width="${legendCellSize}" height="${legendCellSize}" rx="0" ry="0" ` +
      `fill="${c}" stroke="var(--usage-hm-border, rgba(15, 23, 42, 0.08))" stroke-width="0.75"/>`
    ).join("");

    // Aggregate stats for the caption row.
    const daySet = new Set<string>();
    for (const s of sessions) {
      const ts = s.last_active || s.first_active;
      if (!ts) continue;
      const k = this._formatDayKey(ts);
      if (k) daySet.add(k);
    }
    const activeDays = daySet.size;
    const captionParts = [
      `<span class="usage-heatmap-num">${activeDays}</span> <span>${t("settings.activeDays") || "active days"}</span>`,
      `<span class="usage-heatmap-num">${numDays}</span> <span>${t("settings.days") || "days"}</span>`,
      `<span class="usage-heatmap-num">24</span> <span>${t("settings.hoursPerDay") || "hours"}</span>`,
    ];
    const captionHtml = captionParts
      .map(p => `<div class="usage-heatmap-stat">${p}</div>`)
      .join('<div class="usage-heatmap-stat-divider"></div>');

    return `
      <div class="usage-chart-box usage-chart-box--compact">
        <div class="usage-chart-title">
          <i data-lucide="calendar-days" class="lucide"></i>
          ${t("settings.activityHeatmap") || "Activity heatmap"}
        </div>
        <div class="usage-heatmap-stats usage-heatmap-stats--compact">${captionHtml}</div>
        <div class="usage-heatmap-scroll">
          <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMinYMid meet" style="width:100%;height:auto;display:block">
            ${cells.join("")}
            ${dayLabels.join("")}
          </svg>
        </div>
        <div class="usage-heatmap-legend usage-heatmap-legend--compact">
          <span class="usage-heatmap-legend-label">${t("settings.less") || "Less"}</span>
          <svg width="${legendW}" height="${legendCellSize}" viewBox="0 0 ${legendW} ${legendCellSize}" xmlns="http://www.w3.org/2000/svg">${legendRects}</svg>
          <span class="usage-heatmap-legend-label">${t("settings.more") || "More"}</span>
        </div>
      </div>`;
  }

  /* ── SVG Chart Helpers ─────────────────────────────────────────── */

  private static readonly CHART_COLORS = [
    "#22c55e", "#3b82f6", "#f59e0b", "#ef4444", "#8b5cf6",
    "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
    "#06b6d4", "#d946ef", "#0ea5e9", "#eab308", "#10b981",
  ];

  private _renderSvgLineChart(sessions: UsageStatsSessionEntry[], accent: string, muted: string): string {
    const W = 600, H = 200;
    const PT = 16, PR = 16, PB = 32, PL = 48;
    const cw = W - PL - PR, ch = H - PT - PB;
    const values = sessions.map(s => s.total_tokens);
    const maxVal = Math.max(...values, 1);
    const n = sessions.length;

    const points = values.map((v, i) => ({
      x: PL + (n > 1 ? (i / (n - 1)) * cw : cw / 2),
      y: PT + ch - (v / maxVal) * ch,
      v,
      s: sessions[i],
    }));

    // Y axis grid
    const gridY: { y: number; label: string }[] = [];
    for (let i = 0; i <= 4; i++) {
      const y = PT + (i / 4) * ch;
      gridY.push({ y, label: this._formatNumber(Math.round(maxVal * (1 - i / 4))) });
    }

    // X axis labels
    const xLabels: { x: number; label: string }[] = [];
    const labelCount = Math.min(n, 6);
    const step = Math.max(1, Math.floor((n - 1) / (labelCount - 1)));
    for (let i = 0; i < n; i += step) {
      xLabels.push({ x: points[i].x, label: this._formatSessionDate(sessions[i].first_active) });
    }
    if (xLabels.length === 0 || xLabels[xLabels.length - 1].x !== points[n - 1].x) {
      xLabels.push({ x: points[n - 1].x, label: this._formatSessionDate(sessions[n - 1].first_active) });
    }

    const polyPts = points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
    const areaPath = n > 0
      ? `M${points[0].x.toFixed(1)},${PT + ch} L${polyPts} L${points[n - 1].x.toFixed(1)},${PT + ch} Z`
      : "";
    const gradId = "lg-" + Math.random().toString(36).slice(2, 8);

    return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block">
      <defs>
        <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${accent}" stop-opacity="0.25"/>
          <stop offset="100%" stop-color="${accent}" stop-opacity="0.02"/>
        </linearGradient>
      </defs>
      ${gridY.map(g => `
        <line x1="${PL}" y1="${g.y}" x2="${W - PR}" y2="${g.y}" stroke="var(--border-light, #222)" stroke-width="1"/>
        <text x="${PL - 6}" y="${g.y + 4}" text-anchor="end" fill="${muted}" font-size="10">${g.label}</text>`).join("")}
      <path d="${areaPath}" fill="url(#${gradId})"/>
      <polyline points="${polyPts}" fill="none" stroke="${accent}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      ${points.map(p => {
        const dateStr = this._formatSessionDate(p.s.first_active);
        const modelName = p.s.model && p.s.model !== "unknown" ? p.s.model : "";
        const tooltipLines = [
          dateStr ? `${t("settings.usageTipDate")}: ${dateStr}` : "",
          `${t("settings.usageTipTokens")}: ${p.v.toLocaleString()}`,
          modelName ? `${t("settings.usageTipModel")}: ${modelName}` : "",
          `${t("settings.usageTipTurns")}: ${p.s.turns}`,
          `${t("settings.usageTipToolCalls")}: ${p.s.tool_calls}`,
        ].filter(Boolean).join("  |  ");
        return `<g style="cursor:pointer">
          <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="10" fill="transparent" stroke="transparent" stroke-width="10">
            <title>${this.esc(tooltipLines)}</title>
          </circle>
          <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" fill="${accent}" stroke="var(--bg-primary, #000)" stroke-width="1.5" pointer-events="none"/>
        </g>`;
      }).join("")}
      ${xLabels.map(xl => `<text x="${xl.x}" y="${H - 4}" text-anchor="middle" fill="${muted}" font-size="9">${this.esc(xl.label)}</text>`).join("")}
    </svg>`;
  }

  private _renderSvgDonutChart(entries: [string, { total_tokens: number }][], accent: string): string {
    const total = entries.reduce((s, [, v]) => s + v.total_tokens, 0);
    const sorted = [...entries].sort((a, b) => b[1].total_tokens - a[1].total_tokens);
    const CX = 100, CY = 100, R = 68, SW = 26;
    const circ = 2 * Math.PI * R;

    let segments = "";
    let offset = 0;
    sorted.forEach(([name, data], i) => {
      const pct = data.total_tokens / total;
      const len = Math.max(circ * pct, 0.5);
      const color = Settings.CHART_COLORS[i % Settings.CHART_COLORS.length];
      const pctDisplay = (pct * 100).toFixed(1);
      const displayName = name === "unknown" ? "Unknown" : name;
      segments += `<g style="cursor:pointer">
        <circle cx="${CX}" cy="${CY}" r="${R}" fill="none" stroke="transparent" stroke-width="${SW + 8}" style="cursor:pointer">
          <title>${this.esc(displayName)}: ${this._formatNumber(data.total_tokens)} tokens (${pctDisplay}%)</title>
        </circle>
        <circle cx="${CX}" cy="${CY}" r="${R}" fill="none" stroke="${color}" stroke-width="${SW}"
        stroke-dasharray="${len.toFixed(1)} ${(circ - len).toFixed(1)}"
        stroke-dashoffset="${(-offset).toFixed(1)}"
        transform="rotate(-90, ${CX}, ${CY})" stroke-linecap="butt" pointer-events="none"/>
      </g>`;
      offset += len;
    });

    // Legend
    const maxLegend = 8;
    const legendItems = sorted.slice(0, maxLegend);
    const legend = legendItems.map(([name, data], i) => {
      const pct = ((data.total_tokens / total) * 100).toFixed(1);
      const displayName = name === "unknown" ? "Unknown" : name;
      return `<div class="donut-legend-item">
        <span class="donut-legend-dot" style="background:${Settings.CHART_COLORS[i % Settings.CHART_COLORS.length]}"></span>
        <span class="donut-legend-label" : displayName)}</span>
        <span class="donut-legend-value">${pct}%</span>
      </div>`;
    }).join("");
    const more = sorted.length > maxLegend ? `<div class="donut-legend-item"><span class="donut-legend-label" style="color:var(--text-muted)">+${sorted.length - maxLegend} more</span></div>` : "";

    return `<div class="donut-wrap">
      <div class="donut-svg">
        <svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;max-width:180px">
          ${segments}
          <text x="${CX}" y="${CY - 6}" text-anchor="middle" fill="var(--text-primary)" font-size="18" font-weight="700">${this._formatNumber(total)}</text>
          <text x="${CX}" y="${CY + 12}" text-anchor="middle" fill="${accent}" font-size="10">tokens</text>
        </svg>
      </div>
      <div class="donut-legend">${legend}${more}</div>
    </div>`;
  }

  private _renderSvgToolBar(entries: [string, number][], secondary: string): string {
    const sorted = [...entries].sort((a, b) => b[1] - a[1]).slice(0, 10);
    if (sorted.length === 0) return `<div class="chart-empty">${t("settings.noToolData")}</div>`;
    const maxVal = sorted[0][1] || 1;
    const BH = 26, H = sorted.length * BH + 8, W = 400, PL = 100, PR = 50, PT = 4;
    const bw = W - PL - PR;

    const bars = sorted.map(([name, count], i) => {
      const y = PT + i * BH;
      const w = (count / maxVal) * bw;
      const color = Settings.CHART_COLORS[(i + 3) % Settings.CHART_COLORS.length];
      return `
        <text x="${PL - 8}" y="${y + 16}" text-anchor="end" fill="var(--text-secondary, #999)" font-size="11">${this.esc(name.length > 16 ? name.slice(0, 16) + "..." : name)}</text>
        <rect x="${PL}" y="${y + 3}" width="${Math.max(w, 4)}" height="18" rx="4" fill="${color}" opacity="0.85"/>
        <text x="${PL + Math.max(w, 4) + 5}" y="${y + 16}" fill="var(--text-primary)" font-size="11" font-weight="600">${count}</text>`;
    }).join("");

    return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block">${bars}</svg>`;
  }

  private _formatSessionDate(timestamp: number): string {
    if (!timestamp || timestamp <= 0) return "";
    const d = new Date(timestamp * 1000);
    const loc = getIntlLocale();
    const now = Date.now();
    if (now - timestamp * 1000 < 86400000) {
      return d.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString(loc, { month: "short", day: "numeric" });
  }

  private _formatNumber(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
    if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
    return n.toLocaleString();
  }

  private renderDeveloper(): void {
    this.panels.developer.innerHTML = `
      <div class="settings-section-title"><i data-lucide="code" class="lucide section-title-icon"></i> ${t("settings.devPanelTitle")}</div>
      <div class="settings-card">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.devDevToolsTitle")}</div>
            <div class="settings-item-desc">${t("settings.devDevToolsDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="dev-open-devtools" data-tooltip="${t("settings.devDevToolsBtn")}"><i data-lucide="terminal" class="lucide"></i></button>
          </div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.devCloseTitle")}</div>
            <div class="settings-item-desc">${t("settings.devCloseDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon btn-icon--danger" id="dev-close-mode" data-tooltip="${t("settings.devCloseBtn")}"><i data-lucide="x-circle" class="lucide"></i></button>
          </div>
        </div>
      </div>
      <div class="settings-section-title"><i data-lucide="rotate-ccw" class="lucide section-title-icon"></i> ${t("settings.devRestartTitle")}</div>
      <div class="settings-card">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.devRestartAppTitle")}</div>
            <div class="settings-item-desc">${t("settings.devRestartAppDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon btn-icon--danger" id="dev-reset-state" data-tooltip="${t("settings.devRestartBtn")}"><i data-lucide="refresh-cw" class="lucide"></i></button>
          </div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.devRestartServerTitle")}</div>
            <div class="settings-item-desc">${t("settings.devRestartServerDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon btn-icon--danger" id="dev-restart-server" data-tooltip="${t("settings.devRestartBtn")}"><i data-lucide="refresh-cw" class="lucide"></i></button>
          </div>
        </div>
      </div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.developer });
    }

    document.getElementById("dev-close-mode")?.addEventListener("click", () => {
      setDevModeEnabled(false);
      this.updateSidebarNav();
      this.switchPanel("general");
    });

    document.getElementById("dev-open-devtools")?.addEventListener("click", () => {
      window.electronAPI?.toggleDevTools();
    });

    document.getElementById("dev-reset-state")?.addEventListener("click", async () => {
      const confirmed = await Dialog.confirm(t("settings.devRestartAppTitle"), t("settings.devRestartAppDesc"), { priority: "high", scope: "app" });
      if (!confirmed) return;
      localStorage.clear();
      location.reload();
    });

    document.getElementById("dev-restart-server")?.addEventListener("click", async () => {
      const running = getState().running;
      if (running) {
        const ok = await Dialog.confirm(t("settings.devRestartRunningTitle"), t("settings.devRestartRunningDesc"), { priority: "high", scope: "app" });
        if (!ok) return;
      }
        const confirmed = await Dialog.confirm(t("settings.devRestartTitle"), t("settings.devRestartServerDesc"), { priority: "high", scope: "app" });
      if (!confirmed) return;
        const prog = Dialog.progress(t("settings.devRestartProgressTitle"), "", { cancellable: false, scope: "app" });
      const cleanup = (window as any).electronAPI?.onRestartProgress?.((data: { progress: number }) => {
        const p = data.progress;
        let msg: string;
        if (p <= 20) msg = t("settings.devRestartPhaseKill");
        else if (p <= 50) msg = t("settings.devRestartPhasePort");
        else msg = t("settings.devRestartPhaseStart");
        prog.update(p, msg);
      });
      try {
        const result = await window.electronAPI?.restartService?.();
        if (result?.success) {
          prog.succeed(t("settings.devRestartSucceed"));
        } else {
          prog.fail(t("settings.devRestartFailed") + (result?.error ? ": " + result.error : ""));
        }
      } catch (err) {
        prog.fail(t("settings.devRestartFailed") + ": " + String(err));
      } finally {
        if (cleanup) cleanup();
      }
    });
  }

  private renderStorage(): void {
    this.panels.storage.innerHTML = `
      <div class="settings-section-title"><i data-lucide="hard-drive" class="lucide section-title-icon"></i> ${t("settings.storage")}</div>
      <div class="settings-card">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.dataCenter")}</div>
            <div class="settings-item-desc">${t("settings.dataCenterDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="storage-open-data-center" data-tooltip="${t("settings.dataCenter")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
          </div>
        </div>
      </div>

      <div class="settings-section-title"><i data-lucide="package" class="lucide section-title-icon"></i> ${t("settings.storageMigration")}</div>
      <div class="settings-card">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.storageExportData")}</div>
            <div class="settings-item-desc">${t("settings.storageExportDataDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="storage-export-data" data-tooltip="${t("settings.storageExportData")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
          </div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.storageImportData")}</div>
            <div class="settings-item-desc">${t("settings.storageImportDataDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="storage-import-data" data-tooltip="${t("settings.storageImportData")}"><i data-lucide="arrow-down-right" class="lucide"></i></button>
          </div>
        </div>
      </div>

      <div class="settings-section-title"><i data-lucide="alert-triangle" class="lucide section-title-icon"></i> ${t("settings.storageDangerZone")}</div>
      <div class="settings-card">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.storageSessions")}</div>
            <div class="settings-item-desc">${t("settings.storageSessionsDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon btn-icon--danger" id="storage-clear-sessions" data-tooltip="${t("settings.storageClearSessions")}"><i data-lucide="trash-2" class="lucide"></i></button>
          </div>
        </div>
      </div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.storage });
    }

    // Open data directory
    // Data center: opens the Encre Agent Desktop Subwindow (ESD) page that shows and manages the data tree
    // (previously two separate entries — the raw data directory and the log
    // viewer — now one page).
    document.getElementById("storage-open-data-center")?.addEventListener("click", async () => {
      await window.electronAPI?.openChildWindow("data-center", t("settings.dataCenter") || "Data Center");
    });

    // Clear sessions
    document.getElementById("storage-clear-sessions")?.addEventListener("click", async () => {
      const ok = await Dialog.confirm(t("common.confirmClearSessionsTitle"), t("common.confirmClearSessions"), { priority: "high", scope: "app" });
      if (ok) {
        send({ type: "clear_all_sessions" });
      }
    });

    // Export all data
    document.getElementById("storage-export-data")?.addEventListener("click", async () => {
      const ok = await Dialog.confirm(t("settings.storageExportTitle"), t("settings.storageExportConfirm"), { priority: "high", scope: "app" });
      if (ok) {
        send({ type: "export_data" } as any);
      }
    });

    // Import all data
    document.getElementById("storage-import-data")?.addEventListener("click", async () => {
      const api = window.electronAPI;
      if (!api?.pickFiles) return;
      const paths = await api.pickFiles();
      if (!paths || paths.length === 0) return;
      const mode = await this._chooseImportMode();
      if (!mode) return;
      send({ type: "import_data", zip_path: paths[0], mode } as any);
    });

    // Clear browser data
    document.getElementById("storage-clear-browser")?.addEventListener("click", async () => {
      const ok = await Dialog.confirm(t("settings.confirmClearBrowserDataTitle"), t("settings.confirmClearBrowserData"), { priority: "high", scope: "app" });
      if (ok) {
        window.electronAPI?.browserClearData?.();
      }
    });

  }

  /** One auxiliary surface's whole configuration, opened as a sub-page card
   *  over the 辅助面板 landing list (the four surfaces — 终端 / 编辑器 /
   *  审查 / 浏览器 — no longer own sidebar entries), in the same shape as the
   *  General panel: a section heading above each framed card of rows.  Every
   *  control persists through `saveSetting` the moment it changes, so there is
   *  nothing to confirm and nothing to discard. */
  private async renderAssistSurface(kind: AssistKind): Promise<void> {
    const s = getState().settings;
    const tFn = t;
    let title = "";
    let bodyHtml = "";
    /** `(dropdown id, onChange)` pairs collected while building the body and
     *  bound once the card is in the document. */
    const dropdowns: Array<[string, (v: string) => void]> = [];
    const str = (key: string, fallback: string): string => {
      const v = s[key];
      return typeof v === "string" && v.trim() !== "" ? v : fallback;
    };
    /** A dropdown row that saves straight through `saveSetting`. */
    const dd = (id: string, key: string, options: DropdownOption[], current: string, titleKey: string, descKey: string): string => {
      dropdowns.push([id, (v) => this.saveSetting(key, v)]);
      return this._assistDropdownRow(id, titleKey, descKey, options, current);
    };

    // Every card is laid out like the General panel: a section heading (题头)
    // above each framed card of rows.  Without the heading the card's own top
    // border sat directly under the dialog header's rule — two parallel lines
    // reading as one stray duplicated divider.
    if (kind === "terminal") {
      title = tFn("settings.terminalPanel");
      let shells: Array<{ name: string; path: string }> = [];
      try {
        shells = (await (window as any).electronAPI?.terminalListShells?.()) || [];
      } catch {
        shells = [];
      }
      const currentShell = str("terminal_shell", "auto");
      const shellOptions: DropdownOption[] = [
        { id: "auto", label: tFn("settings.optAuto") },
        ...shells.map((x) => ({ id: x.path, label: x.name })),
      ];
      const fontOptions: DropdownOption[] = [
        { id: "default", label: tFn("settings.optDefault") },
        { id: "cascadia", label: "Cascadia Code" },
        { id: "jetbrains", label: "JetBrains Mono" },
        { id: "firacode", label: "Fira Code" },
        { id: "sourcecode", label: "Source Code Pro" },
        { id: "consolas", label: "Consolas" },
        { id: "menlo", label: "Menlo" },
        { id: "courier", label: "Courier New" },
      ];
      const cursorOptions: DropdownOption[] = [
        { id: "bar", label: tFn("settings.optBar") },
        { id: "block", label: tFn("settings.optBlock") },
        { id: "underline", label: tFn("settings.optUnderline") },
      ];
      const inactiveOptions: DropdownOption[] = [
        { id: "outline", label: tFn("settings.optOutline") },
        { id: "block", label: tFn("settings.optBlock") },
        { id: "bar", label: tFn("settings.optBar") },
        { id: "underline", label: tFn("settings.optUnderline") },
        { id: "none", label: tFn("settings.optNone") },
      ];
      bodyHtml = `
        <div class="settings-section-title"><i data-lucide="settings" class="lucide section-title-icon"></i> ${tFn("settings.basicSettings")}</div>
        <div class="settings-card">
          ${this._assistNumberRow("terminal-font-size", "settings.terminalFontSize", "settings.terminalFontSizeDesc", Settings._assistNum(s.terminal_font_size, 13), 9, 32)}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-font", "terminal_font_family", fontOptions, str("terminal_font_family", "default"), "settings.terminalFontFamily", "settings.terminalFontFamilyDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistNumberRow("terminal-line-height", "settings.terminalLineHeight", "settings.terminalLineHeightDesc", Settings._assistFloat(s.terminal_line_height, 1.3), 0.8, 2, 0.05)}
          <div class="settings-item-divider"></div>
          ${this._assistNumberRow("terminal-scrollback", "settings.terminalScrollback", "settings.terminalScrollbackDesc", Settings._assistNum(s.terminal_scrollback, 10000), 1000, 200000, 1000)}
        </div>

        <div class="settings-section-title"><i data-lucide="text-cursor-input" class="lucide section-title-icon"></i> ${tFn("settings.sectionCursor")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("terminal-cursor-blink", "settings.terminalCursorBlink", "settings.terminalCursorBlinkDesc", s.terminal_cursor_blink, true)}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-cursor", "terminal_cursor_style", cursorOptions, str("terminal_cursor_style", "bar"), "settings.terminalCursorStyle", "settings.terminalCursorStyleDesc")}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-cursor-inactive", "terminal_cursor_inactive", inactiveOptions, str("terminal_cursor_inactive", "outline"), "settings.terminalCursorInactive", "settings.terminalCursorInactiveDesc")}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-cursor-width", "terminal_cursor_width", [
            { id: "1", label: "1" },
            { id: "2", label: "2" },
          ], String(Settings._assistNum(s.terminal_cursor_width, 1)), "settings.terminalCursorWidth", "settings.terminalCursorWidthDesc")}
        </div>

        <div class="settings-section-title"><i data-lucide="palette" class="lucide section-title-icon"></i> ${tFn("settings.sectionAppearance")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("terminal-bold-bright", "settings.terminalBoldBright", "settings.terminalBoldBrightDesc", s.terminal_bold_bright, true)}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-font-weight", "terminal_font_weight", [
            { id: "normal", label: tFn("settings.optDefault") },
            { id: "bold", label: tFn("settings.optBold") },
          ], str("terminal_font_weight", "normal"), "settings.terminalFontWeight", "settings.terminalFontWeightDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistNumberRow("terminal-letter-spacing", "settings.terminalLetterSpacing", "settings.terminalLetterSpacingDesc", Settings._assistNum(s.terminal_letter_spacing, 0), -5, 10)}
          <div class="settings-item-divider"></div>
          ${this._assistNumberRow("terminal-min-contrast", "settings.terminalMinContrast", "settings.terminalMinContrastDesc", Settings._assistNum(s.terminal_min_contrast, 1), 1, 21, 0.5)}
        </div>

        <div class="settings-section-title"><i data-lucide="mouse-pointer-click" class="lucide section-title-icon"></i> ${tFn("settings.sectionBehavior")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("terminal-copy-on-select", "settings.terminalCopyOnSelect", "settings.terminalCopyOnSelectDesc", s.terminal_copy_on_select, false)}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-right-click", "terminal_right_click", [
            { id: "menu", label: tFn("settings.optMenu") },
            { id: "paste", label: tFn("settings.optPaste") },
            { id: "word", label: tFn("settings.optWord") },
          ], str("terminal_right_click", "menu"), "settings.terminalRightClick", "settings.terminalRightClickDesc")}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-bell", "terminal_bell", [
            { id: "visual", label: tFn("settings.optVisual") },
            { id: "none", label: tFn("settings.optNone") },
          ], str("terminal_bell", "visual"), "settings.terminalBell", "settings.terminalBellDesc")}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-cols", "terminal_cols_mode", [
            { id: "auto", label: tFn("settings.optFit") },
            { id: "80", label: "80" },
            { id: "120", label: "120" },
          ], str("terminal_cols_mode", "auto"), "settings.terminalColsMode", "settings.terminalColsModeDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("terminal-convert-eol", "settings.terminalConvertEol", "settings.terminalConvertEolDesc", s.terminal_convert_eol, false)}
          <div class="settings-item-divider"></div>
          ${this._assistTextRow("terminal-word-separator", "settings.terminalWordSeparator", "settings.terminalWordSeparatorDesc", str("terminal_word_separator", DEFAULT_TERM_WORD_SEPARATORS))}
        </div>

        <div class="settings-section-title"><i data-lucide="terminal-square" class="lucide section-title-icon"></i> ${tFn("settings.sectionShell")}</div>
        <div class="settings-card">
          ${dd("dd-terminal-shell", "terminal_shell", shellOptions, currentShell, "settings.terminalShell", "settings.terminalShellDesc")}
          <div class="settings-item-divider"></div>
          ${dd("dd-terminal-start-dir", "terminal_start_dir", [
            { id: "home", label: tFn("settings.optHome") },
            { id: "workspace", label: tFn("settings.optWorkspace") },
          ], str("terminal_start_dir", "home"), "settings.terminalStartDir", "settings.terminalStartDirDesc")}
        </div>`;
    } else if (kind === "editor") {
      title = tFn("settings.editorPanel");
      const whitespaceOptions: DropdownOption[] = [
        { id: "none", label: tFn("settings.optWhitespaceNever") },
        { id: "boundary", label: tFn("settings.optWhitespaceBoundary") },
        { id: "selection", label: tFn("settings.optWhitespaceSelection") },
        { id: "all", label: tFn("settings.optWhitespaceAll") },
      ];
      bodyHtml = `
        <div class="settings-section-title"><i data-lucide="settings" class="lucide section-title-icon"></i> ${tFn("settings.basicSettings")}</div>
        <div class="settings-card">
          ${this._assistNumberRow("editor-font-size", "settings.editorFontSize", "settings.editorFontSizeDesc", Settings._assistNum(s.editor_font_size, 13), 9, 32)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-line-numbers", "settings.editorLineNumbers", "settings.editorLineNumbersDesc", s.editor_line_numbers, true)}
          <div class="settings-item-divider"></div>
          ${dd("dd-editor-tab-size", "editor_tab_size", [
            { id: "2", label: "2" },
            { id: "4", label: "4" },
            { id: "8", label: "8" },
          ], String(Settings._assistNum(s.editor_tab_size, 4)), "settings.editorTabSize", "settings.editorTabSizeDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-detect-indentation", "settings.editorDetectIndentation", "settings.editorDetectIndentationDesc", s.editor_detect_indentation, true)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-use-spaces", "settings.editorUseSpaces", "settings.editorUseSpacesDesc", s.editor_use_spaces, true)}
          <div class="settings-item-divider"></div>
          ${dd("dd-editor-tab-indent", "editor_tab_indent", [
            { id: "none", label: tFn("settings.optIndentKeep") },
            { id: "indent", label: tFn("settings.optIndentIndent") },
            { id: "indentOutdent", label: tFn("settings.optIndentIndentOutdent") },
            { id: "outdent", label: tFn("settings.optIndentOutdent") },
          ], str("editor_tab_indent", "indent"), "settings.editorIndentWithTab", "settings.editorIndentWithTabDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistNumberRow("editor-wrap-column", "settings.editorWrapColumn", "settings.editorWrapColumnDesc", Settings._assistNum(s.editor_word_wrap_column, 80), 20, 200)}
        </div>

        <div class="settings-section-title"><i data-lucide="palette" class="lucide section-title-icon"></i> ${tFn("settings.sectionAppearance")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("editor-word-wrap", "settings.editorWordWrap", "settings.editorWordWrapDesc", s.editor_word_wrap, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-minimap", "settings.editorMinimap", "settings.editorMinimapDesc", s.editor_minimap, false)}
          <div class="settings-item-divider"></div>
          ${dd("dd-editor-render-whitespace", "editor_render_whitespace", whitespaceOptions, str("editor_render_whitespace", "selection"), "settings.editorRenderWhitespace", "settings.editorRenderWhitespaceDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-font-ligatures", "settings.editorFontLigatures", "settings.editorFontLigaturesDesc", s.editor_font_ligatures, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-sticky-scroll", "settings.editorStickyScroll", "settings.editorStickyScrollDesc", s.editor_sticky_scroll, true)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-folding", "settings.editorFolding", "settings.editorFoldingDesc", s.editor_folding, true)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-glyph-margin", "settings.editorGlyphMargin", "settings.editorGlyphMarginDesc", s.editor_glyph_margin, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-rulers", "settings.editorRulers", "settings.editorRulersDesc", s.editor_rulers, true)}
        </div>

        <div class="settings-section-title"><i data-lucide="mouse-pointer-click" class="lucide section-title-icon"></i> ${tFn("settings.sectionBehavior")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("editor-cursor-blink", "settings.editorCursorBlink", "settings.editorCursorBlinkDesc", s.editor_cursor_blink, true)}
          <div class="settings-item-divider"></div>
          ${dd("dd-editor-cursor-style", "editor_cursor_style", [
            { id: "line", label: tFn("settings.optCursorLine") },
            { id: "line-thin", label: tFn("settings.optCursorLineThin") },
            { id: "underline", label: tFn("settings.optCursorUnderline") },
            { id: "underline-thin", label: tFn("settings.optCursorUnderlineThin") },
            { id: "block", label: tFn("settings.optCursorBlock") },
            { id: "block-outline", label: tFn("settings.optCursorBlockOutline") },
          ], str("editor_cursor_style", "line"), "settings.editorCursorStyle", "settings.editorCursorStyleDesc")}
          <div class="settings-item-divider"></div>
          ${dd("dd-editor-cursor-blinking", "editor_cursor_blinking", [
            { id: "smooth", label: tFn("settings.optBlinkSmooth") },
            { id: "phase", label: tFn("settings.optBlinkPhase") },
            { id: "solid", label: tFn("settings.optBlinkSolid") },
            { id: "expand", label: tFn("settings.optBlinkExpand") },
            { id: "blink", label: tFn("settings.optBlinkBlink") },
          ], str("editor_cursor_blinking", "smooth"), "settings.editorCursorBlinking", "settings.editorCursorBlinkingDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-format-on-paste", "settings.editorFormatOnPaste", "settings.editorFormatOnPasteDesc", s.editor_format_on_paste, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("editor-auto-save", "settings.editorAutoSave", "settings.editorAutoSaveDesc", s.editor_auto_save, false)}
        </div>`;
    } else if (kind === "review") {
      title = tFn("settings.reviewPanel");
      bodyHtml = `
        <div class="settings-section-title"><i data-lucide="settings" class="lucide section-title-icon"></i> ${tFn("settings.basicSettings")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("review-split-view", "settings.reviewSplitView", "settings.reviewSplitViewDesc", s.review_split_view, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("review-collapse-unified", "settings.reviewCollapseUnified", "settings.reviewCollapseUnifiedDesc", s.review_collapse_unified, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("review-word-diff", "settings.reviewWordDiff", "settings.reviewWordDiffDesc", s.review_word_diff, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("review-hide-whitespace", "settings.reviewHideWhitespace", "settings.reviewHideWhitespaceDesc", s.review_hide_whitespace, false)}
        </div>

        <div class="settings-section-title"><i data-lucide="wrap-text" class="lucide section-title-icon"></i> ${tFn("settings.sectionDiff")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("review-wrap", "settings.reviewWrap", "settings.reviewWrapDesc", s.review_wrap, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("review-full-file", "settings.reviewFullFile", "settings.reviewFullFileDesc", s.review_full_file, true)}
          <div class="settings-item-divider"></div>
          ${this._assistNumberRow("review-max-lines", "settings.reviewMaxLines", "settings.reviewMaxLinesDesc", Settings._assistNum(s.review_max_lines, 4000), 200, 100000, 100)}
          <div class="settings-item-divider"></div>
          ${dd("dd-review-default-filter", "review_default_filter", [
            { id: "all", label: tFn("settings.optDiffAll") },
            { id: "unstaged", label: tFn("settings.optDiffUnstaged") },
            { id: "staged", label: tFn("settings.optDiffStaged") },
            { id: "branch", label: tFn("settings.optDiffBranch") },
            { id: "lastRound", label: tFn("settings.optDiffLastRound") },
          ], str("review_default_filter", "all"), "settings.reviewDefaultFilter", "settings.reviewDefaultFilterDesc")}
        </div>`;
    } else {
      title = tFn("settings.browserPanel");
      const currentSearchEngine = (s.default_search_engine as string) || "bing";
      const searchEngineOptions: DropdownOption[] = SEARCH_ENGINES.map(e => ({ id: e.id, label: e.name, icon: searchEngineIconSrc(e.id) }));
      bodyHtml = `
        <div class="settings-section-title"><i data-lucide="settings" class="lucide section-title-icon"></i> ${tFn("settings.basicSettings")}</div>
        <div class="settings-card">
          <div class="settings-item-row">
            <div class="settings-item-info">
              <div class="settings-item-title">${tFn("settings.defaultSearchEngine")}</div>
              <div class="settings-item-desc">${tFn("settings.defaultSearchEngineDesc")}</div>
            </div>
            <div class="settings-item-control">
              ${this.renderDropdown("dd-search-engine", searchEngineOptions, currentSearchEngine, () => {})}
            </div>
          </div>
          <div class="settings-item-divider"></div>
          ${dd("dd-browser-homepage", "browser_homepage", [
            { id: "search", label: tFn("settings.optFollowEngine") },
            { id: "about:blank", label: tFn("settings.optBlankPage") },
          ], str("browser_homepage", "search"), "settings.browserHomepage", "settings.browserHomepageDesc")}
          <div class="settings-item-divider"></div>
          <div class="settings-item-row">
            <div class="settings-item-info">
              <div class="settings-item-title">${tFn("settings.browserData")}</div>
              <div class="settings-item-desc">${tFn("settings.browserDataDesc")}</div>
            </div>
            <div class="settings-item-control">
              <button class="btn-icon btn-icon--danger" id="browser-clear-data" data-tooltip="${tFn("settings.clear")}"><i data-lucide="trash-2" class="lucide"></i></button>
            </div>
          </div>
        </div>

        <div class="settings-section-title"><i data-lucide="shield" class="lucide section-title-icon"></i> ${tFn("settings.sectionPrivacy")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("browser-record-history", "settings.browserRecordHistory", "settings.browserRecordHistoryDesc", s.browser_record_history, true)}
          <div class="settings-item-divider"></div>
          ${dd("dd-browser-history-limit", "browser_history_limit", [
            { id: "500", label: "500" },
            { id: "2000", label: "2000" },
            { id: "5000", label: "5000" },
            { id: "20000", label: "20000" },
          ], String(Settings._assistNum(s.browser_history_limit, 5000)), "settings.browserHistoryLimit", "settings.browserHistoryLimitDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("browser-do-not-track", "settings.browserDoNotTrack", "settings.browserDoNotTrackDesc", s.browser_do_not_track, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("browser-block-third-party-cookies", "settings.browserBlockThirdPartyCookies", "settings.browserBlockThirdPartyCookiesDesc", s.browser_block_third_party_cookies, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("browser-deny-permissions", "settings.browserDenyPermissions", "settings.browserDenyPermissionsDesc", s.browser_deny_site_permissions, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("browser-block-popups", "settings.browserBlockPopups", "settings.browserBlockPopupsDesc", s.browser_block_popups, false)}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("browser-fresh-session", "settings.browserFreshSession", "settings.browserFreshSessionDesc", s.browser_fresh_session, false)}
        </div>

        <div class="settings-section-title"><i data-lucide="key-round" class="lucide section-title-icon"></i> ${tFn("settings.sectionPasswords")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("browser-autosave-credentials", "settings.browserAutosave", "settings.browserAutosaveDesc", s.browser_autosave_credentials, true)}
        </div>

        <div class="settings-section-title"><i data-lucide="download" class="lucide section-title-icon"></i> ${tFn("settings.sectionDownloads")}</div>
        <div class="settings-card">
          ${this._assistToggleRow("browser-ask-download", "settings.browserAskDownload", "settings.browserAskDownloadDesc", s.browser_ask_before_download, true)}
          <div class="settings-item-divider"></div>
          ${this._assistPathRow("browser-download-dir", "settings.browserDownloadDir", "settings.browserDownloadDirDesc", str("browser_download_dir", "default"))}
        </div>

        <div class="settings-section-title"><i data-lucide="palette" class="lucide section-title-icon"></i> ${tFn("settings.sectionAppearance")}</div>
        <div class="settings-card">
          ${dd("dd-browser-zoom", "browser_zoom_percent", [
            { id: "75", label: "75%" },
            { id: "90", label: "90%" },
            { id: "110", label: "110%" },
            { id: "125", label: "125%" },
            { id: "150", label: "150%" },
            { id: "100", label: "100%" },
          ], String(Settings._assistNum(s.browser_zoom_percent, 75)), "settings.browserZoom", "settings.browserZoomDesc")}
          <div class="settings-item-divider"></div>
          ${this._assistToggleRow("browser-force-light", "settings.browserForceLight", "settings.browserForceLightDesc", s.browser_force_light_page, true)}
          <div class="settings-item-divider"></div>
          ${dd("dd-browser-identity", "browser_identity", [
            { id: "desktop", label: tFn("settings.optDesktop") },
            { id: "mobile", label: tFn("settings.optPhone") },
          ], str("browser_identity", "desktop"), "settings.browserIdentity", "settings.browserIdentityDesc")}
        </div>

        <div class="settings-section-title"><i data-lucide="refresh-cw" class="lucide section-title-icon"></i> ${tFn("settings.importExport")}</div>
        <div class="settings-card">
          <div class="settings-item-row">
            <div class="settings-item-info">
              <div class="settings-item-title">${tFn("settings.importBrowserData")}</div>
              <div class="settings-item-desc">${tFn("settings.importBrowserDataDesc")}</div>
            </div>
            <div class="settings-item-control">
              <button class="btn-icon" id="browser-import-btn" data-tooltip="${tFn("settings.import")}"><i data-lucide="arrow-down-right" class="lucide"></i></button>
            </div>
          </div>
          <div class="settings-item-divider"></div>
          <div class="settings-item-row">
            <div class="settings-item-info">
              <div class="settings-item-title">${tFn("settings.exportAllBrowserData")}</div>
              <div class="settings-item-desc">${tFn("settings.exportAllBrowserDataDesc")}</div>
            </div>
            <div class="settings-item-control">
              <button class="btn-icon" id="browser-export-all" data-tooltip="${tFn("settings.export")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
            </div>
          </div>
        </div>`;
    }

    // The surface opens as a model-edit-style sub-page card: the same
    // container grammar as the per-model detail view inside the provider
    // dialog — a floating card over the assist panel, top-bar back arrow,
    // footer hidden (every control persists through `saveSetting` the moment
    // it changes, so Cancel/Confirm would be dead weight).
    const panel = this.panels.assist;
    if (!panel) return;
    const pageTitle = tFn("settings.assistEditTitle", { name: title });
    const { overlay } = this._showPanelEditor("assist", panel, pageTitle, bodyHtml, true);
    const back = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
    if (back) back.style.display = "";
    const footer = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    if (footer) footer.style.display = "none";
    // Assist-style editors save instantly: re-baseline on every edit so the
    // shared dirty check never fires and the back arrow closes straight away.
    this._keepPanelEditorClean(overlay);

    const root = overlay;
    this._bindAssistPanelControls(root);
    dropdowns.forEach(([id, cb]) => this.bindDropdown(id, cb));

    if (kind === "browser") {
      this.bindDropdown("dd-search-engine", (v) => {
        this.saveSetting("default_search_engine", v);
        const engine = SEARCH_ENGINES.find(e => e.id === v);
        if (engine) this.saveSetting("default_search_engine_url", engine.searchUrl);
      });

      root.querySelector("#browser-clear-data")?.addEventListener("click", async () => {
        const ok = await Dialog.confirm(t("settings.confirmClearBrowserDataTitle"), t("settings.confirmClearBrowserData"), { priority: "high", scope: "app" });
        if (ok) window.electronAPI?.browserClearData?.();
      });
      root.querySelector("#browser-import-btn")?.addEventListener("click", () => { void this._openBrowserImportSubpage(); });
      root.querySelector("#browser-export-all")?.addEventListener("click", () => this._exportAllBrowserData());

      // The folder is picked, never typed, so an invalid path cannot be saved.
      // Nothing displays until a folder is picked; the box then shows the path.
      root.querySelector("#browser-download-dir-pick")?.addEventListener("click", async () => {
        const dir = await window.electronAPI?.pickDirectory?.();
        if (!dir) return;
        this.saveSetting("browser_download_dir", dir);
        const box = root.querySelector<HTMLElement>("#browser-download-dir");
        if (box) {
          box.dataset.empty = "0";
          box.title = dir;
          const text = box.querySelector<HTMLElement>(".settings-path-text");
          if (text) text.textContent = dir;
        }
      });
    }
  }

  /** The Assist Panels landing list: one "Data Center" control per auxiliary
   *  surface (Terminal / Explorer / Review / Browser), copied from the storage
   *  page — title + description on the left, a persistent top-right
   *  `btn-icon` arrow on the right that opens the surface's editor card.
   *  The section header matches every other panel (usage, general, memory…):
   *  section title icon + label, and the gear button on the same row (same
   *  spot as the model panel's) opens the home-board tile manager. */
  private renderAssistPanel(): void {
    // One editor per panel: while a surface card is open the landing list is
    // skipped so the open draft survives panel hops untouched.
    if (this._panelEditorActive("assist")) return;
    const panel = this.panels.assist;
    if (!panel) return;
    const surfaces: AssistKind[] = ["terminal", "editor", "review", "browser"];
    const rows = surfaces
      .map((kind) => `
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t(`settings.${kind}Panel`)}</div>
            <div class="settings-item-desc">${t(`settings.${kind}PanelDesc`)}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" data-kind="${kind}" data-tooltip="${t(`settings.${kind}Panel`)}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
          </div>
        </div>`)
      .join('<div class="settings-item-divider"></div>');
    panel.innerHTML = `
      <div class="settings-section-title">
        <i data-lucide="layout-dashboard" class="lucide section-title-icon"></i> ${t("sidebar.assist")}
        <button class="btn-icon" id="btn-assist-tiles-manage" data-tooltip="${t("settings.assistTilesManage")}" style="margin-inline-start:auto"><i data-lucide="settings" class="lucide"></i></button>
      </div>
      <div class="settings-card">${rows}</div>`;
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: panel });
    }
    panel.querySelector("#btn-assist-tiles-manage")?.addEventListener("click", () => this._openAssistTilesEditor());
    panel.querySelectorAll<HTMLButtonElement>(".btn-icon[data-kind]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const kind = btn.dataset.kind as AssistKind;
        if (kind) void this.renderAssistSurface(kind);
      });
    });
  }

  /** Working copy of the home-board tile list while the tile manager is
   *  open; null means no manager session (every change persists straight
   *  through `saveSetting("assist_tiles", ...)`). */
  private _tileCfg: AssistTileCfg[] | null = null;
  /** Tile (by id) currently selected on the preview board; its editor row
   *  is the only one shown below the board. */
  private _activeTileId: string | null = null;

  /** The home-board tile manager, opened as a model-edit-style sub-page card
   *  over the Assist Panels list: top-bar back arrow, no footer (every edit
   *  persists instantly), a live 3×3 preview board — HTML5 drag to move /
   *  swap tiles, hover a corner for the window-style resize cursor and drag
   *  to reshape the tile (1×1, 2×1, 1×2 or 2×2), click to select — and below
   *  it the selected tile's editor card only (name, icon grid, visibility,
   *  delete), plus add / reset actions in the section-title row. */
  private _openAssistTilesEditor(): void {
    const panel = this.panels.assist;
    if (!panel) return;
    this._tileCfg = parseAssistTiles((getState().settings as Record<string, unknown>)["assist_tiles"]);
    this._activeTileId = null;
    const title = t("settings.assistTilesTitle");
    const bodyHtml = this._renderAssistTilesBody();
    const { overlay } = this._showPanelEditor("assist", panel, title, bodyHtml, true);
    const back = overlay.querySelector(".panel-editor-back") as HTMLElement | null;
    if (back) back.style.display = "";
    const footer = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    if (footer) footer.style.display = "none";
    this._keepPanelEditorClean(overlay);
    this._bindAssistTilesControls(overlay);
  }

  /** Row markup of the tile manager card: a live drag-and-drop preview board
   *  (always the free 3×3 grid, same packing rules as the real home page) on
   *  top, and below it the editor card of the currently selected tile only
   *  (click a tile on the board to select it).  Rebuilt after every change. */
  private _renderAssistTilesBody(): string {
    const cfg = this._tileCfg ?? [];
    const activeIdx = this._activeTileId ? cfg.findIndex((tl) => tl.id === this._activeTileId) : -1;

    // Selected tile's editor card — nothing else, per the interaction design:
    // click a card on the board, then edit just that card below.
    let listHtml: string;
    if (activeIdx >= 0) {
      const tl = cfg[activeIdx];
      const iconCells = ASSIST_TILE_ICONS.map((ic) =>
        `<button type="button" class="assist-icon-cell${ic === tl.icon ? " assist-icon-cell--on" : ""}" data-icon="${ic}"><i data-lucide="${ic}" class="lucide"></i></button>`).join("");
      listHtml = `
      <div class="settings-card assist-tile-editor" data-idx="${activeIdx}">
        <div class="assist-tile-editor-head">
          <span class="assist-tile-preview"><i data-lucide="${this.esc(tl.icon)}" class="lucide"></i></span>
          <span class="assist-tile-editor-name">${this.esc(tl.label || tl.id)}</span>
        </div>
        <div class="settings-item-row">
          <div class="settings-item-info"><div class="settings-item-title">${t("settings.assistTileNameLabel")}</div></div>
          <div class="settings-item-control"><input type="text" class="settings-text-input assist-tile-label" data-tile-action="label" value="${this.esc(tl.label || tl.id)}" placeholder="${this.esc(tl.id)}" spellcheck="false" /></div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info"><div class="settings-item-title">${t("settings.assistTileIconLabel")}</div></div>
          <div class="settings-item-control"><div class="assist-icon-grid">${iconCells}</div></div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info"><div class="settings-item-title">${t("settings.assistTileVisibleLabel")}</div></div>
          <div class="settings-item-control">
            <label class="toggle-switch"><input type="checkbox" data-tile-action="visible" ${tl.hidden ? "" : "checked"} /><span class="toggle-slider"></span></label>
          </div>
        </div>
        ${DEFAULT_TILE_IDS.includes(tl.id) ? "" : `
        <div class="settings-item-divider"></div>
        <div class="assist-tile-editor-foot">
          <button class="btn-icon btn-icon--danger" data-tile-action="delete" data-tooltip="${t("settings.assistTileDelete")}"><i data-lucide="trash-2" class="lucide"></i></button>
        </div>`}
      </div>`;
    } else {
      listHtml = `<div class="assist-tile-hint">${t("settings.assistTilesSelectHint")}</div>`;
    }

    // Preview board — always 3×3, mirrors the home page's free-grid packing.
    // Hidden tiles stay on their slot, greyed out (they only vanish from the
    // real home board) so they can be re-enabled from here.
    const { placed } = layoutHomeTiles(cfg, { includeHidden: true });
    const p0 = placed.filter((p) => p.page === 0);
    const mainCell = (p: { r: number; c: number }) => p.r * 3 + p.c;
    const occupied = new Set<number>(p0.flatMap((p) => this._spanCells(p.r, p.c, p.w, p.h)));
    const tileBtn = (p: (typeof p0)[number]) => {
      const idx = cfg.indexOf(p.tile);
      const active = cfg[idx]?.id === this._activeTileId ? " assist-tile--active" : "";
      const hiddenCls = p.tile.hidden ? " assist-tile--hidden" : "";
      return `
      <button type="button" class="si-grid-tile assist-tile${active}${hiddenCls}" draggable="true" data-idx="${idx}" data-cell="${mainCell(p)}" data-w="${p.w}" data-h="${p.h}"
        style="grid-area:${p.r + 1} / ${p.c + 1} / span ${p.h} / span ${p.w}">
        <i data-lucide="${this.esc(p.tile.icon)}" class="lucide si-grid-icon"></i>
        <span class="si-grid-label">${this.esc(p.tile.label || p.tile.id)}</span>
      </button>`;
    };
    const tilesHtml = p0.map((p) => tileBtn(p)).join("")
      + [0, 1, 2, 3, 4, 5, 6, 7, 8].filter((i) => !occupied.has(i))
        .map((i) => `<div class="si-grid-empty assist-drop" data-cell="${i}" style="grid-area:${Math.floor(i / 3) + 1} / ${(i % 3) + 1}"></div>`).join("");
    const overflow = placed.length - p0.length;
    const note = overflow > 0
      ? `<div class="assist-board-note">${t("settings.assistBoardOverflow").replace("{n}", String(overflow))}</div>`
      : "";

    return `
      <div class="settings-section-title">
        <i data-lucide="layout-dashboard" class="lucide section-title-icon"></i> ${t("settings.assistTilesTitle")}
        <button class="btn-icon" id="assist-tiles-add" data-tooltip="${t("settings.assistTileAdd")}" style="margin-inline-start:auto"><i data-lucide="plus" class="lucide"></i></button>
        <button class="btn-icon" id="assist-tiles-reset" data-tooltip="${t("settings.assistTileReset")}"><i data-lucide="rotate-ccw" class="lucide"></i></button>
      </div>
      <div class="model-manage-desc">${t("settings.assistTilesDesc")}</div>
      <div class="assist-board-wrap">
        <div class="si-grid si-grid--free assist-board" id="assist-board">${tilesHtml}</div>
      </div>
      ${note}
      ${listHtml}`;
  }

  /** Wire the tile manager: header actions, the drag-and-drop preview board
   *  (HTML5 drag to move / swap, hover-corner + drag to resize with a live
   *  preview, click to select) and the selected tile's editor card.  Called
   *  again after every structural rebuild. */
  private _bindAssistTilesControls(overlay: HTMLElement): void {
    const persist = () => {
      if (this._tileCfg) this.saveSetting("assist_tiles", JSON.stringify(this._tileCfg));
    };
    const repaint = () => {
      // Rebuild the whole editor body (the section title included, so the
      // add / reset buttons stay wired through one path).
      const editorBody = overlay.querySelector(".dialog-body") as HTMLElement | null;
      if (editorBody) editorBody.innerHTML = this._renderAssistTilesBody();
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: overlay });
      }
      this._bindAssistTilesControls(overlay);
      this._keepPanelEditorClean(overlay);
    };
    // Header actions
    overlay.querySelector("#assist-tiles-add")?.addEventListener("click", () => {
      if (!this._tileCfg) return;
      const tile: AssistTileCfg = { id: `custom-${Date.now().toString(36)}`, icon: "files", label: t("settings.assistTileNewName"), size: "s" };
      this._tileCfg.push(tile);
      this._activeTileId = tile.id;
      persist();
      repaint();
    });
    overlay.querySelector("#assist-tiles-reset")?.addEventListener("click", () => {
      this._tileCfg = defaultHomeTiles();
      this._activeTileId = null;
      persist();
      repaint();
    });

    // ---- Preview board ----
    const board = overlay.querySelector("#assist-board") as HTMLElement | null;
    if (board) {
      let dragIdx = -1;
      board.querySelectorAll<HTMLElement>(".assist-tile").forEach((el) => {
        const idx = parseInt(el.dataset.idx || "-1", 10);
        // Click a tile to select it for editing (its row appears below);
        // clicking the active tile again deselects it and collapses the form.
        el.addEventListener("click", () => {
          const id = this._tileCfg?.[idx]?.id ?? null;
          const next = id === this._activeTileId ? null : id;
          if (next !== this._activeTileId) {
            this._activeTileId = next;
            repaint();
          }
        });
        // HTML5 drag to move / swap.
        el.addEventListener("dragstart", (e: DragEvent) => {
          dragIdx = idx;
          el.classList.add("assist-tile--dragging");
          if (e.dataTransfer) {
            e.dataTransfer.setData("text/plain", String(dragIdx));
            e.dataTransfer.effectAllowed = "move";
          }
        });
        el.addEventListener("dragend", () => {
          el.classList.remove("assist-tile--dragging");
          this._clearBoardHover(board);
          dragIdx = -1;
        });
        // Window-style resize without visible handles: hovering near a tile
        // corner switches the cursor to the diagonal resize arrow, and drag
        // from there reshapes the tile live — 1×1, horizontal 2×1, vertical
        // 1×2 or square 2×2, always grid-locked.  The grabbed corner follows
        // the pointer while the opposite corner stays anchored; the tile
        // itself is the only preview (no dashed ghost outlines).
        const cornerAt = (ev: PointerEvent): "tl" | "tr" | "bl" | "br" | null => {
          const rect = el.getBoundingClientRect();
          const x = ev.clientX - rect.left;
          const y = ev.clientY - rect.top;
          const z = Math.min(20, Math.max(12, rect.width * 0.22));
          const left = x <= z;
          const right = x >= rect.width - z;
          const top = y <= z;
          const bottom = y >= rect.height - z;
          if (left && top) return "tl";
          if (right && top) return "tr";
          if (left && bottom) return "bl";
          if (right && bottom) return "br";
          return null;
        };
        const RESIZE_CURSORS: Record<"tl" | "tr" | "bl" | "br", string> = {
          tl: "nwse-resize", br: "nwse-resize", tr: "nesw-resize", bl: "nesw-resize",
        };
        let resizing = false;
        el.addEventListener("pointermove", (ev) => {
          if (resizing || dragIdx >= 0) return;
          const corner = cornerAt(ev);
          el.style.cursor = corner ? RESIZE_CURSORS[corner] : "";
        });
        el.addEventListener("pointerleave", () => {
          if (!resizing) el.style.cursor = "";
        });
        el.addEventListener("pointerdown", (ev) => {
          const corner = cornerAt(ev);
          if (!corner || dragIdx >= 0) return;
          const cell0 = parseInt(el.dataset.cell || "-1", 10);
          if (cell0 < 0) return;
          ev.preventDefault();
          ev.stopPropagation();
          el.draggable = false; // keep the resize drag from starting a tile move
          resizing = true;
          const origArea = el.style.gridArea;
          // Anchor = the tile corner opposite the grabbed one; the block
          // grows / shrinks between the pointer and this fixed point.
          const r0 = Math.floor(cell0 / 3);
          const c0 = cell0 % 3;
          const w0 = parseInt(el.dataset.w || "1", 10) || 1;
          const h0 = parseInt(el.dataset.h || "1", 10) || 1;
          const ar = corner === "tl" || corner === "tr" ? r0 + h0 - 1 : r0;
          const ac = corner === "tl" || corner === "bl" ? c0 + w0 - 1 : c0;
          let target: { r: number; c: number; w: number; h: number } | null = null;
          const move = (me: PointerEvent) => {
            // Clamp the pointer into the board so dragging past the edge
            // extends the block to its maximum instead of cancelling.
            const rect = board!.getBoundingClientRect();
            const pc = Math.min(2, Math.max(0, Math.floor(((me.clientX - rect.left) / rect.width) * 3)));
            const pr = Math.min(2, Math.max(0, Math.floor(((me.clientY - rect.top) / rect.height) * 3)));
            const shape = this._resizeShape(corner, ar, ac, pr, pc);
            if (shape && this._tileFits(idx, shape.r, shape.c, shape.w, shape.h)) {
              target = shape;
              el.style.gridArea = `${shape.r + 1} / ${shape.c + 1} / span ${shape.h} / span ${shape.w}`;
            } else {
              target = null;
              el.style.gridArea = origArea;
            }
          };
          const finish = () => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", finish);
            window.removeEventListener("pointercancel", finish);
            resizing = false;
            el.draggable = true;
            el.style.cursor = "";
            if (target && this._applyResizeShape(idx, target)) {
              persist();
              repaint();
            } else {
              el.style.gridArea = origArea;
            }
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", finish);
          window.addEventListener("pointercancel", finish);
        });
      });
      board.addEventListener("dragover", (e: DragEvent) => {
        if (dragIdx < 0) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
        this._highlightBoardCell(board, e);
      });
      board.addEventListener("dragleave", (e: DragEvent) => {
        if ((e.target as HTMLElement) === board) this._clearBoardHover(board);
      });
      board.addEventListener("drop", (e: DragEvent) => {
        e.preventDefault();
        this._clearBoardHover(board);
        if (dragIdx < 0) return;
        const cell = this._boardCellAt(board, e.clientX, e.clientY);
        if (cell < 0) {
          dragIdx = -1;
          return;
        }
        if (this._applyBoardDrop(dragIdx, cell)) {
          // The drop only mutates the working copy (and persists it); the
          // manager board itself must be rebuilt here, or the tile keeps its
          // old grid-area and visually snaps back to where it came from.
          repaint();
        } else {
          showToast(t("settings.assistBoardNoFit"), "", "info");
        }
        dragIdx = -1;
      });
    }

    // ---- Selected tile's editor card ----
    overlay.querySelectorAll<HTMLElement>(".assist-tile-editor").forEach((card) => {
      const idx = parseInt(card.dataset.idx || "-1", 10);
      if (idx < 0 || !this._tileCfg || idx >= this._tileCfg.length) return;
      // Name: persist per keystroke and mirror the text onto the board tile
      // and the card head without a repaint (a repaint would drop focus).
      card.querySelector<HTMLInputElement>('[data-tile-action="label"]')?.addEventListener("input", () => {
        if (!this._tileCfg || !this._tileCfg[idx]) return;
        const value = card.querySelector<HTMLInputElement>('[data-tile-action="label"]')!.value;
        this._tileCfg[idx].label = value;
        persist();
        const shown = value || this._tileCfg[idx].id;
        if (board) {
          const tileLabel = board.querySelector<HTMLElement>(`.assist-tile[data-idx="${idx}"] .si-grid-label`);
          if (tileLabel) tileLabel.textContent = shown;
        }
        const head = card.querySelector<HTMLElement>(".assist-tile-editor-name");
        if (head) head.textContent = shown;
      });
      // Icon: click-to-pick grid.
      card.querySelectorAll<HTMLElement>(".assist-icon-cell").forEach((cell) => {
        cell.addEventListener("click", () => {
          if (!this._tileCfg || !this._tileCfg[idx]) return;
          this._tileCfg[idx].icon = cell.dataset.icon || this._tileCfg[idx].icon;
          persist();
          repaint();
        });
      });
      // Visibility on the home board.
      card.querySelector<HTMLInputElement>('[data-tile-action="visible"]')?.addEventListener("change", () => {
        if (!this._tileCfg || !this._tileCfg[idx]) return;
        this._tileCfg[idx].hidden = !card.querySelector<HTMLInputElement>('[data-tile-action="visible"]')!.checked;
        persist();
        repaint();
      });
      // Delete (custom tiles only).
      card.querySelector<HTMLButtonElement>('[data-tile-action="delete"]')?.addEventListener("click", () => {
        if (!this._tileCfg) return;
        if (this._tileCfg[idx]?.id === this._activeTileId) this._activeTileId = null;
        this._tileCfg.splice(idx, 1);
        persist();
        repaint();
      });
    });
  }

  /** Cell index (0..8) under the pointer on the preview board, or -1. */
  private _boardCellAt(board: HTMLElement, x: number, y: number): number {
    const rect = board.getBoundingClientRect();
    const col = Math.floor(((x - rect.left) / rect.width) * 3);
    const row = Math.floor(((y - rect.top) / rect.height) * 3);
    if (col < 0 || col > 2 || row < 0 || row > 2) return -1;
    return row * 3 + col;
  }

  /** Highlight whichever cell (tile main cell or empty slot) sits under the
   *  dragged tile. */
  private _highlightBoardCell(board: HTMLElement, e: DragEvent): void {
    this._clearBoardHover(board);
    const cell = this._boardCellAt(board, e.clientX, e.clientY);
    if (cell < 0) return;
    board.querySelector<HTMLElement>(`[data-cell="${cell}"]`)?.classList.add("assist-drop-hover");
  }

  private _clearBoardHover(board: HTMLElement): void {
    board.querySelectorAll(".assist-drop-hover").forEach((el) => el.classList.remove("assist-drop-hover"));
  }

  /** Pin every page-0 tile (hidden ones included — the manager lays them
   *  out greyed out) to the main cell it currently occupies, so a later
   *  layout run can never reshuffle tiles the user did not touch.  Call it
   *  BEFORE mutating the dragged / resized tile, so the pin records where
   *  everything else already sits.  Tiles overflowing to later pages stay
   *  unpinned (their cells are page-0 coordinates and they may legally flow
   *  back into freed page-0 space). */
  private _pinTileCells(): void {
    const cfg = this._tileCfg;
    if (!cfg) return;
    const { placed } = layoutHomeTiles(cfg, { includeHidden: true });
    for (const p of placed) {
      if (p.page === 0) p.tile.cell = p.r * 3 + p.c;
    }
  }

  /** Drop a dragged tile onto cell `cell` of the preview board (always the
   *  free 3×3 grid).  Returns false (data untouched) when the move cannot
   *  fit.  Landing on another tile swaps their main cells — only when both
   *  tiles keep their own shape inside the grid and clear of every other
   *  tile; landing on an empty cell claims it with the tile's own shape. */
  private _applyBoardDrop(srcIdx: number, cell: number): boolean {
    const cfg = this._tileCfg;
    if (!cfg || srcIdx < 0 || srcIdx >= cfg.length) return true;
    const src = cfg[srcIdx];
    const { placed } = layoutHomeTiles(cfg, { includeHidden: true });
    const p0 = placed.filter((p) => p.page === 0);
    const srcPlaced = p0.find((p) => p.tile === src);
    const covered = p0.find((p) => this._spanCells(p.r, p.c, p.w, p.h).includes(cell));
    const occupied = new Set<number>(p0.filter((p) => p.tile !== src).flatMap((p) =>
      this._spanCells(p.r, p.c, p.w, p.h)));
    if (!covered || covered.tile === src) {
      // Landing on an empty cell (or on itself).
      const w = srcPlaced?.w ?? 1;
      const h = srcPlaced?.h ?? 1;
      const r = Math.floor(cell / 3);
      const c = cell % 3;
      if (r + h > 3 || c + w > 3) return false;
      if (!this._spanCells(r, c, w, h).every((ix) => !occupied.has(ix))) return false;
      // Pin everyone else at their current spot FIRST: for a legacy config
      // that never had explicit cells the array order can differ from the
      // visual layout, and pinning after the move would reshuffle them.
      this._pinTileCells();
      src.cell = cell;
      this.saveSetting("assist_tiles", JSON.stringify(cfg));
      return true;
    }
    // Landing on another tile: swap main cells, validating both sides — an
    // illegal swap would write a pin that cannot be honored and reshuffle
    // untouched tiles on the next layout run.
    const dst = covered.tile;
    const srcCell = src.cell ?? (srcPlaced ? srcPlaced.r * 3 + srcPlaced.c : undefined);
    const dstCell = dst.cell ?? covered.r * 3 + covered.c;
    if (srcCell == null || dstCell == null) return false;
    const others = new Set<number>(p0.filter((p) => p.tile !== src && p.tile !== dst).flatMap((p) =>
      this._spanCells(p.r, p.c, p.w, p.h)));
    const srcAtDst = { r: Math.floor(dstCell / 3), c: dstCell % 3, w: srcPlaced?.w ?? 1, h: srcPlaced?.h ?? 1 };
    const dstAtSrc = { r: Math.floor(srcCell / 3), c: srcCell % 3, w: covered.w, h: covered.h };
    if (srcAtDst.r + srcAtDst.h > 3 || srcAtDst.c + srcAtDst.w > 3) return false;
    if (dstAtSrc.r + dstAtSrc.h > 3 || dstAtSrc.c + dstAtSrc.w > 3) return false;
    const aCells = this._spanCells(srcAtDst.r, srcAtDst.c, srcAtDst.w, srcAtDst.h);
    const bCells = this._spanCells(dstAtSrc.r, dstAtSrc.c, dstAtSrc.w, dstAtSrc.h);
    if (aCells.some((ix) => others.has(ix) || bCells.includes(ix))) return false;
    if (bCells.some((ix) => others.has(ix))) return false;
    this._pinTileCells();
    src.cell = dstCell;
    dst.cell = srcCell;
    this.saveSetting("assist_tiles", JSON.stringify(cfg));
    return true;
  }

  /** Shape (top-left cell + spans) of the block when the grabbed tile corner
   *  follows the pointer cell (pr, pc) while the opposite corner stays
   *  anchored at (ar, ac).  Spans clamp to 1..3 (the board edge). */
  private _resizeShape(
    corner: "tl" | "tr" | "bl" | "br", ar: number, ac: number, pr: number, pc: number,
  ): { r: number; c: number; w: number; h: number } {
    // Spans may reach the board edge (3) in either dimension — the anchor
    // fixes the opposite corner, the pointer drags the grabbed one.  Overlap
    // is rejected by the live fit check, not by an arbitrary size cap.
    const wMax = corner === "tl" || corner === "bl" ? ac + 1 : 3 - ac;
    const hMax = corner === "tl" || corner === "tr" ? ar + 1 : 3 - ar;
    const rawW = corner === "tl" || corner === "bl" ? ac - pc + 1 : pc - ac + 1;
    const rawH = corner === "tl" || corner === "tr" ? ar - pr + 1 : pr - ar + 1;
    const w = Math.min(wMax, Math.max(1, rawW));
    const h = Math.min(hMax, Math.max(1, rawH));
    return {
      r: corner === "tl" || corner === "tr" ? ar - h + 1 : ar,
      c: corner === "tl" || corner === "bl" ? ac - w + 1 : ac,
      w,
      h,
    };
  }

  /** Grid cells (page-0 indices) covered by a block with its top-left at
   *  (r, c) and spans (w, h). */
  private _spanCells(r: number, c: number, w: number, h: number): number[] {
    const out: number[] = [];
    for (let i = 0; i < h; i++) {
      for (let j = 0; j < w; j++) out.push((r + i) * 3 + (c + j));
    }
    return out;
  }

  /** Whether a block with its top-left at (r, c) and spans (w, h) can host
   *  tile `idx`: inside the 3×3 grid, with no other page-0 tile overlapping
   *  the block. */
  private _tileFits(idx: number, r: number, c: number, w: number, h: number): boolean {
    const cfg = this._tileCfg;
    if (!cfg || idx < 0 || idx >= cfg.length) return false;
    if (r < 0 || c < 0 || r + h > 3 || c + w > 3) return false;
    const tl = cfg[idx];
    const { placed } = layoutHomeTiles(cfg, { includeHidden: true });
    const occupied = new Set<number>(placed.filter((x) => x.tile !== tl && x.page === 0).flatMap((x) =>
      this._spanCells(x.r, x.c, x.w, x.h)));
    return this._spanCells(r, c, w, h).every((ix) => !occupied.has(ix));
  }

  /** Commit a corner-drag resize: the tile takes the dragged shape.  The
   *  size code derives from the spans (2×1 → "w", 1×2 → "h", 2×2 → "m",
   *  else "s").  Returns false (data untouched) when the shape cannot fit. */
  private _applyResizeShape(idx: number, shape: { r: number; c: number; w: number; h: number }): boolean {
    const cfg = this._tileCfg;
    if (!cfg || idx < 0 || idx >= cfg.length) return false;
    if (!this._tileFits(idx, shape.r, shape.c, shape.w, shape.h)) return false;
    // Pin everyone else at their current spot before reshaping this tile
    // (same legacy-config reasoning as in _applyBoardDrop).  The exact spans
    // are stored explicitly — any rectangle up to 3×3 is a legal shape; the
    // legacy `size` bucket is only refreshed when the shape still matches one.
    this._pinTileCells();
    cfg[idx].w = shape.w;
    cfg[idx].h = shape.h;
    cfg[idx].size = shape.w === 2 && shape.h === 2 ? "m" : shape.w === 2 ? "w" : shape.h === 2 ? "h" : shape.w === 1 && shape.h === 1 ? "s" : cfg[idx].size;
    cfg[idx].cell = shape.r * 3 + shape.c;
    return true;
  }

  /** Reads an auxiliary-panel numeric setting.  Values arrive from the backend
   *  both as numbers and as strings (the WS `configure` path is stringly
   *  typed), so every read goes through the same coercion. */
  private static _assistNum(value: unknown, fallback: number): number {
    const n = typeof value === "number" ? value : parseInt(String(value ?? ""), 10);
    return Number.isFinite(n) ? n : fallback;
  }

  /** Same coercion for the fractional knobs (line height), where an integer
   *  parse would silently flatten 1.3 to 1. */
  private static _assistFloat(value: unknown, fallback: number): number {
    const n = typeof value === "number" ? value : parseFloat(String(value ?? ""));
    return Number.isFinite(n) ? n : fallback;
  }

  private _assistToggleRow(id: string, titleKey: string, descKey: string, value: unknown, def: boolean): string {
    const on = value === undefined ? def : isEnabled(value);
    return `
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t(titleKey)}</div>
            <div class="settings-item-desc">${t(descKey)}</div>
          </div>
          <div class="settings-item-control">
            <label class="toggle-switch">
              <input type="checkbox" id="${id}" ${on ? "checked" : ""} />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>`;
  }

  private _assistNumberRow(id: string, titleKey: string, descKey: string, value: number, min: number, max: number, step = 1): string {
    return `
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t(titleKey)}</div>
            <div class="settings-item-desc">${t(descKey)}</div>
          </div>
          <div class="settings-item-control">
            <input type="number" id="${id}" class="settings-text-input" min="${min}" max="${max}" step="${step}" value="${value}" />
          </div>
        </div>`;
  }

  /** A "pick one" row for the assist cards.  `renderDropdown` only returns
   *  markup -- the callback is wired by `bindDropdown` once the node is in the
   *  document -- so the caller collects `(id, key)` pairs and binds them after
   *  the card mounts. */
  private _assistDropdownRow(id: string, titleKey: string, descKey: string, options: DropdownOption[], current: string): string {
    return `
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t(titleKey)}</div>
            <div class="settings-item-desc">${t(descKey)}</div>
          </div>
          <div class="settings-item-control">
            ${this.renderDropdown(id, options, current, () => {})}
          </div>
        </div>`;
  }

  /** A free-text row.  Commits on every keystroke (the caller wires `input`),
   *  because these cards are instant-save -- there is no Save to press. */
  private _assistTextRow(id: string, titleKey: string, descKey: string, value: string): string {
    return `
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t(titleKey)}</div>
            <div class="settings-item-desc">${t(descKey)}</div>
          </div>
          <div class="settings-item-control settings-path-control">
            <input type="text" id="${id}" class="settings-text-input" value="${this.esc(value)}" spellcheck="false" />
          </div>
        </div>`;
  }

  /** A read-only path row with a folder picker.  The value is a filesystem
   *  path a user picks rather than types, so the field is never editable and
   *  only the button can write it.  The box stays empty until a folder has
   *  been picked — the OS default is deliberately not displayed. */
  private _assistPathRow(id: string, titleKey: string, descKey: string, value: string): string {
    const empty = value === "default" || value === "";
    return `
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t(titleKey)}</div>
            <div class="settings-item-desc">${t(descKey)}</div>
          </div>
          <div class="settings-item-control settings-path-control">
            <div class="settings-path-value" id="${id}" data-empty="${empty ? "1" : "0"}"${empty ? "" : ` title="${this.esc(value)}"`}>
              <span class="settings-path-text">${this.esc(empty ? t("settings.optNone") : value)}</span>
            </div>
            <button class="btn-icon" id="${id}-pick" data-tooltip="${t("settings.chooseFolder")}"><i data-lucide="folder-open" class="lucide"></i></button>
          </div>
        </div>`;
  }

  /** Wires the terminal / editor / review rows inside an assist editor card.
   *  Each control writes one `EncreConfig` field through the shared
   *  {@link saveSetting} path, so they persist alongside every other UI
   *  setting and are picked up by the session panels on their next open. */
  private _bindAssistPanelControls(root: HTMLElement): void {
    const toggles: Array<[string, string]> = [
      ["terminal-cursor-blink", "terminal_cursor_blink"],
      ["terminal-bold-bright", "terminal_bold_bright"],
      ["terminal-copy-on-select", "terminal_copy_on_select"],
      ["terminal-convert-eol", "terminal_convert_eol"],
      ["editor-word-wrap", "editor_word_wrap"],
      ["editor-minimap", "editor_minimap"],
      ["editor-line-numbers", "editor_line_numbers"],
      ["editor-detect-indentation", "editor_detect_indentation"],
      ["editor-cursor-blink", "editor_cursor_blink"],
      ["editor-font-ligatures", "editor_font_ligatures"],
      ["editor-sticky-scroll", "editor_sticky_scroll"],
      ["editor-auto-save", "editor_auto_save"],
      ["editor-use-spaces", "editor_use_spaces"],
      ["editor-folding", "editor_folding"],
      ["editor-glyph-margin", "editor_glyph_margin"],
      ["editor-format-on-paste", "editor_format_on_paste"],
      ["editor-rulers", "editor_rulers"],
      ["review-split-view", "review_split_view"],
      ["review-collapse-unified", "review_collapse_unified"],
      ["review-word-diff", "review_word_diff"],
      ["review-hide-whitespace", "review_hide_whitespace"],
      ["review-wrap", "review_wrap"],
      ["review-full-file", "review_full_file"],
      ["browser-record-history", "browser_record_history"],
      ["browser-do-not-track", "browser_do_not_track"],
      ["browser-deny-permissions", "browser_deny_site_permissions"],
      ["browser-block-third-party-cookies", "browser_block_third_party_cookies"],
      ["browser-block-popups", "browser_block_popups"],
      ["browser-fresh-session", "browser_fresh_session"],
      ["browser-ask-download", "browser_ask_before_download"],
      ["browser-force-light", "browser_force_light_page"],
      ["browser-autosave-credentials", "browser_autosave_credentials"],
    ];
    for (const [id, key] of toggles) {
      root.querySelector<HTMLElement>(`#${id}`)?.addEventListener("change", (e) => {
        this.saveSetting(key, (e.target as HTMLInputElement).checked ? "true" : "false");
      });
    }

    // `float` rows (line height) keep their decimals; the rest are integers.
    const numbers: Array<[string, string, number, number, boolean?]> = [
      ["terminal-font-size", "terminal_font_size", 9, 32],
      ["terminal-line-height", "terminal_line_height", 0.8, 2, true],
      ["terminal-scrollback", "terminal_scrollback", 1000, 200000],
      ["terminal-letter-spacing", "terminal_letter_spacing", -5, 10],
      ["terminal-min-contrast", "terminal_min_contrast", 1, 21, true],
      ["editor-font-size", "editor_font_size", 9, 32],
      ["editor-wrap-column", "editor_word_wrap_column", 20, 200],
      ["review-max-lines", "review_max_lines", 200, 100000, true],
    ];
    for (const [id, key, min, max, isFloat] of numbers) {
      const el = root.querySelector<HTMLInputElement>(`#${id}`);
      if (!el) continue;
      const commit = () => {
        const parsed = isFloat ? parseFloat(el.value) : parseInt(el.value, 10);
        if (!Number.isFinite(parsed)) {
          el.value = String(isFloat ? Settings._assistFloat(getState().settings[key], min) : Settings._assistNum(getState().settings[key], min));
          return;
        }
        const clamped = Math.min(max, Math.max(min, parsed));
        if (clamped !== parsed) el.value = String(clamped);
        this.saveSetting(key, String(clamped));
      };
      el.addEventListener("change", commit);
      // `change` alone waits for blur, which a +/- spinner never triggers.
      el.addEventListener("input", commit);
    }

    // Free-text rows commit per keystroke; an empty box falls back to the
    // documented default rather than persisting a meaningless "".
    const texts: Array<[string, string, string]> = [
      ["terminal-word-separator", "terminal_word_separator", DEFAULT_TERM_WORD_SEPARATORS],
    ];
    for (const [id, key, fallback] of texts) {
      const el = root.querySelector<HTMLInputElement>(`#${id}`);
      if (!el) continue;
      el.addEventListener("input", () => {
        const v = el.value;
        this.saveSetting(key, v === "" ? fallback : v);
      });
    }
  }

  /** The "Import browser data" sub-page, opened INSIDE the assist editor
   *  container (the same surface-swap grammar as the per-model detail view):
   *  a source dropdown lists the detected browsers and every importable data
   *  type renders as a checkable row, so one click batch-imports everything
   *  ticked.  The back arrow returns to the browser card. */
  private async _openBrowserImportSubpage(): Promise<void> {
    const api = (window as any).electronAPI;
    const panel = this.panels.assist;
    if (!api || !panel) return;

    type Detected = { id: string; name: string; profilePath: string; hasBookmarks: boolean; hasCookies: boolean; hasHistory: boolean };
    let browsers: Detected[] = [];
    try {
      browsers = (await api.detectBrowsers()) || [];
    } catch {
      browsers = [];
    }

    const sourceOptions: DropdownOption[] = browsers.map((b) => ({ id: b.id, label: b.name }));
    /** `(type id, title key, availability on the detected browser)` triplets
     *  driving both the checkable rows and the per-source re-sync. */
    const typeRows: Array<[string, string, (b: Detected) => boolean]> = [
      ["bookmarks", "settings.bookmarks", (b) => b.hasBookmarks],
      ["history", "settings.history", (b) => b.hasHistory],
      ["cookies", "settings.cookies", (b) => b.hasCookies],
    ];
    const typeRowHtml = typeRows.map(([type, key]) => `
          <div class="settings-item-row">
            <div class="settings-item-info">
              <div class="settings-item-title">${t(key)}</div>
            </div>
            <div class="settings-item-control">
              <input type="checkbox" class="browser-import-type" data-type="${type}" checked />
            </div>
          </div>`).join(`
          <div class="settings-item-divider"></div>`);

    const bodyHtml = browsers.length === 0
      ? `<div class="model-empty"><i data-lucide="globe" class="lucide"></i><span>${t("settings.noBrowsersFound")}</span></div>`
      : `
        <div class="settings-section-title"><i data-lucide="globe" class="lucide section-title-icon"></i> ${t("settings.importSource")}</div>
        <div class="settings-card">
          <div class="settings-item-row">
            <div class="settings-item-info">
              <div class="settings-item-title">${t("settings.importSource")}</div>
              <div class="settings-item-desc">${t("settings.importBrowserDataDesc")}</div>
            </div>
            <div class="settings-item-control" style="width:min(340px,56%)">
              ${this.renderDropdown("browser-import-source", sourceOptions, sourceOptions[0]?.id || "", () => {})}
            </div>
          </div>
        </div>

        <div class="settings-section-title"><i data-lucide="list-checks" class="lucide section-title-icon"></i> ${t("settings.importDataTypes")}</div>
        <div class="settings-card">${typeRowHtml}</div>

        <div style="display:flex;justify-content:flex-end;gap:8px">
          <button class="btn btn--primary" id="browser-import-run">${t("settings.import")}</button>
        </div>`;

    // Same panel slot as the browser card: mounting swaps the editor content
    // in place (one editor per panel), i.e. the per-model detail-view grammar.
    const { overlay } = this._showPanelEditor("assist", panel, t("settings.importBrowserData"), bodyHtml, true);
    const footer = overlay.querySelector(".dialog-footer") as HTMLElement | null;
    if (footer) footer.style.display = "none";
    // The back arrow returns to the browser card, not to the landing list:
    // clone the button to drop the shell's close wiring, then rebind.
    const back = overlay.querySelector(".panel-editor-back") as HTMLButtonElement | null;
    if (back) {
      const fresh = back.cloneNode(true) as HTMLButtonElement;
      back.replaceWith(fresh);
      fresh.style.display = "";
      fresh.addEventListener("click", () => { void this.renderAssistSurface("browser"); });
    }
    if (browsers.length === 0) return;

    let selectedSource = sourceOptions[0]?.id || "";
    // Availability flips with the source browser: a type the browser has no
    // data for is unticked and disabled rather than silently skipping.
    const syncAvailability = (browserId: string): void => {
      const b = browsers.find((x) => x.id === browserId);
      overlay.querySelectorAll<HTMLInputElement>(".browser-import-type").forEach((cb) => {
        const row = typeRows.find(([ty]) => ty === (cb.dataset.type || ""));
        const has = row && b ? row[2](b) : false;
        cb.disabled = !has;
        cb.checked = has;
      });
    };
    syncAvailability(selectedSource);
    this.bindDropdown("browser-import-source", (v) => {
      selectedSource = v;
      syncAvailability(v);
    });

    const runBtn = overlay.querySelector("#browser-import-run") as HTMLButtonElement | null;
    runBtn?.addEventListener("click", async () => {
      const b = browsers.find((x) => x.id === selectedSource);
      if (!b) return;
      const wanted = new Set<string>();
      overlay.querySelectorAll<HTMLInputElement>(".browser-import-type").forEach((cb) => {
        if (cb.checked && !cb.disabled && cb.dataset.type) wanted.add(cb.dataset.type);
      });
      if (wanted.size === 0) {
        showToast(t("settings.importNoSelection"), "error");
        return;
      }
      runBtn.disabled = true;
      runBtn.textContent = t("settings.importing");
      try {
        const res = await api.importBrowserData(b.id, b.profilePath);
        if (res.success && res.data) {
          // Unticked types are stripped before saving, so only the selected
          // subset reaches the app store (save-imported-data skips empties).
          await api.saveImportedBrowserData({
            bookmarks: wanted.has("bookmarks") ? res.data.bookmarks : null,
            history: wanted.has("history") ? (res.data.history || []) : [],
            cookies: wanted.has("cookies") ? (res.data.cookies || []) : [],
          });
          showToast(t("settings.importSuccess"), "success");
          void this.renderAssistSurface("browser");
        } else {
          showToast(t("settings.importFailed") + (res.error ? ": " + res.error : ""), "error");
          runBtn.disabled = false;
          runBtn.textContent = t("settings.import");
        }
      } catch (e: any) {
        showToast(t("settings.importFailed") + (e?.message ? ": " + e.message : ""), "error");
        runBtn.disabled = false;
        runBtn.textContent = t("settings.import");
      }
    });
  }

  private _exportAllBrowserData(): void {
    const api = (window as any).electronAPI;
    if (!api?.exportAllBrowserData) return;
    api.exportAllBrowserData().then((res: any) => {
      if (res.success) {
        showToast(t("settings.exportSuccess"), "success");
      } else if (res.error !== "canceled") {
        showToast(t("settings.exportFailed") + (res.error ? ": " + res.error : ""), "error");
      }
    });
  }

  private renderAbout(): void {
    const tFn = t;

    const legalLinks: Array<{ icon: string; label: string; key: string }> = [
      { icon: "file-text", label: tFn("settings.aboutLicense"), key: "license" },
      { icon: "shield", label: tFn("settings.aboutPrivacy"), key: "privacy" },
      { icon: "scroll", label: tFn("settings.aboutTerms"), key: "terms" },
      { icon: "file-signature", label: tFn("settings.aboutAgreement"), key: "agreement" },
      { icon: "book-open-check", label: tFn("settings.aboutContentRules"), key: "content-rules" },
      { icon: "heart", label: tFn("settings.aboutThanks"), key: "thanks" },
      { icon: "database", label: tFn("settings.aboutDataRules"), key: "data-rules" },
      { icon: "shield", label: tFn("settings.aboutMinors"), key: "minors" },
    ];

    const supportLinks: Array<{ icon: string; label: string; key: string }> = [
      { icon: "book-open", label: tFn("settings.aboutDocs"), key: "docs" },
      { icon: "bug", label: tFn("settings.aboutReportBug"), key: "report-bug" },
      { icon: "users", label: tFn("settings.aboutCommunity"), key: "community" },
      { icon: "mail", label: tFn("settings.aboutContact"), key: "contact" },
    ];

    const legalHtml = legalLinks.map(l => `
      <button class="about-link-row" type="button" data-link="${l.key}" data-label="${this.esc(l.label)}">
        <span class="about-link-icon"><i data-lucide="${l.icon}"></i></span>
        <span class="about-link-label">${l.label}</span>
        <span class="about-link-chevron"><i data-lucide="chevron-right"></i></span>
      </button>
    `).join("");

    const supportHtml = supportLinks.map(l => `
      <button class="about-link-row" type="button" data-link="${l.key}" data-label="${this.esc(l.label)}">
        <span class="about-link-icon"><i data-lucide="${l.icon}"></i></span>
        <span class="about-link-label">${l.label}</span>
        <span class="about-link-chevron"><i data-lucide="chevron-right"></i></span>
      </button>
    `).join("");

    const dv = this._versions?.desktop || APP_VERSION;
    const av = this._versions?.agent || APP_VERSION;

    this.panels.about.innerHTML = `
      <div class="about-banner">
        <div class="about-banner-glow"></div>
        <div class="about-banner-title"></div>
      </div>

      <div class="about-info-card">
        <div class="about-info-row" data-key="version" data-version="agent">
          <span class="about-info-label">Encre Agent</span>
          <span class="about-info-value">v${av}</span>
        </div>
        <div class="about-info-row" data-key="version" data-version="desktop">
          <span class="about-info-label">Encre Agent Desktop</span>
          <span class="about-info-value">v${dv}</span>
        </div>

      </div>

      <div class="about-links-card">
        <div class="about-links-group-title">${tFn("settings.aboutLegalTitle")}</div>
        ${legalHtml}
        <div class="about-links-divider"></div>
        <div class="about-links-group-title">${tFn("settings.aboutSupportTitle")}</div>
        ${supportHtml}
        <div class="about-links-divider"></div>
        <button class="about-link-row" type="button" data-action="check-update">
          <span class="about-link-icon"><i data-lucide="refresh-cw"></i></span>
          <span class="about-link-label">${tFn("settings.aboutCheckUpdate")}</span>
          <span class="about-link-chevron"><i data-lucide="chevron-right"></i></span>
        </button>
      </div>

      <div class="about-copyright">${tFn("settings.copyright")}</div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.about });
    }

    // Wire link row clicks
    // Legal / policy rows are NOT web URLs: each key is an in-app ESD child
    // window view (see LEGAL_DOC_VIEWS in app.ts).  Routing them through
    // openRoutedLink silently no-ops (a bare key is not a parseable URL),
    // which is exactly the "click does nothing, no error" bug.
    const legalDocViews: ReadonlySet<string> = new Set([
      "license", "privacy", "terms", "agreement",
      "content-rules", "thanks", "data-rules", "minors",
    ]);
    this.panels.about.querySelectorAll<HTMLButtonElement>(".about-link-row[data-link]").forEach(btn => {
      btn.addEventListener("click", () => {
        const link = btn.getAttribute("data-link");
        if (link) {
          const label = btn.getAttribute("data-label") || link;
          if (legalDocViews.has(link)) {
            void window.electronAPI?.openChildWindow(link, label);
            return;
          }
          const urlMap: Record<string, string> = {
            "docs": "https://github.com/mf2023/Encre#readme",
            "report-bug": "https://github.com/mf2023/Encre/issues/new",
            "community": "https://github.com/mf2023/Encre/discussions",
            "contact": "mailto:dunimd@outlook.com",
          };
          openRoutedLink(urlMap[link] || link, label);
        }
      });
    });
  }

}
