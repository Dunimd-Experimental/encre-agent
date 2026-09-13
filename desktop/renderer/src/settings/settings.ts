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
import { initSettingsDomEvents } from "./events_bind.js";
import { renderModelImpl, renderGatewayImpl } from "./model-panels.js";
import { renderSearchFilterImpl, renderDropdownImpl, modeHintImpl, bindDropdownImpl, renderGeneralImpl, renderShortcutsImpl, saveSettingImpl, saveThemeImpl, escImpl, _maxTokensDefaultImpl } from "./general.js";
import { bindVersionTapUnlockImpl, loadVersionsImpl, handleAgentTapImpl, handleDesktopTapImpl, openImpl, openModelCreateImpl, updateSidebarNavImpl, closeImpl, _cleanupTransientOverlaysImpl, focusSearchImpl, onSearchInputImpl, filterNavItemsImpl, _updateDividerVisibilityImpl, highlightInPanelImpl, isHighlightableImpl, clearHighlightsImpl, refreshCurrentPanelImpl, switchPanelImpl, showSkillDetailImpl, showSkillListImpl, showMcpCreateImpl, showMcpListImpl, showAgentCreateImpl, showAgentListImpl, showModelCreateImpl, showModelEditImpl, renderAllImpl } from "./lifecycle.js";
// Chart.js is lazy-loaded on first render of the usage panel (see
// _renderUsageSection) so it no longer blocks startup parsing.
import { showTooltipAt, hideTooltip } from "../ui/tooltip.js";
import { attachInputExpand } from "../ui/input-expand.js";
import { SEARCH_ENGINES, getDefaultSearchEngine } from "../features/browser.js";
import { searchSettingsNavItems, SEARCH_FILTER_META, SETTINGS_NAV_ITEMS } from "../features/search.js";
import { setSearchFilter } from "../core/state.js";

