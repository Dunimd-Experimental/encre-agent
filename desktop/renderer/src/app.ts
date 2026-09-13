console.log("[perf] app.js eval start epoch=" + Date.now());
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

import { connect, send, sendRetry, sendSwitchBranch, sendRollback } from "./core/ws.js";
import { handleEvent, init as streamInit, onAutomationShowResult, setRequestedSessionId, onAutomationStreamEvent, refreshAllData, pullBootData, holdNonCriticalPlanes, releaseNonCriticalPlanes } from "./core/stream.js";
import {
  addUserMessage,
  addMessage,
  startAssistantMessage,
  setSessionState,
  setTheme,
  setThemePreference,
  setActiveToolId,
  setSettings,
  getState,
  subscribe,
  isEnabled,
  resetChat,
  initNotificationPersistence,
  showToast,
  clearAttachments,
  setInputMode,
  clearMessages,
  setSessionId,
  addAttachments,
  pushQueuedPrompt,
  setPendingQueueCount,
  removeQueuedPromptAt,
  moveQueuedPrompt,
  clearQueuedPrompts,
  removeLastMessage,
  setTempChat,
  markTempChatSession,
  setSubAgentView,
  clearSubAgentBreadcrumb,
  restoreMessages,
  getTraySessions,
  removeBranchMessages,
  clearAllNotifications,
  setModelConfigs,
  setAgentConfig,
  getUnreadCount,
} from "./core/state.js";
import { Chat, formatAgentLabel, getAgentName } from "./chat/chat.js";
import { initTooltip } from "./ui/tooltip.js";
import { SplashScreen } from "./ui/splash.js";
import { Tools } from "./features/tools.js";
import { Settings, type PanelId } from "./settings/settings.js";
import { Files, getFileIcon } from "./features/files.js";
import { Session, showRenameDialogForSession } from "./session/session.js";
import { ViewManager } from "./ui/viewmanager.js";
import { Search, commandActions } from "./features/search.js";
import { Agents } from "./features/agents.js";
import { Notifications } from "./ui/notifications.js";
import { Workspace, WorkspaceManager } from "./session/workspace.js";
import { Permissions } from "./features/permissions.js";
import { AutomationPanel } from "./session/iclaw.js";
import { Automation } from "./session/automation.js";
import { ModeTransitionManager, type AppMode } from "./chat/mode-transition.js";
import { SessionInner, TabDef } from "./session/session_inner.js";
import { renderMarkdown } from "./chat/chat.js";
import { EALoader } from "./features/ealoader.js";
import { t, getLocale, onLocaleChange, applyI18n, setLocale, LOCALES, getIntlLocale, ensureLocale } from "./features/i18n.js";
import { Dialog } from "./ui/dialog.js";
import { applyTabTooltip } from "./ui/tab-tooltip.js";
import { lookupShortcut, augmentTitle, formatShortcut, platformLabel } from "./features/shortcutDisplay.js";
import type { Message } from "./core/types.js";
import { SLASH_COMMANDS, matchingSlashCommands, parseSlashInput, type SlashCommand } from "./features/slash_commands.js";
import { mountNebula } from "./features/easter-egg.js";
import { clampElLeft, clampIntoViewport, FLOAT_MARGIN, showContextMenu } from "./ui/context-menu.js";
import { BrowserView } from "./features/browser.js";

type ChildTab = {
  view: string;
  label: string;
  title?: string;
  favicon?: string;
  browserView?: BrowserView;
  _contentEl?: HTMLElement;
  _nebulaCleanup?: () => void;
};

/**
 * Child-window views rendered as an in-app jurisdiction-aware legal document
 * (markdown fetched over `getDocumentContent`, with the region switcher shown
 * in the doc toolbar). Must stay in sync with `LEGAL_DOC_FILES` in main.ts —
 * `thanks` has no per-region variants but reuses the same viewer.
 */
const LEGAL_DOC_VIEWS: ReadonlySet<string> = new Set([
  "privacy",
  "terms",
  "agreement",
  "content-rules",
  "minors",
  "data-rules",
  "thanks",
]);

/** Default legal-doc region for a UI locale (used before the user picks one). */
function defaultDocRegion(locale: string): string {
  switch (locale) {
    case "zh": return "cn";
    case "zh-Hant": return "tw";
    case "ja": return "jp";
    case "ko": return "kr";
    case "de": return "ch";
    case "tr": return "tr";
    case "es": return "mx";
    case "pt": return "br";
    case "ar": return "ae";
    case "he": return "il";
    default: return "us";
  }
}

/** Maps a legal region value to its translation-key label. */
const DOC_REGION_LABEL_KEYS: Record<string, string> = {
  us: "settings.aboutRegionUs",
  ca: "settings.aboutRegionCa",
  mx: "settings.aboutRegionMx",
  br: "settings.aboutRegionBr",
  eu: "settings.aboutRegionEu",
  uk: "settings.aboutRegionUk",
  ch: "settings.aboutRegionCh",
  tr: "settings.aboutRegionTr",
  cn: "settings.aboutRegionCn",
  jp: "settings.aboutRegionJp",
  kr: "settings.aboutRegionKr",
  tw: "settings.aboutRegionTw",
  hk: "settings.aboutRegionHk",
  mo: "settings.aboutRegionMo",
  sg: "settings.aboutRegionSg",
  in: "settings.aboutRegionIn",
  ae: "settings.aboutRegionAe",
  sa: "settings.aboutRegionSa",
  il: "settings.aboutRegionIl",
  za: "settings.aboutRegionZa",
  ng: "settings.aboutRegionNg",
  ke: "settings.aboutRegionKe",
  eg: "settings.aboutRegionEg",
  au: "settings.aboutRegionAu",
  nz: "settings.aboutRegionNz",
};

function regionLabelKey(region: string): string {
  return DOC_REGION_LABEL_KEYS[region] || "settings.aboutRegionUs";
}

(window as any).__state_setActiveToolId = setActiveToolId;
(window as any).sendRetry = sendRetry;
(window as any).sendSwitchBranch = sendSwitchBranch;
(window as any).sendRollback = sendRollback;

class App {
  private chat: Chat;
  private splash: SplashScreen;
  private tools: Tools;
  private settings: Settings;
  private files!: Files;
  private session!: Session;
  private viewManager!: ViewManager;
  private notifications!: Notifications;
  private permissions: Permissions;
  private workspace!: Workspace;
  private workspaceManager!: WorkspaceManager;
  private automationPanel!: AutomationPanel;
  private automation!: Automation;
  private modeTransition!: ModeTransitionManager;
  private sessionInner!: SessionInner;
  private search!: Search;
  private input: HTMLTextAreaElement;
  private btnSend: HTMLButtonElement;
  private btnStop: HTMLButtonElement;
  private tokenCountEl: HTMLElement;
  private welcomeScreen: HTMLElement;
  private messageList: HTMLElement;
  private summaryPanel: HTMLElement;
  private userToggledSidebar = false;
  private skipNextInput = false;
  private _slashActive = false;
  private _currentChipMode = "";
  private _persistentMode = "";
  private _summaryDoneCollapsed: boolean | null = null;
  /** Per-group collapsed state for summary reference groups. */
  private _refCollapsedState: Map<string, boolean> = new Map();
  /** Active slash *command* (distinct from a mode).  Mirrors the backend
   *  ``session.metadata["active_command"]`` slot: a sticky prompt injection
   *  that stays across turns until cleared.  ``null`` = no command active. */
  private _activeCommand: { name: string; prompt?: string; icon?: string; title?: string } | null = null;
  private _welcomeTitleAnimating = false;
  /** True once the <-> iwork content-entrance listener is attached. */
  private _modeEnterBound = false;
  /** True once the sidebar mode-entrance listener is attached. */
  private _sidebarEnterBound = false;
  private _isChild = false;
  private _childView = "";
  private _tabs: ChildTab[] = [];
  /** Origin → permissions the page asked for, refreshed on a short TTL. */
  private _permCache = new Map<string, { list: Array<{ name: string; granted: boolean }>; at: number }>();
  private _activeTabIndex = -1;
  private _region = defaultDocRegion(getLocale());
  private _activeAutomationJobId = "";
  private _keybindActions: Record<string, () => void> = {};
  private _inputHistory: string[] = [];
  private _inputHistoryIdx: number = -1;
  /** Per-session unsent input drafts, keyed by session id.  Saved while the
   *  user types and restored when the session becomes active again so that
   *  switching away and back does not lose what was being composed. */
  private _drafts = new Map<string, string>();
  private _shortcutsApplied: boolean = false;
  private _shortcutSub: (() => void) | undefined;
  /** Pending auto-resize animation frame for the composer */
  private _inputResizeRaf = 0;
  /** True while the composer is in its expanded (taller) mode. */
  private _inputExpanded = false;
  /** Timer clearing the temporary height-animation class after a toggle. */
  private _inputAnimTimer = 0;
  /**
   * Pending rAF that lands the expanded editor's viewport on the last line.
   * Deferred on purpose — see `scheduleInputScrollToEnd()`.
   */
  private _inputScrollEndRaf = 0;
  /** Timer that hands the composer slot back after an animated collapse. */
  private _inputCollapseTimer = 0;
  /**
   * Cached height of `#input-area` measured while it sits in the normal
   * flex flow (i.e. NOT `position: absolute`).  When the composer switches
   * to its expanded "floating overlay" mode, `#input-area` exits the
   * document flow and the chat container would suddenly grow to fill the
   * empty slot, pushing the timeline down to the bottom of the visible
   * area.  We avoid that jump by mirroring this cached height into
   * `#main-content-clip`'s `padding-bottom` for the duration of the
   * overlay so the chat container's geometry stays put.  Updated whenever
   * `#input-area` resizes (textarea auto-grow, viewport changes, etc.).
   */
  private _inputNaturalHeight = 0;
  /** ResizeObserver used to keep `_inputNaturalHeight` in sync. */
  private _inputAreaResizeObs: ResizeObserver | null = null;
  private inputExpandBtn: HTMLButtonElement;
  private inputArea: HTMLElement;
  /** Cache key of the last queue-card render; skips DOM rebuilds when unchanged. */
  private _queueRenderKey = "";