/** Wraps an async operation with a loading spinner on the button. */
export async function withLoading<T>(btn: HTMLButtonElement, fn: () => Promise<T>): Promise<T> {
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

const APP_VERSION = "0.4.3";

export type PanelId = "general" | "usage" | "shortcuts" | "storage" | "browser" | "model" | "gateway" | "index" | "skills" | "rules" | "permissions" | "mcp" | "agent" | "about" | "developer" | "memory" | "search";

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

interface DropdownOption { id: string; label: string; icon?: string }

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

const ADAPTER_DEFS: AdapterDef[] = [
  { id: "bluebubbles", name: "BlueBubbles", desc: "Connect to a BlueBubbles server to send and receive iMessage", fields: [
    { key: "server_url", labelKey: "fieldServerUrl", type: "text", placeholderKey: "placeholderServerUrl" },
    { key: "api_key", labelKey: "fieldApiKey", type: "password", placeholderKey: "placeholderApiKey" },
  ], docs: "https://bluebubbles.app/" },
  { id: "dingtalk", name: "DingTalk", desc: "Connect to a DingTalk custom bot webhook to send work notifications and group messages", fields: [
    { key: "client_id", labelKey: "fieldDingtalkClientId", type: "text", placeholderKey: "placeholderClientId" },
    { key: "client_secret", labelKey: "fieldDingtalkClientSecret", type: "password", placeholderKey: "placeholderClientSecret" },
  ], docs: "https://open.dingtalk.com/document/orgapp/create-and-configure-a-robot" },
  { id: "discord", name: "Discord", desc: "Integrate Discord bots to manage server channel messages and interactions", fields: [
    { key: "token", labelKey: "fieldBotToken", type: "password", placeholderKey: "placeholderBotToken" },
  ], docs: "https://discord.com/developers/applications" },
  { id: "email", name: "Email", desc: "Send and receive emails via SMTP/IMAP, with auto-reply and processing support", fields: [
    { key: "smtp_host", labelKey: "fieldSmtpHost", type: "text", placeholderKey: "placeholderSmtpHost" },
    { key: "smtp_port", labelKey: "fieldSmtpPort", type: "number", placeholderKey: "placeholderSmtpPort" },
    { key: "smtp_user", labelKey: "fieldSmtpUser", type: "text", placeholderKey: "placeholderSmtpUser" },
    { key: "smtp_pass", labelKey: "fieldSmtpPass", type: "password", placeholderKey: "placeholderSmtpPass" },
    { key: "imap_host", labelKey: "fieldImapHost", type: "text", placeholderKey: "placeholderImapHost" },
    { key: "imap_port", labelKey: "fieldImapPort", type: "number", placeholderKey: "placeholderImapPort" },
  ]},
  { id: "feishu", name: "Feishu", desc: "Connect to the Feishu open platform to receive bot events and reply to messages", fields: [
    { key: "app_id", labelKey: "fieldAppId", type: "text", placeholderKey: "placeholderAppId" },
    { key: "app_secret", labelKey: "fieldAppSecret", type: "password", placeholderKey: "placeholderAppSecret" },
  ], docs: "https://open.feishu.cn/document/home/develop-a-bot-in-5-minutes" },
  { id: "google_chat", name: "Google Chat", desc: "Connect to Google Chat spaces to send and receive messages via Webhook", fields: [
    { key: "webhook_url", labelKey: "fieldWebhookUrl", type: "text", placeholderKey: "placeholderWebhookUrl" },
    { key: "service_account", labelKey: "fieldServiceAccount", type: "textarea", placeholderKey: "placeholderServiceAccount" },
  ], docs: "https://developers.google.com/chat" },
  { id: "homeassistant", name: "Home Assistant", desc: "Connect to the Home Assistant smart home platform to control devices and query state", fields: [
    { key: "server_url", labelKey: "fieldServerUrl", type: "text", placeholderKey: "placeholderServerUrl" },
    { key: "access_token", labelKey: "fieldLongLivedToken", type: "password", placeholderKey: "placeholderAccessToken" },
  ], docs: "https://www.home-assistant.io/docs/authentication/" },
  { id: "irc", name: "IRC", desc: "Connect to an IRC server to join channels and auto-respond to messages", fields: [
    { key: "server", labelKey: "fieldServer", type: "text", placeholderKey: "placeholderServer" },
    { key: "port", labelKey: "fieldPort", type: "number", placeholderKey: "placeholderPort" },
    { key: "nickname", labelKey: "fieldNickname", type: "text", placeholderKey: "placeholderNickname" },
    { key: "password", labelKey: "fieldPassword", type: "password", placeholderKey: "placeholderPassword" },
  ]},
  { id: "line", name: "LINE", desc: "Connect to the LINE Messaging API to handle friend and group chat messages", fields: [
    { key: "channel_access_token", labelKey: "fieldChannelAccessToken", type: "password", placeholderKey: "placeholderChannelAccessToken" },
    { key: "channel_secret", labelKey: "fieldChannelSecret", type: "password", placeholderKey: "placeholderChannelSecret" },
  ], docs: "https://developers.line.biz/en/services/messaging-api/" },
  { id: "matrix", name: "Matrix", desc: "Connect to the Matrix decentralized communication network to join rooms and auto-respond to messages", fields: [
    { key: "homeserver_url", labelKey: "fieldHomeserverUrl", type: "text", placeholderKey: "placeholderHomeserverUrl" },
    { key: "access_token", labelKey: "fieldAccessToken", type: "password", placeholderKey: "placeholderAccessToken" },
  ], docs: "https://matrix.org/docs/guides/" },
  { id: "mattermost", name: "Mattermost", desc: "Integrate the Mattermost team collaboration platform to listen and send channel messages", fields: [
    { key: "server_url", labelKey: "fieldServerUrl", type: "text", placeholderKey: "placeholderServerUrl" },
    { key: "token", labelKey: "fieldBotToken", type: "password", placeholderKey: "placeholderBotToken" },
  ], docs: "https://developers.mattermost.com/" },
  { id: "ntfy", name: "ntfy", desc: "Send notification messages via the ntfy push service", fields: [
    { key: "topic", labelKey: "fieldTopic", type: "text", placeholderKey: "placeholderTopic" },
    { key: "server_url", labelKey: "fieldServerUrl", type: "text", placeholderKey: "placeholderServerUrl" },
  ], docs: "https://ntfy.sh/docs/" },
  { id: "photon", name: "Photon", desc: "Connect to the Photon social platform for messaging and interaction", fields: [
    { key: "api_url", labelKey: "fieldApiUrl", type: "text", placeholderKey: "placeholderApiUrl" },
    { key: "api_key", labelKey: "fieldApiKey", type: "password", placeholderKey: "placeholderApiKey" },
  ]},
  { id: "qqbot", name: "QQ", desc: "Connect to the QQ platform to receive and reply to group and private chat messages in real time", fields: [
    { key: "app_id", labelKey: "fieldAppId", type: "text", placeholderKey: "placeholderAppId" },
    { key: "client_secret", labelKey: "fieldClientSecret", type: "password", placeholderKey: "placeholderClientSecret" },
  ], docs: "https://bot.q.qq.com/wiki/" },
  { id: "raft", name: "Raft", desc: "Connect to the Raft distributed communication network to handle inter-node messages", fields: [
    { key: "node_id", labelKey: "fieldNodeId", type: "text", placeholderKey: "placeholderNodeId" },
    { key: "peers", labelKey: "fieldPeers", type: "text", placeholderKey: "placeholderPeers" },
  ]},
  { id: "signal", name: "Signal", desc: "Connect to the Signal messaging service to send and receive encrypted messages via REST API", fields: [
    { key: "phone_number", labelKey: "fieldPhoneNumber", type: "text", placeholderKey: "placeholderPhoneNumber" },
    { key: "api_url", labelKey: "fieldApiUrl", type: "text", placeholderKey: "placeholderApiUrl" },
  ]},
  { id: "simplex", name: "SimpleX", desc: "Connect to the SimpleX decentralized messaging platform for private secure communication", fields: [
    { key: "server_url", labelKey: "fieldServerUrl", type: "text", placeholderKey: "placeholderServerUrl" },
    { key: "display_name", labelKey: "fieldDisplayName", type: "text", placeholderKey: "placeholderDisplayName" },
  ], docs: "https://simplex.chat/" },
  { id: "slack", name: "Slack", desc: "Integrate a Slack workspace to listen on and send channel messages via Bot Token", fields: [
    { key: "token", labelKey: "fieldBotToken", type: "password", placeholderKey: "placeholderBotToken" },
    { key: "signing_secret", labelKey: "fieldSigningSecret", type: "password", placeholderKey: "placeholderSigningSecret" },
  ], docs: "https://api.slack.com/apps" },
  { id: "sms", name: "SMS", desc: "Connect to an SMS provider API to send and receive SMS notifications", fields: [
    { key: "provider", labelKey: "fieldProvider", type: "text", placeholderKey: "placeholderProvider" },
    { key: "account_sid", labelKey: "fieldAccountSid", type: "text", placeholderKey: "placeholderAccountSid" },
    { key: "auth_token", labelKey: "fieldAuthToken", type: "password", placeholderKey: "placeholderAuthToken" },
  ]},
  { id: "teams", name: "Microsoft Teams", desc: "Integrate Microsoft Teams to handle channel and group chat messages via Bot Framework", fields: [
    { key: "app_id", labelKey: "fieldAppId", type: "text", placeholderKey: "placeholderAppId" },
    { key: "app_password", labelKey: "fieldAppPassword", type: "password", placeholderKey: "placeholderAppPassword" },
  ], docs: "https://learn.microsoft.com/en-us/microsoftteams/platform/" },
  { id: "telegram", name: "Telegram", desc: "Connect to the Telegram Bot API to automatically handle commands and conversations in channels and direct messages", fields: [
    { key: "token", labelKey: "fieldBotToken", type: "password", placeholderKey: "placeholderBotToken" },
  ], docs: "https://core.telegram.org/bots#how-do-i-create-a-bot" },
  { id: "webhook", name: "Webhook", desc: "Start a Webhook listener to receive HTTP callbacks from external systems", fields: [
    { key: "listen_path", labelKey: "fieldListenPath", type: "text", placeholderKey: "placeholderListenPath" },
    { key: "secret", labelKey: "fieldSecret", type: "password", placeholderKey: "placeholderSecret" },
  ]},
  { id: "weixin", name: "WeChat", desc: "After configuring iLink Bot, connect to WeChat to send and receive messages and auto-reply", fields: [], docs: "https://www.wechatbot.dev/zh/protocol" },
  { id: "wecom", name: "WeCom", desc: "Connect to a WeCom custom app to enable internal message notifications and collaboration", fields: [
    { key: "token", labelKey: "fieldWecomToken", type: "text", placeholderKey: "placeholderWecomToken" },
    { key: "encoding_aes_key", labelKey: "fieldEncodingAesKey", type: "password", placeholderKey: "placeholderEncodingAesKey" },
    { key: "receive_id", labelKey: "fieldWecomReceiveId", type: "text", placeholderKey: "placeholderWecomReceiveId" },
  ], docs: "https://developer.work.weixin.qq.com/document/" },
  { id: "whatsapp", name: "WhatsApp", desc: "Connect to the WhatsApp Business API to handle customer messages and conversations", fields: [
    { key: "phone_number_id", labelKey: "fieldPhoneNumberId", type: "text", placeholderKey: "placeholderPhoneNumberId" },
    { key: "access_token", labelKey: "fieldAccessToken", type: "password", placeholderKey: "placeholderAccessToken" },
  ], docs: "https://developers.facebook.com/docs/whatsapp/" },
  { id: "yuanbao", name: "Yuanbao", desc: "Connect to the Yuanbao open platform to exchange messages via API", fields: [
    { key: "app_key", labelKey: "fieldAppKey", type: "text", placeholderKey: "placeholderAppKey" },
    { key: "app_secret", labelKey: "fieldAppSecret", type: "password", placeholderKey: "placeholderAppSecret" },
  ]},
];

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
  private _recordingShortcutId: string | null = null;
  private _captureHandler: ((ev: KeyboardEvent) => void) | null = null;

  constructor() {
    this.nav = document.querySelector(".sidebar-settings-items")!;
    this.panels = {
      general: document.getElementById("panel-general")!,
      usage: document.getElementById("panel-usage")!,
      shortcuts: document.getElementById("panel-shortcuts")!,
      storage: document.getElementById("panel-storage")!,
      model: document.getElementById("panel-model")!,
      gateway: document.getElementById("panel-gateway")!,
      browser: document.getElementById("panel-browser")!,
      index: document.getElementById("panel-index")!,
      skills: document.getElementById("panel-skills")!,
      rules: document.getElementById("panel-rules")!,
      permissions: document.getElementById("panel-permissions")!,
      mcp: document.getElementById("panel-mcp")!,
      agent: document.getElementById("panel-agent")!,
      about: document.getElementById("panel-about")!,
      developer: document.getElementById("panel-developer")!,
      memory: document.getElementById("panel-memory")!,
      search: document.getElementById("panel-search")!,
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

    initSettingsDomEvents.call(this);
  }

  private bindVersionTapUnlock(): void { bindVersionTapUnlockImpl.call(this); }
  private async loadVersions(): Promise<void> { return loadVersionsImpl.call(this); }
  private handleAgentTap(): void { handleAgentTapImpl.call(this); }
  private handleDesktopTap(): void { handleDesktopTapImpl.call(this); }
  open(): void { openImpl.call(this); }
  public openModelCreate(): void { openModelCreateImpl.call(this); }
  private updateSidebarNav(): void { updateSidebarNavImpl.call(this); }
  close(): void { closeImpl.call(this); }
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
  private showModelCreate(): void { showModelCreateImpl.call(this); }
  private showModelEdit(idx: number): void { showModelEditImpl.call(this, idx); }
  private showModelPoolSettings(idx: number): void { this._renderModelPoolSettingsDialog(idx); }
  renderAll(): void { renderAllImpl.call(this); }

  private renderSearchFilter(): void { renderSearchFilterImpl.call(this); }
  private renderDropdown(id: string, options: DropdownOption[], currentId: string, onChange: (val: string) => void): string { return renderDropdownImpl.call(this, id, options, currentId, onChange); }
  private modeHint(modeKey: string): string { return modeHintImpl.call(this, modeKey); }
  private bindDropdown(id: string, onChange: (val: string) => void): void { bindDropdownImpl.call(this, id, onChange); }

  private renderGeneral(): void { renderGeneralImpl.call(this); }

  private renderShortcuts(): void { renderShortcutsImpl.call(this); }

  private saveSetting(key: string, value: string): void { saveSettingImpl.call(this, key, value); }

  private saveTheme(value: string): void { saveThemeImpl.call(this, value); }

  private esc(s: string): string { return escImpl(s); }

  private _maxTokensDefault(backendType: string, context?: number): number { return _maxTokensDefaultImpl(backendType, context); }

  private renderModel(): void { renderModelImpl.call(this); }

  private renderGateway(): void { renderGatewayImpl.call(this); }

  /** Open a modal dialog (model-config style) for any adapter. */
  private _showAdapterConfigDialog(defId: string): void {
    const def = ADAPTER_DEFS.find(d => d.id === defId);
    if (!def) return;
    const st = getState();
    const s = st.settings;
    const tFn = t;

    // Build bodyHtml from fields
    let bodyHtml = "";
    if (defId === "weixin") {
      const gs = st.gatewayStatus;
      const adapters = gs?.adapters ?? [];
      const statusInfo = adapters.find(a => a.name === "weixin");
      const connected = statusInfo?.connected ?? false;
      const appId = (s[`adapter_weixin_app_id` as keyof typeof s] as string) ?? "";
      const token = (s[`adapter_weixin_token` as keyof typeof s] as string) ?? "";
      const apiUrl = (s[`adapter_weixin_api_url` as keyof typeof s] as string) ?? "";

      if (connected && appId) {
        bodyHtml = `
          <div class="model-form-row" style="user-select:none;-webkit-user-select:none">
            <label class="model-form-label">${tFn("settings.fieldAppId")}</label>
            <input type="text" class="model-form-input" value="${this.esc(appId)}" readonly style="user-select:none;-webkit-user-select:none" />
          </div>
          <div class="model-form-row" style="user-select:none;-webkit-user-select:none">
            <label class="model-form-label"><span class="model-form-required">*</span>${tFn("settings.fieldToken")}</label>
            <div class="model-form-input-wrap" style="width:100%">
              <input type="password" id="dlg-weixin-token-input" class="model-form-input model-form-input--reveal" value="${this.esc(token)}" readonly style="user-select:none;-webkit-user-select:none" />
              <button class="model-form-eye-btn" id="dlg-weixin-eye-btn" type="button" data-eye-target="dlg-weixin-token-input" title="显示">
                <i data-lucide="eye"></i>
              </button>
            </div>
          </div>
          <div class="model-form-row" style="user-select:none;-webkit-user-select:none">
            <label class="model-form-label">${tFn("settings.fieldWeixinApiUrl")}</label>
            <input type="text" class="model-form-input" value="${this.esc(apiUrl)}" readonly style="user-select:none;-webkit-user-select:none" />
          </div>`;
      } else {
        // Not connected — show QR scan area
        bodyHtml = `<div id="dlg-weixin-qr-area" style="text-align:center;padding:8px 0">
          <div style="position:relative;display:inline-block;margin:0 auto">
            <img id="dlg-weixin-qr-img" style="display:none;width:240px;height:240px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.12)" />
            <div id="dlg-weixin-qr-success" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,0.35);border-radius:12px;align-items:center;justify-content:center">
              <div style="width:56px;height:56px;border-radius:50%;background:#07C160;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 24 24" width="32" height="32" style="color:#fff;display:block"><path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
              </div>
            </div>
            <div id="dlg-weixin-qr-refresh" style="display:none;position:absolute;inset:0;background:rgba(0,0,0,0.35);border-radius:12px;align-items:center;justify-content:center;cursor:pointer">
              <div style="width:56px;height:56px;border-radius:50%;background:#9aa0a6;display:flex;align-items:center;justify-content:center">
                <svg viewBox="0 0 24 24" width="32" height="32" style="color:#fff;display:block"><path fill="currentColor" d="M17.65 6.35A7.96 7.96 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
              </div>
            </div>
          </div>
          <div id="dlg-weixin-qr-status" style="padding:8px 0 0;font-size:14px;color:var(--text-muted)">${tFn("settings.wechatScanning")}</div>
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
                    <textarea id="dlg-${defId}-${f.key}" class="model-form-input" style="resize:vertical;min-height:80px" spellcheck="false"${phAttr}>${this.esc(val)}</textarea>
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
    const models = st.modelConfigs || [];
    const selectedModels = parseTargetModelSelection(s[`adapter_${defId}_models` as keyof typeof s] as string);
    bodyHtml += renderTargetModelSelection({ id: defId, models, selected: selectedModels, t: tFn, disabled: st.settings.model_pool_enabled === false });

    const titleKey = `settings.adapterName${defId.charAt(0).toUpperCase() + defId.slice(1)}`;
    const title = tFn(titleKey);
    const { overlay, close } = this._showFormDialog(title, bodyHtml, false);
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    // Bind reveal-eye toggles for every password field in this dialog.
    this._bindEyeToggles(overlay);
    // Bind model selection click events
    bindTargetModelSelection(overlay, defId);

    // Inject docs link into title bar
    if (def.docs) {
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
            const url = def.docs || "";
            const behavior = (getState().settings.default_link_behavior as string) || "system";
            if (behavior === "in_app") {
              window.electronAPI?.openChildWindow?.(url, url);
            } else {
              window.electronAPI?.openExternal?.(url);
            }
          });
        }
      }
    }

    if (defId === "weixin") {
      const gs = getState().gatewayStatus;
      const adapters = gs?.adapters ?? [];
      const statusInfo = adapters.find(a => a.name === "weixin");
      const connected = statusInfo?.connected ?? false;
      const appId = (getState().settings as any)[`adapter_weixin_app_id`] ?? "";

      if (connected && appId) {
        // Connected: turn OK button into "解绑" (danger), add eye toggle
        okBtn.textContent = tFn("settings.wechatUnbind");
        okBtn.className = "btn";
        const unbindFn = () => {
          Dialog.confirm(tFn("settings.wechatUnbind"), tFn("settings.wechatUnbindConfirm"), { container: document.getElementById("settings-view") ?? undefined }).then((confirmed) => {
            if (confirmed) {
              const unbindConfig: Record<string, any> = {
                adapter_weixin_enabled: false, adapter_weixin_app_id: "",
                adapter_weixin_token: "", adapter_weixin_api_url: "",
              };
              send({ type: "configure", config: unbindConfig });
              const current = { ...getState().settings,
                adapter_weixin_enabled: false, adapter_weixin_app_id: "",
                adapter_weixin_token: "", adapter_weixin_api_url: "",
              };
              setSettings(current as any);
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

        // Start QR scan when dialog opens
        send({ type: "wechat_scan", adapter_id: "weixin" });

        // QR refresh overlay click
        const refreshEl = overlay.querySelector("#dlg-weixin-qr-refresh") as HTMLElement;
        if (refreshEl) {
          refreshEl.addEventListener("click", () => {
            refreshEl.style.display = "none";
            const statusEl = document.getElementById("dlg-weixin-qr-status");
            if (statusEl) { statusEl.textContent = tFn("settings.wechatScanning"); statusEl.style.display = "block"; }
            const img = document.getElementById("dlg-weixin-qr-img") as HTMLImageElement | null;
            if (img) { img.style.display = "none"; img.src = ""; }
            send({ type: "wechat_scan", adapter_id: "weixin" });
          });
        }

        // Override cancel to clean up
        const cancelBtn = overlay.querySelector("#dialog-form-cancel") as HTMLButtonElement;
        if (cancelBtn) {
          cancelBtn.addEventListener("click", () => { this.renderGateway(); close(); });
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

  private _buildModelOptions(providerId: string): DropdownOption[] {
    const p = this._findProvider(providerId);
    const opts: DropdownOption[] = (p?.models || []).map(m => ({
      id: m.id,
      label: `${m.label} (${m.context.toLocaleString()} ctx)`,
    }));
    if (p && p.allow_custom) {
      opts.push({ id: "__custom__", label: t("common.custom") || "Custom" });
    }
    return opts;
  }

private _bindModelSelect(): void {
    this.bindDropdown("new-model-select", (val) => {
      const backend = (document.getElementById("new-model-backend-hidden") as HTMLInputElement)?.value || "";
      const provider = this._findProvider(backend);
      const modelIdRow = document.getElementById("model-id-row");
      const modelIdInput = document.getElementById("new-model-id") as HTMLInputElement;
      const nameInput = document.getElementById("new-model-name") as HTMLInputElement;
      const tokensInput = document.getElementById("new-model-tokens") as HTMLInputElement;
      const urlInput = document.getElementById("new-model-url") as HTMLInputElement;

      if (val === "__custom__") {
        if (modelIdRow) modelIdRow.style.display = "";
        if (modelIdInput) {
          modelIdInput.value = "";
          modelIdInput.dataset.userModified = "true";
        }
        if (urlInput) urlInput.readOnly = false;
        if (tokensInput) tokensInput.readOnly = false;
      } else {
        if (modelIdRow) modelIdRow.style.display = "none";
        const model = provider?.models.find(m => m.id === val);
        if (modelIdInput) {
          modelIdInput.value = model?.id || val;
        }
        if (nameInput && model && !nameInput.dataset.userModified) {
          nameInput.value = model.label;
        }
        if (tokensInput && model) {
          tokensInput.value = String(this._maxTokensDefault(backend, model.context));
          tokensInput.readOnly = true;
        }
        if (urlInput && provider) {
          urlInput.value = provider.base_url;
          urlInput.readOnly = true;
        }
      }
    });
  }

  private _renderModelCreateDialog(editIdx?: number): void {
    const isEdit = editIdx !== undefined;
    const existing = isEdit ? getState().modelConfigs[editIdx!] : null;
    const title = isEdit ? `${t("settings.model")} "${this.esc(existing?.name || "")}"` : t("settings.addEditModel");
    const btnLabel = isEdit ? t("settings.saveChanges") : t("settings.addModel");
    const catalog = this._getCatalog();

    const providers = [...catalog.providers].sort((a, b) => a.label.localeCompare(b.label));
    const providerOptions: DropdownOption[] = providers.map(p => ({ id: p.id, label: p.label }));
    const initialProviderId = existing ? existing.backend_type : (providers.length > 0 ? providers[0].id : "deepseek");
    const provider = this._findProvider(initialProviderId);
    const firstModelId = provider && provider.models.length > 0 ? provider.models[0].id : null;

    let initialModelSelectValue: string;
    if (existing) {
      const matched = provider?.models.find(m => m.id === existing.model_id);
      initialModelSelectValue = matched ? matched.id : "__custom__";
    } else {
      initialModelSelectValue = firstModelId || "__custom__";
    }
    const isCurated = initialModelSelectValue !== "__custom__";
    const modelOptions = this._buildModelOptions(initialProviderId);

    let initialTokens = 4096;
    if (existing) {
      initialTokens = existing.max_tokens;
    } else if (isCurated && provider) {
      const model = provider.models.find(m => m.id === initialModelSelectValue);
      initialTokens = this._maxTokensDefault(initialProviderId, model?.context);
    } else {
      initialTokens = this._maxTokensDefault(initialProviderId);
    }

    const initialThinkingConfig = existing?.thinking_config;
    const initialThinkingEnabled = initialThinkingConfig ? initialThinkingConfig.selectable !== false : true;

    const bodyHtml = `
      <div class="model-form-row">
        <label class="model-form-label" for="model-create-backend">
          <span class="model-form-required">*</span>${t("settings.providerSelect")}
        </label>
        <div class="model-form-dropdown-row">
          ${this.renderDropdown("model-create-backend", providerOptions, initialProviderId, () => {})}
          <input type="hidden" id="new-model-backend-hidden" value="${this.esc(initialProviderId)}" />
        </div>
      </div>
      <div class="model-form-row" id="model-select-row">
        <label class="model-form-label" for="new-model-select">
          <span class="model-form-required">*</span>${t("settings.model")}
        </label>
        <div class="model-form-dropdown-row" style="display:flex;align-items:center;gap:10px">
          ${modelOptions.length > 0 ? this.renderDropdown("new-model-select", modelOptions, initialModelSelectValue, () => {}) : `<div class="model-form-hint">${t("settings.customModelIdHint")}</div>`}
        </div>
      </div>
      <div class="model-form-row" id="model-id-row"${isCurated ? ` style="display:none"` : ""}>
        <label class="model-form-label" for="new-model-id">
          <span class="model-form-required">*</span>${t("settings.modelId")}
        </label>
        <input type="text" id="new-model-id" class="model-form-input" placeholder="${t("settings.deepseekChatExample")}" value="${existing ? this.esc(existing.model_id) : (isCurated ? this.esc(initialModelSelectValue) : '')}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label" for="new-model-name">
          <span class="model-form-required">*</span>${t("settings.displayName")}
        </label>
        <input type="text" id="new-model-name" class="model-form-input" placeholder="${t("settings.modelNamePlaceholder")}" value="${existing ? this.esc(existing.name) : ""}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label" for="new-model-apikey">
          <span class="model-form-required">*</span>${t("settings.apiKey")}
          ${provider && provider.docs ? `<a href="${this.esc(provider.docs)}" class="model-get-apikey-link">${t("settings.getApiKey")}</a>` : ""}
        </label>
        <div class="model-form-input-wrap">
          <input type="password" id="new-model-apikey" class="model-form-input model-form-input--reveal" placeholder="${t("settings.enterApiKey")}" value="${existing ? this.esc(existing.api_key) : ""}" autocomplete="off" />
          <button class="model-form-eye-btn" type="button" data-eye-target="new-model-apikey" title="显示"><i data-lucide="eye"></i></button>
        </div>
      </div>
      <div class="model-form-row">
        <label class="model-form-label model-form-label--inline" for="new-model-url">
          ${t("settings.baseUrl")}
          <button type="button" class="model-form-help-btn" data-tooltip="${t("settings.baseUrlHint")}" tabindex="-1">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
              <path d="M12 17h.01"/>
            </svg>
          </button>
        </label>
        <input type="text" id="new-model-url" class="model-form-input" placeholder="${t("settings.baseUrlPlaceholder")}" value="${existing ? this.esc(existing.base_url) : (provider ? this.esc(provider.base_url) : "")}" ${isCurated ? "readonly" : ""} />
      </div>
      <div class="model-form-row">
        <label class="model-form-label" for="new-model-tokens">${t("settings.maxTokens")}</label>
        <div class="model-form-input-wrap">
          <input type="number" id="new-model-tokens" class="model-form-input" min="1" value="${initialTokens}" ${isCurated ? "readonly" : ""} />
          <div class="model-form-spinners">
            <button type="button" class="model-form-spinner-btn" data-spinner="up" tabindex="-1">
              <svg width="10" height="6" viewBox="0 0 10 6" fill="none">
                <path d="M1 5L5 1L9 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            <button type="button" class="model-form-spinner-btn" data-spinner="down" tabindex="-1">
              <svg width="10" height="6" viewBox="0 0 10 6" fill="none">
                <path d="M1 1L5 5L9 1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
          </div>
        </div>
      </div>
      <div class="model-form-thinking-card" style="margin-bottom:12px">
        <div class="model-form-thinking-info">
          <div class="model-form-thinking-title">${t("settings.multimodalToggle")}</div>
          <div class="model-form-thinking-desc">${t("settings.multimodalHint")}</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="new-model-multimodal" ${existing?.multimodal ? "checked" : ""} />
          <span class="toggle-slider"></span>
        </label>
      </div>
      <div class="model-form-thinking-card">
        <div class="model-form-thinking-info">
          <div class="model-form-thinking-title">${t("settings.enableThinkingLevel")}</div>
          <div class="model-form-thinking-desc">${t("settings.enableThinkingLevelHint")}</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="new-model-thinking-enabled" ${initialThinkingEnabled ? "checked" : ""} />
          <span class="toggle-slider"></span>
        </label>
      </div>`;

    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = btnLabel;
    this._bindEyeToggles(overlay);

    const _wireApiKeyLink = () => {
      const apiKeyLink = document.querySelector(".model-get-apikey-link") as HTMLAnchorElement;
      if (!apiKeyLink) return;
      const p = this._findProvider((document.getElementById("new-model-backend-hidden") as HTMLInputElement)?.value);
      apiKeyLink.href = p?.docs || "#";
      apiKeyLink.style.display = p?.docs ? "" : "none";
      apiKeyLink.onclick = (e) => {
        e.preventDefault();
        const url = apiKeyLink.href;
        if (!url || url === "#") return;
        const behavior = (getState().settings.default_link_behavior as string) || "system";
        if (behavior === "in_app") {
          window.electronAPI?.openChildWindow(url, url);
        } else {
          window.electronAPI?.openExternal(url);
        }
      };
    };
    _wireApiKeyLink();

    this.bindDropdown("model-create-backend", (v) => {
      const hidden = document.getElementById("new-model-backend-hidden") as HTMLInputElement;
      if (hidden) hidden.value = v;
      _wireApiKeyLink();

      const selectRow = document.getElementById("model-select-row");
      if (!selectRow) return;
      const newOpts = this._buildModelOptions(v);
      const providerForBackend = this._findProvider(v);
      const firstModelId = providerForBackend && providerForBackend.models.length > 0 ? providerForBackend.models[0].id : null;
      const defaultVal = firstModelId || "__custom__";

      // Switching providers is an explicit user action -- always reset the
      // base URL to the new provider's default, and clear the manual-edit
      // flag so the user can still type their own URL afterwards.  Without
      // this, switching to a provider that has no curated models (or
      // picking "Custom") would leave the previous provider's URL in the
      // field, which is wrong.
      const urlInput2 = document.getElementById("new-model-url") as HTMLInputElement;
      if (urlInput2) {
        urlInput2.value = providerForBackend?.base_url || "";
        delete urlInput2.dataset.userModified;
      }

      if (newOpts.length === 0) {
        selectRow.innerHTML = `
          <label class="model-form-label" for="new-model-select">
            <span class="model-form-required">*</span>${t("settings.model")}
          </label>
          <div class="model-form-hint">${t("common.customModelIdHint")}</div>`;
        const modelIdRow2 = document.getElementById("model-id-row");
        if (modelIdRow2) modelIdRow2.style.display = "";
        const modelIdInput = document.getElementById("new-model-id") as HTMLInputElement;
        if (modelIdInput) { modelIdInput.value = ""; modelIdInput.dataset.userModified = "true"; }
        if (urlInput2) urlInput2.readOnly = false;
        const tokensInput2 = document.getElementById("new-model-tokens") as HTMLInputElement;
        if (tokensInput2) tokensInput2.readOnly = false;
      } else {
        const isCustom = defaultVal === "__custom__";
        selectRow.innerHTML = `
          <label class="model-form-label" for="new-model-select">
            <span class="model-form-required">*</span>${t("settings.model")}
          </label>
          <div class="model-form-dropdown-row">
            ${this.renderDropdown("new-model-select", newOpts, defaultVal, () => {})}
          </div>`;
        this._bindModelSelect();
        const modelIdRow2 = document.getElementById("model-id-row");
        const mid = document.getElementById("new-model-id") as HTMLInputElement;
        const tokensInput2 = document.getElementById("new-model-tokens") as HTMLInputElement;
        if (isCustom) {
          if (modelIdRow2) modelIdRow2.style.display = "";
          if (mid) { mid.value = ""; mid.dataset.userModified = "true"; }
          if (urlInput2) urlInput2.readOnly = false;
          if (tokensInput2) tokensInput2.readOnly = false;
        } else {
          if (modelIdRow2) modelIdRow2.style.display = "none";
          const model = providerForBackend?.models.find((m) => m.id === defaultVal);
          if (mid) mid.value = model?.id || defaultVal;
          if (urlInput2 && providerForBackend) { urlInput2.readOnly = true; }
          if (tokensInput2 && model) { tokensInput2.value = String(this._maxTokensDefault(v, model.context)); tokensInput2.readOnly = true; }
        }
        const nameInput2 = document.getElementById("new-model-name") as HTMLInputElement;
        const model = providerForBackend?.models.find((m) => m.id === defaultVal);
        if (nameInput2 && model && !nameInput2.dataset.userModified) {
          nameInput2.value = model.label;
        }
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: selectRow });
        }
      }
    });

    if (modelOptions.length > 0) {
      this._bindModelSelect();
    }

    document.getElementById("new-model-url")?.addEventListener("input", () => {
      const el = document.getElementById("new-model-url") as HTMLInputElement;
      if (el) el.dataset.userModified = "true";
    });
    document.getElementById("new-model-id")?.addEventListener("input", () => {
      const el = document.getElementById("new-model-id") as HTMLInputElement;
      if (el) el.dataset.userModified = "true";
    });
    document.getElementById("new-model-name")?.addEventListener("input", () => {
      const el = document.getElementById("new-model-name") as HTMLInputElement;
      if (el) el.dataset.userModified = "true";
    });

    // Custom number spinner buttons for max tokens
    document.querySelectorAll(".model-form-spinner-btn[data-spinner]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = (btn.closest(".model-form-input-wrap")?.querySelector(".model-form-input[type='number']")) as HTMLInputElement;
        if (!input || input.readOnly) return;
        const step = parseInt(input.step) || 1;
        const min = parseInt(input.min) || 0;
        const current = parseInt(input.value) || 0;
        if (btn.getAttribute("data-spinner") === "up") {
          input.value = String(Math.max(min, current + step));
        } else {
          input.value = String(Math.max(min, current - step));
        }
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });

    okBtn.addEventListener("click", async () => {
      const name = (document.getElementById("new-model-name") as HTMLInputElement)?.value.trim();
      const modelIdEl = document.getElementById("new-model-id") as HTMLInputElement;
      const modelId = modelIdEl?.value?.trim() || "";
      const backend = (document.getElementById("new-model-backend-hidden") as HTMLInputElement)?.value || initialProviderId;
      const apiKey = (document.getElementById("new-model-apikey") as HTMLInputElement)?.value.trim();
      const baseUrl = (document.getElementById("new-model-url") as HTMLInputElement)?.value.trim();
      const maxTokens = Math.max(1, parseInt((document.getElementById("new-model-tokens") as HTMLInputElement)?.value || "4096")) || 4096;
      console.log("[model-create] name=%j modelId=%j apiKey=%s backend=%j baseUrl=%j maxTokens=%d", name, modelId, apiKey ? "***" : "", backend, baseUrl, maxTokens);
      console.log("[model-create] modelIdEl exists=", !!modelIdEl, "value=", modelIdEl?.value, "display=", modelIdEl?.style?.display);
      if (!name || !modelId || !apiKey) {
        console.warn("[model-create] validation failed — name=%s modelId=%s apiKey=%s", name ? "ok" : "MISSING", modelId ? "ok" : "MISSING", apiKey ? "ok" : "MISSING");
        showToast(t("common.pleaseFillRequired"), "", "error", "Settings");
        return;
      }

      const multimodalInput = document.getElementById("new-model-multimodal") as HTMLInputElement;
      const thinkingEnabledInput = document.getElementById("new-model-thinking-enabled") as HTMLInputElement;
      const thinkingLevelEnabled = thinkingEnabledInput?.checked ?? true;
      const previousLevel = initialThinkingConfig?.level || "medium";
      const thinkingConfig = thinkingLevelEnabled
        ? { type: "enabled" as const, enabled: true, level: previousLevel, selectable: true }
        : { type: "enabled" as const, enabled: true, level: "", selectable: false };
      send({
        type: "validate_model", backend_type: backend, api_key: apiKey,
        base_url: baseUrl, model_id: modelId, max_tokens: maxTokens,
        name, model_index: isEdit && editIdx !== undefined ? editIdx : -1,
        multimodal: multimodalInput?.checked ?? false,
        thinking_config: thinkingConfig,
      });

      let validated = false;
      await withLoading(okBtn, async () => {
        try {
          await waitForModelValidation();
          validated = true;
        } catch (e: any) {
          showToast(t("common.connectionFailed") + (e ? `: ${e}` : ""), "", "error", "Settings");
        }
      });

      if (!validated) return;

      // Validation succeeded: the backend has already persisted the model and
      // pushed the authoritative list via models_updated (which updated state
      // before model_validated resolved).  No second round-trip to lose — just
      // refresh from the synced state and close.
      this.renderModel();
      close();
    });

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

    const bodyHtml = `
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
      <div class="model-form-thinking-card">
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
      </div>`;

    const { overlay, close } = this._showFormDialog(
      tFn("settings.modelPoolSettings"),
      bodyHtml,
      false,
    );
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = tFn("common.confirm");

    okBtn.addEventListener("click", () => {
      const poolToggle = overlay.querySelector<HTMLInputElement>("#model-pool-enabled-toggle");
      const poolOn = poolToggle ? poolToggle.checked : poolEnabled;
      const fallbackToggle = overlay.querySelector<HTMLInputElement>("#model-pool-fallback-toggle");
      const fallbackOn = fallbackToggle ? fallbackToggle.checked : current;
      const currentSettings = {
        ...getState().settings,
        model_pool_enabled: poolOn,
        model_pool_fallback_enabled: fallbackOn,
      };
      setSettings(currentSettings);
      send({ type: "configure", config: { model_pool_enabled: poolOn, model_pool_fallback_enabled: fallbackOn } });
      this.renderModel();
      close();
    });
  }

  private renderIndex(): void {
    const st = getState();
    const tFn = t;

    const docsHtml = this._renderDocList(st.docsList, tFn);

    const html = `
      <div class="settings-section-title"><i data-lucide="file-text" class="lucide section-title-icon"></i> ${tFn("settings.documentManage")}</div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${tFn("settings.docManagementDesc")}</div>
          <div class="settings-dropdown-wrap doc-add-dropdown" id="doc-add-dropdown-wrap">
            <button class="btn-add-model-top" id="doc-add-trigger" type="button">
              <i data-lucide="plus" class="lucide" style="width:14px;height:14px"></i>
              <span>${tFn("settings.addDocument")}</span>
              <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
            </button>
            <div class="settings-dropdown" id="doc-add-dropdown">
              <div class="settings-dropdown-item" data-action="local">
                <i data-lucide="file-plus" class="lucide" style="width:14px;height:14px"></i>
                <span>${tFn("settings.addDocFromFile")}</span>
              </div>
              <div class="settings-dropdown-item" data-action="url">
                <i data-lucide="link" class="lucide" style="width:14px;height:14px"></i>
                <span>${tFn("settings.addDocFromUrl")}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="docs-list-flat" id="doc-list-items">
          ${docsHtml}
        </div>
      </div>`;

    this.panels.index.innerHTML = html;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.index });
    }
  }

  private _renderDocList(docs: import("../core/types.js").DocumentEntry[], tFn: any): string {
    if (docs.length === 0) {
      return `<div class="model-empty"><i data-lucide="file-text" class="lucide"></i><span>${tFn("settings.noDocuments")}</span></div>`;
    }
    const header = `
      <div class="model-table-header">
        <div class="model-table-cell model-cell-name">${tFn("settings.docName")}</div>
        <div class="model-table-cell model-cell-provider">${tFn("settings.docSize")}</div>
        <div class="model-table-cell model-cell-actions">${tFn("settings.actions")}</div>
      </div>`;
    let rows = "";
    for (const d of docs) {
      const isLoading = d.status === "loading";
      const sizeStr = isLoading
        ? `<span class="doc-loading-spinner"></span>`
        : d.size > 1024 * 1024
          ? (d.size / 1024 / 1024).toFixed(1) + " MB"
          : d.size > 1024
            ? (d.size / 1024).toFixed(1) + " KB"
            : d.size + " B";
      const sourceLabel = isLoading ? tFn("common.loading") : (d.source === "url" ? tFn("common.url") : tFn("common.local"));
      rows += `
        <div class="model-table-row${isLoading ? " doc-row-loading" : ""}" data-doc-id="${d.id}">
          <div class="model-table-cell model-cell-name">
            <i data-lucide="${d.source === "url" ? "link" : "file"}" class="lucide" style="width:14px;height:14px;margin-right:6px;vertical-align:middle"></i>
            ${this._escapeHtml(d.name)}
          </div>
          <div class="model-table-cell model-cell-provider">
            <span class="skill-desc-text">${sourceLabel}</span>
            <div class="skill-aliases-sub">${sizeStr}</div>
          </div>
          <div class="model-table-cell model-cell-actions">
            <button class="btn-icon btn-icon--danger" data-action="delete-doc" data-doc-id="${d.id}" data-doc-name="${this._escapeHtml(d.name)}" data-tooltip="${tFn("settings.delete")}">
              <i data-lucide="trash-2" class="lucide"></i>
            </button>
          </div>
        </div>`;
    }
    return `<div class="model-table">${header}${rows}</div>`;
  }

  private _showDocNameDialog(defaultName: string, onConfirm: (name: string) => void, tFn: any): void {
    const overlay = document.createElement("div");
    overlay.className = "toast-overlay";
    overlay.innerHTML = `
      <div class="toast-dialog">
        <div class="toast-title">${tFn("settings.docName")}</div>
        <div class="input-wrapper input-wrapper--dialog">
          <input id="dialog-doc-name" value="${this._escapeHtml(defaultName)}" placeholder="${tFn("settings.docNamePlaceholder")}" style="width:100%;border:none;outline:none;background:transparent;color:var(--text-primary);font-family:inherit;font-size:13px">
        </div>
        <div class="toast-actions">
          <button class="btn" id="dialog-doc-cancel">${tFn("common.cancel")}</button>
          <button class="btn btn--primary" id="dialog-doc-ok">${tFn("common.confirm")}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const input = overlay.querySelector("#dialog-doc-name") as HTMLInputElement;
    input?.focus();
    input?.select();

    overlay.querySelector("#dialog-doc-ok")?.addEventListener("click", () => {
      const name = input?.value?.trim() || defaultName;
      onConfirm(name);
      overlay.remove();
    });
    overlay.querySelector("#dialog-doc-cancel")?.addEventListener("click", () => overlay.remove());
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  }

  private _showDocUrlDialog(tFn: any): void {
    const overlay = document.createElement("div");
    overlay.className = "toast-overlay";
    overlay.innerHTML = `
      <div class="toast-dialog">
        <div class="toast-title">${tFn("settings.addDocFromUrl")}</div>
        <input class="toast-input" id="dialog-doc-name-input" placeholder="${tFn("settings.docNamePlaceholder")}">
        <input class="toast-input" id="dialog-doc-url-input" placeholder="${tFn("settings.docUrlPlaceholder")}" style="margin-top:8px">
        <div class="toast-actions">
          <button class="btn" id="dialog-url-cancel">${tFn("common.cancel")}</button>
          <button class="btn btn--primary" id="dialog-url-ok">${tFn("common.confirm")}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const nameInput = overlay.querySelector("#dialog-doc-name-input") as HTMLInputElement;
    const urlInput = overlay.querySelector("#dialog-doc-url-input") as HTMLInputElement;
    urlInput?.focus();

    overlay.querySelector("#dialog-url-ok")?.addEventListener("click", () => {
      const name = nameInput?.value?.trim() || "";
      const url = urlInput?.value?.trim() || "";
      if (!url) return;
      send({ type: "add_document", name, url } as any);
      overlay.remove();
    });
    overlay.querySelector("#dialog-url-cancel")?.addEventListener("click", () => overlay.remove());
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  }
  private _escapeHtml(str: string): string {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  private _showFormDialog(title: string, bodyHtml: string, wide: boolean = false): {
    overlay: HTMLElement;
    close: () => void;
  } {
    const overlay = document.createElement("div");
    overlay.className = "toast-overlay";
    overlay.innerHTML = `
      <div class="toast-dialog ${wide ? 'dialog-wide' : ''}">
        <div class="toast-title">${this._escapeHtml(title)}</div>
        <div class="dialog-body">${bodyHtml}</div>
        <div class="dialog-footer">
          <button class="btn" id="dialog-form-cancel">${t("common.cancel")}</button>
          <button class="btn btn--primary" id="dialog-form-ok">${t("common.confirm")}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    // Single hook for every form dialog: any `.input-wrapper--dialog` holding a
    // multi-line textarea gets the composer's expand toggle (top-right, hover-
    // revealed).  New dialogs built on this entry get it for free.
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

    const close = () => overlay.remove();
    overlay.querySelector("#dialog-form-cancel")?.addEventListener("click", close);
    // No "click outside to dismiss" -- the form has unsaved edits and the
    // user has to deliberately press Cancel to discard them.  Also avoids
    // accidental dismissal when the user clicks on dropdown menus that
    // temporarily extend beyond the dialog bounds.

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }

    return { overlay, close };
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
        overlay.remove();
        done(v);
      };

      const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
      okBtn.addEventListener("click", async () => {
        if (selected === "replace") {
          const sure = await Dialog.confirm(
            t("settings.storageImportReplaceTitle"),
            t("settings.storageImportReplaceConfirm"),
            { priority: "high", container: document.getElementById("settings-view") ?? undefined },
          );
          if (!sure) return;
        }
        finish(selected);
      });
      overlay.querySelector("#dialog-form-cancel")?.addEventListener("click", () => finish(null));
    });
  }

  private renderSkills(): void {
    const st = getState();
    const skills: SkillInfo[] = st.skillsList;
    console.log("[DEBUG renderSkills] skills count:", skills.length, "skillsList:", skills.map(s=>s.name).join(","));
    const enabled = new Set(st.enabledSkills);

    let rowsHtml = "";
    for (const sk of skills) {
      const isOn = enabled.has(sk.name);
      const isUser = sk.source === "user" || sk.source === "project";
      const canDelete = sk.source !== "bundled" && sk.source !== "managed";
      const aliases = sk.aliases && sk.aliases.length > 0
        ? sk.aliases.join(", ") : "";
      rowsHtml += `
        <div class="model-table-row" data-skill="${this.esc(sk.name)}">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">${this.esc(sk.name)}</span>
          </div>
          <div class="model-table-cell model-cell-provider">
            <span class="skill-desc-text">${this.esc(sk.description)}</span>
            ${aliases ? `<div class="skill-aliases-sub">${t("settings.aliases")}: ${this.esc(aliases)}</div>` : ""}
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
      </div>`;

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
            <div class="skill-aliases-sub">${this.esc(cmd.description)}</div>
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
        <label class="model-form-label">${t("settings.commandName")} <span class="required-star">*</span></label>
        <input type="text" id="cmd-name" class="model-form-input" value="${this.esc(existing?.name || "")}" placeholder="${t("settings.commandNamePlaceholder")}" ${isEdit ? "readonly" : ""} />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.commandTitle")} <span class="required-star">*</span></label>
        <input type="text" id="cmd-title" class="model-form-input" value="${this.esc(existing?.title || "")}" placeholder="${t("settings.commandTitlePlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.commandDescription")}</label>
        <input type="text" id="cmd-description" class="model-form-input" value="${this.esc(existing?.description || "")}" placeholder="${t("settings.commandDescriptionPlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.commandIcon")}</label>
        <input type="text" id="cmd-icon" class="model-form-input" value="${this.esc(existing?.icon && existing.icon !== "command" ? existing.icon : "wand-2")}" placeholder="${t("settings.commandIconPlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.commandPrompt")}</label>
        <div class="input-wrapper input-wrapper--dialog">
          <textarea id="cmd-prompt" class="setting-rich-text" placeholder="${t("settings.commandPromptPlaceholder")}" style="min-height:80px">${this.esc(existing?.prompt || "")}</textarea>
        </div>
      </div>`;

    const title = isEdit ? `${t("settings.command")} "${this.esc(existing?.name || "")}"` : t("settings.createCommand");
    const { overlay, close } = this._showFormDialog(title, bodyHtml);

    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = isEdit ? t("settings.saveChanges") : t("settings.createCommand");
    okBtn.addEventListener("click", () => {
      const name = (document.getElementById("cmd-name") as HTMLInputElement)?.value?.trim();
      if (!name) {
        Dialog.alert(t("settings.error"), t("settings.commandNamePlaceholder"), { container: document.getElementById("settings-view") ?? undefined });
        return;
      }
      if (!/^[a-zA-Z0-9][a-zA-Z0-9-]*$/.test(name)) {
        Dialog.alert(t("settings.error"), t("settings.invalidCommandName"), { container: document.getElementById("settings-view") ?? undefined });
        return;
      }

      const iconVal = (document.getElementById("cmd-icon") as HTMLInputElement)?.value?.trim() || "wand-2";
      const newCmd: CustomCommand = {
        name,
        title: (document.getElementById("cmd-title") as HTMLInputElement)?.value?.trim() || name,
        description: (document.getElementById("cmd-description") as HTMLInputElement)?.value?.trim() || "",
        icon: iconVal === "command" ? "wand-2" : iconVal,
        prompt: (document.getElementById("cmd-prompt") as HTMLTextAreaElement)?.value?.trim() || undefined,
      };

      if (isEdit) {
        const updated = st.customCommands.map(c => c.name === editName ? newCmd : c);
        this.saveCustomCommands(updated);
      } else {
        if (st.customCommands.some(c => c.name === name)) {
          Dialog.alert(t("settings.error"), t("settings.invalidCommandName"), { container: document.getElementById("settings-view") ?? undefined });
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
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.source")}</label>
        <input type="text" class="model-form-input" value="${sk.source}" readonly />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.description")}</label>
        <input type="text" class="model-form-input" value="${this.esc(sk.description)}" readonly />
      </div>
      ${extraFields}
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.skillContent")}</label>
        ${canEdit
          ? `<div class="input-wrapper input-wrapper--dialog"><textarea id="skill-content-editor" class="setting-rich-text" style="min-height:300px">${this.esc(fullContent)}</textarea></div>`
           : `<div class="msg-text" style="max-height:60vh;overflow-y:auto;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:8px">${renderMarkdown(content)}</div>`}
      </div>`;

    const title = canEdit ? `${t("settings.skill")} "${sk.name}"` : sk.name;
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
      const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
      okBtn.remove();
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
            <span class="model-active-tag" style="margin-left:8px">${sourceLabel}</span>
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
    const title = isEdit ? `${t("settings.mcpServer")} "${this.esc(existing?.name || "")}"` : t("settings.addMcpServer");

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

  private renderRules(): void {
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
      ? `<div class="model-empty"><i data-lucide="file-check" class="lucide"></i><span>${t("settings.noGlobalRules")}</span></div>`
      : `
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${t("settings.ruleSource")}</div>
            <div class="model-table-cell model-cell-provider">${t("settings.memoryTime")}</div>
            <div class="model-table-cell model-cell-actions">${t("settings.actions")}</div>
          </div>
          ${rowsHtml}
        </div>`;

    this.panels.rules.innerHTML = `
      <div class="settings-section-title"><i data-lucide="file-check" class="lucide section-title-icon"></i> ${t("settings.globalRules")}</div>
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
    this.panels.rules.querySelectorAll("[data-action='edit-rule']").forEach((btn) => {
      btn.addEventListener("click", () => {
        const name = (btn as HTMLElement).getAttribute("data-name") || "";
        send({ type: "get_global_rule_content", name });
      });
    });

    // Bind delete
    this.panels.rules.querySelectorAll("[data-action='delete-rule']").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const name = (btn as HTMLElement).getAttribute("data-name") || "";
        if (await Dialog.confirm(t("common.confirmDeleteTitle"), t("common.confirmDelete", { name: `${name}.md` }), { container: document.getElementById("settings-view") ?? undefined })) {
          send({ type: "delete_global_rule", name });
        }
      });
    });

    document.getElementById("btn-add-global-rule")?.addEventListener("click", () => {
      this._showRuleFormDialog("", "", false);
    });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.rules });
    }
    // The unified push already delivers global_rules_list on connect and
    // after every rule save/delete — nothing to fetch here.
  }

  private _showRuleFormDialog(name: string, content: string, isEdit: boolean): void {
    const title = isEdit ? `${t("settings.rules")} "${this.esc(name)}"` : t("settings.addGlobalRule");
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

  private renderPermissions(): void {
    const st = getState();
    const policies = st.permissionPolicies || { tools: {}, capabilities: {} };

    // Request persisted policies on first open.
    if (Object.keys(policies.tools).length === 0 && Object.keys(policies.capabilities).length === 0) {
      send({ type: "configure", config: { permission_settings: {} } });
    }

    const options: DropdownOption[] = [
      { id: "allow", label: t("permissions.settings.allow") },
      { id: "deny", label: t("permissions.settings.deny") },
      { id: "ask", label: t("permissions.settings.ask") },
    ];

    // Tool key → Display name mapping. Only listed tools appear in the panel.
    const dangerousTools: Record<string, string> = {
      "apply_patch": "Apply Patch",
      "archive": "Archive",
      "bash": "Bash",
      "bash_kill": "Bash Kill",
      "batch_api": "Batch API",
      "browser": "Browser",
      "cloud_storage": "Cloud Storage",
      "computer_use": "Computer Use",
      "create_embeddings": "Create Embeddings",
      "create_moderation": "Create Moderation",
      "cron_create": "Cron Create",
      "cron_delete": "Cron Delete",
      "database": "Database",
      "deploy": "Deploy",
      "desktop": "Desktop",
      "docker": "Docker",
      "edit_image": "Edit Image",
      "email": "Email",
      "env_manager": "Env Manager",
      "file_api": "File API",
      "file_edit": "File Edit",
      "file_write": "File Write",
      "fine_tuning_api": "Fine Tuning API",
      "generate_image": "Generate Image",
      "git": "Git",
      "github": "GitHub",
      "image_variation": "Image Variation",
      "lint_format": "Lint Format",
      "manage": "Manage",
      "notify": "Notify",
      "rest_client": "REST Client",
      "ssh": "SSH",
      "task_create": "Task Create",
      "task_stop": "Task Stop",
      "transcribe_audio": "Transcribe Audio",
      "translate_audio": "Translate Audio",
      "vlm_computer_use": "VLM Computer Use",
    };

    const toolKeys = Object.keys(dangerousTools);

    const ensureEntries = (keys: string[], existing: Record<string, import("../core/types.js").PermissionPolicy>) => {
      return keys.map((key) => [key, existing[key] || { value: "allow", source: "default" }] as [string, import("../core/types.js").PermissionPolicy]);
    };

    const toolEntries = ensureEntries(toolKeys, policies.tools);

    const renderRows = (entries: Array<[string, import("../core/types.js").PermissionPolicy]>) => {
      if (entries.length === 0) {
        return `<div class="empty-state">${t("permissions.settings.noItems")}</div>`;
      }
      return entries.map(([name, policy]) => {
        const displayName = dangerousTools[name] || name;
        const descKey = `permissions.settings.desc.${name}`;
        const desc = t(descKey as any) === descKey ? "" : t(descKey as any);
        const currentVal = policy.value === "default" ? "allow" : policy.value;
        return `
          <div class="settings-item-row" data-perm-group="tools" data-perm-name="${this.esc(name)}">
            <div class="settings-item-info">
              <div class="settings-item-title">
                ${this.esc(displayName)}
              </div>
              ${desc ? `<div class="settings-item-desc">${this.esc(desc)}</div>` : ""}
            </div>
            <div class="settings-item-control">
              ${this.renderDropdown(`perm-dd-tools-${name}`, options, currentVal, () => {})}
            </div>
          </div>
        `;
      }).join("");
    };

    const toolsHtml = renderRows(toolEntries);

    // The per-tool rules below are honoured only by the "custom" permission
    // mode; the three preset modes (manual / auto / full access) ignore them
    // entirely, both in this panel and in the Rust engine.
    this.panels.permissions.innerHTML = `
      <div class="settings-section-title"><i data-lucide="shield" class="lucide section-title-icon"></i> ${t("permissions.settings.title")}</div>
      <div class="permission-mode-hint">${this.esc(t("permissions.settings.customOnlyNote"))}</div>
      <div class="settings-card">${toolsHtml}</div>
    `;

    // Auto-save on every dropdown change.
    for (const [name] of toolEntries) {
      this.bindDropdown(`perm-dd-tools-${name}`, (val) => {
        const payload: Record<string, string> = {};
        for (const [n] of toolEntries) {
          const key = `perm-dd-tools-${n}`;
          const wrap = document.getElementById(`${key}-wrap`);
          const trigger = wrap?.querySelector(".settings-dropdown-trigger span");
          const selectedOption = options.find(o => o.label === trigger?.textContent);
          payload[n] = selectedOption?.id ?? "allow";
        }
        payload[name] = val;
        send({ type: "configure", config: { permission_settings: payload } });
        const next: import("../core/types.js").PermissionPolicies = { tools: {}, capabilities: {} };
        for (const [n] of toolEntries) {
          next.tools[n] = { value: (payload[n] as any) || "allow", source: "user" };
        }
        setPermissionPolicies(next);
      });
    }

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.permissions });
    }
  }

  private renderMcp(): void {
    this.renderMcpList();
  }

  private renderAgent(): void {
    const agents = getState().subAgents || [];

    const tableContent = agents.length === 0
      ? `<div class="model-empty"><i data-lucide="sparkles" class="lucide"></i><span>${t("settings.noSubAgentsYet")}</span></div>`
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
                <span class="model-name-text"><i data-lucide="sparkles" class="lucide" style="width:14px;height:14px;vertical-align:-2px;margin-right:6px;color:var(--accent)"></i>${this.esc(a.name)}</span>
                <span class="model-active-tag" style="margin-left:8px">${sourceLabel}</span>
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
      <div class="settings-section-title"><i data-lucide="sparkles" class="lucide section-title-icon"></i> ${t("settings.agentManagement")}</div>
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

    const bodyHtml = `
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.subAgentName")} <span class="required-star">*</span></label>
        <input type="text" id="agent-name" class="model-form-input" value="${this.esc(existing?.name || "")}" placeholder="${t("settings.subAgentNamePlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.description")}</label>
        <input type="text" id="agent-description" class="model-form-input" value="${this.esc(existing?.description || "")}" placeholder="${t("settings.subAgentDescPlaceholder")}" />
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("settings.systemPrompt")}</label>
        <div class="input-wrapper input-wrapper--dialog" style="margin-top:6px">
          <textarea id="agent-system-prompt" class="setting-rich-text" placeholder="${t("settings.subAgentSystemPromptPlaceholder")}" style="min-height:80px">${this.esc(existing?.system_prompt || "")}</textarea>
        </div>
      </div>`;

    const title = isEdit ? `${t("settings.subAgent")} "${this.esc(existing?.name || "")}"` : t("settings.addSubAgent");
    const { overlay, close } = this._showFormDialog(title, bodyHtml, true);

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

      const newAgent = { name, description, system_prompt: systemPrompt };

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

  private renderMemory(): void {
    const entries = getState().memoryList;
    const tFn = t;

    let rowsHtml = "";
    for (const entry of entries) {
      const title = entry.title || entry.name.replace(/\.md$/i, "");
      const memType = entry.type
        ? `<span class="model-active-tag" style="margin-left:8px">${this.esc(entry.type)}</span>`
        : "";
      rowsHtml += `
        <div class="model-table-row">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">${this.esc(title)}</span>
            ${memType}
          </div>
          <div class="model-table-cell model-cell-actions">
            <button class="btn-icon" data-action="view-memory" data-path="${this.esc(entry.path)}" data-tooltip="${tFn("settings.view")}">
              <i data-lucide="arrow-up-right" class="lucide"></i>
            </button>
          </div>
        </div>`;
    }

    const tableHtml = entries.length === 0
      ? `<div class="model-empty"><i data-lucide="brain" class="lucide"></i><span>${tFn("settings.noMemoryEntries")}</span></div>`
      : `
        <div class="model-table">
          <div class="model-table-header">
            <div class="model-table-cell model-cell-name">${tFn("settings.name")}</div>
            <div class="model-table-cell model-cell-actions">${tFn("settings.actions")}</div>
          </div>
          ${rowsHtml}
        </div>`;

    this.panels.memory.innerHTML = `
      <div class="settings-section-title"><i data-lucide="brain" class="lucide section-title-icon"></i> ${tFn("settings.memory")}</div>
      <div class="settings-card">
        <div class="model-manage-header">
          <div class="model-manage-desc">${tFn("settings.memoryDesc")}</div>
          <button class="btn-add-model-top" id="btn-refresh-memory">
            <i data-lucide="refresh-cw" class="lucide"></i>
            <span>${tFn("settings.refresh")}</span>
          </button>
        </div>
        ${tableHtml}
      </div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.memory });
    }
  }

  private _showMemoryDetailDialog(path: string): void {
    const tFn = t;
    const fileName = path.replace(/\.md$/i, "").split("/").pop() || path;
    const bodyHtml = `
      <div class="model-form-row">
        <label class="model-form-label">${tFn("settings.memoryContent")}</label>
        <div id="memory-detail-content" class="msg-text" style="max-height:60vh;overflow-y:auto;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:8px">${tFn("settings.loading")}</div>
      </div>`;
    const { overlay, close } = this._showFormDialog(fileName, bodyHtml, true);
    const cancelBtn = overlay.querySelector("#dialog-form-cancel") as HTMLElement | null;
    if (cancelBtn) cancelBtn.style.display = "none";
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.textContent = tFn("header.close");
    okBtn.addEventListener("click", close);

    send({ type: "get_memory_detail", path });
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

    // Legend: every group with >0 tokens gets a swatch + name + total.
    const legendHtml = groupOrder
      .filter((g) => (groupTotals[g]?.tokens || 0) > 0)
      .map((g) => {
        const color = groupColor[g];
        const total = groupTotals[g]?.tokens || 0;
        const status = groupTotals[g]?.status;
        const tag =
          groupBy === "model" && status === "deleted"
            ? `<span class="usage-model-tag usage-model-tag--deleted">${t("settings.deletedModel")}</span>`
            : groupBy === "model" && status === "unknown"
              ? `<span class="usage-model-tag usage-model-tag--unknown">${t("settings.unknownModel")}</span>`
              : "";
        const display = g.length > 32 ? this.esc(g.slice(0, 32) + "...") : this.esc(g);
        return `
          <div class="usage-legend-item">
            <span class="usage-model-dot" style="background:${color}"></span>
            <span class="usage-legend-name">${display}</span>
            ${tag}
            <span class="usage-legend-tokens">${this._formatNumber(total)}</span>
          </div>`;
      })
      .join("");

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
      wrap?.classList.remove("is-ready"); // show shimmer during the swap
      const model = this._computeUsageChartModel(stats.sessions || [], mode);
      if (this._usageBarChart) {
        this._usageBarChart.data.labels = model.labels as any;
        this._usageBarChart.data.datasets = model.datasets as any;
        this._usageBarChart.update();
      }
      const legendEl = el.querySelector(".usage-legend") as HTMLElement | null;
      if (legendEl) legendEl.innerHTML = model.legendHtml;
      const titleEl = el.querySelector(".usage-chart-title-text") as HTMLElement | null;
      if (titleEl) {
        titleEl.textContent = t(mode === "model" ? "settings.dailyUsageByModel" : "settings.dailyUsageByChannel");
      }
      // Reveal the chart on the next frame so the shimmer sweep is visible.
      requestAnimationFrame(() => wrap?.classList.add("is-ready"));
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
      yuanbao: "元宝",
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
        `style="font-size:12px;font-weight:var(--font-weight-medium,500);font-family:var(--font-family-default);">` +
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
          : "var(--border-border-neutral-l1, rgba(224,226,242,0.18))";
        const strokeW = isToday && b === 0 ? 1 : 0.4;
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
      `fill="${c}" stroke="var(--border-border-neutral-l1, rgba(224,226,242,0.18))" stroke-width="0.4"/>`
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
      <div class="settings-section-title" style="margin-top:24px"><i data-lucide="rotate-ccw" class="lucide section-title-icon"></i> ${t("settings.devRestartTitle")}</div>
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
      const confirmed = await Dialog.confirm(t("settings.devRestartAppTitle"), t("settings.devRestartAppDesc"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
      if (!confirmed) return;
      localStorage.clear();
      location.reload();
    });

    document.getElementById("dev-restart-server")?.addEventListener("click", async () => {
      const running = getState().running;
      if (running) {
        const ok = await Dialog.confirm(t("settings.devRestartRunningTitle"), t("settings.devRestartRunningDesc"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
        if (!ok) return;
      }
        const confirmed = await Dialog.confirm(t("settings.devRestartTitle"), t("settings.devRestartServerDesc"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
      if (!confirmed) return;
        const prog = Dialog.progress(t("settings.devRestartProgressTitle"), "", { cancellable: false, container: document.getElementById("settings-view") ?? undefined });
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
            <div class="settings-item-title">${t("settings.storageDataDir")}</div>
            <div class="settings-item-desc">${t("settings.aboutOpenDataDir")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="storage-open-data-dir" data-tooltip="${t("settings.aboutOpenDataDir")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
          </div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.aboutLogs")}</div>
            <div class="settings-item-desc">${t("settings.aboutLogsDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="storage-open-logs" data-tooltip="${t("settings.aboutLogs")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
          </div>
        </div>
      </div>

      <div class="settings-section-title" style="margin-top:24px"><i data-lucide="package" class="lucide section-title-icon"></i> ${t("settings.storageMigration")}</div>
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

      <div class="settings-section-title" style="margin-top:24px"><i data-lucide="alert-triangle" class="lucide section-title-icon"></i> ${t("settings.storageDangerZone")}</div>
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
    document.getElementById("storage-open-data-dir")?.addEventListener("click", async () => {
      const appPath = await window.electronAPI?.getAppPath?.();
      if (appPath) {
        window.electronAPI?.openFolder?.(appPath);
      }
    });

    // Clear sessions
    document.getElementById("storage-clear-sessions")?.addEventListener("click", async () => {
      const ok = await Dialog.confirm(t("common.confirmClearSessionsTitle"), t("common.confirmClearSessions"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
      if (ok) {
        send({ type: "clear_all_sessions" } as any);
      }
    });

    // Export all data
    document.getElementById("storage-export-data")?.addEventListener("click", async () => {
      const ok = await Dialog.confirm(t("settings.storageExportTitle"), t("settings.storageExportConfirm"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
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
      const ok = await Dialog.confirm(t("settings.confirmClearBrowserDataTitle"), t("settings.confirmClearBrowserData"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
      if (ok) {
        window.electronAPI?.browserClearData?.();
      }
    });

    // Open logs
    document.getElementById("storage-open-logs")?.addEventListener("click", async () => {
      await window.electronAPI?.openChildWindow("logs", t("settings.aboutLogs") || "Logs");
    });
  }

  private renderBrowser(): void {
    const st = getState();
    const s = st.settings;
    const currentSearchEngine = (s.default_search_engine as string) || "bing";
    const searchEngineOptions: DropdownOption[] = SEARCH_ENGINES.map(e => ({ id: e.id, label: e.name, icon: searchEngineIconSrc(e.id) }));
    const currentSearchEngineUrl = SEARCH_ENGINES.find(e => e.id === currentSearchEngine)?.searchUrl || "https://www.bing.com/search?q={query}";

    this.panels.browser.innerHTML = `
      <div class="settings-section-title"><i data-lucide="globe" class="lucide section-title-icon"></i> ${t("settings.browserSettings")}</div>

      <div class="settings-card">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.defaultSearchEngine")}</div>
            <div class="settings-item-desc">${t("settings.defaultSearchEngineDesc")}</div>
          </div>
          <div class="settings-item-control">
            ${this.renderDropdown("dd-search-engine", searchEngineOptions, currentSearchEngine, (v) => { this.saveSetting("default_search_engine", v); this.renderBrowser(); })}
          </div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.browserData")}</div>
            <div class="settings-item-desc">${t("settings.browserDataDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon btn-icon--danger" id="browser-clear-data" data-tooltip="${t("settings.clear")}"><i data-lucide="trash-2" class="lucide"></i></button>
          </div>
        </div>
      </div>

      <div class="settings-section-title" style="margin-top:24px"><i data-lucide="refresh-cw" class="lucide section-title-icon"></i> ${t("settings.importExport")}</div>
      <div class="settings-card">
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.importBrowserData")}</div>
            <div class="settings-item-desc">${t("settings.importBrowserDataDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="browser-import-btn" data-tooltip="${t("settings.import")}"><i data-lucide="arrow-down-right" class="lucide"></i></button>
          </div>
        </div>
        <div class="settings-item-divider"></div>
        <div class="settings-item-row">
          <div class="settings-item-info">
            <div class="settings-item-title">${t("settings.exportAllBrowserData")}</div>
            <div class="settings-item-desc">${t("settings.exportAllBrowserDataDesc")}</div>
          </div>
          <div class="settings-item-control">
            <button class="btn-icon" id="browser-export-all" data-tooltip="${t("settings.export")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
          </div>
        </div>
      </div>

      <div class="settings-section-title" style="margin-top:24px"><i data-lucide="bookmark" class="lucide section-title-icon"></i> ${t("settings.bookmarks")}
        <button class="btn-icon" id="browser-export-bookmarks" style="margin-left:auto" data-tooltip="${t("settings.export")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
      </div>
      <div id="browser-bookmarks-list" class="model-table" style="max-height:240px;overflow-y:auto;border:1px solid var(--border);border-radius:var(--radius-sm)"></div>

      <div class="settings-section-title" style="margin-top:24px"><i data-lucide="clock" class="lucide section-title-icon"></i> ${t("settings.history")}
        <button class="btn-icon btn-icon--danger" id="browser-clear-history" style="margin-left:auto" data-tooltip="${t("settings.clear")}"><i data-lucide="trash-2" class="lucide"></i></button>
        <button class="btn-icon" id="browser-export-history" data-tooltip="${t("settings.export")}"><i data-lucide="arrow-up-right" class="lucide"></i></button>
      </div>
      <div id="browser-history-list" class="model-table" style="max-height:240px;overflow-y:auto;border:1px solid var(--border);border-radius:var(--radius-sm)"></div>`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.panels.browser });
    }

    this.bindDropdown("dd-search-engine", (v) => {
      this.saveSetting("default_search_engine", v);
      const engine = SEARCH_ENGINES.find(e => e.id === v);
      if (engine) {
        this.saveSetting("default_search_engine_url", engine.searchUrl);
      }
      this.renderBrowser();
    });

    document.getElementById("browser-clear-data")?.addEventListener("click", async () => {
      const ok = await Dialog.confirm(t("settings.confirmClearBrowserDataTitle"), t("settings.confirmClearBrowserData"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
      if (ok) {
        window.electronAPI?.browserClearData?.();
        this.renderBrowser();
      }
    });

    document.getElementById("browser-clear-history")?.addEventListener("click", async () => {
      const ok = await Dialog.confirm(t("settings.confirmClearHistoryTitle"), t("settings.confirmClearHistory"), { priority: "high", container: document.getElementById("settings-view") ?? undefined });
      if (ok) {
        window.electronAPI?.clearHistory?.();
        this.renderBrowser();
      }
    });

    document.getElementById("browser-import-btn")?.addEventListener("click", () => this._showImportDialog());
    document.getElementById("browser-export-all")?.addEventListener("click", () => this._exportAllBrowserData());
    document.getElementById("browser-export-bookmarks")?.addEventListener("click", () => this._exportBookmarks());
    document.getElementById("browser-export-history")?.addEventListener("click", () => this._exportHistory());

    this._loadBrowserLists();
  }

  private _showImportDialog(): void {
    const api = (window as any).electronAPI;
    if (!api) return;

    const bodyHtml = `
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">${t("settings.importBrowserDataDesc")}</div>
      <div id="import-browser-list" class="model-table" style="min-height:60px">
        <div class="model-empty"><i data-lucide="loader-circle" class="lucide"></i><span>${t("settings.scanning")}</span></div>
      </div>`;
    const { overlay, close } = this._showFormDialog(t("settings.importBrowserData"), bodyHtml, true);
    const okBtn = overlay.querySelector("#dialog-form-ok") as HTMLButtonElement;
    okBtn.remove();

    const listEl = overlay.querySelector("#import-browser-list") as HTMLElement;

    api.detectBrowsers().then((browsers: any[]) => {
      if (!browsers || browsers.length === 0) {
        listEl.innerHTML = `<div class="model-empty"><i data-lucide="globe" class="lucide"></i><span>${t("settings.noBrowsersFound")}</span></div>`;
        if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: listEl });
        return;
      }
      listEl.innerHTML = browsers.map((b: any) => `
        <div class="model-table-row">
          <div class="model-table-cell model-cell-name">
            <span class="model-name-text">${this.esc(b.name)}</span>
          </div>
          <div class="model-table-cell model-cell-actions">
            <button class="btn-icon" data-action="browser-import" data-browser-id="${this.esc(b.id)}" data-profile="${this.esc(b.profilePath)}" data-tooltip="${t("settings.import")}"><i data-lucide="arrow-down-right" class="lucide"></i></button>
          </div>
        </div>
      `).join("");
      listEl.querySelectorAll("[data-action='browser-import']").forEach(btn => {
        btn.addEventListener("click", async () => {
          const browserId = btn.getAttribute("data-browser-id");
          const profilePath = btn.getAttribute("data-profile");
          if (!browserId || !profilePath) return;
          btn.setAttribute("data-tooltip", t("settings.importing"));
          (btn as HTMLButtonElement).disabled = true;
          const res = await api.importBrowserData(browserId, profilePath);
          if (res.success && res.data) {
            await api.saveImportedBrowserData(res.data);
            showToast(t("settings.importSuccess"), "success");
            close();
            this.renderBrowser();
          } else {
            showToast(t("settings.importFailed") + (res.error ? ": " + res.error : ""), "error");
            btn.setAttribute("data-tooltip", t("settings.import"));
            (btn as HTMLButtonElement).disabled = false;
          }
        });
      });
    }).catch(() => {
      listEl.innerHTML = `<div class="model-empty"><i data-lucide="alert-triangle" class="lucide"></i><span>${t("settings.scanFailed")}</span></div>`;
      if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: listEl });
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

  private _exportBookmarks(): void {
    const api = (window as any).electronAPI;
    if (!api?.getBookmarks) return;
    api.getBookmarks().then((data: any) => {
      const all: Array<{ name: string; url: string }> = [];
      function collect(children: any[]) {
        for (const c of children || []) {
          if (c.type === "url") all.push({ name: c.name, url: c.url || "" });
          if (c.type === "folder" && c.children) collect(c.children);
        }
      }
      if (data?.roots) {
        collect(data.roots.bookmark_bar?.children);
        collect(data.roots.other?.children);
      }
      let html = `<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
  <DT><H3>Bookmarks Bar</H3>
  <DL><p>\n`;
      for (const bm of all) {
        const name = bm.name.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        html += `    <DT><A HREF="${bm.url}" ADD_DATE="${Math.floor(Date.now() / 1000)}">${name}</A>\n`;
      }
      html += `  </DL><p>
</DL><p>`;
      api.exportFile({ content: html, defaultName: "bookmarks.html", filters: [{ name: t("settings.exportBookmarkFile"), extensions: ["html"] }] });
    }).catch(() => {});
  }

  private _exportHistory(): void {
    const api = (window as any).electronAPI;
    if (!api?.getHistory) return;
    api.getHistory().then((history: any[]) => {
      let csv = "url,title,visit_time,visit_count\n";
      for (const entry of history || []) {
        const url = (entry.url || "").replace(/"/g, '""');
        const title = (entry.title || "").replace(/"/g, '""');
        const time = entry.visit_time ? new Date(entry.visit_time).toISOString() : "";
        const count = entry.visit_count || 1;
        csv += `"${url}","${title}","${time}",${count}\n`;
      }
      api.exportFile({ content: csv, defaultName: "history.csv", filters: [{ name: t("settings.exportCsvFile"), extensions: ["csv"] }] });
    }).catch(() => {});
  }

  private _displayUrl(url: string): string {
    try { return decodeURI(url); } catch { return url; }
  }

  private _formatTime(ts: number): string {
    const d = new Date(ts);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    if (diff < 60000) return t("settings.justNow");
    if (diff < 3600000) return Math.floor(diff / 60000) + "m ago";
    if (diff < 86400000) return Math.floor(diff / 3600000) + "h ago";
    if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()) return t("settings.today");
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${y}-${m}-${day} ${hh}:${mm}`;
  }

  private _openUrl(url: string): void {
    const behavior = (getState().settings.default_link_behavior as string) || "system";
    const api = (window as any).electronAPI;
    if (!api) return;
    if (behavior === "in_app" && api.openChildWindow) {
      api.openChildWindow(url, url);
    } else if (api.openExternal) {
      api.openExternal(url);
    }
  }

  private _loadBrowserLists(): void {
    const api = (window as any).electronAPI;
    if (!api) return;

    // Bookmarks
    const bookmarksList = document.getElementById("browser-bookmarks-list");
    if (bookmarksList && api.getBookmarks) {
      api.getBookmarks().then((data: any) => {
        const all: Array<{ name: string; url: string }> = [];
        function collect(children: any[]) {
          for (const c of children || []) {
            if (c.type === "url") all.push({ name: c.name, url: c.url || "" });
            if (c.type === "folder" && c.children) collect(c.children);
          }
        }
        if (data?.roots) {
          collect(data.roots.bookmark_bar?.children);
          collect(data.roots.other?.children);
        }
        if (all.length === 0) {
          bookmarksList.innerHTML = `<div class="model-empty"><i data-lucide="bookmark" class="lucide"></i><span>${t("settings.noBookmarks")}</span></div>`;
          return;
        }
        bookmarksList.innerHTML = all.map((bm, i) => `
          <div class="model-table-row">
            <div class="model-table-cell model-cell-name" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              <span class="model-name-text">${this.esc(bm.name)}</span>
            </div>
            <div class="model-table-cell model-cell-provider" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              ${this.esc(this._displayUrl(bm.url))}
            </div>
            <div class="model-table-cell model-cell-actions">
              <button class="btn-icon" data-action="bm-navigate" data-url="${this.esc(bm.url)}" data-tooltip="${t("settings.openInBrowser")}">
                <i data-lucide="arrow-up-right" class="lucide"></i>
              </button>
              <button class="btn-icon btn-icon--danger" data-action="bm-remove" data-url="${this.esc(bm.url)}" data-tooltip="${t("settings.remove")}">
                <i data-lucide="x" class="lucide"></i>
              </button>
            </div>
          </div>
        `).join("");
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: bookmarksList });
        }
        bookmarksList.querySelectorAll("[data-action='bm-navigate']").forEach(btn => {
          btn.addEventListener("click", () => {
            const url = btn.getAttribute("data-url");
            if (url) this._openUrl(url);
          });
        });
        bookmarksList.querySelectorAll("[data-action='bm-remove']").forEach(btn => {
          btn.addEventListener("click", () => {
            const url = btn.getAttribute("data-url");
            if (url && api.removeBookmark) {
              api.removeBookmark(url).then(() => this.renderBrowser());
            }
          });
        });
      }).catch(() => {});
    }

    // History
    const historyList = document.getElementById("browser-history-list");
    if (historyList && api.getHistory) {
      api.getHistory().then((history: any[]) => {
        if (!history || history.length === 0) {
          historyList.innerHTML = `<div class="model-empty"><i data-lucide="history" class="lucide"></i><span>${t("settings.noHistory")}</span></div>`;
          return;
        }
        historyList.innerHTML = history.map((entry) => {
          const ts = entry.visit_time ? this._formatTime(entry.visit_time) : "";
          return `
          <div class="model-table-row">
            <div class="model-table-cell model-cell-name" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              <span class="model-name-text">${this.esc(entry.title || entry.url)}</span>
            </div>
            <div class="model-table-cell model-cell-provider">
              ${ts}
            </div>
            <div class="model-table-cell model-cell-actions">
              <button class="btn-icon" data-action="hist-navigate" data-url="${this.esc(entry.url)}" data-tooltip="${t("settings.openInBrowser")}">
                <i data-lucide="arrow-up-right" class="lucide"></i>
              </button>
            </div>
          </div>`;
        }).join("");
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: historyList });
        }
        historyList.querySelectorAll("[data-action='hist-navigate']").forEach(btn => {
          btn.addEventListener("click", () => {
            const url = btn.getAttribute("data-url");
            if (url) this._openUrl(url);
          });
        });
      }).catch(() => {});
    }
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
    this.panels.about.querySelectorAll<HTMLButtonElement>(".about-link-row[data-link]").forEach(btn => {
      btn.addEventListener("click", () => {
        const link = btn.getAttribute("data-link");
        if (link) {
          const label = btn.getAttribute("data-label") || link;
          const behavior = (getState().settings.default_link_behavior as string) || "system";
          const urlMap: Record<string, string> = {
            "report-bug": "https://github.com/mf2023/Encre/issues/new",
            "community": "https://github.com/mf2023/Encre/discussions",
            "contact": "mailto:dunimd@outlook.com",
          };
          const url = urlMap[link];
          if (url && url.startsWith("mailto:")) {
            window.electronAPI?.openExternal(url);
          } else if (behavior === "system") {
            if (url) {
              window.electronAPI?.openExternal(url);
            } else {
              window.electronAPI?.openChildWindow(link, label);
            }
          } else {
            if (url) {
              window.electronAPI?.openChildWindow(url, label);
            } else {
              window.electronAPI?.openChildWindow(link, label);
            }
          }
        }
      });
    });
  }

}