  constructor() {
    this.input = document.getElementById("prompt-input") as HTMLTextAreaElement;
    this.inputExpandBtn = document.getElementById("btn-input-expand") as HTMLButtonElement;
    this.inputArea = document.getElementById("input-area")!;
    this.btnSend = document.getElementById("btn-send") as HTMLButtonElement;
    this.btnStop = document.getElementById("btn-stop") as HTMLButtonElement;
    this.tokenCountEl = document.getElementById("token-count")!;
    this.welcomeScreen = document.getElementById("welcome-screen")!;
    this.messageList = document.getElementById("message-list")!;
    this.summaryPanel = document.getElementById("summary-panel")!;

    // Expose the cleanup function on window so other modules (settings,
    // search, etc.) can request a content-area reset without needing a
    // direct import.  Use a getter that re-binds to ``this`` so callers
    // always invoke the bound method even after hot-reloads.
    (window as any).__appCleanupContentArea = (opts?: { keepAutomationFlag?: boolean }) =>
      this.cleanupContentArea(opts);
    // Expose the app instance on window so stream.ts can update the mode chip
    (window as any).__app = this;

    this.splash = new SplashScreen();
    this.chat = new Chat();
    // Expose chat render so settings can refresh the message list after
    // the settings overlay tears down the chat DOM.
    (window as any).__chatRender = () => this.chat.render();
    // Force a full re-render (resets the render-key cache) -- needed after
    // cleanupContentArea() empties #message-list, because a plain render()
    // would see the unchanged render key and skip fullRender, leaving the
    // chat blank when returning from settings.
    (window as any).__chatForceRender = () => this.chat.renderForce();
    // Replay the content-area entrance. Exposed for the same reason as the
    // two hooks above: returning from settings rebuilds the chat DOM, and
    // without this the content simply snaps back into place.
    (window as any).__appContentEnter = () => this.replayContentEnter();
    // Re-expand the app-level sidebar after the session inner sidebar is
    // hidden. Guards on userToggledSidebar so a manual collapse is respected,
    // and on viewport width so a genuinely narrow window stays collapsed
    // (the sidebar would only overlay content there anyway).
    (window as any).__appRestoreSidebar = () => this.restoreAppSidebar();
    this.tools = new Tools();
this.settings = new Settings();
    this.permissions = new Permissions();
    streamInit(this.chat, this.tools, this.permissions, this.settings);
    this.bindGlobalLinkInterceptor();
    this.bindInput();
    this.updatePlaceholder();
    this.bindSummaryPanel();
    this.bindSearchOverlay();
    this.initKeybindActions();
    this.bindKeyboardShortcuts();
    this.applyShortcutHints();
    this.initTheme();
    initTooltip();
    this.bindResponsiveSidebar();

    // Forward workspace mode to tray + refresh dual session list on mode change
    let lastTrayMode = "";
    ;
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    subscribe(() => {
      const mode = getState().workspaceMode;
      if (mode && mode !== lastTrayMode) {
        lastTrayMode = mode;
        if (window.electronAPI) {
          window.electronAPI.trayModeUpdate(mode);
        }
        // Tray sessions (normal+iwork) come from the unified sessions_all
        // push — nothing new to fetch on a tray view switch.
      }
    });

    // Update stats display when telemetry changes
    subscribe(() => this.updateStats());

    // Auto-update send/stop button: show send when input has text,
    // show stop only when running AND input is empty.
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    subscribe(() => {
      const running = getState().running;
      const hasText = this.getPlainText().length > 0;
      if (hasText) {
        this.btnSend.style.display = "flex";
        this.btnStop.style.display = "none";
      } else if (running) {
        this.btnSend.style.display = "none";
        this.btnStop.style.display = "flex";
      } else {
        this.btnSend.style.display = "flex";
        this.btnStop.style.display = "none";
      }
      this.btnStop.classList.remove("cancelling");
      this.btnStop.style.pointerEvents = "";
    this.btnSend.disabled = !hasText && !this.effectiveMode() && !this.hasCommandChip() && getState().attachments.length === 0;
    });

    // Sync inputMode state changes to the input border / chip via the
    // single updateChipState() path.  Previously this wrote data-input-mode
    // directly from state.inputMode while updateChipState() wrote it from
    // _persistentMode -- two sources fighting over one attribute.  Now both
    // funnel through effectiveMode() (chip OR persistent), so the border
    // always reflects the mode that will actually be sent.
    let prevInputMode = "";
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    let prevTheme = "";
    subscribe(() => {
      const s = getState();
      if (s.inputMode !== prevInputMode) {
        prevInputMode = s.inputMode;
        this.updateChipState();
      }
      const curTheme = s.themePreference;
      if (curTheme !== prevTheme) {
        prevTheme = curTheme;
        this.applyThemeIcons(document.documentElement.getAttribute("data-theme") === "dark");
      }
    });

    // Update session bar name when session changes
    subscribe(() => this.updateSessionBarName());
    subscribe(() => this.updatePlaceholder());
    subscribe(() => this._renderQueueCard());

    // Sync temp chat button active state
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    subscribe(() => {
      const btn = document.getElementById("btn-temp-chat");
      if (btn) {
        btn.classList.toggle("active", getState().tempChat);
      }
    });

    // Child window mode — detect ?child=VIEW_NAME from URL
    const params = new URLSearchParams(window.location.search);
    const childView = params.get("child");
    if (childView) {
      this._isChild = true;
      this._childView = childView;
      this.initChildMode(childView, params.get("label") || childView);
    }

    // Defer non-critical module initialization (Files, Session, Workspace,
    // Automation, etc.) so the constructor returns quickly and the splash
    // can hide earlier.  Use a double-rAF (NOT a microtask) so the heavy
    // init runs AFTER the first paint — the splash is visible immediately
    // and the deferred work happens in the gap while the renderer waits for
    // the backend WS handshake.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => this.initDeferred());
    });
  }

  private initDeferred(): void {
    // Module initialization (deferred to reduce constructor time)
    window.addEventListener("open-settings-panel", ((e: CustomEvent) => {
      const panel = e.detail?.panel;
      this.automationPanel?.hide();
      this.settings.open();
      if (panel) this.settings.switchPanel(panel as PanelId);
    }) as EventListener);
    if (window.electronAPI) {
      window.electronAPI.onChildEvent("open-settings-panel", (panel: string) => {
        this.automationPanel?.hide();
        this.settings.open();
        if (panel) this.settings.switchPanel(panel as PanelId);
      });
    }
    this.files = new Files(this.input);

    this.session = new Session();
    this.viewManager = new ViewManager();
    this.search = new Search();
    this.registerCommandActions();
    new Agents();
    this.notifications = new Notifications();
    // Keep the Encre menu's unread counter in sync (viewing/dismissing changes it).
    this.notifications.onUnreadCountChange = () => this.refreshEncreMenu();
    initNotificationPersistence(() => this.notifications.syncSeenIds());
    this.initEncreMenu();
    this.workspace = new Workspace();
    this.workspaceManager = new WorkspaceManager(this.workspace);
    document.getElementById("btn-workspace-nav")?.addEventListener("click", () => this.workspaceManager.open());
    this.automationPanel = new AutomationPanel();
    this.automation = new Automation();
    this.automation.setChatRenderer(this.chat);

    this.automationPanel.onShow = () => this.automation.render();
    onAutomationShowResult((result: any) => {
      this.automation.updateExecutionResult(result);
    });
    onAutomationStreamEvent((event) => {
      const { job_id, event_type, event_data } = event;
      if (event_type === "start") {
        this._activeAutomationJobId = job_id;
        const data = event_data as any;
        const autoOpen = isEnabled(getState().settings?.automation_auto_open_view);
        if (!autoOpen) return;
        this.automationPanel.show();
        this.automation.openExecution({
          id: job_id, job_id, name: data.name || t("app.automationDefaultName"),
          prompt: data.prompt || "", result: "", tag: "", state: "RUNNING",
          session_id: data.session_id, messages: [],
        });
        return;
      }
      this.automation.updateExecution(job_id, event_type, event_data as Record<string, unknown>);
      if (event_type === "finish") this._activeAutomationJobId = "";
    });

    const onAnyModeChange = (): void => {
      // Was this switch already carried by the automation panel's own
      // slide? If so the area-level move is the transition, and stacking a
      // second entrance on the content underneath it would just fight the
      // slide. Only the chat-side switch (normal ⇄ iwork) needs one.
      const viaAutomation = this.automationPanel.isActive;
      this.automationPanel.hide();
      this.exitTempChat();
      this.closeSessionInnerSidebar();
      this.cleanupContentArea({ keepAutomationFlag: false });
      // Mode switches (normal ↔ iwork ↔ automation) never trigger session_ready,
      // so restore the active session's draft here after cleanup saved it.
      this.restoreDraft();
      this.chat.render();
      this._welcomeTitleAnimating = true;
      this.updatePlaceholder();
      this.animateWelcomeTitle(getState().workspaceMode);
      if (!viaAutomation) this.replayContentEnter();
    };
    this.workspace.onModeChange = onAnyModeChange;
    this.modeTransition = new ModeTransitionManager({
      workspace: this.workspace,
      automationPanel: this.automationPanel,
      onModeChange: onAnyModeChange,
      onLeaveAutomation: () => {
        if (this.automation.isDetailVisible()) this.automation.hideDetail();
      },
    });

    // Button bindings reference automation/automationPanel/modeTransition/
    // files, so they must run after those deferred modules exist.
    this.bindToolbarButtons();

    let _lastWsMode = "";
    subscribe(() => {
      const mode = getState().workspaceMode;
      if (mode === _lastWsMode) return;
      _lastWsMode = mode;
      const appEl = document.getElementById("app");
      if (mode === "iwork") {
        appEl?.classList.add("workspace-mode");
      } else {
        appEl?.classList.remove("workspace-mode");
      }
    });
    this.sessionInner = new SessionInner();
    this.chat.onViewChanges = (path: string) => {
      const st = getState();
      const artifact = st.artifacts.find(a => a.path === path);
      this.sessionInner.showReviewTab(path, artifact || undefined);
    };
    (window as any).__sessionInner = this.sessionInner;
    (window as any).__workspaceMgr = this.workspaceManager;

    // Mirror sidebar collapse state to body
    const appEl = document.getElementById("app");
    if (appEl) {
      const observer = new MutationObserver(() => {
        document.body.classList.toggle("sidebar-collapsed", appEl.classList.contains("sidebar-collapsed"));
      });
      observer.observe(appEl, { attributes: true, attributeFilter: ["class"] });
      document.body.classList.toggle("sidebar-collapsed", appEl.classList.contains("sidebar-collapsed"));
    }

    // Forward locale to tray
    let lastTrayLocale = "";
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });
    subscribe(() => {
      const locale = getState().settings.language as string;
      if (locale && locale !== lastTrayLocale) {
        lastTrayLocale = locale;
        if (window.electronAPI) {
          window.electronAPI.trayLocaleUpdate(locale);
          window.electronAPI.browserLanguageUpdate(locale);
        }
      }
    });

    // Forward resolved theme to tray popup
    let lastTrayThemePref = "";
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });
    subscribe(() => {
      const pref = getState().themePreference;
      if (pref && pref !== lastTrayThemePref) {
        lastTrayThemePref = pref;
        if (window.electronAPI) {
          window.electronAPI.trayThemeUpdate(pref);
        }
      }
    });

    // Re-fetch models when backend or base_url changes
    let lastBackend = getState().settings.backend;
    let lastBaseUrl = getState().settings.base_url;
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });
    subscribe(() => {
      const s = getState();
      if (s.settings.backend !== lastBackend || s.settings.base_url !== lastBaseUrl) {
        lastBackend = s.settings.backend;
        lastBaseUrl = s.settings.base_url;
        if (s.connected) {
          this.fetchModels();
        }
      }
    });

    // Listen for session switch from tray popup
    if (window.electronAPI) {
      window.electronAPI.onSwitchWorkspace((path: string) => {
        this.settings.close();
        void this.workspace.open(path);
      });
      window.electronAPI.onSwitchSession((sessionId: string) => {
        this.settings.close();
        const st = getState();
        if (!sessionId || sessionId === st.sessionId) return;
        const tray = getTraySessions();
        const entry =
          tray.iwork.find((s: any) => s.session_id === sessionId) ||
          tray.normal.find((s: any) => s.session_id === sessionId);
        const isIworkSession = tray.iwork.some((s: any) => s.session_id === sessionId);
        const wsPath: string =
          (entry && (entry.metadata?.workspace || (entry.metadata as any)?.workspace_path)) || "";
        const requestId = crypto.randomUUID();
        if (isIworkSession && wsPath) {
          (window as any).__pendingTrayResume = { sessionId, requestId };
        }
        if (isIworkSession && st.workspaceMode !== "iwork") {
          const orig = this.workspace.onModeChange;
          this.workspace.onModeChange = () => {
            this.closeSessionInnerSidebar();
            this.cleanupContentArea({ keepAutomationFlag: false });
            this.chat.renderForce();
            this.workspace.onModeChange = orig;
          };
          this.workspace.enter();
        } else if (!isIworkSession && st.workspaceMode === "iwork") {
          this.workspace.forceExit();
        } else {
          this.cleanupContentArea({ keepAutomationFlag: false });
        }
        setRequestedSessionId(sessionId, requestId);
        const _norm = (p: string) => p.replace(/\\/g, "/").toLowerCase();
        const _trayPath = wsPath ? _norm(wsPath) : "";
        const _activePath = st.activeWorkspace ? _norm(st.activeWorkspace) : "";
        if (_trayPath && _trayPath !== _activePath) {
          setSessionId("");
          send({ type: "open_workspace", path: wsPath, request_id: requestId });
        } else {
          setSessionId(sessionId);
          send({ type: "resume", session_id: sessionId, request_id: requestId });
          delete (window as any).__pendingTrayResume;
          this.chat.render();
        }
      });
    }

    // Re-render everything when locale changes
    onLocaleChange(() => {
      applyI18n();
      this.chat.render();
      this.session?.render();
      this.tools.render();
      this.notifications?.render();
      this.sessionInner?.renderForce();
      this.settings.renderAll();
      this.automation?.render();
      this.updateStats();
      this.bindInputModelSelector();
      this.bindInputWorkspaceSelector();
    });

    // Apply startup mode
    let _startupModeApplied = false;
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });
    const _maybeApplyStartupMode = (): void => {
      const st = getState();
      if (!st.connected) return;
      if (_startupModeApplied) return;
      if (!("startup_session_mode" in st.settings)) return;
      _startupModeApplied = true;
      const mode = st.settings.startup_session_mode as string;
      // No data pull here: the backend's connect push (`_push_all_data`) has
      // already delivered every plane for this mode — `sessions_list` is
      // channel-filtered and `sessions_all` carries both channels, and the push
      // repeats on every mode change. The old refreshAllData() call here was a
      // second full 12-plane sweep during boot (measured: config_data ×3,
      // sessions_list ×3, models_list ×2). A push that never materialises is
      // covered by pullBootData()'s fallback instead.
      if (mode === "automation") {
        this.modeTransition.toggleAutomation();
      }
    };
    // Fire once on registration in case config_data already arrived before
    // initDeferred() ran; subsequent changes are covered by the subscription.
    _maybeApplyStartupMode();
    subscribe(_maybeApplyStartupMode);

    // Session menu toggle
    document.getElementById("btn-session-menu")?.addEventListener("click", () => {
      const panel = document.getElementById("session-inner-sidebar");
      const app = document.getElementById("app");
      const mainBody = document.getElementById("main-body");
      if (!panel || !app || !mainBody) return;
      if (panel.classList.contains("hidden")) {
        this.sessionInner.restoreWidth();
        panel.classList.remove("hidden");
        mainBody.classList.remove("sidebar-hidden");
        app.classList.add("sidebar-collapsed");
        this.sessionInner.renderForce();
        this.sessionInner.saveSidebarVisibility();
      } else {
        this.sessionInner.saveWidth();
        panel.classList.add("hidden");
        mainBody.classList.add("sidebar-hidden");
        this.sessionInner.saveSidebarVisibility();
        // The inner sidebar's open state collapsed the app sidebar; undo
        // that so closing the inner sidebar re-expands the app sidebar.
        this.restoreAppSidebar();
      }
    });

    this.renderSlashDropdown();
    applyI18n();
    window.addEventListener("slash-commands-updated", () => {
      this.renderSlashDropdown();
      const chipMode = this._currentChipMode;
      if (chipMode) {
        const cmd = SLASH_COMMANDS.find(c => c.id === chipMode);
        if (cmd) {
          const chip = this.input.querySelector('.mode-chip[data-mode]') as HTMLElement | null;
          if (chip) {
            const iconEl = chip.querySelector('.chip-icon') as HTMLElement | null;
            const labelEl = chip.querySelector('span:not(.chip-icon)') as HTMLElement | null;
            if (iconEl) iconEl.setAttribute('data-lucide', cmd.icon);
            if (labelEl) labelEl.textContent = cmd.title;
            if ((window as any).lucide) (window as any).lucide.createIcons();
          }
        }
      }
      this.chat.render();
      this.updateChipState();
    });
  }

  private initChildMode(view: string, label: string): void {
    document.title = "ESD";
    document.body.classList.add("child-mode");

    const inputArea = document.getElementById("input-area");
    if (inputArea) inputArea.style.display = "none";
    const chatView = document.getElementById("chat-view");
    if (chatView) chatView.style.display = "none";
    const searchView = document.getElementById("search-view");
    if (searchView) searchView.style.display = "none";
    const agentsView = document.getElementById("agents-view");
    if (agentsView) agentsView.style.display = "none";

    // Add brand label and tabs container into header bar
    const header = document.getElementById("header-bar");
    if (header) {
      const brand = document.createElement("span");
      brand.id = "child-header-brand";
      brand.className = "header-brand";
      brand.textContent = "ESD";

      const tabsEl = document.createElement("div");
      tabsEl.id = "child-header-tabs";
      tabsEl.className = "header-tabs";

      const headerLeft = header.querySelector(".header-left");
      if (headerLeft) {
        headerLeft.after(brand);
        brand.after(tabsEl);
      } else {
        header.appendChild(brand);
        header.appendChild(tabsEl);
      }
    }

    this._tabs = [];
    this._activeTabIndex = -1;
    this.addTab(view, label);

    if (window.electronAPI) {
      window.electronAPI.onChildAddTab((v: string, l: string) => {
        this.addTab(v, l);
      });
    }

    window.addEventListener("browser-open-tab", ((e: CustomEvent) => {
      this.addTab(e.detail.url, e.detail.label);
    }) as EventListener);
  }

  private addTab(view: string, label: string): void {
    const existing = this._tabs.findIndex(t => t.view === view);
    if (existing >= 0) {
      this.switchTab(existing);
      return;
    }
    this._tabs.push({ view, label });
    this.switchTab(this._tabs.length - 1);
  }

  private switchTab(index: number): void {
    if (index < 0 || index >= this._tabs.length) return;
    if (index === this._activeTabIndex) return;
    this._activeTabIndex = index;
    this.renderTabBar();
    if (this._tabs[index].view.startsWith("http") || this._tabs[index].view.startsWith("file:") || this._tabs[index].view.startsWith("about:")) {
      // Webview tabs keep their state – just show/hide the childView wrapper
      // so the webview is never recreated.
      this._switchWebviewTab(index);
    } else {
      this.renderTabContent(this._tabs[index]);
    }
  }

  private _switchWebviewTab(index: number): void {
    const tab = this._tabs[index];
    const childViewEl = document.getElementById("child-view");
    if (!childViewEl) return;
    childViewEl.classList.remove("hidden");
    if (tab._contentEl) {
      this._showTabContent(tab);
      return;
    }
    // renderTabContent() creates the tab content and then calls
    // _showTabContent(tab), which hides every other tab.
    this.renderTabContent(tab);
  }

  // Same tab model as the sidebar (session_inner): every tab container is a
  // .tab-panel absolutely positioned inside #child-view. Only the active tab
  // carries .active — browser panels are shown via visibility (see
  // .tab-panel[data-type="browser"] in styles.css), other panels via
  // display. Reuses the exact same CSS rules as the sidebar tabs.
  private _showTabContent(tab: ChildTab): void {
    for (const t of this._tabs) {
      if (t._contentEl) {
        t._contentEl.classList.toggle("active", t === tab);
      }
    }
  }

  private closeTab(index: number): void {
    const removed = this._tabs[index];
    if (removed?._nebulaCleanup) {
      removed._nebulaCleanup();
    }
    if (removed?.browserView) {
      removed.browserView.destroy();
    }
    if (removed?._contentEl) {
      removed._contentEl.remove();
    }
    this._tabs.splice(index, 1);
    if (this._tabs.length === 0) {
      window.electronAPI?.windowClose();
      return;
    }
    if (this._activeTabIndex >= this._tabs.length) {
      this._activeTabIndex = this._tabs.length - 1;
    } else if (index < this._activeTabIndex) {
      this._activeTabIndex--;
    }
    this.renderTabBar();
    this.renderTabContent(this._tabs[this._activeTabIndex]);
  }

  private renderTabBar(): void {
    const tabsEl = document.getElementById("child-header-tabs");
    if (!tabsEl) return;
    tabsEl.innerHTML = this._tabs.map((tab, i) => {
      const displayLabel = tab.title || tab.label;
      const faviconHtml = tab.favicon
        ? `<img class="tab-favicon" src="${this.esc(tab.favicon)}" onerror="this.style.display='none';this.nextElementSibling.style.display=''"><i data-lucide="globe" class="lucide lucide-sm" style="display:none"></i>`
        : `<i data-lucide="globe" class="lucide lucide-sm"></i>`;
      return `<button class="tab${i === this._activeTabIndex ? " active" : ""}" data-index="${i}" draggable="true">
        ${faviconHtml}
        <span class="tab-label">${this.esc(displayLabel)}</span>
        <span class="tab-close" data-index="${i}" data-tooltip="${t("sessionInner.closeTab")}"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg></span>
      </button>`;
    }).join("");
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: tabsEl });
    }
    this.refreshTabTooltips();

    let dragSrcIdx: number | null = null;
    tabsEl.querySelectorAll("button[data-index]").forEach(btn => {
      const idx = parseInt(btn.getAttribute("data-index")!, 10);

      btn.addEventListener("mousedown", (e: Event) => {
        const me = e as MouseEvent;
        if (me.button === 1) me.preventDefault();
      });

      // Click to switch
      btn.addEventListener("click", (e) => {
        if ((e.target as HTMLElement).closest(".tab-close")) return;
        this.switchTab(idx);
      });

      // Middle-click to close
      btn.addEventListener("auxclick", (e) => {
        const ev = e as MouseEvent;
        if (ev.button === 1) {
          ev.preventDefault();
          this.closeTab(idx);
        }
      });

      // Close button
      const closeBtn = btn.querySelector(".tab-close");
      if (closeBtn) {
        closeBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const ci = parseInt((closeBtn as HTMLElement).getAttribute("data-index") || "0");
          this.closeTab(ci);
        });
      }

      // Drag-and-drop reorder
      btn.addEventListener("dragstart", (e) => {
        const ev = e as DragEvent;
        dragSrcIdx = idx;
        ev.dataTransfer?.setData("text/plain", String(idx));
        btn.classList.add("dragging");
      });
      btn.addEventListener("dragend", () => {
        btn.classList.remove("dragging");
        dragSrcIdx = null;
        tabsEl.querySelectorAll(".tab.drop-target").forEach(el => el.classList.remove("drop-target"));
      });
      btn.addEventListener("dragover", (e) => {
        e.preventDefault();
        if (dragSrcIdx !== null && dragSrcIdx !== idx) {
          tabsEl.querySelectorAll(".tab.drop-target").forEach(el => el.classList.remove("drop-target"));
          btn.classList.add("drop-target");
        }
      });
      btn.addEventListener("dragleave", () => {
        btn.classList.remove("drop-target");
      });
      btn.addEventListener("drop", (e) => {
        e.preventDefault();
        btn.classList.remove("drop-target");
        if (dragSrcIdx !== null && dragSrcIdx !== idx) {
          const [moved] = this._tabs.splice(dragSrcIdx, 1);
          this._tabs.splice(idx, 0, moved);
          if (this._activeTabIndex === dragSrcIdx) {
            this._activeTabIndex = idx;
          } else if (dragSrcIdx < idx && this._activeTabIndex > dragSrcIdx && this._activeTabIndex <= idx) {
            this._activeTabIndex--;
          } else if (dragSrcIdx > idx && this._activeTabIndex >= idx && this._activeTabIndex < dragSrcIdx) {
            this._activeTabIndex++;
          }
          this.renderTabBar();
          this.renderTabContent(this._tabs[this._activeTabIndex]);
        }
        dragSrcIdx = null;
      });
    });
    tabsEl.addEventListener("wheel", (e) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        tabsEl.scrollLeft += e.deltaY;
      }
    }, { passive: false });
  }

  /**
   * Keep the tab hover tooltips in sync. Deliberately the same shape as the
   * session-sidebar tooltips (see ui/tab-tooltip.ts): page title, then the
   * full URL, with the page favicon as the leading icon.
   */
  private refreshTabTooltips(): void {
    const tabsEl = document.getElementById("child-header-tabs");
    if (!tabsEl) return;
    this._tabs.forEach((tab, i) => {
      const el = tabsEl.querySelector<HTMLElement>(`.tab[data-index="${i}"]`);
      if (!el) return;
      const bv = tab.browserView;
      const liveUrl = (bv?.getUrl() || "").trim();
      const liveTitle = (bv?.getTitle() || "").trim();
      const isNavigable = /^(https?|file):/i.test(tab.view || "");
      const url = liveUrl || (isNavigable ? tab.view : "");
      const title = liveTitle || tab.title || tab.label;
      applyTabTooltip(el, {
        title: title || url || tab.label,
        icon: tab.favicon,
        location: url,
        isUrl: /^https?:/i.test(url),
      }, getIntlLocale());
      // Permissions arrive asynchronously; the tooltip is repainted once known.
      void this.attachPermissions(el, url);
    });
  }

  /**
   * Resolve the permissions the page asked for and write them onto the tab
   * tooltip. Reads `browser:get-site-info`, whose data comes from the main
   * process permission-request handler — i.e. what the page really used.
   */
  private async attachPermissions(el: HTMLElement, url: string): Promise<void> {
    if (!url || !/^https?:/i.test(url)) return;
    let origin = "";
    try { origin = new URL(url).origin; } catch { return; }
    const hit = this._permCache.get(origin);
    let list: Array<{ name: string; granted: boolean }>;
    if (hit && Date.now() - hit.at < 5000) {
      list = hit.list;
    } else {
      try {
        const info = await window.electronAPI?.getSiteInfo(url);
        list = info?.permissions || [];
      } catch {
        list = [];
      }
      this._permCache.set(origin, { list, at: Date.now() });
      this._permCache.set(url, { list, at: Date.now() });
    }
    if (!list.length) return;
    const idx = Number(el.dataset.index);
    const tab = this._tabs[idx];
    if (!tab) return;
    const bv = tab.browserView;
    const liveUrl = (bv?.getUrl() || "").trim();
    const title = (bv?.getTitle() || "").trim() || tab.title || tab.label;
    applyTabTooltip(el, {
      title: title || url || tab.label,
      icon: tab.favicon,
      location: liveUrl || url,
      isUrl: /^https?:/i.test(liveUrl || url),
      permissions: list,
    }, getIntlLocale());
  }

  private renderTabContent(tab: ChildTab): void {
    const childViewEl = document.getElementById("child-view");
    if (!childViewEl) return;
    childViewEl.classList.remove("hidden");

    if (tab._contentEl) {
      this._showTabContent(tab);
      return;
    }

    const container = document.createElement("div");
    // Reuse the exact same tab container as the sidebar: .tab-panel is
    // position:absolute;inset:0 within #child-view (position:relative) and is
    // shown/hidden via the .active class (browser panels via visibility,
    // see styles.css .tab-panel / .tab-panel[data-type="browser"]). This is
    // the identical mechanism session_inner uses for its browser tabs.
    container.className = "tab-panel";
    tab._contentEl = container;
    childViewEl.appendChild(container);

if (tab.view.startsWith("http://") || tab.view.startsWith("https://") || tab.view.startsWith("file:") || tab.view === "about:blank") {
      const startUrl = tab.view === "about:blank" ? "" : tab.view;
      container.dataset.type = "browser";
      const bv = new BrowserView(container, {
        startUrl,
        onFaviconChange: (favicon: string) => {
          tab.favicon = favicon;
          this.renderTabBar();
        },
        onNewWindow: (newUrl: string) => {
          this.addTab(newUrl, newUrl);
        },
      });
      // BrowserView overwrites container.style.cssText; restore the overflow
      // the sidebar browser panels use (panel.style.overflow = "hidden").
      container.style.overflow = "hidden";
      tab.browserView = bv;
      bv.webview.addEventListener("page-title-updated", (e: any) => {
        tab.title = e.title || "";
        this.renderTabBar();
      });
      // Keep the hover URL fresh across in-page and hard navigations.
      bv.webview.addEventListener("did-navigate", () => this.refreshTabTooltips());
      bv.webview.addEventListener("did-navigate-in-page", () => this.refreshTabTooltips());
    } else if (tab.view === "license") {
      container.innerHTML = `<div class="child-view-content"><pre class="child-raw-text">${t("common.loading")}</pre></div>`;
      if (window.electronAPI) {
        window.electronAPI.getLicenseContent().then((content: string) => {
          const pre = container.querySelector(".child-raw-text");
          if (pre) pre.textContent = content;
        });
      }
    } else if (LEGAL_DOC_VIEWS.has(tab.view)) {
      container.innerHTML = `
        <div class="child-doc-container">
          <div class="child-doc-toolbar">
            <div class="settings-dropdown-wrap" id="child-region-wrap">
              <button class="settings-dropdown-trigger" id="child-region-trigger" type="button">
                <span>${t(regionLabelKey(this._region))}</span>
                <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
              </button>
              <div class="settings-dropdown" id="child-region-dropdown">
                ${this.buildRegionDropdownItems()}
              </div>
            </div>
          </div>
          <div class="child-view-content"><div class="child-view-body msg-text"><p>Loading...</p></div></div>
        </div>`;

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: container });
      }

      const loadDoc = (region: string) => {
        const body = container.querySelector(".child-view-body");
        if (body) body.innerHTML = "<p>Loading...</p>";
        if (window.electronAPI) {
          window.electronAPI.getDocumentContent(tab.view, region).then((content: string) => {
            const bodyEl = container.querySelector(".child-view-body");
            if (bodyEl) bodyEl.innerHTML = renderMarkdown(content);
          });
        }
      };

      this.bindChildRegionDropdown("child-region", (val) => {
        this._region = val;
        if (window.electronAPI && window.electronAPI.setDocRegion) {
          window.electronAPI.setDocRegion(val);
        }
        loadDoc(val);
      });

      if (window.electronAPI && window.electronAPI.getDocRegion) {
        window.electronAPI
          .getDocRegion()
          .then((saved: string) => {
            if (saved && this.isValidDocRegion(saved)) {
              this._region = saved;
              const trigger = document.getElementById("child-region-trigger");
              const dropdown = document.getElementById("child-region-dropdown");
              const item = dropdown?.querySelector(`.settings-dropdown-item[data-value="${saved}"]`) as HTMLElement | null;
              if (trigger && item) {
                trigger.querySelector("span")!.textContent = item.textContent || "";
                dropdown!.querySelectorAll(".settings-dropdown-item").forEach((el) => el.classList.remove("selected"));
                item.classList.add("selected");
              }
            }
          })
          .catch(() => {})
          .finally(() => loadDoc(this._region));
      } else {
        loadDoc(this._region);
      }
    } else if (tab.view === "easter-egg") {
      container.innerHTML = `<div class="easter-egg-container" style="position:absolute;inset:0;overflow:hidden;background:#000"></div>`;
      const eggEl = container.querySelector(".easter-egg-container") as HTMLElement;
      if (eggEl) {
        tab._nebulaCleanup = mountNebula(eggEl);
      }
    } else if (tab.view === "logs") {
      container.innerHTML = `
        <div class="log-view">
          <div class="log-table">
            <div class="log-table-header">
              <div class="log-cell log-cell-ts">${t("settings.aboutLogTimestamp")}</div>
              <div class="log-cell log-cell-lvl">${t("settings.aboutLogLevel")}</div>
              <div class="log-cell log-cell-src">${t("settings.aboutLogSource")}</div>
              <div class="log-cell log-cell-msg">${t("settings.aboutLogMessage")}</div>
            </div>
            <div class="log-table-body" id="log-table-body">
              <div class="log-loading">${t("settings.aboutLogLoading")}</div>
            </div>
          </div>
        </div>
        <div class="context-menu hidden" id="log-context-menu"></div>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: container });
      }
      this._initLogView(container);
    } else if (tab.view.startsWith("mailto:")) {
      window.location.href = tab.view;
    } else {
      container.innerHTML = `<div class="si-panel-empty" style="flex:1;gap:14px">
        <i data-lucide="file-question" class="lucide" style="width:32px;height:32px;color:var(--text-muted)"></i>
        <div class="si-panel-empty-title">${this.esc(tab.label)}</div>
        <div class="si-panel-empty-sub" style="font-size:12px;color:var(--text-muted)">${t("app.comingSoon")}</div>
      </div>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: container });
      }
    }

    // Show the newly created tab and hide every other one. Must run AFTER
    // content creation: BrowserView overwrites container.style.cssText with
    // display:flex, so this is the single place that normalizes visibility
    // for all tab types (webview / docs / logs / easter-egg …).
    this._showTabContent(tab);
  }

  private _initLogView(container: HTMLElement): void {
    const bodyEl = container.querySelector("#log-table-body") as HTMLElement | null;
    const ctxMenu = container.querySelector("#log-context-menu") as HTMLElement | null;

    let allEntries: { timestamp: string; level: string; source: string; message: string }[] = [];
    let ctxEntry: typeof allEntries[0] | null = null;

    const render = () => {
      if (!bodyEl) return;
      ctxMenu?.classList.add("hidden");
      if (allEntries.length === 0) {
        bodyEl.innerHTML = `<div class="si-empty-center"><i data-lucide="scroll-text" class="lucide"></i><span class="si-empty-title">${t("settings.aboutLogNoLogs")}</span></div>`;
        if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: bodyEl });
        return;
      }
      bodyEl.innerHTML = allEntries.map((e) => {
        const levelClass = "log-lvl-" + e.level.toLowerCase();
        const ts = e.timestamp.replace(/,\d{3}$/, "");
        return `<div class="log-row">
          <div class="log-cell log-cell-ts">${ts}</div>
          <div class="log-cell log-cell-lvl"><span class="log-level-badge ${levelClass}">${e.level}</span></div>
          <div class="log-cell log-cell-src">${this.esc(e.source)}</div>
          <div class="log-cell log-cell-msg"><span class="log-msg-text">${this.esc(e.message)}</span></div>
        </div>`;
      }).join("");
    };

    // ── Context menu on .log-row right-click ────────────────────────
    const onCtx = (ev: MouseEvent) => {
      const row = (ev.target as HTMLElement).closest(".log-row") as HTMLElement | null;
      if (!row || !ctxMenu) return;
      const idx = Array.from(bodyEl?.querySelectorAll(".log-row") || []).indexOf(row);
      const entry = allEntries[idx];
      if (!entry) return;
      ctxEntry = entry;
      // Keep the right-clicked row highlighted while the menu is open —
      // hover alone fades out as soon as the pointer moves onto the menu.
      container.querySelectorAll(".log-row.ctx-target").forEach((r) => r.classList.remove("ctx-target"));
      row.classList.add("ctx-target");
      ctxMenu.innerHTML = `
        <div class="context-menu-item" data-action="copy-row"><i data-lucide="copy" class="lucide lucide-sm"></i>${t("settings.aboutLogCopyRow")}</div>
        <div class="context-menu-item" data-action="copy-all"><i data-lucide="files" class="lucide lucide-sm"></i>${t("settings.aboutLogCopyAll")}</div>`;
      showContextMenu(ctxMenu, ev.clientX, ev.clientY);
      requestAnimationFrame(() => (window as any).lucide.createIcons({ root: ctxMenu }));
      ev.preventDefault();
    };
    const boundCtx = onCtx.bind(this);
    document.addEventListener("contextmenu", boundCtx);

    // ── Context menu item clicks ────────────────────────────────────
    if (ctxMenu) {
      ctxMenu.addEventListener("click", (ev) => {
        const item = (ev.target as HTMLElement).closest(".context-menu-item") as HTMLElement | null;
        if (!item) return;
        const action = item.getAttribute("data-action");
        ctxMenu.classList.add("hidden");
        container.querySelectorAll(".log-row.ctx-target").forEach((r) => r.classList.remove("ctx-target"));
        if (action === "copy-row" && ctxEntry) {
          navigator.clipboard.writeText(`[${ctxEntry.timestamp}] [${ctxEntry.level}] ${ctxEntry.source}: ${ctxEntry.message}`).catch(() => {});
        } else if (action === "copy-all") {
          const text = allEntries.map(e => `[${e.timestamp}] [${e.level}] ${e.source}: ${e.message}`).join("\n");
          navigator.clipboard.writeText(text).catch(() => {});
        }
      });
    }

    document.addEventListener("click", () => {
      ctxMenu?.classList.add("hidden");
      container.querySelectorAll(".log-row.ctx-target").forEach((r) => r.classList.remove("ctx-target"));
    }, false);

    // ── Lazy / windowed loading (scroll triggers backend batch fetch) ──
    const api = window.electronAPI;
    if (api) {
      let loadedCount = 0;
      let total = 0;
      let loading = false;
      const BATCH = 500;

      const loadMore = async () => {
        if (loading) return;
        if (total > 0 && loadedCount >= total) return;
        loading = true;
        try {
          const result = await api.getLogs({ offset: loadedCount, limit: BATCH });
          if (result.entries && result.entries.length > 0) {
            allEntries.push(...result.entries);
            loadedCount += result.entries.length;
            total = result.total;
            render();
          }
        } catch {
          if (loadedCount === 0 && bodyEl) {
            bodyEl.innerHTML = `<div class="si-empty-center"><i data-lucide="scroll-text" class="lucide"></i><span class="si-empty-title">${t("settings.aboutLogNoLogs")}</span></div>`;
            if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root: bodyEl });
          }
        }
        loading = false;
      };

      loadMore();

      if (bodyEl) {
        bodyEl.addEventListener("scroll", () => {
          const scrollBottom = bodyEl.scrollTop + bodyEl.clientHeight;
          if (scrollBottom >= bodyEl.scrollHeight - 200) {
            loadMore();
          }
        });
      }

      // ── Auto-refresh: poll for new entries every 5 seconds ──────
      let lastSize = -1;
      const refreshIntervalId = window.setInterval(async () => {
        if (!bodyEl || !document.body.contains(bodyEl)) {
          clearInterval(refreshIntervalId);
          return;
        }
        if (loading) return;
        try {
          const info = await api.getLogFileInfo();
          if (!info.exists) return;
          if (lastSize >= 0 && info.size === lastSize) return;
          lastSize = info.size;
          // File changed — reload from top
          loading = true;
          const result = await api.getLogs({ offset: 0, limit: BATCH });
          if (result && result.entries) {
            allEntries = result.entries;
            loadedCount = result.entries.length;
            total = result.total;
            render();
          }
          loading = false;
        } catch {
          loading = false;
        }
      }, 5000);
    }
  }

  private static readonly DOC_REGION_GROUPS: { labelKey: string; regions: { value: string; labelKey: string }[] }[] = [
    {
      labelKey: "settings.aboutRegionGroupAmericas",
      regions: [
        { value: "us", labelKey: "settings.aboutRegionUs" },
        { value: "ca", labelKey: "settings.aboutRegionCa" },
        { value: "mx", labelKey: "settings.aboutRegionMx" },
        { value: "br", labelKey: "settings.aboutRegionBr" },
      ],
    },
    {
      labelKey: "settings.aboutRegionGroupEurope",
      regions: [
        { value: "eu", labelKey: "settings.aboutRegionEu" },
        { value: "uk", labelKey: "settings.aboutRegionUk" },
        { value: "ch", labelKey: "settings.aboutRegionCh" },
        { value: "tr", labelKey: "settings.aboutRegionTr" },
      ],
    },
    {
      labelKey: "settings.aboutRegionGroupAsia",
      regions: [
        { value: "cn", labelKey: "settings.aboutRegionCn" },
        { value: "jp", labelKey: "settings.aboutRegionJp" },
        { value: "kr", labelKey: "settings.aboutRegionKr" },
        { value: "tw", labelKey: "settings.aboutRegionTw" },
        { value: "hk", labelKey: "settings.aboutRegionHk" },
        { value: "mo", labelKey: "settings.aboutRegionMo" },
        { value: "sg", labelKey: "settings.aboutRegionSg" },
        { value: "in", labelKey: "settings.aboutRegionIn" },
        { value: "ae", labelKey: "settings.aboutRegionAe" },
        { value: "sa", labelKey: "settings.aboutRegionSa" },
        { value: "il", labelKey: "settings.aboutRegionIl" },
      ],
    },
    {
      labelKey: "settings.aboutRegionGroupAfrica",
      regions: [
        { value: "za", labelKey: "settings.aboutRegionZa" },
        { value: "ng", labelKey: "settings.aboutRegionNg" },
        { value: "ke", labelKey: "settings.aboutRegionKe" },
        { value: "eg", labelKey: "settings.aboutRegionEg" },
      ],
    },
    {
      labelKey: "settings.aboutRegionGroupOceania",
      regions: [
        { value: "au", labelKey: "settings.aboutRegionAu" },
        { value: "nz", labelKey: "settings.aboutRegionNz" },
      ],
    },
  ];

  private isValidDocRegion(value: string): boolean {
    return App.DOC_REGION_GROUPS.some((g) => g.regions.some((r) => r.value === value));
  }

  private buildRegionDropdownItems(): string {
    return App.DOC_REGION_GROUPS.map(
      (group) =>
        `<div class="settings-dropdown-group">${t(group.labelKey)}</div>` +
        group.regions
          .map(
            (r) =>
              `<div class="settings-dropdown-item${r.value === this._region ? " selected" : ""}" data-value="${r.value}">${t(r.labelKey)}</div>`
          )
          .join("")
    ).join("");
  }

  private bindChildRegionDropdown(id: string, onChange: (val: string) => void): void {
    const wrap = document.getElementById(`${id}-wrap`);
    const trigger = document.getElementById(`${id}-trigger`);
    const dropdown = document.getElementById(`${id}-dropdown`);
    if (!wrap || !trigger || !dropdown) return;

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = dropdown.classList.contains("open");
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      if (!isOpen) dropdown.classList.add("open");
    });

    dropdown.querySelectorAll(".settings-dropdown-item").forEach((item) => {
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        const val = (item as HTMLElement).getAttribute("data-value") || "";
        const label = (item as HTMLElement).textContent || "";
        trigger.querySelector("span")!.textContent = label;
        dropdown.classList.remove("open");
        dropdown.querySelectorAll(".settings-dropdown-item").forEach((el) => el.classList.remove("selected"));
        (item as HTMLElement).classList.add("selected");
        onChange(val);
      });
    });

    document.addEventListener("click", (e) => {
      if (!wrap.contains(e.target as Node)) {
        dropdown.classList.remove("open");
      }
    });
  }

  private updateStats(): void {
    if (!this.tokenCountEl) return;
    const s = getState();
    let totalIn = 0;
    let totalOut = 0;
    for (const msg of s.messages) {
      if (msg.role === "assistant" && msg.tokenUsage) {
        totalIn += msg.tokenUsage.input_tokens;
        totalOut += msg.tokenUsage.output_tokens;
      }
    }
    const totalAll = totalIn + totalOut;
    if (s.telemetry) {
      this.tokenCountEl.textContent = t("app.toolsTokens", { count: s.telemetry.total_tool_calls, tokens: this.fmtTokens(totalAll) });
      this.tokenCountEl.dataset.tooltip = t("app.inputOutput", { input: this.fmtTokens(totalIn), output: this.fmtTokens(totalOut) }) + ` | ${t("app.turns")}: ${s.telemetry.total_turns} | ` + `${t("app.duration")}: ${s.telemetry.session_duration_s.toFixed(0)}s`;
    } else if (totalAll > 0) {
      this.tokenCountEl.textContent = `${this.fmtTokens(totalAll)} ${t("app.tokens")}`;
      this.tokenCountEl.dataset.tooltip = t("app.inputOutput", { input: this.fmtTokens(totalIn), output: this.fmtTokens(totalOut) });
    } else {
      this.tokenCountEl.textContent = "";
    }
  }

  private fmtTokens(n: number): string {
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  private updateSessionBarName(): void {
    const s = getState();
    const bar = document.getElementById("session-bar");
    const nameEl = document.getElementById("session-bar-name") as HTMLElement | null;

    if (!bar) return;

    const hasMessages = s.messages.length > 0;
    const isSubAgentActive = !!(s.subAgentView || s.subAgentBreadcrumb.length > 0);
    bar.style.display = (hasMessages || isSubAgentActive) ? "" : "none";

    if (!nameEl) return;
    const isAutomationView = !!(window as any).__isAutomationView;
    const rootLabel = isAutomationView
      ? t("automation.title")
      : s.tempChat
      ? t("session.tempChatActive")
      : (() => {
          const entry = s.sessionsList.find((x) => x.session_id === s.sessionId);
          return s.sessionId
            ? (entry?.name || entry?.preview || s.sessionId.slice(0, 8))
            : t("session.newSession");
        })();
    const inputArea = document.getElementById("input-area");
    const sessionMenu = document.getElementById("btn-session-menu");
    const mainContent = document.getElementById("main-content");
    const breadcrumb = s.subAgentBreadcrumb;
    const _truncate = (s: string, max = 18) => s.length > max ? s.slice(0, max) + "…" : s;
    const isSubAgentView = !!(s.subAgentView || breadcrumb.length > 0);
    // Show/hide the automation back button in the header bar
    const autoBackBtn = document.getElementById("btn-automation-back");
    const toggleBtn = document.getElementById("btn-toggle-sidebar");
    const searchBtn = document.getElementById("btn-sidebar-search");
    // isDetailVisible(): we're inside an automation execution's sub-agent
    // detail view. The header shows the Back button + the sidebar Search
    // button (so search is reachable). The sidebar-toggle stays visible but
    // disabled (greyed out) since the sidebar is force-collapsed in automation
    // (disabled state is managed by AutomationPanel).
    const isAutomationDetail = this.automation?.isDetailVisible?.() ?? false;
    const isAutomationSubAgent = (isAutomationView && isSubAgentView) || isAutomationDetail;
    if (autoBackBtn && toggleBtn && searchBtn) {
      if (isAutomationDetail) {
        autoBackBtn.classList.remove("hidden");
        toggleBtn.style.display = "none";
        searchBtn.style.display = "";
      } else if (isAutomationSubAgent) {
        autoBackBtn.classList.remove("hidden");
        toggleBtn.style.display = "none";
        searchBtn.style.display = "none";
      } else {
        autoBackBtn.classList.add("hidden");
        toggleBtn.style.display = "";
        searchBtn.style.display = "";
      }
    }

    if (isSubAgentView) {
      // Build breadcrumb HTML: root / sub1 / sub2 / ... / current
      // The root crumb represents the session that spawned the first sub-agent.
      // For a session-backed sub-agent the active session IS the sub-agent's
      // own session, so `rootLabel` (derived from the current session) would
      // mislabel the root — resolve the real parent label from the first
      // breadcrumb entry's stored sessionId instead.
      let rootCrumbLabel = rootLabel;
      const rootCrumbSessionId = breadcrumb.length > 0 ? breadcrumb[0].sessionId : s.sessionId;
      if (rootCrumbSessionId && rootCrumbSessionId !== s.sessionId) {
        const rootEntry = s.sessionsList.find((x) => x.session_id === rootCrumbSessionId);
        rootCrumbLabel = rootEntry?.name || rootEntry?.preview || rootCrumbSessionId.slice(0, 8);
      }
      let crumbsHtml = `<button class="session-crumb session-crumb-root" data-crumb-index="-1" type="button" data-tooltip="${this.esc(rootCrumbLabel)}">${this.esc(_truncate(rootCrumbLabel))}</button>`;
      // Track whether the last breadcrumb entry IS the current sub-agent view
      // to avoid visual duplicates (breadcrumb button + current span for same level).
      let lastBreadcrumbIsCurrent = false;
      for (let i = 0; i < breadcrumb.length; i++) {
        const entry = breadcrumb[i];
        const isLast = i === breadcrumb.length - 1;
        const label = formatAgentLabel(entry.name);
        crumbsHtml += `<span class="session-crumb-sep">/</span>`;
        if (isLast && (!s.subAgentView || entry.toolCallId === s.subAgentView.id)) {
          // Last crumb either has no active sub-agent view (currently at this level),
          // or it IS the active sub-agent view — render as current, not a button.
          crumbsHtml += `<span class="session-crumb-current" data-tooltip="${this.esc(label)}">${this.esc(_truncate(label))}</span>`;
          if (entry.toolCallId === s.subAgentView?.id) {
            lastBreadcrumbIsCurrent = true;
          }
        } else {
          crumbsHtml += `<button class="session-crumb" data-crumb-index="${i}" type="button" data-tooltip="${this.esc(label)}">${this.esc(_truncate(label))}</button>`;
        }
      }
      // If a sub-agent view is active and NOT already shown as the last breadcrumb,
      // show the current agent name.
      if (s.subAgentView && !lastBreadcrumbIsCurrent) {
        const curLabel = formatAgentLabel(getAgentName(s.subAgentView));
        crumbsHtml += `<span class="session-crumb-sep">/</span>`;
        crumbsHtml += `<span class="session-crumb-current" data-tooltip="${this.esc(curLabel)}">${this.esc(_truncate(curLabel))}</span>`;
      }
      nameEl.innerHTML = crumbsHtml;
      // Bind click handlers for each breadcrumb level
      nameEl.querySelectorAll("[data-crumb-index]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = parseInt((btn as HTMLElement).dataset.crumbIndex || "-1", 10);
          if (idx < 0) {
            (window as any).__goToRootView?.();
          } else {
            (window as any).__navigateToBreadcrumb?.(idx);
          }
        });
      });
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: nameEl });
      }
      // Hide input area and session menu in sub-agent view
      if (inputArea) inputArea.style.display = "none";
      if (sessionMenu) sessionMenu.style.display = "none";
      if (mainContent) mainContent.classList.add("sub-agent-active");
      return;
    }
    // Restore input area and session menu when leaving sub-agent view
    // Skip in child mode (ESD): child window has its own tabs and no chat input.
    const isChildMode = document.body.classList.contains("child-mode");
    if (!isChildMode) {
      if (inputArea) inputArea.style.display = "";
      if (sessionMenu) sessionMenu.style.display = "";
    }
    if (mainContent) mainContent.classList.remove("sub-agent-active");
    nameEl.textContent = rootLabel;
  }

  private _renderQueueCard(): void {
    const s = getState();
    const card = document.getElementById("queue-card");
    const body = document.getElementById("queue-card-body");
    const statusBar = document.getElementById("chat-status-bar");
    if (!card || !body) return;
    const active = s.pendingQueueCount > 0 && s.queuedPrompts.length > 0;
    const key = active
      ? s.queuedPrompts.map((p) => `${p.mode}\u0000${p.text}`).join("\u0001")
      : "";

    if (!active) {
      if (!card.classList.contains("hidden")) card.classList.add("hidden");
      if (statusBar) statusBar.style.maxWidth = "";
      this._queueRenderKey = "";
      return;
    }

    card.classList.remove("hidden");
    if (statusBar) statusBar.style.maxWidth = "calc(var(--input-max-w) - 100px)";

    // Streaming emits fire on every token update. Skip rebuilding the DOM
    // (and re-creating lucide icons) unless the queue actually changed —
    // this removes the flicker, layout thrash, and the drag-focus breakage
    // that came from replacing the drag source element mid-drag.
    if (key === this._queueRenderKey) return;
    this._queueRenderKey = key;

    body.innerHTML = s.queuedPrompts.map((p, i) =>
      `<div class="queue-item" draggable="true" data-queue-index="${i}">
        <span class="queue-item-drag-handle" data-queue-index="${i}">
          <i data-lucide="grip-vertical" class="lucide"></i>
        </span>
        <span class="queue-item-text">${this.esc(p.text)}</span>
        <button class="queue-item-send" draggable="false" data-queue-index="${i}" title="${t("chat.sendDirectly")}">
          <i data-lucide="send" class="lucide"></i>
        </button>
        <button class="queue-item-remove" draggable="false" data-queue-index="${i}">
          <i data-lucide="x" class="lucide"></i>
        </button>
      </div>`
    ).join("");
    if ((window as any).lucide) {
      (window as any).lucide.createIcons(body);
    }

    // Drag-and-drop reorder
    let dragSrcIdx: number | null = null;
    const items = body.querySelectorAll<HTMLElement>(".queue-item");
    items.forEach(item => {
      const idx = parseInt(item.getAttribute("data-queue-index") || "0", 10);

      item.addEventListener("dragstart", (e) => {
        dragSrcIdx = idx;
        (e as DragEvent).dataTransfer?.setData("text/plain", String(idx));
        item.classList.add("dragging");
      });

      item.addEventListener("dragend", () => {
        item.classList.remove("dragging");
        dragSrcIdx = null;
        items.forEach(el => el.classList.remove("drop-target"));
      });

      item.addEventListener("dragover", (e) => {
        e.preventDefault();
        if (dragSrcIdx !== null && dragSrcIdx !== idx) {
          items.forEach(el => el.classList.remove("drop-target"));
          item.classList.add("drop-target");
        }
      });

      item.addEventListener("dragleave", () => {
        item.classList.remove("drop-target");
      });

      item.addEventListener("drop", (e) => {
        e.preventDefault();
        item.classList.remove("drop-target");
        if (dragSrcIdx !== null && dragSrcIdx !== idx) {
          moveQueuedPrompt(dragSrcIdx, idx);
          // Defer the rebuild so the drag source isn't destroyed mid-drop,
          // which can leave the renderer's pointer/drag state stuck and make
          // the prompt input un-focusable afterwards.
          requestAnimationFrame(() => this._renderQueueCard());
        }
        dragSrcIdx = null;
      });
    });

    // Remove buttons
    body.querySelectorAll(".queue-item-remove").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const idx = parseInt((btn as HTMLElement).dataset.queueIndex || "0", 10);
        removeQueuedPromptAt(idx);
      });
    });

    // Send-directly buttons: cancel current task & move this item to front
    body.querySelectorAll(".queue-item-send").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const idx = parseInt((btn as HTMLElement).dataset.queueIndex || "0", 10);
        const st = getState();
        // Move this item to front of queue
        if (idx > 0) {
          moveQueuedPrompt(idx, 0);
        }
        // Cancel current running task so queue drain picks it up
        if (st.running) {
          this.btnStop.classList.add("cancelling");
          this.btnStop.style.pointerEvents = "none";
          send({ type: "cancel", session_id: st.sessionId });
        } else {
          // Nothing running: send directly from queue
          const qp = getState().queuedPrompts[0];
          if (qp) {
            removeQueuedPromptAt(0);
            addUserMessage(qp.text, qp.mode);
            startAssistantMessage();
            setSessionState("running");
            this.updateUIState(true);
            const text = typeof qp.text === "string" ? qp.text : "";
            if (text) {
              send({ type: "run", prompt: text, session_id: st.sessionId || undefined } as any);
            }
          }
        }
      });
    });
  }

  async start(): Promise<void> {
    (window as any).__perfStart = performance.now();

    // The main window is shown immediately by the main process; the in-app
    // splash screen covers boot while the startup mode's data is filled, then
    // fades out on reveal. Remaining slices (other modes' data) keep loading
    // in the background via stream events after reveal.
    this.splash.show();

    await new Promise<void>(r => requestAnimationFrame(() => r()));

    // Lazy locale tables: make sure the selected language's chunk is installed
    // before anything is painted, so a non-zh/en session never flashes the
    // English fallback first. zh/en resolve synchronously (no await cost).
    await ensureLocale(getLocale());

    const _connStart = performance.now();
    // Hold back side-panel planes from the moment we connect: the backend
    // pushes the whole snapshot on connect, and applying the non-critical
    // planes during boot only steals main-thread time from the reveal.
    holdNonCriticalPlanes();
    await connect(handleEvent);
    if (getState().connected) {
      console.log("[perf] ws open after %dms", Math.round(performance.now() - _connStart));
      // The backend pushes the full snapshot on connect (`_push_all_data`), so
      // boot pulls only the plane that push does not cover (archived sessions)
      // instead of re-requesting — and re-rendering — the same 12 planes.
      pullBootData();
    }

    // Wait for the startup mode's priority data so the window opens already
    // populated for the mode the user chose. Hard-capped so a zero-session
    // user (or a slow model provider blocking list_models) cannot hold the
    // reveal forever.
    const _fillDeadline = performance.now() + 3000;
    while (performance.now() < _fillDeadline) {
      if (this._priorityDataReady()) break;
      await new Promise((r) => setTimeout(r, 80));
    }
    console.log("[perf] initial data present at %dms", Math.round(performance.now() - (window as any).__perfStart));

    if (getState().connected) {
      // Reveal the (now data-filled) main UI, then let the held-back
      // side-panel planes land in one batch — they no longer compete with the
      // boot-critical planes for the main thread while the splash is covering.
      this.splash.hide();
      releaseNonCriticalPlanes();
    } else {
      // Backend never came up: keep the splash showing the error + retry.
      await this.handleSplashError(t("app.splashNoConnect"));
      return;
    }
  }

  /**
   * True once the data the *selected startup mode* needs is present, so the
   * window opens in that mode already populated. Other modes' data (workspace
   * lists, automation jobs, …) keeps filling in the background afterwards.
   */
  private _priorityDataReady(): boolean {
    const st = getState();
    if (!st.connected) return false;
    // config_data has been processed (settings populated from backend).
    if (!("startup_session_mode" in st.settings)) return false;
    const mode = st.settings.startup_session_mode as string;
    if (mode === "automation") {
      return this.modeTransition.getCurrentMode() === "automation";
    }
    if (mode === "iwork") {
      return st.sessionsList.length > 0 || st.allSessions.length > 0;
    }
    // normal (通用): general-mode chat needs the session lists.
    return st.sessionsList.length > 0 || st.allSessions.length > 0;
  }

  private async handleSplashError(message: string): Promise<void> {
    // Ensure the splash (with the error/retry UI) is actually visible — it is
    // not shown at boot anymore since the main window reveals the real UI.
    this.splash.show();
    let detail = "";
    try {
      const status = await window.electronAPI?.getServiceStatus?.();
      if (status?.error) {
        detail = status.error;
      }
    } catch {}
    this.splash.showError(message, detail, async () => {
      try {
        const result = await window.electronAPI?.restartService?.();
        if (result?.success) {
          location.reload();
        } else {
          this.splash.showError(
            t("app.splashRestartFailed"),
            result?.error || "",
            () => this.handleSplashError(t("app.splashRetry"))
          );
        }
      } catch (err) {
        this.splash.showError(
          t("app.splashRestartFailed"),
          String(err),
          () => this.handleSplashError(t("app.splashRetry"))
        );
      }
    });
    // Block until the user resolves the error (page reloads or they keep trying)
    await new Promise<void>(() => {});
  }

  private bindInput(): void {
    this.btnSend.addEventListener("click", () => this.submit());
    this.btnStop.addEventListener("click", () => this.cancel());

    // Toolbar mode chip X button - exits persistent mode
    const btnModeClose = document.getElementById("btn-mode-close");
    if (btnModeClose) {
      btnModeClose.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const oldMode = this._persistentMode;
        this._persistentMode = "";
        this.updateChipState();
        this.updatePlaceholder();
        const st = getState();
        console.log("[mode-close] clearing mode:", oldMode, "sessionId:", st.sessionId);
        send({ type: "set_mode", mode: "", session_id: st.sessionId || undefined } as any);
      });
    }

    this.input.addEventListener("keydown", (e) => {
      const sendMode = (getState().settings.shortcut_send_mode as string) || "enter";
      const isCtrlEnter = sendMode === "ctrl_enter";
      const isSendKey = isCtrlEnter
        ? e.key === "Enter" && (e.ctrlKey || e.metaKey)
        : e.key === "Enter" && !e.shiftKey;

      if (isSendKey) {
        e.preventDefault();
        const text = this.getPlainText();

        // Don't re-activate slash command when a mode chip is already present
        if (!this.hasModeChip() && !this.hasCommandChip()) {
          // Check slash command match (e.g. /plan → activate mode)
          const parsed = parseSlashInput(text);
          if (parsed) {
            this.activateSlashCommand(parsed.command, parsed.rest);
            return;
          }
        }

        // If slash menu is active, check for matched command first
        if (this._slashActive) {
          const dd = document.getElementById("mention-dropdown");
          const bm = document.getElementById("btn-mention");
          const matched = dd?.querySelector(".mention-match") as HTMLElement | null;
          if (matched) {
            const action = matched.getAttribute("data-action");
            const cmd = SLASH_COMMANDS.find(c => c.id === action || c.name === action);
            if (cmd) {
              const m = text.match(/^\/[^\s]+\s+([\s\S]*)$/);
              this.activateSlashCommand(cmd, m ? m[1] : undefined);
              return;
            }
          }
          dd?.classList.add("hidden");
          bm?.classList.remove("active");
          this._slashActive = false;
          this.updatePlaceholder();
          this.resizeInput();
          this.updateSendButton();
          return;
        }

        this.submit();
      }
      if (e.key === "Escape") {
        if (this.hasCommandChip()) {
          this.deactivateCommandChip();
          return;
        }
        if (this._slashActive) {
          this._slashActive = false;
          this.input.innerHTML = "";
          const dd = document.getElementById("mention-dropdown");
          const bm = document.getElementById("btn-mention");
          dd?.classList.add("hidden");
          bm?.classList.remove("active");
          this.updatePlaceholder();
          this.resizeInput();
          this.updateSendButton();
          return;
        }
        this.cancel();
      }
    });

    this.input.addEventListener("focus", () => {
      const chip = this.input.querySelector('.mode-chip[data-mode]');
      if (!chip) return;
      const sel = window.getSelection();
      if (!sel || !sel.rangeCount) return;
      const range = sel.getRangeAt(0);
      const fakeEls = this.input.querySelectorAll('[contenteditable="false"]');
      const lastFake = fakeEls[fakeEls.length - 1];
      if (!lastFake) return;
      const afterLast = new Range();
      afterLast.setStartAfter(lastFake);
      afterLast.collapse(true);
      if (range.compareBoundaryPoints(Range.END_TO_END, afterLast) < 0) {
        sel.removeAllRanges();
        sel.addRange(afterLast);
      }
    });

    this.input.addEventListener("input", () => {
      if (this.skipNextInput) {
        this.skipNextInput = false;
        return;
      }
      this.detectSlashCommand();
      this.handleManualSlashCommand();
      this.updateChipState();
      this.resizeInput();
      this.updateInputExpandButton();
      this.updatePlaceholder();
      this.updateSendButton();
      this.saveCurrentDraft();
    });

    // Composer expand/shrink toggle (top-right inside the input box).
    this.inputExpandBtn.addEventListener("click", () => this.toggleInputExpand());

    // Strip all formatting on paste so the composer always contains plain text.
    this.input.addEventListener("paste", (e) => {
      e.preventDefault();
      const text = e.clipboardData?.getData("text/plain") ?? "";
      if (!text) return;
      document.execCommand("insertText", false, text);
      // Explicit resize: execCommand may dispatch "input" before the new
      // text is fully committed to layout; resizeInput re-measures on rAF.
      this.resizeInput();
    });

    // Watch for mode-chip removal so we can clean up state when user deletes it
    const chipObserver = new MutationObserver(() => {
      const mode = this.getCurrentMode();
      if (this._currentChipMode && !mode) {
        this._currentChipMode = "";
        this.updateChipState();
        this.updatePlaceholder();
        this.updateSendButton();
      } else if (mode) {
        this._currentChipMode = mode;
      }
    });
    chipObserver.observe(this.input, { childList: true, subtree: true });

    // Re-measure whenever the composer content changes without a native
    // "input" event (e.g. textContent/innerHTML writes from chat/search/
    // stream restore the last user message into the box). Resize coalesces
    // to a single rAF per frame, so this is cheap even on every keystroke.
    const inputContentObserver = new MutationObserver(() => {
      this.resizeInput();
      this.updateInputExpandButton();
    });
    inputContentObserver.observe(this.input, { childList: true, characterData: true, subtree: true });

    // Mirror `#input-area`'s natural height so we can preserve the chat
    // container's geometry when the composer floats (setInputExpanded).
    // Updating `padding-bottom` is gated by the expanded state and lives
    // in syncInputAreaHeight() — this observer only refreshes the cache.
    this._inputAreaResizeObs = new ResizeObserver(() => this.refreshInputNaturalHeight());
    this._inputAreaResizeObs.observe(this.inputArea);
    this.refreshInputNaturalHeight();
    // Re-apply the overlay padding after viewport changes (the composer
    // itself is observed, so this is mostly belt-and-braces).
    window.addEventListener("resize", () => this.refreshInputNaturalHeight());

    // ── Toolbar mode-chip close button ────────────────────────────
    const closeBtn = document.getElementById("btn-mode-close");
    if (closeBtn) {
      closeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.deactivateModeChip();
      });
    }
    // ── Toolbar command-chip close button ─────────────────────────
    const cmdCloseBtn = document.getElementById("btn-command-close");
    if (cmdCloseBtn) {
      cmdCloseBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.deactivateCommandChip();
      });
    }
  }

  private detectSlashCommand(): void {
    const text = this.getPlainText();
    const mentionDropdown = document.getElementById("mention-dropdown");
    const btnMention = document.getElementById("btn-mention");
    if (!mentionDropdown || !btnMention) return;

    const mainPage = mentionDropdown.querySelector('[data-page="main"]');
    const slashPage = mentionDropdown.querySelector('[data-page="slash"]');

    // Clear all match highlights
    mentionDropdown.querySelectorAll(".mention-match").forEach(el => el.classList.remove("mention-match"));

    if (text.startsWith("/") && !this.hasModeChip() && !this.hasCommandChip()) {
      // Re-render slash dropdown to pick up any custom commands that
      // arrived after initial render (e.g. via slash-commands-updated).
      this.renderSlashDropdown();
      if (mentionDropdown.classList.contains("hidden")) {
        mentionDropdown.classList.remove("hidden");
        btnMention.classList.add("active");
      }
      this._slashActive = true;

      // Slash mode → always show only slash sub-page, never Files/Folder
      mainPage?.classList.add("hidden");
      slashPage?.classList.remove("hidden");

      // Highlight matching slash commands (name OR alias, case-insensitive).
      const query = text.slice(1).toLowerCase();
      if (query) {
        mentionDropdown.querySelectorAll('[data-slash]').forEach(el => {
          const name = (el.getAttribute("data-slash") || "").toLowerCase();
          const aliases = (el.getAttribute("data-aliases") || "").split(",").filter(Boolean);
          const matched = name.startsWith(query) || aliases.some((a) => a.startsWith(query));
          if (matched) {
            el.classList.add("mention-match");
          }
        });
      }
    } else if (this._slashActive) {
      mentionDropdown.classList.add("hidden");
      btnMention.classList.remove("active");
      this._slashActive = false;
    }
  }

  private handleManualSlashCommand(): void {
    const text = this.getPlainText();

    if (this.hasModeChip() || this.hasCommandChip()) return;
    const parsed = parseSlashInput(text);
    if (!parsed) return;
    this.activateSlashCommand(parsed.command, parsed.rest);
  }

  private registerCommandActions(): void {
    commandActions["settings"] = () => { this.automationPanel.hide(); this.settings.open(); };
    commandActions["model-management"] = () => { this.automationPanel.hide(); this.settings.open(); this.settings.switchPanel("model"); };
    commandActions["skills-management"] = () => { this.automationPanel.hide(); this.settings.open(); this.settings.switchPanel("skills"); };
    commandActions["mcp-servers"] = () => { this.automationPanel.hide(); this.settings.open(); this.settings.switchPanel("mcp"); };
    commandActions["agent-config"] = () => { this.automationPanel.hide(); this.settings.open(); this.settings.switchPanel("agent"); };
    commandActions["about"] = () => { this.automationPanel.hide(); this.settings.open(); this.settings.switchPanel("about"); };
    commandActions["theme-dark"] = () => {
      setThemePreference("dark");
      setTheme("dark");
      localStorage.setItem("encre-theme", "dark");
    };
    commandActions["theme-light"] = () => {
      setThemePreference("light");
      setTheme("light");
      localStorage.setItem("encre-theme", "light");
    };
    commandActions["theme-system"] = () => {
      setThemePreference("system");
      const isDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      setTheme(isDark ? "dark" : "light");
      localStorage.setItem("encre-theme", "system");
    };
    for (const code of LOCALES) {
      commandActions[`language-${code}`] = () => {
        const current = { ...getState().settings, language: code };
        setSettings(current);
        setLocale(code);
        send({ type: "configure", config: { language: code } });
      };
    }
    commandActions["new-session"] = () => {
      this.automationPanel.hide();
      this.exitTempChat();
      this.cleanupContentArea({ keepAutomationFlag: false });
      resetChat();
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "new_session", request_id: requestId });
    };
    commandActions["keyboard-shortcuts"] = () => {
    };
    commandActions["automation"] = () => { this.automationPanel.show(); };
    commandActions["workflow"] = () => { this.automationPanel.show(); };
  }

  private bindGlobalLinkInterceptor(): void {
    document.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      if (!target || !target.closest) return;
      const link = target.closest("a") as HTMLAnchorElement | null;
      if (!link || !link.href) return;
      const behavior = (getState().settings.default_link_behavior as string) || "system";
      if (behavior !== "in_app") return;
      let url = link.href;
      if (!url) return;
      const isWeb = /^(https?:\/\/|www\.)/i.test(url);
      if (!isWeb) return;
      if (/^www\./i.test(url)) url = "https://" + url;
      e.preventDefault();
      e.stopPropagation();
      const api = (window as any).electronAPI;
      if (api?.openChildWindow) {
        api.openChildWindow(url, url);
      } else {
        window.open(url, "_blank");
      }
    }, true);
  }

  private bindToolbarButtons(): void {
    const toggleSidebar = document.getElementById("btn-toggle-sidebar");
    toggleSidebar?.addEventListener("click", () => {
      this.userToggledSidebar = true;
      document.getElementById("app")?.classList.toggle("sidebar-collapsed");
    });

    // Automation back button: returns to the automation panel
    document.getElementById("btn-automation-back")?.addEventListener("click", () => {
      if (this.automation.isDetailVisible()) {
        this.automation.hideDetail();
      } else {
        (window as any).__closeSubAgentView?.();
      }
    });

    // ── Header mode switch (shared .seg component with the tray popup) ──
    // Unified through ModeTransitionManager so all transitions are properly
    // sequenced (no racing between automation hide and workspace enter/exit).
    const modeSeg = document.getElementById("mode-seg");
    if (modeSeg) {
      const items = modeSeg.querySelectorAll<HTMLElement>(".seg-item");
      items.forEach((item) => {
        item.addEventListener("click", () => {
          const mode = item.getAttribute("data-mode");
          // Every seg tab switches straight to its own mode — clicking the
          // already-active automation tab is a no-op, never a hop back to a
          // previously-visited mode. (Keyboard/tray keep the toggle.)
          this.modeTransition.switchMode(mode as AppMode);
        });
      });
      const syncSegFromState = () => {
        let active: string;
        if (this.automationPanel.isActive) {
          active = "automation";
        } else {
          active = getState().workspaceMode === "iwork" ? "iwork" : "normal";
        }
        items.forEach((el) => {
          const isActive = el.getAttribute("data-mode") === active;
          el.classList.toggle("active", isActive);
          el.setAttribute("aria-selected", isActive ? "true" : "false");
        });
      };
      syncSegFromState();
      subscribe(syncSegFromState);
      this.automationPanel.onHide = syncSegFromState;
    }

    const newTaskBtn = document.querySelector('.nav-item[data-view="chat"]');
    newTaskBtn?.addEventListener("click", async () => {
      // Hide automation view if active (no mode switch - just clean up)
      if (this.automationPanel.isActive) {
        await this.automationPanel.hide();
      }
      this.exitTempChat();
      this.cleanupContentArea({ keepAutomationFlag: false });
      resetChat();
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "new_session", request_id: requestId });
    });

    const tempChatBtn = document.getElementById("btn-temp-chat");
    tempChatBtn?.addEventListener("click", () => {
      if (getState().tempChat) return;
      // Temp chat is a normal-mode feature; ignore in workspace mode.
      if (getState().workspaceMode === "iwork") return;
      this.automationPanel.hide();
      this.exitTempChat();
      this.cleanupContentArea({ keepAutomationFlag: false });
      resetChat();
      setTempChat(true);
      tempChatBtn?.classList.add("active");
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "new_session", request_id: requestId });
    });

    const btnSettingsTrigger = document.getElementById("btn-settings-trigger");
    btnSettingsTrigger?.addEventListener("click", () => {
      this.automationPanel.hide();
      this.settings.open();
    });

    // @ button: upward dropdown
    const btnMention = document.getElementById("btn-mention");
    const mentionDropdown = document.getElementById("mention-dropdown");
    btnMention?.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasHidden = mentionDropdown?.classList.contains("hidden");
      mentionDropdown?.classList.toggle("hidden");
      btnMention?.classList.toggle("active");
      this._slashActive = false;
      if (wasHidden) {
        const mainPage = mentionDropdown?.querySelector('[data-page="main"]');
        const slashPage = mentionDropdown?.querySelector('[data-page="slash"]');
        if (mainPage) mainPage.classList.remove("hidden");
        if (slashPage) slashPage.classList.add("hidden");
      }
    });

    // Close dropdown on outside click
    document.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      if (target.closest("#prompt-input") && this._slashActive) return;
      if (!target.closest("#btn-mention") && !target.closest("#mention-dropdown")) {
        mentionDropdown?.classList.add("hidden");
        btnMention?.classList.remove("active");
        this._slashActive = false;
      }
    });

    // Dropdown item actions
    mentionDropdown?.addEventListener("click", async (e) => {
      const item = (e.target as HTMLElement).closest(".mention-dropdown-item") as HTMLElement | null;
      if (!item) return;
      const action = item.getAttribute("data-action");

      // Sub-navigation: Slash Commands (enter sub-page)
      if (action === "sub-slash") {
        const mainPage = mentionDropdown.querySelector('[data-page="main"]');
        const slashPage = mentionDropdown.querySelector('[data-page="slash"]');
        mainPage?.classList.add("hidden");
        slashPage?.classList.remove("hidden");
        return;
      }

      // Sub-navigation: back to main
      if (action === "back-main") {
        const mainPage = mentionDropdown.querySelector('[data-page="main"]');
        const slashPage = mentionDropdown.querySelector('[data-page="slash"]');
        slashPage?.classList.add("hidden");
        mainPage?.classList.remove("hidden");
        return;
      }

      mentionDropdown.classList.add("hidden");
      btnMention?.classList.remove("active");

      // Don't re-activate if a mode chip already exists
      if (this.hasModeChip()) return;

      const cmd = SLASH_COMMANDS.find(c => c.id === action || c.name === action);
      if (cmd) {
        this.activateSlashCommand(cmd);
        if (cmd.kind === "mode") {
          this.input.focus();
        }
        return;
      }

      switch (action) {
        case "upload-file":
          this._slashActive = false;
          await this.files.promptForFiles();
          break;
        case "select-folder":
          this._slashActive = false;
          await this.files.promptForFolder();
          break;
      }
    });

    this.bindInputModelSelector();
    this.bindInputWorkspaceSelector();
  }

  private bindSummaryPanel(): void {
    const btn = document.getElementById("btn-summary-panel");
    const panel = document.getElementById("summary-panel");
    if (!btn || !panel) return;

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isHidden = panel.classList.contains("hidden");
      if (isHidden) {
        panel.classList.remove("hidden");
        btn.classList.add("active");
        this.renderSummaryPanel();
      } else {
        panel.classList.add("hidden");
        btn.classList.remove("active");
      }
    });

    // Close panel on outside click
    document.addEventListener("click", (e) => {
      if (panel.classList.contains("hidden")) return;
      if (!panel.contains(e.target as Node) && !btn.contains(e.target as Node)) {
        panel.classList.add("hidden");
        btn.classList.remove("active");
      }
    });

    // "View all" link → open review tab in sidebar
    const viewAll = document.getElementById("summary-view-all");
    if (viewAll) {
      viewAll.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        panel.classList.add("hidden");
        btn.classList.remove("active");
        const st = getState();
        const first = st.artifacts?.[0];
        if (st.workspaceMode === "iwork") {
          this.sessionInner.showReviewTab("", undefined);
        } else {
          this.sessionInner.showReviewTab(first?.path || "", first || undefined);
        }
      });
    }

    // Re-render when state changes
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    subscribe(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    // Single delegated listener for the done-section collapse toggle.
    // Set up once during binding so re-renders (which overwrite innerHTML)
    // never accumulate duplicate handlers.
    const progressEl = document.getElementById("summary-progress-items");
    if (progressEl) {
      progressEl.addEventListener("click", (e) => {
        const header = (e.target as HTMLElement).closest(".sp-todo-done-header");
        if (!header) return;
        e.stopPropagation();
        const section = header.parentElement as HTMLElement | null;
        if (!section) return;
        const nowCollapsed = section.classList.toggle("collapsed");
        this._summaryDoneCollapsed = nowCollapsed;
      });
    }
  }

  /** Force-open the summary panel (used when the model first creates todos). */
  openSummaryPanel(): void {
    const btn = document.getElementById("btn-summary-panel");
    if (this.summaryPanel.classList.contains("hidden")) {
      this.summaryPanel.classList.remove("hidden");
      if (btn) btn.classList.add("active");
      this.renderSummaryPanel();
    }
  }

  private renderSummaryPanel(): void {
    const st = getState();
    this.renderSummaryProgress(st);
    this.renderSummaryArtifacts(st);
    this.renderSummaryReferences(st);
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons();
    }
  }

  private renderSummaryProgress(st: ReturnType<typeof getState>): void {
    const el = document.getElementById("summary-progress-items");
    if (!el) return;
    const items = st.planItems;
    if (!items || items.length === 0) {
      el.innerHTML = `<div style="padding:6px 0;font-size:12px;color:var(--text-muted)">${t("sessionInner.noProgress")}</div>`;
      return;
    }
    const active = items.filter((i: any) => i.status !== "done");
    const done = items.filter((i: any) => i.status === "done");
    const parts: string[] = [];
    const renderItem = (item: any) => {
      let cls = "sp-todo-item";
      let icon = "circle";
      if (item.status === "done") { cls += " sp-todo-done"; icon = "check-circle-2"; }
      else if (item.status === "active") { cls += " sp-todo-active"; icon = "loader"; }
      else { cls += " sp-todo-pending"; }
      return `<div class="${cls}">
        <i data-lucide="${icon}" class="lucide lucide-sm sp-todo-icon"></i>
        <span class="sp-todo-text">${this.esc(item.text)}</span>
      </div>`;
    };
    active.slice(0, 8).forEach((i: any) => parts.push(renderItem(i)));
    if (done.length > 0) {
      // Auto-collapse once there are a few items, but remember the user's
      // own toggle so a later completion re-render does not re-collapse a
      // section the user just expanded.
      const collapsed = this._summaryDoneCollapsed ?? done.length > 2;
      parts.push(`<div class="sp-todo-done-section${collapsed ? " collapsed" : ""}">
        <div class="sp-todo-done-header">
          <i data-lucide="check-circle-2" class="lucide lucide-sm sp-todo-done-icon"></i>
          <span class="sp-todo-done-label">${t("sessionInner.doneItems", { count: done.length })}</span>
          <i data-lucide="chevron-down" class="lucide lucide-sm sp-todo-chevron"></i>
        </div>
        <div class="sp-todo-done-body">${done.slice(0, 8).map(renderItem).join("")}</div>
      </div>`);
    }
    el.innerHTML = parts.join("");
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: el });
    }
  }

  private renderSummaryArtifacts(st: ReturnType<typeof getState>): void {
    const el = document.getElementById("summary-artifacts-items");
    if (!el) return;
    const viewAll = document.getElementById("summary-view-all");
    const artifacts = st.artifacts;
    if (!artifacts || artifacts.length === 0) {
      if (viewAll) viewAll.classList.add("hidden");
      el.innerHTML = `<div style="padding:6px 0;font-size:12px;color:var(--text-muted)">${t("sessionInner.noArtifacts")}</div>`;
      return;
    }
    if (viewAll) viewAll.classList.remove("hidden");
    el.innerHTML = artifacts.slice(0, 6).map((a: any) => {
      const iconSrc = getFileIcon(a.name);
      const adds = a.diff_text ? (a.diff_text.match(/^\+/gm) || []).length : 0;
      const dels = a.diff_text ? (a.diff_text.match(/^-/gm) || []).length : 0;
      return `<div class="sp-artifact-item">
        <img src="${iconSrc}" class="sp-artifact-icon icon" style="width:14px;height:14px">
        <span class="sp-artifact-name">${this.esc(a.name)}</span>
        <span class="sp-artifact-stats">
          <span class="si-diff-add">+${adds}</span>
          <span class="si-diff-remove">-${dels}</span>
        </span>
        <a class="sp-artifact-review" href="#" data-path="${this.esc(a.path)}">
          <i data-lucide="eye" class="lucide lucide-xs"></i>
        </a>
      </div>`;
    }).join("");
    el.querySelectorAll(".sp-artifact-review").forEach((link) => {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const path = (link as HTMLElement).dataset.path!;
        this.summaryPanel.classList.add("hidden");
        const sbBtn = document.getElementById("btn-summary-panel");
        if (sbBtn) sbBtn.classList.remove("active");
        if (this.chat.onViewChanges) this.chat.onViewChanges(path);
      });
    });
  }

  private renderSummaryReferences(st: ReturnType<typeof getState>): void {
    const el = document.getElementById("summary-references-items");
    if (!el) return;
    const refs = st.references || [];
    if (refs.length === 0) {
      el.innerHTML = `<div style="padding:6px 0;font-size:12px;color:var(--text-muted)">${t("sessionInner.noRefs")}</div>`;
      return;
    }
    const shown = refs.slice(-12).reverse();

    // De-duplicate: the same memory file / search may be referenced many
    // times during a run; showing each repetition under an already-grouped
    // header is pure noise.
    const seen = new Set<string>();
    const unique = shown.filter((r) => {
      const key = `${r.tool}\u0000${r.summary}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });

    const groups: { label: string; icon: string; refs: any[] }[] = [
      { label: t("sessionInner.referencesWeb") || "Web", icon: "globe", refs: [] },
      { label: t("sessionInner.referencesMemory") || "Memory", icon: "brain", refs: [] },
      { label: t("sessionInner.referencesMcp") || "MCP", icon: "cable", refs: [] },
    ];
    for (const r of unique) {
      const t = r.tool.toLowerCase();
      if (t.startsWith("mcp__")) {
        groups[2].refs.push(r);
      } else if (t.includes("memory") || t.includes("profile")) {
        groups[1].refs.push(r);
      } else {
        groups[0].refs.push(r);
      }
    }

    const renderItem = (r: any) => {
      const text = stripRefTypePrefix(r.tool, r.summary);
      return `<div class="sp-ref-item">
        <span class="sp-ref-item-text" title="${this.esc(r.summary)}">${this.esc(text)}</span>
      </div>`;
    };

    const parts: string[] = [];
    for (const g of groups) {
      if (g.refs.length === 0) continue;
      const allHtml = g.refs.map(renderItem).join("");
      const groupKey = `${g.label}`;
      const isCollapsed = this._refCollapsedState.get(groupKey) !== false;

      parts.push(`<div class="${isCollapsed ? "collapsed" : ""}" data-ref-group="${groupKey}">
        <div class="sp-ref-header" data-ref-group="${groupKey}">
          <i data-lucide="${g.icon}" class="lucide lucide-sm sp-ref-header-icon"></i>
          <span class="sp-ref-header-label">${g.label}</span>
          <i data-lucide="chevron-down" class="lucide lucide-sm sp-ref-chevron"></i>
        </div>
        <div class="sp-ref-body">${allHtml}</div>
      </div>`);
    }

    el.innerHTML = parts.join("");

    // Attach event listeners for toggle
    el.querySelectorAll<HTMLElement>(".sp-ref-header").forEach((header) => {
      const groupKey = header.dataset.refGroup || "";
      header.addEventListener("click", () => {
        const groupEl = header.parentElement as HTMLElement;
        if (!groupEl) return;
        const wasCollapsed = groupEl.classList.contains("collapsed");
        groupEl.classList.toggle("collapsed");
        this._refCollapsedState.set(groupKey, !wasCollapsed);
      });
    });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: el });
    }
  }

  private esc(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }

  private bindInputModelSelector(): void {
    const selector = document.getElementById("input-model-selector");
    const dropdown = document.getElementById("input-model-dropdown");
    if (!selector || !dropdown) return;

    const formatLevel = (lvl: string): string => {
      if (!lvl || lvl === "default") return t("settings.thinkingDefault") || "Default";
      return lvl.charAt(0).toUpperCase() + lvl.slice(1);
    };

    const getThinkingLevels = (): string[] => {
      return ["low", "medium", "high"];
    };

    const isThinkingSelectable = (m: import("./core/types.js").ModelConfigMeta | undefined): boolean => {
      if (!m) return false;
      const tc = m.thinking_config;
      return tc ? tc.selectable === true : false;
    };

    const getCurrentLevel = (m: import("./core/types.js").ModelConfigMeta | undefined): string => {
      if (!m) return "default";
      const tc = m.thinking_config;
      const lvl = tc?.level || "";
      return lvl || "default";
    };

    // Permission-mode helpers: four user-facing modes; legacy session
    // values (accept_edits, plan, ...) collapse onto their closest card.
    const PERMISSION_MODES: Array<{ id: string; labelKey: string; icon: string }> = [
      { id: "default", labelKey: "permissions.modes.manual", icon: "user-check" },
      { id: "auto", labelKey: "permissions.modes.auto", icon: "shield-check" },
      { id: "bypass", labelKey: "permissions.modes.full", icon: "shield-off" },
      { id: "custom", labelKey: "permissions.modes.custom", icon: "sliders-horizontal" },
    ];
    const permissionModeGroup = (m: string): string =>
      m === "auto" ? "auto" : m === "bypass" ? "bypass" : m === "custom" ? "custom" : "default";
    const getPermissionModeLabel = (m: string): string => {
      const id = permissionModeGroup(m);
      const found = PERMISSION_MODES.find((x) => x.id === id);
      return found ? t(found.labelKey) : m;
    };

    const renderMainPage = (mainPage: HTMLElement) => {
      const st = getState();
      const allModels = st.modelConfigs || [];
      let activeIdx = st.activeModelIndex;
      const active = allModels[activeIdx];
      const enabledModels = allModels.filter((m) => m.enabled !== false);
      // Apply workspace model pool restriction when in iwork mode:
      // only show models that the active workspace has explicitly allowed.
      let models = enabledModels;
      if (st.workspaceMode === "iwork" && st.activeWorkspace) {
        const wsConfig = st.workspaceConfigs[st.activeWorkspace];
        if (wsConfig?.models_enabled !== false && wsConfig?.models && wsConfig.models.length > 0) {
          const allowedSet = new Set(wsConfig.models);
          models = enabledModels.filter((m) => allowedSet.has(m.model_id));
        }
      }
      let isActiveUsable = active && active.enabled !== false;

      // Auto-fallback: if the active model is disabled or not in the model pool,
      // pick the first available model and update the backend
      if (!isActiveUsable || (models.length > 0 && !models.some(m => m.model_id === active?.model_id))) {
        const fallback = models[0];
        if (fallback) {
          const fallbackIdx = allModels.indexOf(fallback);
          if (fallbackIdx >= 0 && fallbackIdx !== activeIdx) {
            activeIdx = fallbackIdx;
            isActiveUsable = true;
            setModelConfigs(allModels, activeIdx);
            send({ type: "set_active_model", model_index: activeIdx });
          }
        }
      }
      const label = isActiveUsable ? (allModels[activeIdx]?.name || allModels[activeIdx]?.model_id || "NONE") : "NONE";
      const labelEl = selector.querySelector(".input-selector-label") as HTMLElement | null;
      if (labelEl) labelEl.textContent = label;

      // Thinking level entry -- only if active model supports it
      let thinkingEntry = "";
      if (isActiveUsable && isThinkingSelectable(active)) {
        const lvlLabel = formatLevel(getCurrentLevel(active));
        thinkingEntry = `
          <button class="mention-dropdown-item" data-action="sub-thinking">
            <span>${t("settings.thinkingLevel")}</span>
            <span style="margin-inline-start:auto;color:var(--text-muted);font-size:12px">${this.esc(lvlLabel)}</span>
            <i data-lucide="chevron-right" class="lucide"></i>
          </button>`;
      }

      // Permission mode entry -- always shown below the thinking entry.
      const currentPermGroup = permissionModeGroup(
        (st as any).agentConfig?.permission_mode || "default",
      );
      const permissionEntry = `
        <button class="mention-dropdown-item" data-action="sub-permission">
          <span>${t("input.permissionMode")}</span>
          <span style="margin-inline-start:auto;color:var(--text-muted);font-size:12px">${this.esc(getPermissionModeLabel(currentPermGroup))}</span>
          <i data-lucide="chevron-right" class="lucide"></i>
        </button>`;

      // Model list — only show CONFIGURED models.  The catalog ("available
      // models") is a settings-side browsing aid for adding new models; it is
      // NOT a pickable model here, so when nothing is configured the selector
      // shows a muted hint plus the add-model action instead of a wall of
      // unconfigured sample models.
      let modelsHtml = "";
      if (models.length === 0) {
        modelsHtml = `<button class="mention-dropdown-item muted" disabled>${t("app.noModelsConfigured")}</button>`;
      } else {
        modelsHtml = models
          .map((m) => {
            const origIdx = allModels.indexOf(m);
            const sel = origIdx === activeIdx ? " active" : "";
            return `<button class="mention-dropdown-item${sel}" data-idx="${origIdx}">${this.esc(m.name || m.model_id)}</button>`;
          })
          .join("");
      }

      const divider = models.length > 0 ? `<div class="mention-dropdown-divider"></div>` : "";

      mainPage.innerHTML = modelsHtml + divider + thinkingEntry + permissionEntry + `
        <button class="mention-dropdown-item add-model-btn" data-action="add-model">
          <i data-lucide="plus" class="lucide"></i>
          <span>${t("settings.addModel")}</span>
        </button>`;

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: mainPage });
      }

      // Model item click
      mainPage.querySelectorAll(".mention-dropdown-item[data-idx]").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          const idx = parseInt((item as HTMLElement).dataset.idx || "0");
          send({ type: "set_active_model", model_index: idx });
          const lbl = selector.querySelector(".input-selector-label") as HTMLElement | null;
          if (lbl) lbl.textContent = allModels[idx]?.name || allModels[idx]?.model_id || t("app.model");
          dropdown.classList.add("hidden");
        });
      });

      // Thinking entry -> navigate to thinking page
      const thinkingBtn = mainPage.querySelector('[data-action="sub-thinking"]') as HTMLElement | null;
      if (thinkingBtn) {
        thinkingBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          mainPage.classList.add("hidden");
          const thinkingPage = dropdown.querySelector('[data-page="thinking"]') as HTMLElement;
          if (thinkingPage) {
            renderThinkingPage(thinkingPage);
            thinkingPage.classList.remove("hidden");
          }
        });
      }

      // Permission entry -> navigate to permission page
      const permissionBtn = mainPage.querySelector('[data-action="sub-permission"]') as HTMLElement | null;
      if (permissionBtn) {
        permissionBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          mainPage.classList.add("hidden");
          const permissionPage = dropdown.querySelector('[data-page="permission"]') as HTMLElement;
          if (permissionPage) {
            renderPermissionPage(permissionPage);
            permissionPage.classList.remove("hidden");
          }
        });
      }

      // Add model
      const addBtn = mainPage.querySelector('[data-action="add-model"]') as HTMLElement | null;
      if (addBtn) {
        addBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          dropdown.classList.add("hidden");
          this.settings.openModelCreate();
        });
      }
    };

    const renderThinkingPage = (thinkingPage: HTMLElement) => {
      const st = getState();
      const active = st.modelConfigs[st.activeModelIndex];
      const currentLvl = getCurrentLevel(active);
      const levels = getThinkingLevels();

      const backBtn = `<button class="mention-dropdown-item" data-action="back-model-main">
        <i data-lucide="arrow-left" class="lucide"></i>
        <span>${t("input.selectThinkingLevel")}</span>
      </button>`;

      const items = levels
        .map((lvl) => `<button class="mention-dropdown-item${lvl === currentLvl ? " active" : ""}" data-level="${lvl}">${this.esc(formatLevel(lvl))}</button>`)
        .join("");

      thinkingPage.innerHTML = backBtn + `<div class="mention-dropdown-divider"></div>` + items;

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: thinkingPage });
      }

      // Back button
      const backEl = thinkingPage.querySelector('[data-action="back-model-main"]') as HTMLElement | null;
      if (backEl) {
        backEl.addEventListener("click", (e) => {
          e.stopPropagation();
          thinkingPage.classList.add("hidden");
          const mainPage = dropdown.querySelector('[data-page="main"]') as HTMLElement;
          if (mainPage) {
            renderMainPage(mainPage);
            mainPage.classList.remove("hidden");
          }
        });
      }

      // Level selection
      thinkingPage.querySelectorAll(".mention-dropdown-item[data-level]").forEach((lvlItem) => {
        lvlItem.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const lvl = (lvlItem as HTMLElement).dataset.level || "medium";
          const currentState = getState();
          const idx = currentState.activeModelIndex;
          const modelsCopy = [...currentState.modelConfigs];
          if (idx < 0 || idx >= modelsCopy.length) return;
          const updated: import("./core/types.js").ModelConfigMeta = {
            ...modelsCopy[idx],
            thinking_config: { type: "enabled", enabled: true, level: lvl, selectable: true },
          };
          modelsCopy[idx] = updated;
          setModelConfigs(modelsCopy, currentState.activeModelIndex);
          send({ type: "update_models", models: modelsCopy, active_model_index: currentState.activeModelIndex });
          // Go back to main page
          thinkingPage.classList.add("hidden");
          const mainPage = dropdown.querySelector('[data-page="main"]') as HTMLElement;
          if (mainPage) {
            renderMainPage(mainPage);
            mainPage.classList.remove("hidden");
          }
        });
      });
    };

    const renderPermissionPage = (permissionPage: HTMLElement) => {
      const st = getState();
      const currentGroup = permissionModeGroup(
        (st as any).agentConfig?.permission_mode || "default",
      );

      const backBtn = `<button class="mention-dropdown-item" data-action="back-model-main">
        <i data-lucide="arrow-left" class="lucide"></i>
        <span>${t("input.selectPermissionMode")}</span>
      </button>`;

      const items = PERMISSION_MODES
        .map((m) => `
          <button class="mention-dropdown-item${m.id === currentGroup ? " active" : ""}" data-perm-mode="${m.id}">
            <i data-lucide="${m.icon}" class="lucide"></i>
            <span>${this.esc(t(m.labelKey))}</span>
          </button>`)
        .join("");

      permissionPage.innerHTML = backBtn
        + `<div class="mention-dropdown-divider"></div>`
        + items;

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: permissionPage });
      }

      // Back button
      const backEl = permissionPage.querySelector('[data-action="back-model-main"]') as HTMLElement | null;
      if (backEl) {
        backEl.addEventListener("click", (e) => {
          e.stopPropagation();
          permissionPage.classList.add("hidden");
          const mainPage = dropdown.querySelector('[data-page="main"]') as HTMLElement;
          if (mainPage) {
            renderMainPage(mainPage);
            mainPage.classList.remove("hidden");
          }
        });
      }

      // Mode selection: persists via update_agent; agent_updated refreshes
      // the store, but re-render immediately for snappy feedback.
      permissionPage.querySelectorAll(".mention-dropdown-item[data-perm-mode]").forEach((item) => {
        item.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const mode = (item as HTMLElement).dataset.permMode || "default";
          const cfg = (getState() as any).agentConfig;
          if (cfg) setAgentConfig({ ...cfg, permission_mode: mode });
          send({ type: "update_agent", permission_mode: mode });
          renderPermissionPage(permissionPage);
        });
      });
    };

    const render = () => {
      // Ensure page structure exists
      let mainPage = dropdown.querySelector('[data-page="main"]') as HTMLElement | null;
      let thinkingPage = dropdown.querySelector('[data-page="thinking"]') as HTMLElement | null;
      let permissionPage = dropdown.querySelector('[data-page="permission"]') as HTMLElement | null;

      if (!mainPage) {
        mainPage = document.createElement("div");
        mainPage.className = "mention-page";
        mainPage.setAttribute("data-page", "main");
        dropdown.appendChild(mainPage);
      }
      if (!thinkingPage) {
        thinkingPage = document.createElement("div");
        thinkingPage.className = "mention-page hidden";
        thinkingPage.setAttribute("data-page", "thinking");
        dropdown.appendChild(thinkingPage);
      }
      if (!permissionPage) {
        permissionPage = document.createElement("div");
        permissionPage.className = "mention-page hidden";
        permissionPage.setAttribute("data-page", "permission");
        dropdown.appendChild(permissionPage);
      }

      // Preserve whichever sub-page is currently open.  Store updates
      // (streaming stats, model list refresh, agent_updated, ...) arrive
      // asynchronously and must never snap the dropdown back to the main
      // page while the user is picking a reasoning level or a permission mode.
      if (!thinkingPage.classList.contains("hidden")) {
        renderThinkingPage(thinkingPage);
        mainPage.classList.add("hidden");
        permissionPage.classList.add("hidden");
        return;
      }
      if (!permissionPage.classList.contains("hidden")) {
        renderPermissionPage(permissionPage);
        mainPage.classList.add("hidden");
        thinkingPage.classList.add("hidden");
        return;
      }

      // No sub-page open: show the main page.
      thinkingPage.classList.add("hidden");
      permissionPage.classList.add("hidden");
      mainPage.classList.remove("hidden");
      renderMainPage(mainPage);
    };

    // Only bind event listeners once
    if (selector.dataset.modelSelectorBound === "true") {
      render();
      return;
    }
    selector.dataset.modelSelectorBound = "true";

    selector.addEventListener("click", (e) => {
      e.stopPropagation();
      // The model and workspace dropdowns must never be open at the same time.
      const wsDropdown = document.getElementById("input-workspace-dropdown");
      if (wsDropdown) wsDropdown.classList.add("hidden");
      if (dropdown.classList.contains("hidden")) {
        // Reopening always starts from the main page, even if a sub-page was
        // left open the last time the dropdown was visible.
        dropdown
          .querySelectorAll('.mention-page[data-page="thinking"], .mention-page[data-page="permission"]')
          .forEach((page) => page.classList.add("hidden"));
        render();
        dropdown.classList.remove("hidden");
        // CSS anchors it with `inset-inline-end: 0`, so under rtl it grows
        // towards the window edge — pull it back when it would overflow.
        clampIntoViewport(dropdown);
      } else {
        dropdown.classList.add("hidden");
      }
    });

    document.addEventListener("click", (e) => {
      if (!selector.contains(e.target as Node) && !dropdown.contains(e.target as Node)) {
        dropdown.classList.add("hidden");
      }
    });

    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    subscribe(() => {
      try {
        render();
        if (!dropdown.classList.contains("hidden")) {
          render();
        }
      } catch (e) {
        console.error("[model-selector] subscribe render failed:", e);
      }
    });

    render();
  }

  /**
   * Workspace selector in the input toolbar (left of the model selector).
   * Mirrors the model selector's UI exactly; only visible in workspace
   * (iwork) mode. Selecting an entry opens that workspace directly.
   */
  private bindInputWorkspaceSelector(): void {
    const wrap = document.getElementById("input-workspace-selector-wrap");
    const selector = document.getElementById("input-workspace-selector");
    const dropdown = document.getElementById("input-workspace-dropdown");
    if (!wrap || !selector || !dropdown) return;

    const render = () => {
      const st = getState();
      const wsList = st.workspaces || [];
      const activePath = st.activeWorkspace;
      const active = wsList.find((w) => w.path === activePath);

      // Only visible on the welcome screen (no messages yet) in workspace
      // mode, for picking which workspace the new session belongs to.
      // After the first send it disappears and the workspace is auto-matched.
      const showWsSelector = st.workspaceMode === "iwork" && (st.messages?.length ?? 0) === 0;
      wrap.classList.toggle("hidden", !showWsSelector);

      const labelEl = selector.querySelector(".input-selector-label") as HTMLElement | null;
      if (labelEl) labelEl.textContent = active ? active.name : t("workspace.selectWorkspace");

      let html: string;
      if (wsList.length === 0) {
        html = `<button class="mention-dropdown-item muted" disabled>${t("workspace.noWorkspaces")}</button>`;
      } else {
        html = wsList
          .map((w) => {
            const sel = w.path === activePath ? " active" : "";
            return `<button class="mention-dropdown-item${sel}" data-ws-path="${this.esc(w.path)}">
              <span>${this.esc(w.name)}</span>
            </button>`;
          })
          .join("");
      }
      dropdown.innerHTML = html;

      dropdown.querySelectorAll(".mention-dropdown-item[data-ws-path]").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          const path = (item as HTMLElement).dataset.wsPath || "";
          dropdown.classList.add("hidden");
          if (!path || path === getState().activeWorkspace) return;
          void this.workspace.open(path);
        });
      });
    };

    // Only bind event listeners once
    if (selector.dataset.wsSelectorBound === "true") {
      render();
      return;
    }
    selector.dataset.wsSelectorBound = "true";

    selector.addEventListener("click", (e) => {
      e.stopPropagation();
      // The model and workspace dropdowns must never be open at the same time.
      const modelDropdown = document.getElementById("input-model-dropdown");
      if (modelDropdown) modelDropdown.classList.add("hidden");
      if (dropdown.classList.contains("hidden")) {
        render();
        dropdown.classList.remove("hidden");
      } else {
        dropdown.classList.add("hidden");
      }
    });

    document.addEventListener("click", (e) => {
      if (!selector.contains(e.target as Node) && !dropdown.contains(e.target as Node)) {
        dropdown.classList.add("hidden");
      }
    });

    subscribe(() => {
      try {
        render();
      } catch (e) {
        console.error("[workspace-selector] subscribe render failed:", e);
      }
    });

    render();
  }

  private activateSlashCommand(cmd: SlashCommand, restText?: string): void {
    this.skipNextInput = true;
    const dd = document.getElementById("mention-dropdown");
    const bm = document.getElementById("btn-mention");
    dd?.classList.add("hidden");
    bm?.classList.remove("active");
    this._slashActive = false;
    this.input.innerHTML = "";

    if (cmd.kind === "mode") {
      this.insertModeChip(cmd.id, restText);
      this._currentChipMode = cmd.id;
      setInputMode(cmd.id);
      this.updateChipState();
      this.updatePlaceholder();
      this.resizeInput();
      this.updateSendButton();
      // Don't send set_mode here — wait for the user to actually send a message.
      // The toolbar chip only appears after the backend confirms the mode.
    } else if (cmd.kind === "action") {
      this.executeSlashAction(cmd, restText);
      this.updatePlaceholder();
      this.resizeInput();
      this.updateSendButton();
    }
  }

  private executeSlashAction(cmd: SlashCommand, restText?: string): void {
    switch (cmd.id) {
      case "new-session":
      case "clear-session":
        this.automationPanel.hide();
        this.exitTempChat();
        this.cleanupContentArea({ keepAutomationFlag: false });
        resetChat();
        {
          const requestId = crypto.randomUUID();
          setRequestedSessionId("", requestId);
          send({ type: "new_session", request_id: requestId });
        }
        return;
      case "steer":
        // steer is a one-shot mid-run injection, not a sticky command.
        this.insertModeChip(cmd.id, restText);
        this._currentChipMode = "steer";
        this.updateChipState();
        this.input.focus();
        return;
      default:
        break;
    }
    // Any other action command with a prompt body -> sticky *command* chip
    // (NOT a mode).  Persist on the backend so its instructions re-inject
    // every turn and survive restart; the backend broadcasts command_changed
    // back to confirm the chip.
    if (cmd.prompt && cmd.prompt.trim()) {
      this.sendSetCommand(cmd, restText);
    } else {
      this.insertCommandChip(cmd, restText);
      this._currentChipMode = cmd.id;
      this.updateChipState();
      this.input.focus();
    }
  }

  /** Activate a sticky slash command: persist on the backend + render chip. */
  private sendSetCommand(cmd: SlashCommand, restText?: string): void {
    const st = getState();
    const rendered = this.renderCommandPrompt(cmd, restText);
    send({
      type: "set_command",
      session_id: st.sessionId || undefined,
      name: cmd.id,
      prompt: rendered,
      icon: cmd.icon,
      title: cmd.title,
    } as any);
    // Optimistically render; command_changed will confirm/correct.
    this._activeCommand = { name: cmd.id, prompt: rendered, icon: cmd.icon, title: cmd.title };
    this.insertCommandChip(cmd, restText);
    this._currentChipMode = cmd.id;
    this.updateChipState();
    this.input.focus();
  }

  /** Substitute {{args}} into the command prompt body (mirrors backend render_prompt). */
  private renderCommandPrompt(cmd: SlashCommand, args?: string): string {
    const body = cmd.prompt || "";
    if (!body) return "";
    const token = args || "";
    return body
      .replace(/\{\{args\}\}/g, token)
      .replace(/\{\{arguments\}\}/g, token)
      .replace(/\{\{user_input\}\}/g, token);
  }

  private deactivateModeChip(): void {
    // A mode can be active without an inline chip: after a restart the
    // backend-confirmed persistent mode only shows the toolbar chip (the
    // inline chip is intentionally not auto-restored).  Close must work in
    // one click in both cases, so guard on effective mode — not the chip.
    if (!this.hasModeChip() && !this._persistentMode) return;
    const mode = this._currentChipMode || this.getCurrentMode() || this._persistentMode;
    this.removeModeChip();
    this._currentChipMode = "";
    this._persistentMode = "";
    this.updateChipState();
    this.updatePlaceholder();
    this.resizeInput();
    this.updateSendButton();
    // Notify backend that the persistent mode is cleared
    if (mode) {
      const st = getState();
      send({ type: "set_mode", mode: "", session_id: st.sessionId || undefined } as any);
    }
  }

  /* ── Contenteditable helpers ─────────────────────────────────── */

  private getPlainText(): string {
    const parts: string[] = [];
    this._collectText(this.input, parts);
    return parts.join('').trim();
  }

  /** Walk DOM nodes recursively, inserting newlines for block-level elements
   *  and `<br>` so that Enter-key and paste line breaks are both preserved
   *  in the serialized text.  Skips `[contenteditable="false"]` chips. */
  private _collectText(node: Node, out: string[]): void {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        out.push(child.textContent || '');
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const el = child as Element;
        if (el.getAttribute('contenteditable') === 'false') continue;
        const tag = el.tagName.toLowerCase();
        if (tag === 'br') {
          out.push('\n');
        } else if (tag === 'div' || tag === 'p') {
          if (out.length > 0) out.push('\n');
          this._collectText(child, out);
        } else {
          this._collectText(child, out);
        }
      }
    }
  }

  private hasModeChip(): boolean {
    return !!this.input.querySelector('.mode-chip[data-mode]');
  }

  private getCurrentMode(): string {
    const chip = this.input.querySelector('.mode-chip[data-mode]');
    if (!chip) return "";
    return chip.getAttribute("data-mode") || "";
  }

  /**
   * Persist the current composer content (text, line breaks and inline
   * chips) under the active session id so it survives a session switch.
   * Called on every input event and right before the input is cleared on
   * session/mode switches.  Empty drafts are discarded.
   */
  private saveCurrentDraft(): void {
    const sid = getState().sessionId;
    if (!sid) return;
    const html = this.input.innerHTML;
    if (html && html.trim()) {
      this._drafts.set(sid, html);
    } else {
      this._drafts.delete(sid);
    }
  }

  /**
   * Restore the saved draft for the given session (falls back to the active
   * session id) into the composer.  Used after a session_ready lands so the
   * text the user was composing before switching away comes back.  Also
   * restores the inline mode-chip so chip + border stay in sync.
   */
  public restoreDraft(sid?: string): void {
    const target = sid || getState().sessionId;
    if (!target) return;
    const html = this._drafts.get(target);
    if (!html) return;
    const input = this.input;
    input.innerHTML = html;
    const chip = input.querySelector('.mode-chip[data-mode]') as HTMLElement | null;
    this._currentChipMode = chip ? (chip.getAttribute("data-mode") || "") : "";
    this.updateChipState();
    this.updatePlaceholder();
    this.updateSendButton();
    this.resizeInput();
    this.updateInputExpandButton();
  }

  private insertModeChip(mode: string, restText?: string): void {
    this.removeModeChip();
    const chip = document.createElement("span");
    chip.contentEditable = "false";
    chip.className = "mode-chip";
    chip.setAttribute("data-mode", mode);
    const cmd = SLASH_COMMANDS.find(c => c.id === mode);
    const label = cmd ? cmd.title : mode;
    const icon = this.resolveIcon(cmd ? cmd.icon : "list-checks");
    chip.innerHTML = `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i><span>${label}</span>`;
    this.input.appendChild(chip);

    if (restText) {
      this.input.appendChild(document.createTextNode(restText));
    }

    // Cursor after chip — typing inserts text after the chip
    const sel = window.getSelection();
    if (sel) {
      const range = document.createRange();
      range.setStartAfter(chip);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    }

    this.refreshIcons();
  }

  private removeModeChip(): void {
    const chip = this.input.querySelector('.mode-chip[data-mode]');
    if (chip) chip.remove();
  }

  /* ── Command chip (sticky slash command, NOT a mode) ─────────── */

  private hasCommandChip(): boolean {
    return !!this.input.querySelector('.command-chip[data-command]');
  }

  private getCurrentCommand(): string {
    const chip = this.input.querySelector('.command-chip[data-command]');
    if (!chip) return "";
    return chip.getAttribute("data-command") || "";
  }

  private insertCommandChip(cmd: SlashCommand, restText?: string): void {
    this.removeCommandChip();
    const chip = document.createElement("span");
    chip.contentEditable = "false";
    chip.className = "command-chip";
    chip.setAttribute("data-command", cmd.id);
    const icon = this.resolveIcon(cmd.icon);
    chip.innerHTML = `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i><span>${cmd.title}</span>`;
    this.input.appendChild(chip);

    if (restText) {
      this.input.appendChild(document.createTextNode(restText));
    }

    const sel = window.getSelection();
    if (sel) {
      const range = document.createRange();
      range.setStartAfter(chip);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    this.refreshIcons();
  }

  private removeCommandChip(): void {
    const chip = this.input.querySelector('.command-chip[data-command]');
    if (chip) chip.remove();
  }

  /** Resolve a Lucide icon name, falling back to the app-wide skills glyph
   *  (``wand-2``) when unknown.  lucide's icon registry is keyed by PascalCase
   *  (e.g. ``ListChecks``), while commands store kebab-case names — normalize
   *  before checking.  ``command`` (macOS ⌘) is intentionally never used. */
  private resolveIcon(name: string): string {
    const candidateRaw = (name && name.trim()) || "wand-2";
    // ``command`` is the macOS ⌘ glyph — legacy default stored in old
    // configs.  Normalize it to the app-wide skills glyph.
    const candidate = candidateRaw === "command" ? "wand-2" : candidateRaw;
    if ((window as any).lucide && (window as any).lucide.icons) {
      const known = (window as any).lucide.icons as Record<string, unknown>;
      const pascal = candidate
        .split("-")
        .map(p => p.charAt(0).toUpperCase() + p.slice(1))
        .join("");
      if (!(candidate in known) && !(pascal in known)) return "wand-2";
    }
    return candidate;
  }

  /** Re-run Lucide createIcons. */
  private refreshIcons(): void {
    if (!(window as any).lucide) return;
    (window as any).lucide.createIcons();
  }

  /** Clear the active sticky command (Esc / chip close). Notifies backend. */
  private deactivateCommandChip(): void {
    const name = this._activeCommand?.name || this.getCurrentCommand();
    this.removeCommandChip();
    this._activeCommand = null;
    if (this._currentChipMode && this._currentChipMode === name) {
      this._currentChipMode = "";
    }
    this.updateChipState();
    this.updatePlaceholder();
    this.resizeInput();
    this.updateSendButton();
    if (name) {
      const st = getState();
      send({ type: "set_command", session_id: st.sessionId || undefined, name: "" } as any);
    }
  }

  /** Render the toolbar command chip from the backend-confirmed active command. */
  private updateToolbarCommandChip(): void {
    const chip = document.getElementById("toolbar-command-chip");
    if (!chip) return;
    const cmd = this._activeCommand;
    if (!cmd || !cmd.name) {
      chip.classList.add("hidden");
      chip.setAttribute("data-command", "");
      return;
    }
    chip.classList.remove("hidden");
    chip.setAttribute("data-command", cmd.name);
    const icon = chip.querySelector(".chip-icon") as HTMLElement;
    const label = chip.querySelector(".toolbar-command-label") as HTMLElement;
    const def = SLASH_COMMANDS.find((c) => c.id === cmd.name);
    if (icon) icon.setAttribute("data-lucide", this.resolveIcon(def?.icon || cmd.icon || "wand-2"));
    if (label) label.textContent = def?.title || cmd.title || cmd.name;
    this.refreshIcons();
  }

  private handleModeEnd(): void {
    this.removeModeChip();
    this._currentChipMode = "";
    setInputMode("");
    this.updateChipState();
    this.updatePlaceholder();
  }

  /** Resolve the currently effective mode (single source of truth).

  The effective mode is what the backend should be told and what the input
  border / toolbar chip must reflect.  It is the chip mode (an active /spec
  or /plan chip the user just inserted) falling back to the backend-confirmed
  persistent mode (so the border survives across turns after the first run).

  Keeping this aligned with submit()'s sendMode resolution guarantees that
  whenever the border shows a mode colour, the very same value is sent on the
  run message -- so "border visible == mode takes effect" always holds.
  */
  private effectiveMode(): string {
    return this.getCurrentMode() || this._persistentMode || "";
  }

  public updateChipState(): void {
    const el = document.getElementById("input-area");
    if (!el) return;
    const mode = this.effectiveMode();
    if (mode) {
      el.setAttribute("data-input-mode", mode);
      // The inline mode-chip is created only by explicit user action
      // (activateSlashCommand / insertModeChip / restoreInputModeChip).
      // It never auto-reappears here: once the backend confirms the mode
      // the input shows ONLY the toolbar mode-chip, not the inline one.
    } else if (this.hasCommandChip()) {
      el.setAttribute("data-input-mode", "command");
    } else {
      el.removeAttribute("data-input-mode");
      // No mode active — remove any stale chip from the input
      if (this.input && this.hasModeChip()) {
        this.removeModeChip();
        this._currentChipMode = "";
      }
    }
    this.updateToolbarModeChip(mode);
    this.updateToolbarCommandChip();
  }

  private updateToolbarModeChip(mode: string): void {
    const chip = document.getElementById("toolbar-mode-chip");
    if (!chip) return;
    if (!mode) {
      chip.classList.add("hidden");
      chip.setAttribute("data-mode", "");
      return;
    }
    chip.classList.remove("hidden");
    chip.setAttribute("data-mode", mode);
    const icon = chip.querySelector(".chip-icon") as HTMLElement;
    const label = chip.querySelector(".toolbar-mode-label") as HTMLElement;
    const cmd = SLASH_COMMANDS.find(c => c.id === mode);
    if (icon) icon.setAttribute("data-lucide", this.resolveIcon(cmd?.icon || "list-checks"));
    if (label) label.textContent = cmd?.title || mode;
    if ((window as any).lucide) {
      (window as any).lucide.createIcons();
    }
  }

  public updatePlaceholder(): void {
    const ph = document.getElementById("prompt-placeholder");
    if (!ph) return;
    const mode = this.getCurrentMode();
    const wsMode = getState().workspaceMode;

    if (mode) {
      ph.textContent = t("input.placeholderMode");
    } else if (wsMode === "iwork") {
      ph.textContent = t("input.placeholderIwork");
    } else {
      ph.textContent = t("input.placeholder");
    }

    // Hide overlay when user typed text, or when any chip present (chip is the visual indicator)
    const hasText = this.getPlainText().length > 0;
    const hasChip = this.hasModeChip() || this.hasCommandChip();
    const hasAttach = !!this.input.querySelector('[data-attach]');
    ph.classList.toggle("hidden", hasText || hasChip || hasAttach);

    // Update welcome screen title based on current mode
    this.updateWelcomeTitle(wsMode);
  }

  private updateWelcomeTitle(mode: string): void {
    const title = document.querySelector(".welcome-title");
    if (!title) return;

    const st = getState();
    const newText = st.tempChat ? t("session.tempChatActive") :
                    mode === "iwork" ? t("welcome.iwork") :
                    t("welcome.title");

    // If an animation is in progress, skip setting text here
    if (this._welcomeTitleAnimating) return;
    title.textContent = newText;
  }

  /**
   * Replay the content entrance after an iwork ⇄ normal switch.
   *
   * The move lives entirely in CSS (`#chat-container.mode-in-forward/back`
   * and `#session-list.mode-in-forward/back`); this only picks the class.
   * Only the changing content moves — the nav row, "All tasks" heading,
   * batch button, input area and disclaimer stay static. The direction is a
   * real full-width push: entering iWork slides in from the inline-end edge
   * (forward), returning to normal from the inline-start edge (back). The two
   * class names carry distinct animation-names because re-adding the one
   * already running would be a no-op — the browser sees no change and never
   * restarts. The class is dropped on animationend, so an interrupted or
   * rapid second switch cannot strand it, and nothing inline is ever written.
   */
  private replayContentEnter(): void {
    const content = document.getElementById("chat-container");
    if (content) {
      if (!this._modeEnterBound) {
        content.addEventListener("animationend", (e) => {
          if (e.animationName.startsWith("mode-in-")) {
            content.classList.remove("mode-in-forward", "mode-in-back");
          }
        });
        this._modeEnterBound = true;
      }
      const next = getState().workspaceMode === "iwork" ? "mode-in-forward" : "mode-in-back";
      content.classList.remove("mode-in-forward", "mode-in-back");
      content.classList.add(next);
    }

    // Only the history rows travel — the nav row (New task / temp-chat /
    // workspace), the "All tasks" heading and the batch button stay put, so
    // the chrome never appears to shift while the list content changes.
    const list = document.getElementById("session-list");
    if (list) {
      if (!this._sidebarEnterBound) {
        list.addEventListener("animationend", (e) => {
          if (e.animationName.startsWith("mode-in-")) {
            list.classList.remove("mode-in-forward", "mode-in-back");
          }
        });
        this._sidebarEnterBound = true;
      }
      const next = getState().workspaceMode === "iwork" ? "mode-in-forward" : "mode-in-back";
      list.classList.remove("mode-in-forward", "mode-in-back");
      list.classList.add(next);
    }
  }

  /** Set the welcome title text for the current mode.
   *
   *  The motion is already carried by the chat-container slide
   *  (`#chat-container.mode-in-forward/back`), so no separate title swap runs
   *  here — stacking a second animation on the title made it look like it
   *  moved twice. */
  private animateWelcomeTitle(mode: string): void {
    this._welcomeTitleAnimating = false;
    const title = document.querySelector(".welcome-title") as HTMLElement | null;
    if (!title) return;
    title.textContent = mode === "iwork" ? t("welcome.iwork") : t("welcome.title");
  }

  /** Programmatically close the session inner sidebar if it's open */
  private closeSessionInnerSidebar(): void {
    const panel = document.getElementById("session-inner-sidebar");
    const mainBody = document.getElementById("main-body");
    if (!panel || !mainBody) return;
    if (!panel.classList.contains("hidden")) {
      this.sessionInner.saveSidebarVisibility();
      this.sessionInner.saveWidth();
      panel.classList.add("hidden");
      mainBody.classList.add("sidebar-hidden");
    }
    // The inner sidebar collapsing the app sidebar was an automatic side
    // effect, not a user choice — undo it so the app sidebar re-expands.
    this.restoreAppSidebar();
  }

  /** Re-expand the app-level sidebar unless the user collapsed it by hand or
   *  the viewport is too narrow to show it inline. */
  private restoreAppSidebar(): void {
    if (this.userToggledSidebar) return;
    if (window.innerWidth <= 920) return;
    document.getElementById("app")?.classList.remove("sidebar-collapsed");
  }

  /**
   * Re-initialize the main content area to its default "fresh chat" state.
   * Called AFTER cleanupContentArea() on session/mode switches so the user
   * sees a clean welcome screen with empty input, no sub-agent header, no
   * queue card, no status bar, and no leftover dropdowns — exactly like
   * the app's first launch.
   */
  private enterChatMode(): void {
    // Show welcome screen, hide message list (chat.render() will
    // override this once state.messages lands).
    if (this.welcomeScreen) {
      this.welcomeScreen.classList.remove("hidden");
    }
    if (this.messageList) {
      this.messageList.classList.add("hidden");
      this.messageList.innerHTML = "";
    }
    // Reset welcome title to default.  animateWelcomeTitle() will replace
    // this with the right "iWork" / "default" text in a moment.
    const title = document.querySelector(".welcome-title") as HTMLElement | null;
    if (title) {
      title.textContent = t("welcome.title");
      // Drop a swap left mid-flight so the next one starts from rest.
      title.classList.remove("is-swapping");
    }
    // Show placeholder, hide mode chip.
    const placeholder = document.getElementById("prompt-placeholder");
    if (placeholder) placeholder.classList.remove("hidden");
    // Force chat.render() to redraw the message list with the new state.
    // It will set welcome vs message-list visibility based on state.messages
    // and re-bind handlers on the freshly-built DOM.
    this.chat.renderForce?.();
    this.chat.render();
    // Re-bind the input model selector in case the model selector's
    // event listener was lost during a heavy innerHTML replacement.
    this.bindInputModelSelector();
    // Re-apply shortcut hints (e.g. when the input area was just rebuilt).
    this.applyShortcutHints();
    // Update placeholder text + welcome title for the new mode.
    this.updatePlaceholder();
    this.updateSessionBarName();
  }

  /**
   * Tear down all transient UI artifacts from the content area so the next
   * render starts from a clean slate.  Called BEFORE enterChatMode() on
   * session/mode switches.
   * @param opts - Optional flags controlling cleanup behaviour.
   */
  private cleanupContentArea(opts?: { keepAutomationFlag?: boolean }): void {
    // ── 0. Automation in-panel sub-agent detail ─────────────────────
    // Tear down an open execution detail (breadcrumb, activeExecution,
    // header buttons) first so it cannot leak into the next mode/session.
    // The mode-switch path already closes it via ModeTransitionManager's
    // onLeaveAutomation before the panel slides away; this is the safety
    // net for every other caller (tray switch, new session, temp chat…).
    if (this.automation.isDetailVisible()) this.automation.hideDetail();

    // ── 1. Sub-agent view + breadcrumb ──────────────────────────────
    // These gate the inline "sub-agent" overlay in #message-list and the
    // breadcrumb chips in the session bar.  Without clearing them, switching
    // sessions leaves a phantom sub-agent header and a stale "X / Y / Z" trail
    // pointing at the previous session's nested agent.
    //
    // NOTE: only the *state* is reset here.  The window navigation handlers
    // (__openSubAgentView / __closeSubAgentView / __goToRootView /
    // __navigateToBreadcrumb) live on the persistent ChatView singleton and
    // read fresh state via getState() at call-time.  They are bound once in
    // the ChatView constructor and must NOT be nulled here: opening a
    // session-backed sub-agent performs a session switch which runs this
    // cleanup, and nulling them would leave the breadcrumb clicks dead (they
    // no-op through `?.`), making it impossible to navigate back to the main
    // agent.
    setSubAgentView(null);
    clearSubAgentBreadcrumb();

    // ── 2. Automation panel state ──────────────────────────────────
    if (!opts?.keepAutomationFlag) {
      (window as any).__isAutomationView = false;
      (window as any).__activeAutomationJobId = "";
    }

    // ── 3. Tool detail panel (right-side aside) ─────────────────────
    // The aside is a sibling of #main-content; it is hidden when no active
    // tool id is set, but the id itself sticks around.  Wipe both.
    setActiveToolId(null);
    const detailPanel = document.getElementById("detail-panel");
    if (detailPanel) {
      detailPanel.classList.add("hidden");
      const detailContent = document.getElementById("detail-content");
      if (detailContent) detailContent.innerHTML = "";
    }

    // ── 4. Mention / @ dropdown (above the prompt input) ───────────
    const mentionDropdown = document.getElementById("mention-dropdown");
    if (mentionDropdown) {
      mentionDropdown.classList.add("hidden");
      // Also reset to the main page so the next open shows Files/Folder,
      // not a stale "back" state from the slash-commands sub-page.
      const mainPage = mentionDropdown.querySelector('[data-page="main"]');
      const slashPage = mentionDropdown.querySelector('[data-page="slash"]');
      if (mainPage) mainPage.classList.remove("hidden");
      if (slashPage) slashPage.classList.add("hidden");
    }
    const btnMention = document.getElementById("btn-mention");
    if (btnMention) btnMention.classList.remove("active");
    this._slashActive = false;

    // ── 5. Model selector dropdown (in input toolbar) ──────────────
    const modelDropdown = document.getElementById("input-model-dropdown");
    if (modelDropdown) modelDropdown.classList.add("hidden");
    // Keep the two input dropdowns mutually exclusive here as well.
    const wsDropdownCleanup = document.getElementById("input-workspace-dropdown");
    if (wsDropdownCleanup) wsDropdownCleanup.classList.add("hidden");

    // ── 6. Chat status bar + queue card ────────────────────────────
    const statusBar = document.getElementById("chat-status-bar");
    if (statusBar) {
      statusBar.classList.add("hidden");
      statusBar.innerHTML = "";
      statusBar.style.maxWidth = "";
    }
    const queueCard = document.getElementById("queue-card");
    if (queueCard) {
      queueCard.classList.add("hidden");
      const qBody = document.getElementById("queue-card-body");
      if (qBody) qBody.innerHTML = "";
    }

    // ── 7. Input area — clear leftover text/chip/attachments ───────
    // Capture the current composer content against the *outgoing* session
    // before it is wiped, so restoreDraft() can bring it back when the user
    // returns to this session.
    this.saveCurrentDraft();
    if (this.input) {
      this.input.innerHTML = "";
      this.input.style.height = "56px";
    }
    this._currentChipMode = "";
    this._persistentMode = "";  // Clear — the new session's mode_changed will set the correct value
    setInputMode("");
    clearAttachments();
    clearQueuedPrompts();
    setPendingQueueCount(0);

    // ── 8. Restore mode chip + border via the single updateChipState() path ──
    // updateChipState() checks effectiveMode() (chip OR _persistentMode)
    // and ensures: border colour, toolbar chip, AND prompt-input chip are
    // all in sync.  The new session's mode_changed will arrive shortly and
    // trigger updateChipState() again with the correct mode.
    // mode_changed for the new session — the chip reappears automatically.
    this.updateChipState();
    const inputAreaEl = document.getElementById("input-area");
    if (inputAreaEl) inputAreaEl.style.display = "";
    const cmdChip = document.getElementById("toolbar-command-chip");
    if (cmdChip) {
      cmdChip.classList.add("hidden");
      cmdChip.setAttribute("data-command", "");
    }
    const sessionMenuBtn = document.getElementById("btn-session-menu");
    if (sessionMenuBtn) sessionMenuBtn.style.display = "";
    const mainContent = document.getElementById("main-content");
    if (mainContent) mainContent.classList.remove("sub-agent-active");

    // ── 9. Search overlay (Ctrl/Cmd+K) ─────────────────────────────
    if (this.search) this.search.close();

    // ── 10. Floating dropdowns that may be left open ───────────────
    // The settings-search overlay, the input-area "model" dropdown, any
    // inline tab-add dropdowns in #session-inner-sidebar, etc.  Anything
    // with `.open` or `.visible` is a transient popup — close it.
    document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
    document.querySelectorAll(".tab-add-dropdown:not(.hidden)").forEach((dd) => dd.classList.add("hidden"));
    document.querySelectorAll(".context-menu:not(.hidden)").forEach((m) => m.classList.add("hidden"));
    document.querySelectorAll(".tooltip:not(.hidden), [data-tooltip-visible]").forEach((t) => {
      t.classList.add("hidden");
      t.removeAttribute("data-tooltip-visible");
    });

    // ── 12. Session inner sidebar — hide the container AND drop tabs ─
    // The user might have opened the right-side #session-inner-sidebar
    // in the previous view (e.g. opened a terminal tab in normal mode and
    // then switched to iWork).  If we only tear down the dynamic tabs and
    // leave the container visible, the user sees an empty blank panel
    // sitting on top of the workspace tree — which is wrong.  Force-hide
    // the container here so the content area is fully reset, regardless
    // of whatever state the previous view left it in.
    const sessionInnerSidebar = document.getElementById("session-inner-sidebar");
    if (sessionInnerSidebar) {
      if (!sessionInnerSidebar.classList.contains("hidden")) {
        this.sessionInner?.saveSidebarVisibility?.();
      }
      this.sessionInner?.saveWidth?.();
      sessionInnerSidebar.classList.add("hidden");
    }
    // Keep tabs alive across session switches (like browser tabs).
    const mainBody = document.getElementById("main-body");
    if (mainBody) mainBody.classList.add("sidebar-hidden");

    // ── 13. Welcome screen transition guard ────────────────────────
    // Force-reset the animation latch so the next mode switch re-animates
    // the title.  Without this the second switch can land on a stale
    // ``_welcomeTitleAnimating=true`` and the title text never updates.
    this._welcomeTitleAnimating = false;

    // ── 14. Force-reset #message-list + welcome screen ─────────────
    // chat.render() will repaint #message-list, but it does not always
    // run synchronously with this cleanup.  Clear it eagerly so we never
    // show a stale message bubble for one frame after switching modes.
    if (this.messageList) {
      this.messageList.innerHTML = "";
      this.messageList.classList.add("hidden");
    }
    if (this.welcomeScreen) {
      this.welcomeScreen.classList.remove("hidden");
      // Reset the welcome title text so animateWelcomeTitle() can always
      // detect a text change and play the slide-in animation on every
      // mode switch.  Use an empty string so it never matches any mode text.
      const title = this.welcomeScreen.querySelector(".welcome-title") as HTMLElement | null;
      if (title) {
        title.textContent = "";
        // Drop a swap left mid-flight so animateWelcomeTitle() always sees a
        // text change and can play a clean entrance.
        title.classList.remove("is-swapping");
      }
    }

    // ── 15. Force-reset #placeholder visibility ────────────────────
    // The placeholder is hidden while the user types or has a chip; the
    // chat render pipeline restores it.  Show it eagerly so the next
    // frame does not flash a blank input.
    const placeholder = document.getElementById("prompt-placeholder");
    if (placeholder) placeholder.classList.remove("hidden");

    // ── 16. Force-hide #automation-view ────────────────────────────
    // AutomationPanel.hide() also does this, but doing it here makes the
    // cleanup robust to races where a mode change lands before the panel
    // has reacted. There are no inline styles to unwind any more: the
    // panel's visibility is a single class, and the move itself is CSS.
    const automationView = document.getElementById("automation-view");
    if (automationView) {
      automationView.classList.add("hidden");
    }
    const sidebarToggle = document.getElementById("btn-toggle-sidebar");
    const searchBtn = document.getElementById("btn-sidebar-search");
    const autoBackBtn = document.getElementById("btn-automation-back");
    if (sidebarToggle) {
      sidebarToggle.classList.remove("hidden");
      sidebarToggle.style.display = "";
      (sidebarToggle as HTMLButtonElement).disabled = false;
    }
    if (searchBtn) {
      // The search button's display is still toggled inline on tab changes;
      // its opacity/transform are no longer touched by anything.
      searchBtn.style.display = "";
    }
    if (autoBackBtn) autoBackBtn.classList.add("hidden");

    // ── 17. Clear #child-view content ──────────────────────────────
    // The child view hosts sub-windows (license, easter-egg, logs, etc.).
    // Switching modes or sessions must not leave those webviews/canvases
    // running in the background — that wastes GPU and is a security smell.
    const childView = document.getElementById("child-view");
    if (childView) {
      childView.classList.add("hidden");
      childView.innerHTML = "";
    }

    // ── 18. Reset session-bar text + btn-session-menu state ───────
    // When sub-agent view is active the session-bar shows a breadcrumb
    // trail; after cleanup we want the plain "New Session" label back.
    const sessionBarName = document.getElementById("session-bar-name");
    if (sessionBarName) {
      sessionBarName.textContent = t("session.newSession");
      sessionBarName.removeAttribute("title");
    }

    // ── 19. Reset #session-bar visibility ─────────────────────────
    // Automation hides #session-bar; if the user was in automation and
    // switched modes, the bar must come back.
    const sessionBar = document.getElementById("session-bar");
    if (sessionBar) {
      sessionBar.classList.remove("hidden");
      sessionBar.style.display = "";
    }

    // ── 20. (removed) main-area inline-style reset ─────────────────
    // Mode switching no longer writes inline positioning onto #main-body,
    // #main-content or #automation-view — the slide is pure CSS. There is
    // consequently no residue to clear when a switch is interrupted, which
    // is what this step used to exist for.

    // ── 21. Scroll position reset for chat-container ───────────────
    // The chat container keeps its scrollTop across renders.  If the user
    // was scrolled deep in a long previous session, switching to a new
    // short session keeps the scroll position at a "blank" area.  Reset
    // to the bottom so the welcome screen / first message is visible.
    const chatContainer = document.getElementById("chat-container");
    if (chatContainer) chatContainer.scrollTop = chatContainer.scrollHeight;

    // ── 22. Scroll-track / scroll-thumb reset ─────────────────────
    // The right-side scroll progress indicator is built by ChatScrollIndicator.
    // Reset its DOM so it does not show a stale position for one frame.
    const scrollTrack = document.getElementById("chat-scroll-track");
    if (scrollTrack) scrollTrack.innerHTML = "";
    const scrollThumb = document.getElementById("chat-scroll-thumb");
    if (scrollThumb) {
      scrollThumb.classList.add("hidden");
      scrollThumb.style.top = "";
    }
    const scrollIndicator = document.getElementById("chat-scroll-indicator");
    if (scrollIndicator) scrollIndicator.classList.add("hidden");
    this.chat.resetScrollButton();

    // ── 23. Reset temp-chat button + clear any temp flags ─────────
    const tempBtn = document.getElementById("btn-temp-chat");
    if (tempBtn) tempBtn.classList.remove("active");
    const btnNewTask = document.querySelector('.nav-item[data-view="chat"]');
    if (btnNewTask) btnNewTask.classList.add("active");

    // ── 24. Clear pending rollout animation classes on #app ────────
    // The body sometimes gets a "sidebar-collapsed" class from the
    // responsive collapse code; if the previous view left it stale
    // and we are NOT in a small viewport, the layout breaks.  Let the
    // responsive observer re-evaluate, but force-clear transient mode
    // classes here.
    const appEl = document.getElementById("app");
    if (appEl) {
      appEl.classList.remove("sub-agent-active");
    }
  }

  /** Exit temp chat: delete the temporary session from server. */
  private exitTempChat(): void {
    if (!getState().tempChat) return;
    const sid = getState().sessionId;
    setTempChat(false);
    if (sid) {
      // Poison the id first so in-flight events can never re-show it.
      markTempChatSession(sid);
      send({ type: "delete_session", session_id: sid });
    }
  }

  /**
   * Auto-height the composer. Height is measured in a requestAnimationFrame
   * callback so the browser has committed the latest DOM mutation and layout
   * first — contenteditable scrollHeight can otherwise lag one frame, which
   * made the box fail to grow (or shrink) right after a big paste or a
   * programmatic textContent/innerHTML write. Calls within a frame coalesce.
   */
  private resizeInput(): void {
    if (this._inputResizeRaf) return;
    this._inputResizeRaf = requestAnimationFrame(() => {
      this._inputResizeRaf = 0;
      if (this._inputExpanded) {
        // Expanded mode: let CSS handle the height via the .expanded class.
        this.input.style.height = "";
        this.updateInputExpandButton();
        return;
      }
      this.input.style.height = "auto";
      this.input.style.maxHeight = "none";
      const contentH = this.input.scrollHeight;
      this.input.style.maxHeight = "";
      const h = Math.min(Math.max(contentH, 56), 320);
      this.input.style.height = `${h}px`;
      this.updateInputExpandButton();
    });
  }

  private countLines(): number {
    const text = this.input.innerText || '';
    if (!text.trim()) return 0;
    // innerText already normalises block-level elements to \n in Chromium
    return text.split('\n').length;
  }

  /** Shows/hides the expand toggle based on plain-text line count. */
  private updateInputExpandButton(): void {
    const maxLines = 5;
    const lines = this.countLines();
    const overflowing = lines > maxLines;
    const show = this._inputExpanded || overflowing;
    this.inputExpandBtn.classList.toggle("btn-hidden", !show);
    this.renderExpandIcon();
    const titleKey = this._inputExpanded ? "input.collapse" : "input.expand";
    this.inputExpandBtn.setAttribute("data-i18n-title", titleKey);
    this.inputExpandBtn.title = t(titleKey as any);
  }

  private renderExpandIcon(): void {
    const expanded = this._inputExpanded;
    const expandIcon = this.inputExpandBtn.querySelector(".expand-icon") as HTMLElement;
    const collapseIcon = this.inputExpandBtn.querySelector(".collapse-icon") as HTMLElement;
    if (expandIcon) expandIcon.style.display = expanded ? "none" : "";
    if (collapseIcon) collapseIcon.style.display = expanded ? "" : "none";
  }

  /**
   * @param animate Play the height transition.  Only the user-facing toggle
   *   animates; the programmatic collapses after send / queue / steer must
   *   snap back instantly (the draft is already cleared there, so animating
   *   would just hold the reserved slot open for another frame or two).
   */
  private setInputExpanded(v: boolean, animate = false): void {
    // Collapsing is not a no-op while an animated collapse is still pending:
    // the send paths need the slot handed back right away, not 340ms later.
    if (this._inputExpanded === v && this._inputCollapseTimer === 0) return;

    if (v) {
      // A collapse animation may still be mid-flight with `#input-area` still
      // absolute.  Settle it first — otherwise `offsetHeight` below would
      // measure the floating overlay instead of the real flex slot.
      this.finishCollapseHandoff();
      // Measure the composer's height while it is STILL in the flex flow,
      // BEFORE the `input-expanded` class turns it into an absolute overlay.
      // Reading offsetHeight here forces a synchronous layout, so the value
      // is exact for the slot being vacated — including any status bar /
      // queue card that is visible this very instant.  Trusting the
      // ResizeObserver cache instead is racy: the callback only runs at the
      // END of a frame, so a status-bar/queue-card that just appeared or
      // disappeared leaves a stale height there.  A stale value makes the
      // padding-bottom placeholder larger/smaller than the real slot, the
      // chat container's height shifts by the difference, and the next
      // streaming `scrollToBottom()` re-pins to the new (wrong) bottom —
      // the user sees the whole timeline jump as if the wheel scrolled.
      const h = this.inputArea.offsetHeight;
      if (h > 0) this._inputNaturalHeight = h;
      this._inputExpanded = true;
      // Temporarily enable the height transition so the expand glides with
      // the app's standard easing; clears once the animation settles so
      // normal typing auto-resize stays snappy.
      this.input.classList.add("input-anim");
      this.armInputAnimTimer();
      this.input.classList.add("expanded");
      this.inputArea.classList.add("input-expanded");
      this.input.style.height = "";
      // Compensate the vacated slot IMMEDIATELY, before any layout-reading
      // step below (updateInputExpandButton reads innerText, resizeInput
      // schedules a rAF, placeCursorAtEnd touches the selection) forces a
      // reflow.  If the padding-bottom placeholder were applied later, the
      // browser would lay out the intermediate state — input-area absolute,
      // no padding-bottom yet — which grows the chat container by the
      // input's height, shrinks the scrollable range, and clamps scrollTop.
      // The timeline then jumps up by roughly the input height and never
      // comes back, even though the placeholder eventually restores the
      // container's height.
      this.syncInputAreaHeight();
      this.updateInputExpandButton();
      this.resizeInput();
      this.placeCursorAtEnd();
      this.input.focus();
      // Safe to touch the editor's scroll offset only now: the vacated slot
      // has already been compensated, so reading `scrollHeight` no longer
      // reflows the chat container (see `scheduleInputScrollToEnd`).
      this.scheduleInputScrollToEnd();
      return;
    }

    const collapsed = this.collapsedInputHeight();

    if (animate) {
      // Keep `#input-area` as an absolute overlay for the WHOLE shrink
      // animation.  While it is out of the flex flow, the chat container keeps
      // the geometry granted by the padding-bottom placeholder, so animating
      // the height can never squeeze the timeline — which is exactly why the
      // collapse used to be instant: settling the height first (the old
      // approach) left nothing left to animate.  The slot is handed back in a
      // single synchronous block once the height has settled.
      const startH = this.input.getBoundingClientRect().height;
      this._inputExpanded = false;
      this.input.classList.remove("input-anim");
      this.input.classList.remove("expanded");
      this.input.style.height = `${startH}px`;
      void this.input.offsetHeight; // commit the start height without easing
      this.input.classList.add("input-anim");
      this.input.style.height = `${collapsed}px`;
      this.armInputAnimTimer();
      window.clearTimeout(this._inputCollapseTimer);
      // `--transition-emphasized` is 0.28s; hand back slightly after it ends.
      this._inputCollapseTimer = window.setTimeout(
        () => this.finishCollapseHandoff(),
        340
      );
      this.updateInputExpandButton();
      this.placeCursorAtEnd();
      this.input.focus();
      return;
    }

    // Instant collapse (programmatic): settle the editor height synchronously
    // BEFORE the `.input-expanded` class is removed, so the composer re-enters
    // the flex flow at its collapsed size.  Two traps here:
    //   1. `.expanded`'s fixed `height` must be dropped first — otherwise the
    //      composer re-enters the flow taller than its natural slot, which
    //      squeezes `#chat-container` by the difference and, when scrolled
    //      near the bottom, pushes the timeline up (scrollTop gets clamped).
    //   2. The `height` transition must be OFF for the re-flow frame:
    //      with `input-anim` active the browser renders the previous frame's
    //      expanded height on the very frame the composer returns to the
    //      flow, which causes the same one-frame squeeze.
    this._inputExpanded = false;
    this.input.classList.remove("expanded");
    this.input.classList.remove("input-anim");
    this.input.style.height = `${collapsed}px`;
    this.finishCollapseHandoff();
    this.updateInputExpandButton();
    this.resizeInput();
    this.placeCursorAtEnd();
    this.input.focus();
    // Composer is back in the flex flow — re-enable the height transition so
    // resizeInput()'s rAF behaves as before, without the re-flow squeeze.
    this.input.classList.add("input-anim");
    this.armInputAnimTimer();
  }

  /** Re-arms the timer that drops the temporary height-transition class. */
  private armInputAnimTimer(): void {
    window.clearTimeout(this._inputAnimTimer);
    this._inputAnimTimer = window.setTimeout(() => {
      this.input.classList.remove("input-anim");
    }, 400);
  }

  /**
   * Height the composer collapses back to — the content height clamped to the
   * same `[56, 320]` window `resizeInput()` uses for the non-expanded state.
   * Measured with the transition OFF so the probe height never animates.
   */
  private collapsedInputHeight(): number {
    const hadAnim = this.input.classList.contains("input-anim");
    if (hadAnim) this.input.classList.remove("input-anim");
    const prevH = this.input.style.height;
    const prevMax = this.input.style.maxHeight;
    this.input.style.height = "auto";
    this.input.style.maxHeight = "none";
    const contentH = this.input.scrollHeight;
    this.input.style.height = prevH;
    this.input.style.maxHeight = prevMax;
    if (hadAnim) this.input.classList.add("input-anim");
    return Math.min(Math.max(contentH, 56), 320);
  }

  /**
   * Final step of a collapse: hand the composer slot back to the flex flow.
   * Removing the overlay class and clearing the compensating padding-bottom
   * MUST happen in ONE synchronous block — split across tasks, the browser
   * would lay out the in-between state (`#chat-container` resizes, its
   * scrollable range shifts, `scrollTop` gets clamped) and the timeline would
   * visibly jump.  Idempotent: safe to call when no collapse is pending.
   */
  private finishCollapseHandoff(): void {
    window.clearTimeout(this._inputCollapseTimer);
    this._inputCollapseTimer = 0;
    if (!this.inputArea.classList.contains("input-expanded")) return;
    this.input.classList.remove("input-anim");
    this.inputArea.classList.remove("input-expanded");
    this.syncInputAreaHeight();
    this.refreshInputNaturalHeight();
  }

  /**
   * The expanded editor has a FIXED height, so a draft longer than that
   * overflows inside the box. Land its viewport on the last line instead of
   * the first — otherwise expanding looks like the tail of the text vanished.
   *
   * MUST be deferred past the current frame. Doing it synchronously inside
   * `setInputExpanded()` reads `scrollHeight` (i.e. forces a layout) at the
   * exact moment `#input-area` has just gone absolute but the compensating
   * `padding-bottom` has not been written yet. The browser then lays out that
   * intermediate state: `#chat-container` grows into the vacated slot, its
   * scrollable range shrinks, and `scrollTop` gets clamped — the timeline
   * visibly jumps up a couple of lines and never comes back.
   */
  private scheduleInputScrollToEnd(): void {
    if (this._inputScrollEndRaf) return;
    this._inputScrollEndRaf = requestAnimationFrame(() => {
      this._inputScrollEndRaf = 0;
      if (!this._inputExpanded) return;
      this.input.scrollTop = this.input.scrollHeight;
    });
  }

  private toggleInputExpand(): void {
    // The user-facing toggle animates both ways; programmatic collapses snap.
    this.setInputExpanded(!this._inputExpanded, true);
  }

  /**
   * Refresh the cached height of `#input-area` while it sits in the normal
   * flex flow.  When the composer switches to its expanded overlay state
   * this is the value pushed into `#main-content-clip`'s `padding-bottom`
   * to keep the chat container's geometry stable.
   */
  private refreshInputNaturalHeight(): void {
    // Skip while the composer is floating — its `offsetHeight` then
    // reflects the tall overlay (up to ~700px), which is NOT the slot
    // size we want to preserve.  The last cache written before the
    // expand is good enough until the user collapses it again.
    // NB: check the CLASS, not `_inputExpanded` — during an animated
    // collapse the flag is already false while the overlay is still up.
    if (this._inputExpanded) return;
    if (this.inputArea.classList.contains("input-expanded")) return;
    const h = this.inputArea.offsetHeight;
    if (h > 0) this._inputNaturalHeight = h;
  }

  /**
   * Apply or clear the `padding-bottom` placeholder on the chat-content
   * clip column, mirroring the composer's natural height while the
   * overlay is open.  Called on every expand/collapse toggle.
   */
  private syncInputAreaHeight(): void {
    const clip = document.getElementById("main-content-clip");
    if (!clip) return;
    if (this._inputExpanded) {
      clip.style.paddingBottom = `${this._inputNaturalHeight}px`;
    } else {
      clip.style.paddingBottom = "0px";
    }
  }

  private updateSendButton(): void {
    const hasText = this.getPlainText().length > 0;
    const running = getState().running;
    if (hasText) {
      this.btnSend.style.display = "flex";
      this.btnStop.style.display = "none";
    } else if (running) {
      this.btnSend.style.display = "none";
      this.btnStop.style.display = "flex";
    } else {
      this.btnSend.style.display = "flex";
      this.btnStop.style.display = "none";
    }
    this.btnSend.disabled = !hasText && !this.effectiveMode() && !this.hasCommandChip();
  }

  private placeCursorAtEnd(): void {
    const sel = window.getSelection();
    if (!sel) return;
    const range = document.createRange();
    range.selectNodeContents(this.input);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  private renderSlashDropdown(): void {
    const slashPage = document.querySelector('[data-page="slash"]');
    if (!slashPage) return;

    const backBtn = `<button class="mention-dropdown-item" data-action="back-main">
      <i data-lucide="arrow-left" class="lucide"></i>
      <span>${t("general.slashCommands")}</span>
    </button>`;

    const mode = getState().workspaceMode;
    const items = matchingSlashCommands("", mode).map(cmd => {
      const aliases = (cmd.aliases && cmd.aliases.length) ? cmd.aliases.join(",") : "";
      return `
      <button class="mention-dropdown-item" data-action="${cmd.id}" data-slash="${cmd.name}" data-kind="${cmd.kind}"${aliases ? ` data-aliases="${aliases}"` : ""}>
        <i data-lucide="${this.resolveIcon(cmd.icon)}" class="lucide mention-icon-${cmd.id}"></i>
        <span>${cmd.title}  <span class="mention-hint">/${cmd.name}</span></span>
      </button>`;
    }).join("");

    slashPage.innerHTML = backBtn + items;

    this.refreshIcons();
  }

  private fetchModels(): void {
    send({ type: "list_models" });
  }

  private bindSearchOverlay(): void {
    const btn = document.getElementById("btn-sidebar-search");
    btn?.addEventListener("click", () => {
      this.search.open();
    });

    const overlay = document.getElementById("search-overlay");
    overlay?.addEventListener("click", (e) => {
      if (e.target === overlay) this.search.close();
    });
  }

  private bindResponsiveSidebar(): void {
    const app = document.getElementById("app");
    if (!app) return;
    // Hysteresis: collapse below COLLAPSE_BP, expand above EXPAND_BP.
    // Inside the dead band, keep current state to avoid flicker.
    // COLLAPSE_BP matches the CSS `@media (max-width: 920px)` breakpoint so
    // the sidebar auto-collapses at the EXACT width where it would otherwise
    // flip into absolute/overlay mode — no gray zone where the sidebar covers
    // the main area but refuses to hide.
    const COLLAPSE_BP = 920;
    const EXPAND_BP = 1280;

    const collapsed = () => app.classList.contains("sidebar-collapsed");

    const check = () => {
      // When the window grows well past EXPAND_BP, clear the user-toggle
      // latch so future shrinks can auto-collapse again. Without this, a
      // single explicit click could permanently freeze the sidebar state.
      if (this.userToggledSidebar && window.innerWidth >= EXPAND_BP) {
        this.userToggledSidebar = false;
      }
      if (this.userToggledSidebar) return; // respect explicit user choice
      const w = window.innerWidth;
      // Use <= so the auto-collapse fires at the exact pixel where the CSS
      // overlay breakpoint kicks in (max-width: 920px). w < COLLAPSE_BP would
      // leave a 1px gray zone where the sidebar flips to absolute but is
      // still considered "expanded".
      if (w <= COLLAPSE_BP && !collapsed()) {
        app.classList.add("sidebar-collapsed");
      } else if (w >= EXPAND_BP && collapsed()) {
        app.classList.remove("sidebar-collapsed");
      }
      // Inside the dead band (COLLAPSE_BP, EXPAND_BP), keep current state.
    };

    // Triple-redundant trigger so the auto-collapse / auto-expand can never
    // silently miss a viewport change:
    //   1. matchMedia change listeners — fire EXACTLY when the viewport
    //      crosses the breakpoint; this is the most reliable signal.
    //   2. window resize event — fires on every viewport tick during drag.
    //   3. ResizeObserver on the documentElement — kept as belt-and-braces;
    //      known to be flaky for the root element but costs nothing.
    const collapseMql = window.matchMedia(`(max-width: ${COLLAPSE_BP}px)`);
    const expandMql = window.matchMedia(`(min-width: ${EXPAND_BP}px)`);
    const onCollapseMq = (ev: MediaQueryListEvent) => { if (ev.matches) check(); };
    const onExpandMq = (ev: MediaQueryListEvent) => { if (ev.matches) check(); };
    if (typeof collapseMql.addEventListener === "function") {
      collapseMql.addEventListener("change", onCollapseMq);
      expandMql.addEventListener("change", onExpandMq);
    } else {
      // Safari < 14 fallback (Electron's Chromium supports the new API).
      collapseMql.addListener(onCollapseMq);
      expandMql.addListener(onExpandMq);
    }
    window.addEventListener("resize", check, { passive: true });
    new ResizeObserver(check).observe(document.documentElement);

    // Initial state: align with viewport size, override persisted state.
    if (window.innerWidth <= COLLAPSE_BP) {
      app.classList.add("sidebar-collapsed");
    } else if (window.innerWidth >= EXPAND_BP) {
      app.classList.remove("sidebar-collapsed");
    }
    check();
    // Intentionally NO click handler on main-area: clicking the content
    // must not collapse the sidebar. Users can dismiss it via the toggle
    // button or by resizing past EXPAND_BP.
  }

  private submit(): void {
    let text = this.getPlainText();
    const st = getState();
    const hasAttachments = st.attachments.length > 0;

    if (!text.trim() && !this.effectiveMode() && !this.hasCommandChip() && !hasAttachments) return;

    // Safety net: if the input text is a known slash command but no chip
    // is active yet (e.g. user clicked the send button instead of Enter,
    // or the input handler didn't catch it), activate the chip first.
    if (!this.hasModeChip() && !this.hasCommandChip()) {
      const parsed = parseSlashInput(text);
      if (parsed && parsed.command.kind === "action") {
        this.activateSlashCommand(parsed.command, parsed.rest);
        return;
      }
    }

    const isQueued = st.running;

    // Send mode: the same effective mode the border reflects (chip OR
    // persistent).  Using effectiveMode() here guarantees "border visible
    // == this exact mode is sent" -- the two can never disagree.
    const sendMode = this.effectiveMode() || undefined;
    console.log("[submit] sendMode:", JSON.stringify(sendMode), "hasChip:", this.hasModeChip(), "chipMode:", this._currentChipMode, "persistent:", this._persistentMode);

    // If user submitted with no text, generate default text
    if (!text.trim()) {
      if (this.hasCommandChip()) {
        text = `<command>${this.getCurrentCommand()}</command>`;
      } else if (hasAttachments) {
        const first = st.attachments[0];
        if (first.mime_type === "text/x-terminal") {
          text = `<terminal>\n${first.content}\n</terminal>`;
        } else {
          text = `<attach n="${first.name}"${st.attachments.length > 1 ? ` c="${st.attachments.length}"` : ""} />`;
        }
      }
    }

    const fileRefs = st.attachments.length > 0 ? st.attachments.map(a => ({ name: a.name, size: a.size, icon: a.mime_type === "text/x-terminal" ? "terminal" : a.mime_type === "text/x-directory" ? "folder" : getFileIcon(a.name), path: a.path, mime_type: a.mime_type })) : undefined;

    if (isQueued) {
      pushQueuedPrompt(text, sendMode);
      clearAttachments();
      this._drafts.delete(st.sessionId || "");
      this.input.innerHTML = "";
      this.input.style.height = "56px";
      this.setInputExpanded(false);
      this._currentChipMode = "";
      setInputMode("");
      this.updateChipState();
      this.updatePlaceholder();
      this.updateSendButton();
      return;
    }

    // /steer: mid-run injection - don't add a user message or start a new turn
    if (this._currentChipMode === "steer") {
      if (!text.trim()) return;
      send({ type: "steer", session_id: st.sessionId || undefined, prompt: text } as any);
      this._drafts.delete(st.sessionId || "");
      this.input.innerHTML = "";
      this.input.style.height = "56px";
      this.setInputExpanded(false);
      this._currentChipMode = "";
      setInputMode("");
      this.updateChipState();
      this.updatePlaceholder();
      this.updateSendButton();
      return;
    }

    addUserMessage(text, sendMode, fileRefs);
    startAssistantMessage();
    setSessionState("running");
    this.updateUIState(true);

    const activeModel = st.modelConfigs.length > 0 ? st.modelConfigs[Math.min(st.activeModelIndex, st.modelConfigs.length - 1)] : undefined;
    const multimodal = activeModel?.multimodal === true;
    const mediaTypes = ["image/", "video/", "audio/"];

    const attachments = st.attachments.length > 0 ? st.attachments
      .filter((a) => {
        if (!multimodal && mediaTypes.some((t) => a.mime_type?.startsWith(t))) {
          showToast(t("files.skippedMedia") + ": " + (a.name || ""), "", "warning", "Files");
          return false;
        }
        return true;
      })
      .map(a => a.mime_type === "text/x-terminal" ? { ...a, content: `<terminal>\n${a.content}\n</terminal>` } : { ...a, content: "", is_binary: false }) : undefined;
    const payload: Record<string, any> = {
      type: "run",
      prompt: text,
      session_id: st.sessionId || undefined,
      attachments: attachments as any,
      temp_chat: st.tempChat || undefined,
    };
    // Poison the session id the moment a temp-chat run starts, so no
    // late rename/broadcast/stream event can ever surface it in the sidebar.
    if (st.tempChat && st.sessionId) {
      markTempChatSession(st.sessionId);
    }
    if (sendMode) {
      payload.mode = sendMode;
      // Include the command's custom prompt if defined
      const cmd = SLASH_COMMANDS.find(c => c.id === sendMode);
      if (cmd?.prompt) {
        payload.mode_prompt = cmd.prompt;
      }
    }
    send(payload as import("./core/types.js").ClientMessage);
    clearAttachments();
    this._inputHistory.push(text);
    this._inputHistoryIdx = this._inputHistory.length;
    // Force scroll to bottom so the user immediately sees the message they sent
    const _chatContainer = document.getElementById("chat-container");
    if (_chatContainer) _chatContainer.scrollTop = _chatContainer.scrollHeight;

    // Clear input — mode chip is hidden during agent execution.
    // A sticky *command* (active_command) is one-shot: it fires on submit
    // and then is cleared.  Unlike plan/spec it does NOT persist across
    // turns in the input chip (the backend re-injects its instructions
    // every turn until the next message).
    this._drafts.delete(st.sessionId || "");
    this.input.innerHTML = "";
    this.input.style.height = "56px";
    this.setInputExpanded(false);
    this._currentChipMode = "";
    setInputMode("");
    // Only persist mode commands (plan/spec) — action commands (init, custom)
    // are one-shot and must not keep the toolbar chip visible after send.
    // The inline mode-chip is intentionally NOT re-inserted here: after
    // sending, the input shows only the toolbar mode-chip + persistent border
    // (the backend confirms via mode_changed); the inline chip is one-shot.
    if (sendMode) {
      const _cmd = SLASH_COMMANDS.find(c => c.id === sendMode);
      if (_cmd?.kind === "mode") {
        this._persistentMode = sendMode;
      }
    }
    // Command chips are one-shot: clear the in-memory slot.
    // The backend clears the active_command in _run_agent's finally
    // block, so we don't send a set_command clear message here
    // (doing so would race with the async system prompt build).
    if (this._activeCommand) {
      this._activeCommand = null;
    }
    this.updateChipState();
    this.updatePlaceholder();
    this.updateSendButton();
    this.chat.render();
  }

  private cancel(): void {
    // Close @ dropdown if open
    const dd = document.getElementById("mention-dropdown");
    const bm = document.getElementById("btn-mention");
    if (dd && !dd.classList.contains("hidden")) {
      dd.classList.add("hidden");
      bm?.classList.remove("active");
      this._slashActive = false;
    }

    const s = getState();
    if (s.running) {
      this.btnStop.classList.add("cancelling");
      this.btnStop.style.pointerEvents = "none";
      send({ type: "cancel", session_id: s.sessionId });
    }

    // Always remove mode chip regardless of running state
    this.deactivateModeChip();
  }

  private updateUIState(running: boolean): void {
    if (running) {
      this.welcomeScreen.classList.add("hidden");
      this.messageList.classList.remove("hidden");
    }
  }

  /** Resume an existing session into the regular chat view. */
  private resumeSession(sid: string): void {
    const requestId = crypto.randomUUID();
    setRequestedSessionId(sid, requestId);
    send({ type: "resume", session_id: sid, request_id: requestId });
  }

  private initKeybindActions(): void {
    const a = this._keybindActions;

    // ── Application ──────────────────────────────────────────────────
    a["quit"] = () => (window as any).electronAPI?.windowClose?.();
    a["devtools"] = () => {
      const devMode = (typeof localStorage !== "undefined") && localStorage.getItem("encre-dev-mode") === "1";
      if (!devMode) return;
      (window as any).electronAPI?.toggleDevTools?.();
    };
    a["reload"] = () => {
      refreshAllData();
      this.chat.render();
    };
    a["fullscreen"] = () => {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        document.documentElement.requestFullscreen();
      }
    };

    // ── Session Management ───────────────────────────────────────────
    a["new_session"] = () => {
      this.automationPanel.hide();
      this.exitTempChat();
      // Clear all residual content-area widgets (sub-agent view, tool
      // detail panel, queue card, model dropdown, …) before requesting
      // a brand-new session.  resetChat() below wipes the state slice
      // but does not touch the DOM directly.
      this.cleanupContentArea({ keepAutomationFlag: false });
      resetChat();
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "new_session", request_id: requestId });
    };
    a["new_temp_chat"] = () => {
      if (getState().tempChat) return;
      // Temp chat is a normal-mode feature; ignore in workspace mode.
      if (getState().workspaceMode === "iwork") return;
      this.automationPanel.hide();
      this.exitTempChat();
      this.cleanupContentArea({ keepAutomationFlag: false });
      resetChat();
      setTempChat(true);
      document.getElementById("btn-temp-chat")?.classList.add("active");
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "new_session", request_id: requestId });
    };
    a["close_tab"] = () => {
      const app = document.getElementById("app");
      if (app?.classList.contains("settings-mode")) {
        this.settings.close();
      } else if (getState().sessionId) {
        const requestId = crypto.randomUUID();
        setRequestedSessionId("", requestId);
        send({ type: "new_session", request_id: requestId });
      }
    };
    a["next_session"] = () => {
      const sessions = getState().sessionsList;
      if (!sessions || sessions.length === 0) return;
      const currentId = getState().sessionId;
      const idx = sessions.findIndex(s => s.session_id === currentId);
      const next = sessions[(idx + 1) % sessions.length];
      if (next) this.resumeSession(next.session_id);
    };
    a["prev_session"] = () => {
      const sessions = getState().sessionsList;
      if (!sessions || sessions.length === 0) return;
      const currentId = getState().sessionId;
      const idx = sessions.findIndex(s => s.session_id === currentId);
      const prev = sessions[(idx - 1 + sessions.length) % sessions.length];
      if (prev) this.resumeSession(prev.session_id);
    };
    a["delete_session"] = async () => {
      const sid = getState().sessionId;
      if (!sid) return;
      const sessions = getState().sessionsList;
      const s = sessions?.find(x => x.session_id === sid);
      const name = s?.name || s?.preview || sid.slice(0, 8);
      const ok = await Dialog.confirm(t("session.confirmDeleteTitle"), t("session.confirmDelete", { name }), { container: document.getElementById("app") ?? undefined });
      if (ok) {
        send({ type: "delete_session", session_id: sid });
        resetChat();
      }
    };
    a["rename_session"] = () => {
      const sid = getState().sessionId;
      if (sid) showRenameDialogForSession(sid);
    };
    a["export_session"] = () => {
      const sid = getState().sessionId;
      if (sid) send({ type: "export_session", session_id: sid });
    };

    // ── Message Operations ──────────────────────────────────────────
    a["edit_last_message"] = () => {
      const btns = document.querySelectorAll<HTMLElement>(".msg-rollback-btn");
      if (btns.length > 0) btns[btns.length - 1].click();
    };
    a["copy_last_response"] = () => {
      const msgs = getState().messages;
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === "assistant" && msgs[i].content) {
          navigator.clipboard.writeText(msgs[i].content).catch(() => {});
          break;
        }
      }
    };
    a["undo_message"] = () => {
      const btns = document.querySelectorAll<HTMLElement>(".msg-rollback-btn");
      if (btns.length > 0) btns[btns.length - 1].click();
    };
    a["delete_message"] = () => {
      const btns = document.querySelectorAll<HTMLElement>(".msg-delete-btn");
      if (btns.length > 0) btns[btns.length - 1].click();
    };
    const doRetry = (mode: "normal" | "detailed" | "concise") => {
      const msgs = getState().messages;
      const lastAssistantIdx = msgs.length - 1;
      if (lastAssistantIdx < 0 || msgs[lastAssistantIdx].role !== "assistant") return;
      let userMsgIdx = -1;
      for (let i = 0; i < lastAssistantIdx; i++) {
        if (msgs[i].role === "user" && msgs[i].mode !== "compact_marker") userMsgIdx++;
      }
      if (userMsgIdx < 0) return;
      const removed = new Set<string>();
      for (let i = lastAssistantIdx; i < msgs.length; i++) removed.add(msgs[i].id);
      removeBranchMessages(removed);
      startAssistantMessage();
      setSessionState("running");
      const branchId = getState().activeBranchId;
      sendRetry(branchId, userMsgIdx, mode);
    };
    a["retry"] = () => doRetry("normal");
    a["retry_detailed"] = () => doRetry("detailed");
    a["retry_concise"] = () => doRetry("concise");

    // ── Search ──────────────────────────────────────────────────────
    a["search_global"] = () => this.search.open();
    a["search_settings"] = () => {
      const app = document.getElementById("app");
      if (app?.classList.contains("settings-mode")) {
        this.settings.focusSearch();
      }
    };

    // ── Settings ─────────────────────────────────────────────────────
    const openSetting = (panel: PanelId) => {
      this.automationPanel.hide();
      this.settings.open();
      this.settings.switchPanel(panel);
    };
    a["open_settings"] = () => openSetting("general");
    a["settings_general"] = () => openSetting("general");
    a["settings_models"] = () => openSetting("model");
    a["settings_skills"] = () => openSetting("skills");
    a["settings_mcp"] = () => openSetting("mcp");
    a["settings_agent"] = () => openSetting("agent");
    a["settings_index"] = () => openSetting("index");
    a["settings_rules"] = () => openSetting("rules");
    a["settings_memory"] = () => openSetting("memory");
    a["settings_usage"] = () => openSetting("usage");
    a["settings_about"] = () => openSetting("about");

    // ── Navigation / Sidebar ─────────────────────────────────────────
    a["toggle_sidebar"] = () => {
      this.userToggledSidebar = true;
      document.getElementById("app")?.classList.toggle("sidebar-collapsed");
    };
    a["view_chat"] = () => this.viewManager.switchTo("chat");
    a["view_automation"] = () => this.modeTransition.toggleAutomation();

    // ── Input Area ───────────────────────────────────────────────────
    a["attach_file"] = () => { this.files.promptForFiles(); };
    a["upload_file"] = () => { this.files.promptForFiles(); };
    a["history_prev"] = () => {
      if (this._inputHistory.length === 0) return;
      if (this._inputHistoryIdx > 0) {
        this._inputHistoryIdx--;
        const mode = this.getCurrentMode();
        this.input.innerHTML = "";
        if (mode) this.insertModeChip(mode);
        document.execCommand("insertText", false, this._inputHistory[this._inputHistoryIdx]);
      }
    };
    a["history_next"] = () => {
      const mode = this.getCurrentMode();
      if (this._inputHistoryIdx < this._inputHistory.length - 1) {
        this._inputHistoryIdx++;
        this.input.innerHTML = "";
        if (mode) this.insertModeChip(mode);
        document.execCommand("insertText", false, this._inputHistory[this._inputHistoryIdx]);
      } else {
        this._inputHistoryIdx = this._inputHistory.length;
        this.input.innerHTML = "";
        if (mode) this.insertModeChip(mode);
      }
    };

    // ── Modes ────────────────────────────────────────────────────────
    a["toggle_plan_mode"] = () => {
      this.input.focus();
      document.execCommand("insertText", false, "/plan ");
    };
    a["toggle_spec_mode"] = () => {
      this.input.focus();
      document.execCommand("insertText", false, "/spec ");
    };
    a["cancel"] = () => this.cancel();

    // ── Panel Tabs (session inner sidebar) ───────────────────────────
    const ensureTab = (id: string) => {
      const tabs = this.sessionInner.getTabs();
      if (tabs.some((t: TabDef) => t.id === id)) {
        this.sessionInner.activateTab(id);
      } else {
        this.sessionInner.createTab(id);
      }
    };
    a["toggle_terminal"] = () => ensureTab("terminal");
    a["toggle_editor"] = () => ensureTab("editor");
    a["toggle_review"] = () => ensureTab("review");
    a["toggle_info"] = () => ensureTab("info");
    a["prev_tab"] = () => {
      const tabs = this.sessionInner.getTabs();
      if (tabs.length < 2) return;
      const active = this.sessionInner.getActiveTab();
      const idx = tabs.findIndex((t: TabDef) => t.id === active);
      const prev = tabs[(idx - 1 + tabs.length) % tabs.length];
      this.sessionInner.activateTab(prev.id);
    };
    a["next_tab"] = () => {
      const tabs = this.sessionInner.getTabs();
      if (tabs.length < 2) return;
      const active = this.sessionInner.getActiveTab();
      const idx = tabs.findIndex((t: TabDef) => t.id === active);
      const next = tabs[(idx + 1) % tabs.length];
      this.sessionInner.activateTab(next.id);
    };

    // ── Theme / Language / Voice ─────────────────────────────────────
    a["toggle_theme"] = () => {
      const current = getState().themePreference || "system";
      const next = current === "dark" ? "light" : current === "light" ? "system" : "dark";
      Dialog.confirm(t("theme.switchTitle"), t("theme.switchConfirm", { label: t(`theme.${next}`) }), { priority: "low", container: document.getElementById("app") ?? undefined }).then(ok => {
        if (!ok) return;
        setThemePreference(next);
        if (next === "system") {
          const isDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
          setTheme(isDark ? "dark" : "light");
        } else {
          setTheme(next as "dark" | "light");
        }
        localStorage.setItem("encre-theme", next);
      });
    };
    a["toggle_language"] = () => {
      // Cycle through ALL supported locales (matches the picker order in LOCALES).
      const current = (getState().settings.language as string) || getLocale();
      const idx = Math.max(0, (LOCALES as readonly string[]).indexOf(current));
      const next = LOCALES[(idx + 1) % LOCALES.length];
      Dialog.confirm(t("language.switchTitle"), t("language.switchConfirm", { label: t(`language.${next}`) }), { priority: "low", container: document.getElementById("app") ?? undefined }).then(ok => {
        if (!ok) return;
        const settings = { ...getState().settings, language: next };
        setSettings(settings);
        setLocale(next);
        send({ type: "configure", config: { language: next } });
      });
    };
    // ── Automation ───────────────────────────────────────────────────
    a["automation_open"] = () => this.modeTransition.toggleAutomation();
    a["automation_new"] = () => {
      document.getElementById("automation-create-btn")?.click();
    };
    a["automation_run"] = () => {
      const firstEdit = document.querySelector<HTMLElement>('[data-action="edit"]');
      firstEdit?.click();
    };
    a["automation_toggle"] = () => {
      const firstToggle = document.querySelector<HTMLElement>(".auto-toggle");
      if (firstToggle) {
        firstToggle.click();
      }
    };
    a["automation_history"] = () => {
      const btn = document.querySelector<HTMLElement>("[data-automation-tab=\"history\"]");
      btn?.click();
    };

    // ── Workspace ────────────────────────────────────────────────────
    a["workspace_open"] = () => this.workspace.openFolder();
    a["workspace_close"] = () => this.workspace.forceExit();
    a["workspace_reindex"] = () => {
      send({ type: "reindex_workspace" } as any);
    };

    // ── Notifications ────────────────────────────────────────────────
    a["notifications_open"] = () => {
      if (!document.getElementById("encre-menu-dropdown")) this.openEncreMenu();
      this.showEncreNotifications();
    };
    a["notifications_clear"] = () => clearAllNotifications();
    a["show_shortcuts"] = () => this.toggleShortcutsCard();

  }

  private applyShortcutHints(): void {
    if (this._shortcutsApplied) return;
    const cfg = (getState().settings.keybinds as any);
    const binds: any[] = cfg?.keybinds || [];
    if (binds.length === 0) {
      if (!this._shortcutSub) this._shortcutSub = subscribe(() => this.applyShortcutHints());
      return;
    }
    this._shortcutsApplied = true;
    if (this._shortcutSub) { this._shortcutSub(); this._shortcutSub = undefined; }
    const els = document.querySelectorAll<HTMLElement>("[data-shortcut]");
    for (const el of els) {
      const id = el.getAttribute("data-shortcut");
      if (!id) continue;
      const shortcut = lookupShortcut(id);
      if (!shortcut) continue;
      const existingTitle = el.getAttribute("title") || el.getAttribute("data-i18n-title") || "";
      if (existingTitle) {
        el.setAttribute("title", augmentTitle(existingTitle, shortcut));
      }
    }
  }

  private toggleShortcutsCard(): void {
    const cfg = (getState().settings.keybinds as any);
    const binds: any[] = cfg?.keybinds || [];
    if (binds.length === 0) return;

    const scroll = document.createElement("div");
    scroll.className = "shortcuts-scroll";

    const grid = document.createElement("div");
    grid.className = "shortcuts-grid";

    const colDesc = document.createElement("div");
    colDesc.className = "sc-col sc-col-desc";
    colDesc.textContent = "Description";
    grid.appendChild(colDesc);

    const colKey = document.createElement("div");
    colKey.className = "sc-col sc-col-key";
    colKey.textContent = "Shortcut";
    grid.appendChild(colKey);

    const divider = document.createElement("div");
    divider.className = "sc-divider";
    grid.appendChild(divider);

    const categories = new Map<string, Array<{ id: string; keys: string[]; desc: string }>>();
    for (const b of binds) {
      const cat = b.category || "general";
      if (!categories.has(cat)) categories.set(cat, []);
      categories.get(cat)!.push({ id: b.id, keys: b.keys, desc: b.description || b.id });
    }

    const CAT_LABELS: Record<string, string> = {
      application: "Application",
      session: "Session",
      messages: "Messages",
      input: "Input",
      modes: "Modes",
      navigation: "Navigation",
      search: "Search",
      settings: "Settings",
      panels: "Panels",
      automation: "Automation",
      workspace: "Workspace",
      notifications: "Notifications",
      appearance: "Appearance",
      general: "General",
    };

    for (const [cat, items] of categories) {
      const cell = document.createElement("div");
      cell.className = "sc-category";
      cell.textContent = CAT_LABELS[cat] || cat.charAt(0).toUpperCase() + cat.slice(1);
      grid.appendChild(cell);

      for (const item of items) {
        const cellDesc = document.createElement("div");
        cellDesc.className = "sc-cell sc-cell-desc";
        cellDesc.textContent = t("shortcuts." + item.id);
        grid.appendChild(cellDesc);

        const keyText = item.keys && item.keys.length > 0
          ? formatShortcut(item.keys[0])
          : "—";
        const cellKey = document.createElement("div");
        cellKey.className = "sc-cell sc-cell-key";
        cellKey.textContent = keyText;
        grid.appendChild(cellKey);
      }
    }

    scroll.appendChild(grid);
    Dialog.showHtmlDialog("Keyboard Shortcuts", scroll, { priority: "low", container: document.getElementById("app") ?? undefined });
  }

  private bindKeyboardShortcuts(): void {
    document.addEventListener("keydown", (e) => {
      // Skip when shortcut recording is active in settings
      if (document.documentElement.dataset.shortcutRecording === "true") return;
      // When shortcuts panel is open, only allow Escape to close settings
      if (document.documentElement.dataset.shortcutPanelActive === "true") {
        if (e.key === "Escape") {
          this.settings.close();
          setActiveToolId(null);
        }
        return;
      }

      const key = e.key;
      const mod = e.ctrlKey || e.metaKey;
      const alt = e.altKey;
      const shift = e.shiftKey;

      let parts: string[] = [];
      if (mod) parts.push("ctrlcmd");
      if (alt) parts.push("alt");
      if (shift) parts.push("shift");

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

      if (pattern === "escape") {
        this.settings.close();
        setActiveToolId(null);
        this.search.close();
        return;
      }

      // Never intercept Shift-only + printable character (e.g. shift+? = typing ?)
      if (shift && !mod && !alt && key.length === 1) {
        return;
      }

      const keybindsCfg = (getState().settings.keybinds as any);
      const binds: any[] = keybindsCfg?.keybinds || [];

      // Block native browser handling for any key that's in the keybinds list
      // and for function keys (F1-F12) that are commonly used as shortcuts
      if (mappedKey.match(/^f(1[0-2]|[1-9])$/) || binds.some((kb) => kb.keys && kb.keys.includes(pattern))) {
        // Don't intercept Ctrl+Z when the composer is focused — let the
        // browser handle the native undo for the contenteditable.
        if (!(mappedKey === 'z' && mod && this.input.contains(document.activeElement))) {
          e.preventDefault();
        }
      }

      for (const kb of binds) {
        if (kb.keys && kb.keys.includes(pattern)) {
          // Skip undo_message when the composer is focused (native browser undo)
          if (kb.id === "undo_message" && this.input.contains(document.activeElement)) {
            return;
          }
          if (kb.id === "search_settings") {
            const app = document.getElementById("app");
            if (!app?.classList.contains("settings-mode")) return;
          }
          const action = this._keybindActions[kb.id];
          if (action) {
            e.preventDefault();
            action();
            return;
          }
        }
      }
    });
  }

  // ── Encre menu (language / theme / notifications / settings) ──────────
  private initEncreMenu(): void {
    const btn = document.getElementById("btn-encre-menu");
    if (!btn) return;

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      this.toggleEncreMenu();
    });

    document.addEventListener("click", () => this.closeEncreMenu());

    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.closeEncreMenu();
    });
  }

  private toggleEncreMenu(): void {
    if (document.getElementById("encre-menu-dropdown")) {
      this.closeEncreMenu();
      return;
    }
    this.openEncreMenu();
  }

  private openEncreMenu(): void {
    const btn = document.getElementById("btn-encre-menu");
    if (!btn) return;

    const menu = document.createElement("div");
    menu.id = "encre-menu-dropdown";
    menu.className = "encre-menu-dropdown";
    document.body.appendChild(menu);
    menu.innerHTML = this.buildMainMenuHtml();
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: menu });
    }

    const rect = btn.getBoundingClientRect();
    menu.style.top = `${rect.bottom + 6}px`;
    // Clamp horizontally into the viewport. Under rtl the anchor button
    // sits at the far right edge and the 240px menu would open mostly
    // off-window (left is a physical offset, it never mirrors).
    menu.style.left = `${clampElLeft(menu, rect.left)}px`;

    menu.addEventListener("click", (e) => {
      e.stopPropagation();
      const row = (e.target as HTMLElement).closest<HTMLElement>("[data-action]");
      if (!row) return;
      const action = row.getAttribute("data-action");
      if (action === "notifications") {
        this.showEncreNotifications();
      } else if (action === "settings") {
        this.closeEncreMenu();
        this.automationPanel?.hide();
        this.settings.open();
      }
    });
  }

  private refreshEncreMenu(): void {
    const menu = document.getElementById("encre-menu-dropdown");
    if (!menu) return;
    const main = menu.querySelector(".encre-menu-main") as HTMLElement | null;
    if (!main) return;
    main.innerHTML = this.buildMainMenuInnerHtml();
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: main });
    }
  }

  private closeEncreMenu(): void {
    const menu = document.getElementById("encre-menu-dropdown");
    if (!menu) return;
    this.notifications.detach();
    menu.remove();
  }

  /** Switches the menu to the notifications sub-view inside the dropdown. */
  private showEncreNotifications(): void {
    const menu = document.getElementById("encre-menu-dropdown");
    if (!menu) return;
    const main = menu.querySelector(".encre-menu-main") as HTMLElement | null;
    const sub = menu.querySelector(".encre-menu-sub") as HTMLElement | null;
    const content = menu.querySelector(".encre-notif-content") as HTMLElement | null;
    if (!main || !sub || !content) return;
    main.classList.add("hidden");
    sub.classList.remove("hidden");
    sub.scrollTop = 0;
    menu.style.width = "480px";
    this.notifications.attach(content, () => {
      this.notifications.resetDetail();
      this.refreshEncreMenu();
      main.classList.remove("hidden");
      sub.classList.add("hidden");
      menu.style.width = "";
    });
  }

  private buildMainMenuHtml(): string {
    return `<div class="encre-menu-main">${this.buildMainMenuInnerHtml()}</div>
      <div class="encre-menu-sub hidden"><div class="encre-notif-content"></div></div>`;
  }

  private buildMainMenuInnerHtml(): string {
    const unread = getUnreadCount();
    return `
      <button type="button" class="encre-menu-item-row encre-menu-action-row" data-action="notifications">
        <div class="encre-menu-item-info">
          <div class="encre-menu-item-title" style="display:flex;align-items:center;gap:8px">
            <i data-lucide="bell" class="lucide" style="width:14px;height:14px;color:var(--text-secondary)"></i>
            ${t("header.notifications")}
          </div>
        </div>
        <div class="encre-menu-item-control">
          ${unread > 0 ? `<span class="encre-menu-unread">${unread}</span>` : ""}
        </div>
      </button>
      <div class="encre-menu-item-divider"></div>
      <button type="button" class="encre-menu-item-row encre-menu-action-row" data-action="settings">
        <div class="encre-menu-item-info">
          <div class="encre-menu-item-title" style="display:flex;align-items:center;gap:8px">
            <i data-lucide="settings" class="lucide" style="width:14px;height:14px;color:var(--text-secondary)"></i>
            ${t("header.settings")}
          </div>
        </div>
      </button>`;
  }

  private async initTheme(): Promise<void> {
    const stored = localStorage.getItem("encre-theme");
    if (stored === "dark" || stored === "light" || stored === "system") {
      setThemePreference(stored);
      setSettings({ ...getState().settings, theme: stored });
    }
    const pref = getState().themePreference;
    const applySystemTheme = () => {
      const isDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      setTheme(isDark ? "dark" : "light");
      this.applyThemeIcons(isDark);
    };

    if (pref === "dark") {
      setTheme("dark");
      this.applyThemeIcons(true);
    } else if (pref === "light") {
      setTheme("light");
      this.applyThemeIcons(false);
    } else {
      applySystemTheme();
    }

    window
      .matchMedia("(prefers-color-scheme: dark)")
      .addEventListener("change", (e) => {
        if (getState().themePreference === "system") {
          setTheme(e.matches ? "dark" : "light");
          this.applyThemeIcons(e.matches);
        }
      });
  }

  private applyThemeIcons(dark: boolean): void {
    const encreMenuIcon = document.getElementById(
      "btn-encre-menu-icon"
    ) as HTMLImageElement | null;
    if (encreMenuIcon) {
      encreMenuIcon.src = dark ? "assets/Encre-dm.svg" : "assets/Encre-lm.svg";
    }
    const welcomeLogo = document.getElementById(
      "welcome-logo"
    ) as HTMLImageElement | null;
    if (welcomeLogo) {
      const iconOnly = dark ? "assets/Encre-dm.svg" : "assets/Encre-lm.svg";
      welcomeLogo.src = iconOnly;
    }
  }
}

const app = new App();
app.start();

/** Strip the type prefix that backend `_extract_ref_summary` prepends to a
 *  reference summary (e.g. "Memory:", "Web search:", "Fetched:").  The
 *  summary panel already groups references under a typed header, so repeating
 *  the type on every row is redundant. */
function stripRefTypePrefix(tool: string, summary: string): string {
  const t = (tool || "").toLowerCase();
  let s = (summary || "").trim();
  if (t.startsWith("mcp__")) {
    return s.replace(/^mcp\s*:\s*/i, "").replace(/^mcp\s+/i, "");
  }
  if (t.includes("memory") || t.includes("profile")) {
    s = s.replace(/^read\s+memory:\s*/i, "");
    s = s.replace(/^deleted\s+memory:\s*/i, "");
    s = s.replace(/^searched\s+memory:\s*/i, "");
    s = s.replace(/^memory:\s*/i, "");
    s = s.replace(/^profile\s*:\s*/i, "");
    return s;
  }
  if (t.startsWith("web_")) {
    s = s.replace(/^web\s+search:\s*/i, "");
    s = s.replace(/^web\s+fetch:\s*/i, "");
    s = s.replace(/^fetched\s*:\s*/i, "");
    return s;
  }
  return s;
}

export function updateRunningUI(running: boolean): void {
  const btnSend = document.getElementById("btn-send") as HTMLButtonElement;
  const btnStop = document.getElementById("btn-stop") as HTMLButtonElement;
  const welcomeScreen = document.getElementById("welcome-screen");
  const messageList = document.getElementById("message-list");

  if (btnSend) btnSend.style.display = running ? "none" : "flex";
  if (btnStop) btnStop.style.display = running ? "flex" : "none";
  if (running && welcomeScreen) welcomeScreen.classList.add("hidden");
  if (running && messageList) messageList.classList.remove("hidden");
}
