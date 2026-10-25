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

console.log("[perf] app.js eval start epoch=" + Date.now());

import { connect, send, sendRetry, sendSwitchBranch, sendRollback, abortConnect } from "./core/ws.js";
import { appendChipWithCaret, moveChipToFront, removeChipWithSeparator, wireChipClose, isChipSeparator } from "./ui/chip.js";
import { handleEvent, init as streamInit, onAutomationShowResult, setRequestedSessionId, onAutomationStreamEvent, refreshAllData, pullBootData, holdNonCriticalPlanes, releaseNonCriticalPlanes, sessionsArrived } from "./core/stream.js";
import { installUiErrorBoundaries, reportUiFault } from "./core/ui_boundary.js";
import { scheduleRender, runScheduledRenderNow } from "./core/render_scheduler.js";
import {
  addUserMessage,
  addMessage,
  startAssistantMessage,
  setSessionState,
  setTheme,
  setThemePreference,
  setThemeSkin,
  THEME_SKINS,
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
  clearQueuedPrompts,
  removeLastMessage,
  setTempChat,
  markTempChatSession,
  setSubAgentView,
  clearSubAgentBreadcrumb,
  restoreMessages,
  getTraySessions,
  removeBranchMessages,
  realUserMessages,
  clearAllNotifications,
  setModelConfigs,
  setProviders,
  locateModelInProviders,
  setAgentConfig,
  getUnreadCount,
  openRoutedLink,
  removeAttachment,
} from "./core/state.js";
import { Chat, formatAgentLabel, getAgentName } from "./chat/chat.js";
import { registerQueueSource } from "./chat/queue-panel.js";
import { initTooltip } from "./ui/tooltip.js";
import { initScrollbarReveal } from "./ui/scrollbar-reveal.js";
import { installSelectionActionBar } from "./ui/selection-action-bar.js";
import { renderCrumbs, type CrumbLevel } from "./ui/crumbs.js";
import { SplashScreen } from "./ui/splash.js";
import { Tools } from "./features/tools.js";
import { Settings, type PanelId } from "./settings/settings.js";
import { Files, getFileIcon, getFolderIcon, getTerminalIcon, attachPlaceholder, buildAttachChip } from "./features/files.js";
import { Session, showRenameDialogForSession } from "./session/session.js";
import { ViewManager } from "./ui/viewmanager.js";
import { Search, commandActions } from "./features/search.js";
import { Agents } from "./features/agents.js";
import { Market } from "./features/market.js";
import { Notifications } from "./ui/notifications.js";
import { Workspace, WorkspaceManager } from "./session/workspace.js";
import { Permissions } from "./ui/permissions.js";
import { AutomationPanel } from "./session/iclaw.js";
import { Automation } from "./session/automation.js";
import { ModeTransitionManager, type AppMode } from "./chat/mode-transition.js";
import { SessionInner, TabDef } from "./session/session_inner.js";
import { t, getLocale, onLocaleChange, applyI18n, setLocale, initLocale, LOCALES, getIntlLocale, ensureLocale } from "./features/i18n.js";
import { Dialog } from "./ui/dialog.js";
import { PERMISSION_MODES, permissionModeGroup, permissionModeLabel, permissionModeIcon, guardPermissionMode, dialogPermissionConfirm } from "./core/permission_mode.js";
import { closeEphemeralSurfaces, registerSurface, unregisterSurface } from "./core/scope.js";
import { applyTabTooltip } from "./ui/tab-tooltip.js";
import { lookupShortcut, augmentTitle, formatShortcut, platformLabel } from "./ui/shortcut-display.js";
import { scopeAllows } from "./ui/keybind-scope.js";
import type { Message, PluginStateEntry } from "./core/types.js";
import { SLASH_COMMANDS, matchingSlashCommands, parseSlashInput, type SlashCommand } from "./ui/slash-commands.js";
import { pluginStates } from "./core/plugin_info.js";
import { pluginIconHtml } from "./ui/plugin_assets.js";
import { clampIntoViewport, FLOAT_MARGIN, showAnchoredDropdown, showContextMenu, renderContextMenuItems, clearContextTargets } from "./ui/context-menu.js";
import { pluginMenuSpecs } from "./features/plugin_contributions.js";
import { resetPendingPanel } from "./features/pending-panel.js";
import { hideAnimated, showAnimated, hideViewAnimated, showViewAnimated } from "./ui/animate.js";

(window as any).__state_setActiveToolId = setActiveToolId;
(window as any).sendRetry = sendRetry;
(window as any).sendSwitchBranch = sendSwitchBranch;
(window as any).sendRollback = sendRollback;

/** Payload of the 「继续」 button on an interrupted turn. It has to read like a
 *  user message: a bare word, capital C, no trailing punctuation. */
const CONTINUE_PROMPT = "Continue";

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
  private market!: Market;
  private modeTransition!: ModeTransitionManager;
  private sessionInner!: SessionInner;
  private search!: Search;
  private input: HTMLTextAreaElement;
  private btnSend: HTMLButtonElement;
  private btnStop: HTMLButtonElement;
  private tokenCountEl: HTMLElement;
  private welcomeScreen: HTMLElement;
  private messageList: HTMLElement;
  /** Resolved from the DOM on every access rather than cached in the
   *  constructor: the summary panel is toggled from several places and a
   *  captured node silently goes stale if it is ever re-created, which leaves
   *  the toggle reading the visibility of a detached element. */
  private get summaryPanel(): HTMLElement {
    return document.getElementById("summary-panel") as HTMLElement;
  }
  private userToggledSidebar = false;
  private skipNextInput = false;
  private _slashActive = false;
  private _currentChipMode = "";
  private _persistentMode = "";
  private _summaryDoneCollapsed: boolean | null = null;
  /** Session id the summary panel content currently belongs to. */
  private _summarySessionId = "";
  /** Whether the summary panel is showing the context-usage detail page
   *  instead of the stacked sections (model-picker style subpage). */
  private _summaryCtxDetail = false;
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
  /** ?child= view this ESD window was created with, mounted once tabs exist. */
  private _initialChildView = "";
  private _initialChildLabel = "";
  /** Origin → permissions the page asked for, refreshed on a short TTL. */
  private _permCache = new Map<string, { list: Array<{ name: string; granted: boolean }>; at: number }>();
  private _activeAutomationJobId = "";
  private _keybindActions: Record<string, () => void> = {};
  private _inputHistory: string[] = [];
  private _inputHistoryIdx: number = -1;
  /** Debounce timer for localStorage draft persistence (one write per typing burst). */
  private _draftPersistTimer: number | null = null;
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

  constructor() {
    this.input = document.getElementById("prompt-input") as HTMLTextAreaElement;
    this.inputExpandBtn = document.getElementById("btn-input-expand") as HTMLButtonElement;
    this.inputArea = document.getElementById("input-area")!;
    this.btnSend = document.getElementById("btn-send") as HTMLButtonElement;
    this.btnStop = document.getElementById("btn-stop") as HTMLButtonElement;
    this.tokenCountEl = document.getElementById("token-count")!;
    this.welcomeScreen = document.getElementById("welcome-screen")!;
    this.messageList = document.getElementById("message-list")!;

    // Expose the cleanup function on window so other modules (settings,
    // search, etc.) can request a content-area reset without needing a
    // direct import.  Use a getter that re-binds to ``this`` so callers
    // always invoke the bound method even after hot-reloads.
    (window as any).__appCleanupContentArea = (opts?: { keepAutomationFlag?: boolean }) =>
      this.cleanupContentArea(opts);
    // Expose the app instance on window so stream.ts can update the mode chip
    (window as any).__app = this;
    // Restore drafts and sent-input history persisted by a previous run so
    // an app restart does not wipe what the user was composing.
    this.loadPersistedInputState();

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
    // The market's 已安装 button hands over to settings' 插件 panel: installed
    // management lives there (one list, one set of rules), so the market does
    // not grow a second copy of it.  Exposed as a window hook because the
    // market view has no reference to the Settings instance.
    (window as any).__encreOpenSettings = (panel?: string) => {
      this.settings.open();
      if (panel) this.settings.switchPanel(panel as Parameters<Settings["switchPanel"]>[0]);
    };
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
    initScrollbarReveal();
    installSelectionActionBar();
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
    }, ["workspaceMode"]);

    // Update stats display when telemetry changes. "messages" changes on
    // every streamed token, but the token counts it feeds only change at turn
    // boundaries, so coalesce the O(history) rescan behind a short timer.
    let statsTimer = 0;
    subscribe(() => {
      if (statsTimer) return;
      statsTimer = window.setTimeout(() => {
        statsTimer = 0;
        this.updateStats();
      }, 250);
    }, ["messages", "telemetry"]);

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
    }, ["running", "attachments", "inputMode"]);

    // Sync inputMode state changes to the input border / chip via the
    // single updateChipState() path.  Previously this wrote data-input-mode
    // directly from state.inputMode while updateChipState() wrote it from
    // _persistentMode -- two sources fighting over one attribute.  Now both
    // funnel through effectiveMode() (chip OR persistent), so the border
    // always reflects the mode that will actually be sent.
    let prevInputMode = "";

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
    }, ["inputMode", "themePreference"]);

    // Update session bar name when session changes. `messages` itself mutates
    // on every token but the bar only uses its count (empty vs non-empty), so
    // a token that does not change the count must not re-run the O(sessions)
    // lookup behind updateSessionBarName().
    let barMsgCount = getState().messages.length;
    subscribe((changed) => {
      const count = getState().messages.length;
      if (changed && changed.size === 1 && changed.has("messages") && count === barMsgCount) return;
      barMsgCount = count;
      this.updateSessionBarName();
    }, ["messages", "subAgentView", "subAgentBreadcrumb", "tempChat", "sessionsList", "sessionId"]);
    subscribe(() => this.updatePlaceholder(), ["workspaceMode", "inputMode"]);
    // The pending panel (floating card above the composer) registers the
    // submit queue as its first source and subscribes to the state it reads,
    // so it repaints itself — later sources (background shells, scheduled
    // runs) just call registerPendingSource() and get a tab.
    registerQueueSource({
      loadForEdit: (text, mode) => this.loadQueuedPromptForEdit(text, mode),
    });

    // Sync temp chat button active state
    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    subscribe(() => {
      const btn = document.getElementById("btn-temp-chat");
      if (btn) {
        btn.classList.toggle("active", getState().tempChat);
      }
    }, ["tempChat"]);

    // Child window mode — detect ?child=VIEW_NAME from URL
    const params = new URLSearchParams(window.location.search);
    const childView = params.get("child");
    if (childView) {
      // Stored, not mounted here: the tab area (sessionInner) does not exist
      // yet during construction; mountInitialChildView opens it once, later.
      this._initialChildView = childView;
      this._initialChildLabel = params.get("label") || childView;
      this.enterChildMode();
    }

    // Defer non-critical module initialization (Files, Session, Workspace,
    // Automation, etc.) so the constructor returns quickly and the splash
    // can hide earlier.  Use a double-rAF (NOT a microtask) so the heavy
    // init runs AFTER the first paint — the splash is visible immediately
    // and the deferred work happens in the gap while the renderer waits for
    // the backend WS handshake.
    App.onFrames(() => this.initDeferred(), 2, 1000);
  }

  /**
   * Run `fn` after `frames` animation frames, but never later than `boundMs`.
   *
   * Chromium does not schedule requestAnimationFrame while it considers the
   * page hidden, and the window is revealed by the main process on
   * `ready-to-show` — a point at which its occlusion/visibility state has not
   * necessarily caught up. Anything gated on a bare rAF therefore waits for
   * the next activation change rather than the next frame, which is how boot
   * ends up half-initialised with an unresponsive-looking window.
   */
  private static onFrames(fn: () => void, frames: number, boundMs: number): void {
    let fired = false;
    const run = () => {
      if (fired) return;
      fired = true;
      clearTimeout(bound);
      fn();
    };
    const bound = setTimeout(run, boundMs);
    let left = frames;
    const step = () => {
      if (--left <= 0) run();
      else requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
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
    this.market = new Market();
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
    }, ["workspaceMode"]);
    this.sessionInner = new SessionInner();
    this.chat.onViewChanges = (path: string) => {
      const st = getState();
      const artifact = st.artifacts.find(a => a.path === path);
      this.sessionInner.showReviewTab(path, artifact || undefined);
    };
    // Files/images the model mentioned open in the in-app file viewer
    // (content-area tabs) — never the Encre Agent Desktop Subwindow (ESD) sub-app or the system browser.
    this.chat.onOpenFile = (path: string) => {
      void this.sessionInner.openFileInEditor(path);
    };
    // 「继续」 on an interrupted turn: send the resume prompt as the user would.
    this.chat.onResume = () => this.resumeInterrupted();
    (window as any).__sessionInner = this.sessionInner;
    (window as any).__workspaceMgr = this.workspaceManager;

    // ESD boot: the tab strip belongs on the title row (ESD · tabs · window
    // buttons), so the hosted page starts directly below it.
    this.hostTabStripInHeader();
    this.mountInitialChildView();

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
    }, ["settings"]);

    // Forward the browser credential auto-save flag to the main process so
    // the encrypted store accepts (or ignores) captured entries. Only a
    // literal "false" disables it — unset defaults to enabled.
    let lastAutosave: boolean | null = null;
    subscribe(() => {
      const v = getState().settings.browser_autosave_credentials;
      const on = !(v === "false" || v === false);
      if (on !== lastAutosave) {
        lastAutosave = on;
        window.electronAPI?.browserAutosaveUpdate?.(on);
      }
    }, ["settings"]);

    // Forward every browser switch to the main process.  Most are read there
    // per request / per prompt, so this only has to keep the two copies in
    // sync; the few Chromium caches per-guest are re-applied by the handler.
    const browserInt = (v: unknown, d: number): number => {
      const n = typeof v === "number" ? v : parseInt(String(v ?? ""), 10);
      return Number.isFinite(n) ? n : d;
    };
    let lastBrowserOpts = "";
    subscribe(() => {
      const s = getState().settings;
      const opts = {
        doNotTrack: isEnabled(s.browser_do_not_track),
        denyPermissions: isEnabled(s.browser_deny_site_permissions),
        blockThirdPartyCookies: isEnabled(s.browser_block_third_party_cookies),
        askBeforeDownload: s.browser_ask_before_download === undefined ? true : isEnabled(s.browser_ask_before_download),
        downloadDir: typeof s.browser_download_dir === "string" && s.browser_download_dir !== "default"
          ? s.browser_download_dir : "",
        forceLightPage: s.browser_force_light_page === undefined ? true : isEnabled(s.browser_force_light_page),
        mobileIdentity: s.browser_identity === "mobile",
        historyLimit: browserInt(s.browser_history_limit, 5000),
        // Omit zoomFactor while the setting has not arrived yet: a freshly
        // spawned window (the ESD sub-app) starts with an empty local
        // snapshot, and pushing the fallback here would clobber the global
        // factor main already applied — resetting every open guest.
        ...(s.browser_zoom_percent === undefined
          ? {}
          : { zoomFactor: browserInt(s.browser_zoom_percent, 75) / 100 }),
        blockPopups: isEnabled(s.browser_block_popups),
      };
      const sig = JSON.stringify(opts);
      if (sig !== lastBrowserOpts) {
        lastBrowserOpts = sig;
        window.electronAPI?.browserOptionsUpdate?.(opts);
      }
    }, ["settings"]);

    // "Don't keep me signed in" is a per-launch wipe: the flag is only knowable
    // once the config snapshot lands, so it runs exactly once and before any
    // tab the user opens could be cleared out from under it.  Bookmarks and
    // history are deliberately left alone.
    let freshSessionChecked = false;
    subscribe(() => {
      if (freshSessionChecked) return;
      const v = getState().settings.browser_fresh_session;
      if (v === undefined) return;
      freshSessionChecked = true;
      if (isEnabled(v)) void window.electronAPI?.browserClearSessionData?.();
    }, ["settings"]);

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
    }, ["themePreference"]);

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
    }, ["settings", "connected"]);

    // Listen for session switch from tray popup
    if (window.electronAPI) {
      window.electronAPI.onSwitchWorkspace((path: string) => {
        this.settings.close();
        void this.workspace.open(path);
      });
      // Quit confirmation: main only sends `quit-request` after the BACKEND
      // confirmed running tasks, so the renderer must always raise the
      // app-scope dialog here (never re-check locally — the renderer's view
      // may be stale when the websocket is disconnected).
      const electronAPI = window.electronAPI;
      window.electronAPI.onQuitRequest(() => {
        void Dialog.confirm(
          t("dialog.quitTitle"),
          t("dialog.quitBody"),
          {
            scope: "app",
            priority: "critical",
            primary: t("dialog.quitConfirm"),
            secondary: t("dialog.cancel"),
          },
        ).then((allowed) => {
          electronAPI.respondQuitConfirm(allowed);
        });
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
          send({ type: "queue_list", session_id: sessionId });
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
    subscribe(_maybeApplyStartupMode, ["connected", "settings"]);

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
        // Manual restore clears the browser-tool auto-open suppression.
        this.sessionInner.markUserSidebarCollapsed(false);
      } else {
        this.sessionInner.saveWidth();
        panel.classList.add("hidden");
        mainBody.classList.add("sidebar-hidden");
        this.sessionInner.saveSidebarVisibility();
        // Manual collapse suppresses browser-tool auto-open until the user
        // restores the sidebar by hand.
        this.sessionInner.markUserSidebarCollapsed(true);
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
            if ((window as any).lucide) (window as any).lucide.createIcons({ root: chip });
          }
        }
      }
      this.chat.render();
      this.updateChipState();
    });
  }

  /** Turn this window into the Encre Agent Desktop Subwindow (ESD): the
   *  child-mode class is what hands the window to the subwindow CSS, which
   *  drops the nav sidebar, the chat column and the composer, and lets the tab
   *  layer fill it. Without the class the sub-app paints a second copy of the
   *  main window with its page shoved into a side panel.
   *
   *  `#app` carries the class too: the rules that must yield while Settings is
   *  open (the nav sidebar has to come back) are scoped to
   *  `#app.child-mode:not(.settings-mode)`, and `settings-mode` lives on `#app`. */
  private enterChildMode(): void {
    document.title = "ESD";
    document.body.classList.add("child-mode");
    document.getElementById("app")?.classList.add("child-mode");
    // Views opened into an already-running subwindow arrive over IPC.
    window.electronAPI?.onChildAddTab?.((view: string, label: string) => {
      this.openChildViewAsTab(view, label);
    });
  }

  /** The subwindow's tab strip is its own header slot (#child-header-tabs),
   *  not the content area's bar: the tab layer renders its list into the title
   *  row while the panels stay below. One tab engine, one visible strip. */
  private hostTabStripInHeader(): void {
    if (!document.body.classList.contains("child-mode")) return;
    const slot = document.getElementById("child-header-tabs");
    if (slot) this.sessionInner.mountTabList(slot);
  }

  /** Route a child-window view request (main-window routed link, legal doc,
   *  data center, easter egg) into this window's full-UI tab area. */
  private openChildViewAsTab(view: string, label: string): void {
    const inner = this.sessionInner;
    if (!inner) return;
    if (/^(https?|file):/i.test(view) || view === "about:blank") {
      void inner.createTab("browser", { startUrl: view === "about:blank" ? "" : view, label });
    } else if (view.startsWith("mailto:")) {
      window.location.href = view;
    } else {
      void inner.createTab(view, { label });
    }
  }

  /** Open the ?child= view this window was created with (ESD boot), once. */
  private mountInitialChildView(): void {
    const view = this._initialChildView;
    if (!view) return;
    this._initialChildView = "";
    this.openChildViewAsTab(view, this._initialChildLabel);
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
      const goToRootView = () => (window as any).__goToRootView?.();
      const navigateToCrumb = (i: number) => (window as any).__navigateToBreadcrumb?.(i);
      const levels: CrumbLevel[] = [{ label: rootCrumbLabel, onActivate: goToRootView }];
      // Track whether the last breadcrumb entry IS the current sub-agent view
      // to avoid visual duplicates (breadcrumb button + current span for same level).
      let lastBreadcrumbIsCurrent = false;
      breadcrumb.forEach((entry, i) => {
        const isLast = i === breadcrumb.length - 1;
        const label = formatAgentLabel(entry.name);
        if (isLast && (!s.subAgentView || entry.toolCallId === s.subAgentView.id)) {
          // Last crumb either has no active sub-agent view (currently at this level),
          // or it IS the active sub-agent view — render as current, not a button.
          levels.push({ label });
          if (entry.toolCallId === s.subAgentView?.id) {
            lastBreadcrumbIsCurrent = true;
          }
        } else {
          levels.push({ label, onActivate: () => navigateToCrumb(i) });
        }
      });
      // If a sub-agent view is active and NOT already shown as the last breadcrumb,
      // show the current agent name.
      if (s.subAgentView && !lastBreadcrumbIsCurrent) {
        levels.push({ label: formatAgentLabel(getAgentName(s.subAgentView)) });
      }
      renderCrumbs(nameEl, levels);
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
    this.setSessionBarName(nameEl, rootLabel);
  }

  /**
   * Write the session-bar label, replaying the shared content enter when —
   * and only when — the text actually changes.
   *
   * Opening or switching a session swaps this label in the same breath as
   * the transcript, but it used to snap: the new name simply replaced the old
   * one with no motion anywhere in the header. Going through here means a
   * session switch reads as one move — tape and title arrive together — while
   * the many calls that re-assert an unchanged name (every render during a
   * stream) stay free.
   *
   * The class is dropped, the layout re-measured and the class re-added so
   * the keyframes restart; `encre-content-enter` carries no fill mode, so
   * nothing survives the 180ms.
   */
  private setSessionBarName(el: HTMLElement | null, text: string): void {
    if (!el || el.textContent === text) return;
    el.textContent = text;
    el.classList.remove("session-enter");
    void el.offsetWidth;
    el.classList.add("session-enter");
  }

  async start(): Promise<void> {
    (window as any).__perfStart = performance.now();
    // One process-wide safety net: uncaught component/async errors are
    // recorded by the shared boundary instead of aborting the app.
    installUiErrorBoundaries();

    // Surface main-thread stalls during startup. A "longtask" entry is any
    // synchronous block over 50 ms; logging the heavy ones makes the exact
    // boot regression visible next time instead of being reported as
    // "everything feels stuck".
    if (typeof PerformanceObserver !== "undefined") {
      try {
        const obs = new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            if (entry.duration >= 80) {
              console.warn(`[perf] long task ${Math.round(entry.duration)}ms at ${Math.round(entry.startTime)}ms`);
            }
          }
        });
        obs.observe({ entryTypes: ["longtask"] });
        (window as any).__perfLongTaskObserver = obs;
      } catch {
        /* longtask not supported */
      }
    }

    // The main window is shown immediately by the main process; the in-app
    // splash screen covers boot while the startup mode's data is filled, then
    // fades out on reveal. Remaining slices (other modes' data) keep loading
    // in the background via stream events after reveal.
    this.splash.show();

    // Lazy locale tables: make sure the selected language's chunk is installed
    // before anything is painted, so a non-zh/en session never flashes the
    // English fallback first. zh/en resolve synchronously (no await cost).
    // Kick the import off BEFORE the splash's first-frame wait and only join
    // it afterwards: the dynamic import overlaps the frame wait instead of
    // serializing ahead of it.
    const localeReady = ensureLocale(getLocale());

    await new Promise<void>((r) => App.onFrames(r, 1, 250));
    await localeReady;

    const _connStart = performance.now();
    // Hold back side-panel planes from the moment we connect: the backend
    // pushes the whole snapshot on connect, and applying the non-critical
    // planes during boot only steals main-thread time from the reveal.
    holdNonCriticalPlanes();
    // Historical transcript blocks render as escaped text until the app is
    // visible, then upgrade to markdown during idle slices. Session replies
    // re-assert this for large histories (see the session_ready handler).
    this.chat.deferMarkdown = true;
    // While the WS connect retries, watch the service status: once the main
    // process reports the backend process actually died (a start attempt
    // failed), stop the retry loop immediately — hammering a dead port just
    // delays the error screen by many loader cycles.
    const _statusPoll = setInterval(async () => {
      if (getState().connected) return;
      try {
        const st = await window.electronAPI?.getServiceStatus?.();
        if (st && st.running === false && st.error) abortConnect();
      } catch {}
    }, 1000);
    await connect(handleEvent);
    clearInterval(_statusPoll);
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
      // The shell is interactive now; upgrade restored answers to markdown
      // in small idle slices instead of one blocking pass.
      this.chat.beginMarkdownUpgrade(120);
    } else {
      // Backend never came up: keep the splash showing the error + retry.
      await this.handleSplashError("app.splashNoConnect");
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
      return this.automationPanel.isActive;
    }
    if (mode === "iwork") {
      // "Arrived", not "non-empty": a zero-session user's (empty) session
      // snapshot is a complete answer, and requiring length > 0 made every
      // fresh install sit on the splash for the full deadline.
      return sessionsArrived();
    }
    // normal (通用): general-mode chat needs the session lists.
    return sessionsArrived();
  }

  private async handleSplashError(titleKey: string): Promise<void> {
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
    // Pass the i18n KEY (not a pre-translated string): the splash locks the
    // document into English before resolving it.
    this.splash.showError(titleKey, detail, async () => {
      // Fade the error out and bring the animated loader back first, then
      // wait for the current loader loop to finish before judging the result
      // so the transition never cuts the animation short.
      this.splash.beginRestart();
      try {
        const result = await window.electronAPI?.restartService?.();
        await this.splash.awaitLoaderCycle();
        if (result?.success) {
          location.reload();
        } else {
          // Slide the error back in but keep the same title so the user
          // can keep hitting the button to retry continuously.
          this.splash.showError(titleKey, result?.error || "");
        }
      } catch (err) {
        await this.splash.awaitLoaderCycle();
        this.splash.showError(titleKey, String(err));
      }
    });
    // Block until the user resolves the error (page reloads on success; on
    // failure the caller keeps the splash open and the restart button stays
    // clickable so the user can hammer it as often as they want).
    await new Promise<void>(() => {});
  }

  private bindInput(): void {
    this.btnSend.addEventListener("click", () => this.submit());
    this.btnStop.addEventListener("click", () => this.cancel());

    this.input.addEventListener("keydown", (e) => {
      // ArrowUp/ArrowDown recall sent-message history when the composer is
      // empty (no text, no chips). With content present the arrows keep
      // their native multiline caret-navigation role.
      if (!e.isComposing && (e.key === "ArrowUp" || e.key === "ArrowDown") && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const composerEmpty = !this.getPlainText().trim() && !this.hasModeChip() && !this.hasCommandChip();
        if (composerEmpty) {
          const action = e.key === "ArrowUp" ? this._keybindActions["history_prev"] : this._keybindActions["history_next"];
          if (action) {
            e.preventDefault();
            action();
            return;
          }
        }
      }
      const sendMode = (getState().settings.shortcut_send_mode as string) || "enter";
      const isCtrlEnter = sendMode === "ctrl_enter";
      const isSendKey = isCtrlEnter
        ? e.key === "Enter" && (e.ctrlKey || e.metaKey)
        : e.key === "Enter" && !e.shiftKey;

      if (isSendKey) {
        e.preventDefault();
        const text = this.getPlainText();

        // Check slash command match (e.g. /plan → activate mode).  Runs
        // regardless of active chips — mode and command are independent
        // channels and may combine in one submit.
        const parsed = parseSlashInput(text);
        if (parsed) {
          this.activateSlashCommand(parsed.command, parsed.rest);
          return;
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
          hideAnimated(dd);
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
          hideAnimated(dd);
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
      this.repairChips();
      this.updateChipState();
      this.resizeInput();
      this.updateInputExpandButton();
      this.updatePlaceholder();
      this.updateSendButton();
      this.saveCurrentDraft();
    });

    // Backspacing at a chip boundary lets Chromium natively delete into the
    // contenteditable=false element, which mangles it (splits it / strips its
    // icon or label children).  Take over: when the collapsed caret sits
    // directly before a chip, remove it atomically through its own close path
    // instead of letting the browser touch its internals.  Deleting the
    // separator space one caret position earlier stays a normal character
    // deletion — the guard only fires at the element boundary.
    this.input.addEventListener("beforeinput", (e) => {
      if ((e as InputEvent).inputType !== "deleteContentBackward") return;
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0 || !sel.isCollapsed) return;
      const { startContainer, startOffset } = sel.getRangeAt(0);
      let prev: Node | null = null;
      if (startContainer.nodeType === Node.TEXT_NODE) {
        if (startOffset > 0) return; // deleting a normal character
        prev = startContainer.previousSibling;
      } else {
        if (startOffset === 0) return; // start of the composer
        prev = startContainer.childNodes[startOffset - 1];
      }
      const chip = (prev as Element | null)?.closest?.(".mode-chip, .command-chip");
      if (!chip || !this.input.contains(chip)) return;
      e.preventDefault();
      if (chip.classList.contains("command-chip")) {
        this.deactivateCommandChip();
      } else if (chip.getAttribute("data-attach") === "true") {
        const path = chip.getAttribute("data-path");
        if (path) removeAttachment(path);
      } else {
        this.removeInlineModeChip();
      }
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
    // Full re-raster hammer on viewport settle: after a drag/resize/DPI
    // switch the compositor can keep showing tiles rastersed for the old
    // viewport (stale fragments at the window edges). Toggling a
    // will-change layer on the root forces every tile to be re-rastered
    // into the current viewport on the next frame. Debounced: resize fires
    // continuously in an interactive drag.
    let rasterHammerTimer: ReturnType<typeof setTimeout> | null = null;
    window.addEventListener(
      "resize",
      () => {
        if (rasterHammerTimer) clearTimeout(rasterHammerTimer);
        rasterHammerTimer = setTimeout(() => {
          rasterHammerTimer = null;
          const rootStyle = document.documentElement.style;
          rootStyle.willChange = "transform";
          requestAnimationFrame(() => {
            if (rootStyle.willChange === "transform") rootStyle.willChange = "";
          });
        }, 150);
      },
      { passive: true },
    );

    // Toolbar chip close: click the icon to deactivate (icon turns × on hover).
    // wireChipClose delegates on the chip container — lucide replaces the inner
    // <i class="chip-icon"> with an <svg>, which would destroy a listener bound
    // to the icon element.
    const tbModeChip = document.getElementById("toolbar-mode-chip");
    if (tbModeChip) wireChipClose(tbModeChip, () => this.deactivateModeChip());
    const tbCmdChip = document.getElementById("toolbar-command-chip");
    if (tbCmdChip) wireChipClose(tbCmdChip, () => this.deactivateCommandChip());
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

    // Open the slash menu on "/" regardless of active chips — a mode chip
    // must not block picking a slash command (they are independent channels).
    if (text.startsWith("/")) {
      // Re-render slash dropdown to pick up any custom commands that
      // arrived after initial render (e.g. via slash-commands-updated).
      this.renderSlashDropdown();
      if (mentionDropdown.classList.contains("hidden")) {
        showAnimated(mentionDropdown);
        this.ownEphemeral(mentionDropdown);
        btnMention.classList.add("active");
      }
      this._slashActive = true;
      mentionDropdown.closest(".input-wrapper")?.classList.add("slash-menu-open");

      // Slash mode → always show only slash sub-page, never Files/Folder
      // (and never a resource sub-page left over from a previous visit).
      mentionDropdown.querySelectorAll('[data-page^="res-"]').forEach((p) => p.classList.add("hidden"));
      mainPage?.classList.add("hidden");
      slashPage?.classList.remove("hidden");

      // Highlight matching slash commands (name OR alias, case-insensitive).
      const query = text.slice(1).toLowerCase();
      if (query) {
        mentionDropdown.querySelectorAll('[data-slash]').forEach(el => {
          const name = (el.getAttribute("data-slash") || "").toLowerCase();
          if (name.startsWith(query)) {
            el.classList.add("mention-match");
          }
        });
      }
    } else if (this._slashActive) {
      hideAnimated(mentionDropdown);
      btnMention.classList.remove("active");
      this._slashActive = false;
      mentionDropdown.closest(".input-wrapper")?.classList.remove("slash-menu-open");
    }
  }

  private handleManualSlashCommand(): void {
    const text = this.getPlainText();

    // No chip guard here: a typed "/cmd" must activate even while a mode or
    // command chip is up — mode and command are independent channels.
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
      const url = link.href;
      // file:// links are handled by their own dedicated flows — never routed.
      if (!url || /^file:/i.test(url)) return;
      // Only intercept web/mailto URLs; everything else keeps native behavior.
      if (!/^(https?:\/\/|www\.|mailto:)/i.test(url)) return;
      e.preventDefault();
      e.stopPropagation();
      openRoutedLink(url);
    }, true);
    // Defense in depth: the main process bounces any in-page navigation to a
    // web URL back here (will-navigate guard) so it goes through the same
    // routing instead of replacing the app document.
    (window as any).electronAPI?.onOpenRoutedUrl?.((url: string) => {
      openRoutedLink(url);
    });
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
        // Automation is a page, not a mode: the seg keeps highlighting the
        // underlying mode (normal / iwork) even while the page is open.
        const active = getState().workspaceMode === "iwork" ? "iwork" : "normal";
        items.forEach((el) => {
          const isActive = el.getAttribute("data-mode") === active;
          el.classList.toggle("active", isActive);
          el.setAttribute("aria-selected", isActive ? "true" : "false");
        });
      };
      syncSegFromState();
      subscribe(syncSegFromState, ["workspaceMode"]);
      this.automationPanel.onHide = syncSegFromState;
    }

    // ── Sidebar entries (automation / plugin market) ──
    // Automation keeps its original behaviour (mode toggle) — only the entry
    // point moved here from the header seg switch. The plugin market entry
    // opens the dedicated content-area market view (features/market.ts).
    const sidebarAutomationBtn = document.getElementById("btn-sidebar-automation");
    sidebarAutomationBtn?.addEventListener("click", () => {
      if (this.market?.active) this.market.close();
      this.toggleAutomationPage();
    });
    const sidebarMarketBtn = document.getElementById("btn-sidebar-market");
    sidebarMarketBtn?.addEventListener("click", () => {
      if (this.automationPanel.isActive) {
        void this.automationPanel.hide();
      }
      // Same contract as automation: entering the market from inside a
      // session exits the session first; closing the market later reveals
      // the fresh new-task state, not the old transcript.
      if (!this.market.active) this.exitSessionForOverlay();
      this.market.toggle();
    });

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

    // + button: upward dropdown
    const btnMention = document.getElementById("btn-mention");
    const mentionDropdown = document.getElementById("mention-dropdown");
    btnMention?.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasHidden = mentionDropdown?.classList.contains("hidden");
      if (mentionDropdown) {
        // Enter/exit both go through the motion helpers: an exit still in
        // flight (the dropdown was closed <120ms ago) has to be cancelled,
        // not raced — otherwise the dropdown finishes fading out first.
        if (wasHidden) {
          // Re-render so mode entries reflect the current workspace mode
          // and any commands that arrived after the last render.
          this.renderSlashDropdown();
          showAnimated(mentionDropdown);
          this.ownEphemeral(mentionDropdown);
        }
        else hideAnimated(mentionDropdown);
      }
      btnMention?.classList.toggle("active");
      this._slashActive = false;
      // Button-opened menus keep the compact trigger-anchored placement.
      mentionDropdown?.closest(".input-wrapper")?.classList.remove("slash-menu-open");
      if (wasHidden) {
        // Reopening always lands on the main page: the slash page and every
        // resource sub-page are folded back in, whatever was left open.
        const mainPage = mentionDropdown?.querySelector('[data-page="main"]');
        mentionDropdown?.querySelectorAll(".mention-page").forEach((p) => {
          if (p !== mainPage) p.classList.add("hidden");
        });
        if (mainPage) mainPage.classList.remove("hidden");
      }
    });

    // Close dropdown on outside click
    document.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      if (target.closest("#prompt-input") && this._slashActive) return;
      if (!target.closest("#btn-mention") && !target.closest("#mention-dropdown")) {
        hideAnimated(mentionDropdown);
        btnMention?.classList.remove("active");
        this._slashActive = false;
        mentionDropdown?.closest(".input-wrapper")?.classList.remove("slash-menu-open");
      }
    });

    // Dropdown item actions
    mentionDropdown?.addEventListener("click", async (e) => {
      const item = (e.target as HTMLElement).closest(".mention-dropdown-item") as HTMLElement | null;
      if (!item) return;
      const action = item.getAttribute("data-action");

      // User-defined resource pick (sub-agent / skill / rule / library
      // document / plugin): resolved from state and run through the shared
      // chip pipeline.  Handled before data-action so a name collision with
      // a slash command id can never hijack the pick.
      const rKind = item.getAttribute("data-resource-kind");
      const rName = item.getAttribute("data-resource-name");
      if (rKind && rName) {
        this.activateResourceChip(rKind, rName);
        return;
      }

      // Sub-navigation: Slash Commands (enter sub-page)
      if (action === "sub-slash") {
        const mainPage = mentionDropdown.querySelector('[data-page="main"]');
        const slashPage = mentionDropdown.querySelector('[data-page="slash"]');
        mainPage?.classList.add("hidden");
        slashPage?.classList.remove("hidden");
        return;
      }

      // Sub-navigation: resource sub-page (agents / skills / connectors /
      // tools / rules / library).  Same one-page-at-a-time pattern the model
      // and reasoning pickers use; the target page is filled in on entry so
      // its list always reflects the latest state.
      if (action === "sub-res") {
        const kind = item.getAttribute("data-res-kind") || "";
        const page = mentionDropdown.querySelector(`[data-page="res-${kind}"]`) as HTMLElement | null;
        if (page) {
          mentionDropdown.querySelectorAll(".mention-page").forEach((p) => {
            if (p.getAttribute("data-page") !== `res-${kind}`) p.classList.add("hidden");
          });
          this.renderResPage(page, kind);
          page.classList.remove("hidden");
        }
        return;
      }

      // Sub-navigation: back to main (from the slash page or any res page)
      if (action === "back-main") {
        const mainPage = mentionDropdown.querySelector('[data-page="main"]');
        mentionDropdown.querySelectorAll(".mention-page").forEach((p) => {
          if (p !== mainPage) p.classList.add("hidden");
        });
        mainPage?.classList.remove("hidden");
        return;
      }

      hideAnimated(mentionDropdown);
      btnMention?.classList.remove("active");

      // Only a re-pick of the *same* mode is a no-op.  Everything else
      // passes: a different mode replaces the chip, and command/action picks
      // coexist with the mode chip (independent channels).
      const existingMode = this.getCurrentMode();
      if (existingMode) {
        const picked = SLASH_COMMANDS.find(c => c.id === action || c.name === action);
        if (picked && picked.kind === "mode" && picked.id === existingMode) return;
      }

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
  }

  private bindSummaryPanel(): void {
    const panel = document.getElementById("summary-panel");
    if (!panel) return;

    // Document-level DELEGATED handling instead of per-element listeners: the
    // toggle button and the panel are looked up on every click, so neither can
    // go stale between bind time and user input. A captured node that gets
    // re-created in that window silently swallows clicks -- the toggle looks
    // painted but the first click does nothing and a second one is required.
    document.addEventListener("click", (e) => {
      const btn = document.getElementById("btn-summary-panel");
      const target = e.target as Node | null;
      if (!btn || !target) return;

      // "View all" link → open review tab in sidebar
      if ((target as HTMLElement).closest?.("#summary-view-all")) {
        e.preventDefault();
        this.closeSummaryPanel();
        const st = getState();
        const first = st.artifacts?.[0];
        if (st.workspaceMode === "iwork") {
          this.sessionInner.showReviewTab("", undefined);
        } else {
          this.sessionInner.showReviewTab(first?.path || "", first || undefined);
        }
        return;
      }

      // Context header (usage + chevron) → open the detail subpage. The
      // back arrow inside the detail view restores the stacked sections.
      if ((target as HTMLElement).closest?.("#summary-context-header")) {
        if (!this._summaryCtxDetail) {
          this._summaryCtxDetail = true;
          this.renderSummaryPanel();
        }
        return;
      }

      if (btn.contains(target)) {
        e.stopPropagation();
        if (this.isSummaryPanelOpen()) this.closeSummaryPanel();
        else this.showSummaryPanel();
        return;
      }

      // Close panel on outside click
      if (this.isSummaryPanelOpen() && !panel.contains(target)) {
        this.closeSummaryPanel();
      }
    });

    document.addEventListener("keydown", (e) => {
      if ((e as KeyboardEvent).key !== "Escape") return;
      if (this.isSummaryPanelOpen()) this.closeSummaryPanel();
    });

    // Re-render when state changes
    onLocaleChange(() => {
      if (this.isSummaryPanelOpen()) this.renderSummaryPanel();
    });

    subscribe(() => {
      const st = getState();
      // Session switched: the open panel belongs to the previous session's
      // context — close it instead of showing stale content. Reopening it
      // re-renders from the active session's snapshot.
      if (st.sessionId !== this._summarySessionId) {
        this._summarySessionId = st.sessionId;
        this._summaryCtxDetail = false;
        this.closeSummaryPanel();
        return;
      }
      if (this.isSummaryPanelOpen()) this.renderSummaryPanel();
    }, ["sessionId", "messages", "contextTokens"]);

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

  /** Single source of truth for "is the panel showing" — the `.hidden` class
   *  on the live node. Every call site reads it through here so no path can
   *  disagree with another about the panel's visibility. */
  private isSummaryPanelOpen(): boolean {
    const panel = this.summaryPanel;
    return !!panel && !panel.classList.contains("hidden");
  }

  /** Reveal the panel and (re)render its content. */
  private showSummaryPanel(): void {
    const panel = this.summaryPanel;
    if (!panel) return;
    panel.classList.remove("hidden");
    const btn = document.getElementById("btn-summary-panel");
    if (btn) btn.classList.add("active");
    this.renderSummaryPanel();
  }

  /** Force-open the summary panel (used when the model first creates todos). */
  openSummaryPanel(): void {
    if (this.isSummaryPanelOpen()) return;
    this.showSummaryPanel();
  }

  /** Hide the summary panel. Called when the active session changes or the
   *  settings overlay opens — the panel must never outlive its session's
   *  context and keep showing stale content. */
  closeSummaryPanel(): void {
    this.summaryPanel.classList.add("hidden");
    const btn = document.getElementById("btn-summary-panel");
    if (btn) btn.classList.remove("active");
  }

  private renderSummaryPanel(): void {
    const st = getState();
    // Context detail mode: the context section takes over the whole card and
    // the stacked sections hide; the back arrow inside it restores them.
    this.summaryPanel.classList.toggle("ctx-detail-mode", this._summaryCtxDetail);
    this.renderSummaryContext(st);
    this.renderSummaryProgress(st);
    this.renderSummaryArtifacts(st);
    this.renderSummaryReferences(st);
    if (typeof (window as any).lucide !== "undefined") {
      // Scoped to the panel on purpose. An unscoped createIcons() re-creates
      // every [data-lucide] node in the document, including the toggle
      // button's own icon -- and lucide does that by replacing the node. This
      // render runs on every state change while the panel is open, so a
      // re-render landing between a mousedown and its mouseup swapped the icon
      // node out from under the pointer: the browser then dispatched `click`
      // on the icons' common ancestor instead of the button, so the button's
      // listener never ran and the first click did nothing. A second click
      // was needed because by then no re-render intervened. Never re-create
      // nodes outside the panel that owns them.
      (window as any).lucide.createIcons({ root: this.summaryPanel });
    }
  }

  /* ── Summary panel: context usage (section header + detail subpage) ── */

  private spFmtTokens(n: number): string {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "K";
    return String(n);
  }

  /** The four display segments of the context composition, in order.
   *  Numbers come exclusively from the backend's per-request breakdown —
   *  the char-based gauge estimate is never shown as category data. */
  private spCtxSegments(st: ReturnType<typeof getState>): Array<{ tokens: number; cls: string }> {
    const bd = st.contextUsageBreakdown;
    const tools = bd?.tools_tokens;
    return [
      { tokens: Math.max(0, bd?.system_prompt_tokens ?? 0), cls: "si-ctx-seg-system" },
      { tokens: Math.max(0, bd?.messages_tokens ?? 0), cls: "si-ctx-seg-messages" },
      { tokens: Math.max(0, tools?.total ?? 0), cls: "si-ctx-seg-tools" },
      { tokens: Math.max(0, bd?.skills_tokens ?? 0), cls: "si-ctx-seg-skills" },
    ];
  }

  private spCtxBarHtml(st: ReturnType<typeof getState>): string {
    const segs = this.spCtxSegments(st);
    const base = st.contextWindow > 0
      ? st.contextWindow
      : (segs.reduce((s, x) => s + x.tokens, 0) || 1);
    return segs.map((seg) => {
      const w = Math.max(0, Math.min(100, Math.round((seg.tokens / base) * 100)));
      return w > 0 ? `<span class="si-ctx-seg ${seg.cls}" style="width:${w}%"></span>` : "";
    }).join("");
  }

  /** Context section of the summary panel. The section header follows the
   *  exact progress/artifacts/references pattern (static in index.html,
   *  i18n-driven); the usage figure sits at its right end. The items area
   *  holds either the composition bar or the full detail subpage. */
  private renderSummaryContext(st: ReturnType<typeof getState>): void {
    const el = document.getElementById("summary-context-items");
    const usageEl = document.getElementById("summary-context-usage");
    if (!el) return;
    const bd = st.contextUsageBreakdown;
    const used = bd ? Math.max(0, bd.total_tokens) : Math.max(0, st.contextTokens || 0);
    const win = bd?.context_window && bd.context_window > 0 ? bd.context_window : (st.contextWindow || 0);

    if (usageEl) {
      if (used > 0) {
        usageEl.classList.remove("is-empty");
        usageEl.innerHTML = `${this.esc(this.spFmtTokens(used))}${win > 0 ? " / " + this.esc(this.spFmtTokens(win)) : ""}<i data-lucide="chevron-right" class="lucide lucide-xs"></i>`;
      } else {
        usageEl.classList.add("is-empty");
        usageEl.innerHTML = `<i data-lucide="chevron-right" class="lucide lucide-xs"></i>`;
      }
    }

    if (this._summaryCtxDetail) {
      el.innerHTML = this.renderSummaryContextDetail(st, used, win);
      const back = el.querySelector("#sp-ctx-back") as HTMLElement | null;
      if (back) {
        back.addEventListener("click", (e) => {
          e.stopPropagation();
          this._summaryCtxDetail = false;
          this.renderSummaryPanel();
        });
      }
      return;
    }

    // Collapsed section view: the header line alone (title left, usage +
    // chevron right). The composition bar belongs to the detail page only.
    el.innerHTML = "";
  }

  /** Full context-usage composition page shown inside the summary card. */
  private renderSummaryContextDetail(st: ReturnType<typeof getState>, used: number, win: number): string {
    const header = `<div class="si-ctx-detail-header">
        <button class="btn-icon" id="sp-ctx-back" title="${t("sidebar.back")}"><i data-lucide="arrow-left" class="lucide"></i></button>
        <span class="si-ctx-detail-title">${t("summary.context")}</span>
      </div>`;

    const bd = st.contextUsageBreakdown;
    if (!bd) {
      // Without a composition there is nothing honest to draw: no bar, no
      // placeholder rows, no "starts after the first turn" copy — the
      // header alone.
      return header;
    }
    // Stats line between the header and the composition bar: total
    // percentage on the left, exact usage on the right — never on the
    // title row itself.
    const pctTotal = win > 0 ? Math.round((Math.max(0, used) / win) * 100) : null;
    const stats = (pctTotal !== null && used > 0)
      ? `<div class="si-ctx-stats">
          <span class="si-ctx-stats-pct">${pctTotal}%</span>
          <span class="si-ctx-stats-usage">${this.esc(`${this.spFmtTokens(used)}${win > 0 ? " / " + this.spFmtTokens(win) : ""}`)}</span>
        </div>`
      : "";
    const barWrap = `<div class="si-ctx-bar-wrap si-ctx-bar-lg"><div class="si-ctx-bar">${this.spCtxBarHtml(st)}</div></div>`;

    const denom = win > 0 ? win : Math.max(1, bd.total_tokens);
    const pct = (n: number) => `${Math.round((Math.max(0, n) / denom) * 100)}%`;
    // Detail lines reuse the summary-panel row recipe verbatim (the
    // sp-artifact-item flex row): colour chip, plain label left, small
    // right-aligned percentage. No token counts, no per-row bars.
    const row = (label: string, tokens: number, cls: string, sub = false): string => `
      <div class="sp-artifact-item${sub ? " si-ctx-line-sub" : ""}">
        <span class="si-ctx-dot ${cls}"></span>
        <span class="sp-artifact-name">${label}</span>
        <span class="sp-artifact-stats">
          <span class="si-ctx-pct">${pct(tokens)}</span>
        </span>
      </div>`;

    const tools = bd.tools_tokens || { total: 0, builtin: 0, mcp: 0, plugin: 0, subagents: 0 };
    const body = [
      row(t("sessionInner.ctxSystemPrompt"), bd.system_prompt_tokens, "si-ctx-seg-system"),
      row(t("sessionInner.ctxMessages"), bd.messages_tokens, "si-ctx-seg-messages"),
      row(t("sessionInner.ctxTools"), tools.total, "si-ctx-seg-tools"),
      row(t("sessionInner.ctxToolsBuiltin"), tools.builtin, "si-ctx-seg-tools", true),
      row(t("sessionInner.ctxToolsSubagents"), tools.subagents, "si-ctx-seg-tools", true),
      row(t("sessionInner.ctxMcp"), tools.mcp, "si-ctx-seg-tools", true),
      row(t("sessionInner.ctxPlugins"), tools.plugin, "si-ctx-seg-tools", true),
      row(t("sessionInner.ctxSkills"), bd.skills_tokens, "si-ctx-seg-skills"),
    ].join("");
    return `${header}${stats}${barWrap}<div class="si-ctx-rows">${body}</div>`;
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
        this.closeSummaryPanel();
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

    // Low-value reference tools that should not clutter the panel:
    //  - memory_search: the summary is just a search query string; the
    //    actual results live in the chat transcript, not behind this row.
    //  - memory_delete: the target file no longer exists, so the row
    //    cannot be opened and only misleads the user.
    const LOW_VALUE_REF_TOOLS = new Set(["memory_search", "memory_delete"]);

    // De-duplicate across the ENTIRE session history (not just the last
    // 12) so that removing noise above doesn't let stale duplicates
    // slip back into the visible window.  Memory-file references
    // collapse onto their normalized file name so create/read/update on
    // the same memory all render as a single row; web-fetch URLs
    // normalize away protocol/www/trailing-slash differences; web-search
    // and memory-search queries collapse on whitespace-normalized text;
    // MCP calls collapse onto the tool name.
    const seen = new Set<string>();
    const unique: any[] = [];
    for (const r of refs) {
      const toolLc = (r.tool || "").toLowerCase();
      if (LOW_VALUE_REF_TOOLS.has(toolLc)) continue;
      const key = refDedupeKey(r);
      if (seen.has(key)) continue;
      seen.add(key);
      unique.push(r);
    }
    const shown = unique.slice(-12).reverse();

    const groups: { label: string; icon: string; refs: any[] }[] = [
      { label: t("sessionInner.referencesWeb") || "Web", icon: "globe", refs: [] },
      { label: t("sessionInner.referencesMemory") || "Memory", icon: "brain", refs: [] },
      { label: t("sessionInner.referencesMcp") || "MCP", icon: "cable", refs: [] },
    ];
    for (const r of shown) {
      const tl = (r.tool || "").toLowerCase();
      if (tl.startsWith("mcp__")) {
        groups[2].refs.push(r);
      } else if (tl.includes("memory") || tl.includes("profile")) {
        groups[1].refs.push(r);
      } else {
        groups[0].refs.push(r);
      }
    }

    const renderItem = (r: any) => {
      const text = stripRefTypePrefix(r.tool, r.summary);
      const display = truncateRefText(text);
      // No per-row icon: the leading space is reserved via CSS padding so the
      // row text aligns with the group header's label above it.
      if (refDedupeKey(r).startsWith("memory:")) {
        const rawName = text.trim();
        const fileName = rawName.toLowerCase().endsWith(".md") ? rawName : `${rawName}.md`;
        return `<div class="sp-ref-item sp-ref-item-action" data-memory="${this.esc(fileName)}" title="${this.esc(r.summary)}">
          <span class="sp-ref-item-text sp-ref-link">${this.esc(display)}</span>
        </div>`;
      }
      // Try URL extraction for ALL non-memory references, not just
      // web_* tools — an MCP result or unknown tool may legitimately
      // carry a URL in its summary, and it should be clickable.
      const url = extractRefUrl(r.summary);
      if (url) {
        return `<div class="sp-ref-item">
          <span class="sp-ref-item-text"><a class="sp-ref-link" href="${this.esc(url)}" title="${this.esc(r.summary)}">${this.esc(display)}</a></span>
        </div>`;
      }
      return `<div class="sp-ref-item">
        <span class="sp-ref-item-text" title="${this.esc(r.summary)}">${this.esc(display)}</span>
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

    // Interactive reference rows: URL links open in the external browser,
    // memory rows jump to the settings memory panel with the file's detail
    // dialog open.
    el.querySelectorAll<HTMLAnchorElement>("a.sp-ref-link[href]").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const href = a.getAttribute("href") || "";
        if (href) openRoutedLink(href);
      });
    });
    el.querySelectorAll<HTMLElement>(".sp-ref-item-action[data-memory]").forEach((row) => {
      row.addEventListener("click", (e) => {
        e.stopPropagation();
        this.closeSummaryPanel();
        const mem = row.dataset.memory || "";
        if (mem) this.settings.openMemoryDetail(mem);
      });
    });

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

  /**
   * Hover text for a model entry (switcher dropdown / collapsed label):
   * names the OWNING provider so two providers that legally expose the same
   * model id / name stay distinguishable.  Returns "" when the flat index
   * cannot be mapped (provider snapshot out of sync) — callers then simply
   * omit the tooltip.
   */
  /** Display name of the provider owning the flat-indexed model.  Single
   *  source of truth for the picker's group headers and the collapsed
   *  selector's tooltip (openai_compatible providers carry a user-defined
   *  name; known backend types use the catalog label). */
  private modelProviderDisplayName(flatIdx: number): string {
    const loc = locateModelInProviders(flatIdx);
    if (!loc) return "";
    const provider = (getState().providers || [])[loc.providerIdx];
    if (!provider) return "";
    const catalog = getState().modelCatalog;
    const catalogLabel = (id: string): string =>
      catalog?.providers?.find((c) => c.id === id)?.label || id;
    return provider.backend_type === "openai_compatible"
      ? (provider.name?.trim() || catalogLabel(provider.backend_type))
      : catalogLabel(provider.backend_type);
  }

  private modelProviderTooltipText(flatIdx: number): string {
    const name = this.modelProviderDisplayName(flatIdx);
    if (!name) return "";
    return `${t("common.modelProviderTooltip")}: ${name}`;
  }

  private bindInputModelSelector(): void {
    const selector = document.getElementById("input-model-selector");
    const dropdown = document.getElementById("input-model-dropdown");
    if (!selector || !dropdown) return;

    const formatLevel = (lvl: string): string => {
      if (!lvl || lvl === "default") return t("settings.thinkingDefault") || "Default";
      return lvl.charAt(0).toUpperCase() + lvl.slice(1);
    };

    // Reasoning levels are a live probe result stored per model (verified
    // against the endpoint at validation time) -- never a hardcoded set.
    // "default" (thinking on, provider-default effort) is always offered
    // first; verified levels follow.  An empty list means no level was
    // accepted (thinking is on/off only for that model).
    const getThinkingLevels = (): string[] => {
      const st = getState();
      const active = st.modelConfigs[st.activeModelIndex];
      return ["default", ...(active?.reasoning_levels || [])];
    };

    const isThinkingSelectable = (m: import("./core/types.js").ModelConfigMeta | undefined): boolean => {
      if (!m) return false;
      // The endpoint itself reported reasoning as unsupported.
      if (m.capabilities?.reasoning === "unsupported") return false;
      const tc = m.thinking_config;
      return tc ? tc.selectable === true : false;
    };

    const getCurrentLevel = (m: import("./core/types.js").ModelConfigMeta | undefined): string => {
      if (!m) return "default";
      const tc = m.thinking_config;
      const lvl = tc?.level || "";
      return lvl || "default";
    };

    // Permission-mode catalogue + guarded switch live in the shared
    // core/permission_mode module — every surface (model switcher, automation
    // dialog, ...) renders its own UI but applies switches through one gate.
    const workspaceLabelSuffix = (): string => {
      const s = getState();
      if (s.workspaceMode !== "iwork") return "";
      const ws = (s.workspaces || []).find((w) => w.path === s.activeWorkspace);
      return " · " + (ws ? ws.name : t("workspace.selectWorkspace"));
    };

    const renderMainPage = (mainPage: HTMLElement) => {
      const st = getState();
      const allModels = st.modelConfigs || [];
      let activeIdx = st.activeModelIndex;
      const active = allModels[activeIdx];
      const enabledModels = allModels.filter((m) => m.enabled !== false);
      // Apply workspace model pool restriction when in iwork mode:
      // only show models that the active workspace has explicitly allowed.
      // The pool is "on" when explicitly enabled OR when a legacy config
      // omitted models_enabled but still carries allowed models.  Critically,
      // an ENABLED pool with an empty selection yields NO usable models
      // (models=[]), not a fallback to the full list.
      let models = enabledModels;
      let poolActive = false;
      if (st.workspaceMode === "iwork" && st.activeWorkspace) {
        const wsConfig = st.workspaceConfigs[st.activeWorkspace];
        const wsModels = Array.isArray(wsConfig?.models) ? wsConfig.models : [];
        poolActive = wsConfig?.models_enabled === true
          || (wsConfig?.models_enabled !== false && wsModels.length > 0);
        if (poolActive) {
          const allowedSet = new Set(wsModels);
          models = enabledModels.filter((m) => allowedSet.has(m.model_id));
        }
      }
      let isActiveUsable = active && active.enabled !== false;
      // When a workspace model pool is active, the active model must itself
      // be inside the pool to count as usable.  An empty pool (enabled with
      // no selection) thus makes nothing usable and the label falls back to
      // "Auto" instead of showing a model that isn't actually selectable.
      if (poolActive) {
        isActiveUsable = isActiveUsable && models.some((m) => m.model_id === active?.model_id);
      }

      // Auto-fallback: if the active model is disabled or not in the model pool,
      // pick the first available model and update the backend.
      //
      // Never do this while `activeModelIndex` still belongs to the previous
      // session (`modelIndexSessionId` lags): a conversation that is being
      // opened has not told us its model yet, and guessing here pushed the
      // fallback model onto it -- the "switching conversations changes the
      // model by itself" bug.
      const modelIndexSynced = !!st.sessionId && st.modelIndexSessionId === st.sessionId;
      if (modelIndexSynced && (!isActiveUsable || (models.length > 0 && !models.some(m => m.model_id === active?.model_id)))) {
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
      const label = (isActiveUsable ? (allModels[activeIdx]?.name || allModels[activeIdx]?.model_id || "Auto") : "Auto") + workspaceLabelSuffix();
      const labelEl = selector.querySelector(".input-selector-label") as HTMLElement | null;
      if (labelEl) {
        labelEl.textContent = label;
        // Hover tooltip on the collapsed selector as well: tells the user at a
        // glance WHICH provider the active model belongs to (two different
        // providers may legally expose the same model id / name).
        if (isActiveUsable) {
          const tip = this.modelProviderTooltipText(activeIdx);
          if (tip) labelEl.dataset.tooltip = tip;
          else delete labelEl.dataset.tooltip;
        } else {
          delete labelEl.dataset.tooltip;
        }
      }

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
          <i data-lucide="${permissionModeIcon(currentPermGroup)}" class="lucide"></i>
          <span>${t("input.permissionMode")}</span>
          <span style="margin-inline-start:auto;color:var(--text-muted);font-size:12px">${this.esc(permissionModeLabel(currentPermGroup))}</span>
          <i data-lucide="chevron-right" class="lucide"></i>
        </button>`;

      // Workspace entry -- only in workspace (iwork) mode, below permission
      // mode and above add-model.  Switching is a pre-conversation decision:
      // once a message has been sent the session belongs to its workspace,
      // so the entry renders muted (greyed out, unclickable) instead of
      // allowing navigation to the sub-page.
      let workspaceEntry = "";
      if (st.workspaceMode === "iwork") {
        const wsList = st.workspaces || [];
        const activeWs = wsList.find((w) => w.path === st.activeWorkspace);
        const wsLabel = activeWs ? activeWs.name : t("workspace.selectWorkspace");
        const wsLocked = (st.messages?.length ?? 0) > 0;
        workspaceEntry = wsLocked
          ? `<button class="mention-dropdown-item" data-action="sub-workspace" disabled title="${t("input.workspaceLocked")}">
              <span>${t("input.workspace")}</span>
              <span style="margin-inline-start:auto;color:var(--text-muted);font-size:12px">${this.esc(wsLabel)}</span>
              <i data-lucide="chevron-right" class="lucide" style="color:var(--text-muted)"></i>
            </button>`
          : `<button class="mention-dropdown-item" data-action="sub-workspace">
              <span>${t("input.workspace")}</span>
              <span style="margin-inline-start:auto;color:var(--text-muted);font-size:12px">${this.esc(wsLabel)}</span>
              <i data-lucide="chevron-right" class="lucide"></i>
            </button>`;
      }

      // Model list — only show CONFIGURED models.  The catalog ("available
      // models") is a settings-side browsing aid for adding new models; it is
      // NOT a pickable model here, so when nothing is configured the selector
      // shows a muted hint plus the add-model action instead of a wall of
      // unconfigured sample models.
      let modelsHtml = "";
      if (models.length === 0) {
        const msg = poolActive ? t("app.noModelsInPool") : t("app.noModelsConfigured");
        modelsHtml = `<button class="mention-dropdown-item muted" disabled>${msg}</button>`;
      } else {
        // Provider-grouped picker (OpenCode-style, no icons): a small muted
        // provider header, the models it owns below, selected model keeping
        // its highlight.  Both levels sort A→Z (provider names, then model
        // names within each group) via a locale-aware, case-insensitive,
        // numeric-aware collator ("GLM-4" < "GLM-10").  The per-row provider
        // tooltip is gone — the header shows it.
        const groups = new Map<string, Array<{ label: string; origIdx: number }>>();
        for (const m of models) {
          const origIdx = allModels.indexOf(m);
          const pname = this.modelProviderDisplayName(origIdx) || t("app.model");
          let list = groups.get(pname);
          if (!list) {
            list = [];
            groups.set(pname, list);
          }
          list.push({ label: m.name || m.model_id || "", origIdx });
        }
        const coll = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
        modelsHtml = [...groups.entries()]
          .sort((a, b) => coll.compare(a[0], b[0]))
          .map(([pname, items]) => {
            items.sort((a, b) => coll.compare(a.label, b.label));
            return `<div class="model-picker-group-label">${this.esc(pname)}</div>`
              + items.map(({ label, origIdx }) => {
                const sel = origIdx === activeIdx ? " active" : "";
                return `<button class="mention-dropdown-item${sel}" data-idx="${origIdx}">${this.esc(label)}</button>`;
              }).join("");
          })
          .join("");
      }

      const divider = models.length > 0 ? `<div class="mention-dropdown-divider"></div>` : "";

      // Two regions, one scroll: the model list scrolls, while reasoning
      // level / permission mode / add-model live in a footer pinned under it.
      // Previously everything shared one flat list, so a long model set pushed
      // the three actions out of reach (and past the bottom of the screen).
      mainPage.innerHTML = `
        <div class="model-picker-list">${modelsHtml}</div>
        <div class="model-picker-actions">${divider}${thinkingEntry}${permissionEntry}${workspaceEntry}
          <button class="mention-dropdown-item add-model-btn" data-action="add-model">
            <span>${t("settings.addModel")}</span>
            <i data-lucide="plus" class="lucide" style="margin-inline-start:auto"></i>
          </button>
        </div>`;

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
          if (lbl) lbl.textContent = (allModels[idx]?.name || allModels[idx]?.model_id || t("app.model")) + workspaceLabelSuffix();
          hideAnimated(dropdown);
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

      // Workspace entry -> navigate to workspace page.  The locked variant
      // carries the disabled attribute, so it never reaches this handler.
      const workspaceBtn = mainPage.querySelector('[data-action="sub-workspace"]:not([disabled])') as HTMLElement | null;
      if (workspaceBtn) {
        workspaceBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          mainPage.classList.add("hidden");
          const wsPage = dropdown.querySelector('[data-page="workspace"]') as HTMLElement;
          if (wsPage) {
            renderWorkspacePage(wsPage);
            wsPage.classList.remove("hidden");
          }
        });
      }

      // Add model
      const addBtn = mainPage.querySelector('[data-action="add-model"]') as HTMLElement | null;
      if (addBtn) {
        addBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          hideAnimated(dropdown);
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
          const lvl = (lvlItem as HTMLElement).dataset.level === "default" ? "" : ((lvlItem as HTMLElement).dataset.level || "");
          const currentState = getState();
          const idx = currentState.activeModelIndex;
          const loc = locateModelInProviders(idx);
          const providers = [...(currentState.providers || [])];
          if (!loc || loc.providerIdx >= providers.length) return;
          const prov = { ...providers[loc.providerIdx] };
          const provModels = [...(prov.models || [])];
          if (loc.modelIdx >= provModels.length) return;
          provModels[loc.modelIdx] = {
            ...provModels[loc.modelIdx],
            thinking_config: { type: "enabled", enabled: true, level: lvl, selectable: true },
          };
          prov.models = provModels;
          providers[loc.providerIdx] = prov;
          setProviders(providers);
          send({ type: "update_providers", providers, active_model_index: currentState.activeModelIndex });
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

      // Mode selection: the switch goes through the shared guarded path —
      // "bypass" needs an explicit danger confirmation, rejection keeps the
      // current mode.  Persists via update_agent; agent_updated refreshes
      // the store, but re-render immediately for snappy feedback.
      permissionPage.querySelectorAll(".mention-dropdown-item[data-perm-mode]").forEach((item) => {
        item.addEventListener("click", (ev) => {
          ev.stopPropagation();
          void (async () => {
            const mode = await guardPermissionMode(
              (item as HTMLElement).dataset.permMode || "default",
              dialogPermissionConfirm,
            );
            if (mode === null) { renderPermissionPage(permissionPage); return; }
            const cfg = (getState() as any).agentConfig;
            if (cfg) setAgentConfig({ ...cfg, permission_mode: mode });
            send({ type: "update_agent", permission_mode: mode });
            renderPermissionPage(permissionPage);
          })();
        });
      });
    };

    const renderWorkspacePage = (wsPage: HTMLElement) => {
      const st = getState();
      const wsList = st.workspaces || [];
      const activePath = st.activeWorkspace;

      const backBtn = `<button class="mention-dropdown-item" data-action="back-model-main">
        <i data-lucide="arrow-left" class="lucide"></i>
        <span>${t("workspace.selectWorkspace")}</span>
      </button>`;

      let items: string;
      if (wsList.length === 0) {
        items = `<button class="mention-dropdown-item muted" disabled>${t("workspace.noWorkspaces")}</button>`;
      } else {
        items = wsList
          .map((w) => {
            const sel = w.path === activePath ? " active" : "";
            return `<button class="mention-dropdown-item${sel}" data-ws-path="${this.esc(w.path)}">
              <span>${this.esc(w.name)}</span>
            </button>`;
          })
          .join("");
      }

      wsPage.innerHTML = backBtn + `<div class="mention-dropdown-divider"></div>` + items;

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: wsPage });
      }

      // Back button
      const backEl = wsPage.querySelector('[data-action="back-model-main"]') as HTMLElement | null;
      if (backEl) {
        backEl.addEventListener("click", (e) => {
          e.stopPropagation();
          wsPage.classList.add("hidden");
          const mainPage = dropdown.querySelector('[data-page="main"]') as HTMLElement;
          if (mainPage) {
            renderMainPage(mainPage);
            mainPage.classList.remove("hidden");
          }
        });
      }

      // Workspace selection: same behavior the standalone selector had --
      // open the workspace and close the dropdown.  Picking the already
      // active workspace just closes.
      wsPage.querySelectorAll(".mention-dropdown-item[data-ws-path]").forEach((item) => {
        item.addEventListener("click", (e) => {
          e.stopPropagation();
          const path = (item as HTMLElement).dataset.wsPath || "";
          hideAnimated(dropdown);
          if (!path || path === getState().activeWorkspace) return;
          void this.workspace.open(path);
        });
      });
    };

    /** Keep the collapsed trigger in sync without rebuilding the menu. */
    const updateCollapsedLabel = () => {
      const s = getState();
      const list = s.modelConfigs || [];
      const idx = Math.min(Math.max(s.activeModelIndex || 0, 0), Math.max(list.length - 1, 0));
      const active = list[idx];
      const usable = !!active;
      const label = (usable ? (active.name || active.model_id || "Auto") : "Auto") + workspaceLabelSuffix();
      const labelEl = selector.querySelector(".input-selector-label") as HTMLElement | null;
      if (!labelEl) return;
      labelEl.textContent = label;
      if (usable) {
        const tip = this.modelProviderTooltipText(idx);
        if (tip) labelEl.dataset.tooltip = tip;
        else delete labelEl.dataset.tooltip;
      } else {
        delete labelEl.dataset.tooltip;
      }
    };

    const render = (force = false) => {
      // While the menu is closed only the collapsed trigger is visible.
      // Rebuilding the whole model page on every config/model push was a
      // measurable boot long task; the open handler calls render(true).
      if (!force && dropdown.classList.contains("hidden")) {
        updateCollapsedLabel();
        return;
      }
      // Ensure page structure exists
      let mainPage = dropdown.querySelector('[data-page="main"]') as HTMLElement | null;
      let thinkingPage = dropdown.querySelector('[data-page="thinking"]') as HTMLElement | null;
      let permissionPage = dropdown.querySelector('[data-page="permission"]') as HTMLElement | null;
      let wsPage = dropdown.querySelector('[data-page="workspace"]') as HTMLElement | null;

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
      if (!wsPage) {
        wsPage = document.createElement("div");
        wsPage.className = "mention-page hidden";
        wsPage.setAttribute("data-page", "workspace");
        dropdown.appendChild(wsPage);
      }

      // Preserve whichever sub-page is currently open.  Store updates
      // (streaming stats, model list refresh, agent_updated, ...) arrive
      // asynchronously and must never snap the dropdown back to the main
      // page while the user is picking a reasoning level or a permission mode.
      if (!thinkingPage.classList.contains("hidden")) {
        renderThinkingPage(thinkingPage);
        mainPage.classList.add("hidden");
        permissionPage.classList.add("hidden");
        wsPage.classList.add("hidden");
        return;
      }
      if (!permissionPage.classList.contains("hidden")) {
        renderPermissionPage(permissionPage);
        mainPage.classList.add("hidden");
        thinkingPage.classList.add("hidden");
        wsPage.classList.add("hidden");
        return;
      }
      if (!wsPage.classList.contains("hidden")) {
        renderWorkspacePage(wsPage);
        mainPage.classList.add("hidden");
        thinkingPage.classList.add("hidden");
        permissionPage.classList.add("hidden");
        return;
      }

      // No sub-page open: show the main page.
      thinkingPage.classList.add("hidden");
      permissionPage.classList.add("hidden");
      wsPage.classList.add("hidden");
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
      if (dropdown.classList.contains("hidden")) {
        // Reopening always starts from the main page, even if a sub-page was
        // left open the last time the dropdown was visible.
        dropdown
          .querySelectorAll('.mention-page[data-page="thinking"], .mention-page[data-page="permission"], .mention-page[data-page="workspace"]')
          .forEach((page) => page.classList.add("hidden"));
        runScheduledRenderNow("model-menu", () => render(true), 40);
        showAnimated(dropdown);
        this.ownEphemeral(dropdown);
        // CSS anchors it with `inset-inline-end: 0`, so under rtl it grows
        // towards the window edge — pull it back when it would overflow.
        clampIntoViewport(dropdown);
      } else {
        hideAnimated(dropdown);
      }
    });

    document.addEventListener("click", (e) => {
      if (!selector.contains(e.target as Node) && !dropdown.contains(e.target as Node)) {
        hideAnimated(dropdown);
      }
    });

    onLocaleChange(() => {
      if (!this.summaryPanel.classList.contains("hidden")) this.renderSummaryPanel();
    });

    // Deliberately NOT subscribed to "messages": streaming text changes that
    // field many times per second, and rebuilding the model list on each one
    // competed with the chat render for the same main thread.
    subscribe(() => {
      try {
        // Closed menu: only the collapsed label is visible. Open menu: the
        // rebuild goes through the same scheduler as every other surface.
        if (dropdown.classList.contains("hidden")) {
          updateCollapsedLabel();
          return;
        }
        scheduleRender("model-menu", () => render(), 40);
      } catch (e) {
        reportUiFault("model-menu", "subscribe", e);
      }
    }, ["modelConfigs", "activeModelIndex", "workspaceMode", "activeWorkspace", "workspaceConfigs", "workspaces"]);

    render();
  }

  /**
   * Workspace selection lives inside the model selector as a sub-page
   * (renderWorkspacePage) -- the standalone workspace indicator was merged
   * into it and is gone.
   */
  private activateSlashCommand(cmd: SlashCommand, restText?: string): void {
    this.skipNextInput = true;
    const slashWasActive = this._slashActive;
    const dd = document.getElementById("mention-dropdown");
    const bm = document.getElementById("btn-mention");
    hideAnimated(dd);
    bm?.classList.remove("active");
    this._slashActive = false;
    dd?.closest(".input-wrapper")?.classList.remove("slash-menu-open");

    // A new slash activation replaces any active command chip first (one
    // command slot); the mode chip is preserved below — mode and command are
    // independent channels (payload.mode vs backend set_command).
    if (this.hasCommandChip()) this.deactivateCommandChip();

    // The composer may already hold a draft (dropdown activation) — never
    // wipe it. A typed invocation ("/plan rest") replaces only its own slash
    // token; a draft that never resolved to a command is kept verbatim and
    // the chip lands in front of it. Non-editable children (attachment chips)
    // are preserved in every branch because we never touch the DOM wholesale
    // unless the input genuinely was a slash command.  The mode chip survives
    // the wipe so a mode + command combination stays intact.
    const savedModeChip = this.input.querySelector('.mode-chip[data-mode]') as HTMLElement | null;
    const savedSep = savedModeChip ? savedModeChip.nextSibling : null;
    const savedSepIsSpace = !!savedSep && savedSep.nodeType === Node.TEXT_NODE && savedSep.textContent === " ";
    const wipeKeepModeChip = () => {
      savedModeChip?.remove();
      if (savedSepIsSpace && savedSep) savedSep.remove();
      this.input.innerHTML = "";
      if (savedModeChip) {
        this.input.appendChild(savedModeChip);
        this.input.appendChild(document.createTextNode(" "));
      }
    };

    let prepend = false;
    if (restText === undefined) {
      const plain = this.getPlainText();
      const parsed = plain ? parseSlashInput(plain) : null;
      if (parsed) {
        restText = parsed.rest;
        wipeKeepModeChip();
      } else if (slashWasActive && plain.startsWith("/")) {
        restText = plain.replace(/^\/[^\s]*\s*/, "");
        wipeKeepModeChip();
      } else {
        prepend = true;
      }
    } else {
      wipeKeepModeChip();
    }

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

    if (prepend) {
      const chip = this.input.querySelector('.mode-chip[data-mode], .command-chip[data-command]');
      if (chip) moveChipToFront(this.input, chip);
      this.input.focus();
      this.placeCursorAtEnd();
    }
  }

  private executeSlashAction(cmd: SlashCommand, restText?: string): void {
    switch (cmd.id) {
      case "steer":
        // steer is a one-shot mid-run injection, not a sticky command.
        this.insertModeChip(cmd.id, restText);
        this._currentChipMode = "steer";
        this.updateChipState();
        this.input.focus();
        return;
      case "help":
        // Gateway/CLI command -- desktop users get pointed at the UI.
        showToast(t("slashCommands.helpDesktopHint"), "", "info");
        this.input.focus();
        return;
      case "model":
        showToast(t("slashCommands.modelDesktopHint"), "", "info");
        this.input.focus();
        return;
      case "sessions":
      case "resume":
        showToast(t("slashCommands.sessionsDesktopHint"), "", "info");
        this.input.focus();
        return;
      case "approve":
      case "deny":
      case "stop":
      case "compact":
      case "status":
        showToast(t("slashCommands.uiFeatureHint"), "", "info");
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
      // Keep the mode slot when a mode chip is present — the command chip
      // must not overwrite the active mode bookkeeping.
      if (!this.hasModeChip()) this._currentChipMode = cmd.id;
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
    // Keep the mode slot when a mode chip is present — see executeSlashAction.
    if (!this.hasModeChip()) this._currentChipMode = cmd.id;
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
   *  in the serialized text.  Attachment chips serialize to an inline
   *  `<attach n="name" />` placeholder at their exact position, so a chip
   *  added mid-sentence renders mid-sentence in the bubble and comes back
   *  to the same spot on rollback edit.  Other non-editable chips are
   *  skipped. */
  private _collectText(node: Node, out: string[]): void {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        out.push(child.textContent || '');
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const el = child as Element;
        if (el.hasAttribute('data-attach')) {
          out.push(attachPlaceholder(el.getAttribute('data-name') || ''));
          continue;
        }
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
      this.persistDraftsSoon();
    } else {
      // Removals are written through immediately: a debounced write could
      // be skipped by an app close, resurrecting an already-sent draft on
      // the next start.
      this._drafts.delete(sid);
      this.persistDrafts();
    }
  }

  /** Restores drafts and sent-input history persisted by a previous run
   *  (localStorage). Corrupted or missing entries are silently ignored. */
  private loadPersistedInputState(): void {
    try {
      const rawDrafts = localStorage.getItem("encre.drafts.v1");
      if (rawDrafts) {
        const entries = JSON.parse(rawDrafts);
        if (Array.isArray(entries)) {
          for (const entry of entries) {
            if (Array.isArray(entry) && typeof entry[0] === "string" && typeof entry[1] === "string") {
              this._drafts.set(entry[0], entry[1]);
            }
          }
        }
      }
      const rawHistory = localStorage.getItem("encre.inputHistory.v1");
      if (rawHistory) {
        const history = JSON.parse(rawHistory);
        if (Array.isArray(history)) {
          this._inputHistory = history.filter((t) => typeof t === "string").slice(-100);
          this._inputHistoryIdx = this._inputHistory.length;
        }
      }
    } catch { /* corrupted storage must not break startup */ }
  }

  /** Debounced localStorage write for the draft map. */
  private persistDraftsSoon(): void {
    if (this._draftPersistTimer !== null) return;
    this._draftPersistTimer = window.setTimeout(() => {
      this._draftPersistTimer = null;
      this.persistDrafts();
    }, 300);
  }

  /** Immediately writes the draft map to localStorage. */
  private persistDrafts(): void {
    try { localStorage.setItem("encre.drafts.v1", JSON.stringify([...this._drafts])); } catch { /* quota or unavailable */ }
  }

  /** Persists the sent-input history (capped at 100 entries). */
  private persistInputHistory(): void {
    try { localStorage.setItem("encre.inputHistory.v1", JSON.stringify(this._inputHistory.slice(-100))); } catch { /* quota or unavailable */ }
  }

  /** Removes a session's draft from the map and the persisted store
   *  (immediate write: removals must never be lost to a pending debounce). */
  private deleteDraft(sid: string): void {
    this._drafts.delete(sid);
    this.persistDrafts();
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

  /**
   * Put a queued prompt back into the composer for another pass.  The queue
   * entry itself is dropped by the caller (server-side), and whatever was
   * already typed is saved as this session's draft first — editing must never
   * eat text that was there before the click.
   */
  private loadQueuedPromptForEdit(text: string, mode?: string): void {
    this.saveCurrentDraft();
    this.input.innerHTML = "";
    if (mode) {
      this.insertModeChip(mode, text);
      this._currentChipMode = mode;
    } else {
      this.input.textContent = text;
    }
    this.updateChipState();
    this.updatePlaceholder();
    this.updateSendButton();
    this.resizeInput();
    this.updateInputExpandButton();
    this.input.focus();
    this.placeCursorAtEnd();
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
    chip.innerHTML = `${icon ? `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i>` : ""}<span>${label}</span>`;
    // Draft-level close: the inline chip only exists pre-send; ending the
    // persistent mode globally is the toolbar chip ×'s job.
    wireChipClose(chip, () => this.removeInlineModeChip());
    // Appends the chip + separator space and places the caret after it so
    // typing continues off the chip border (see ui/chip.ts).
    appendChipWithCaret(this.input, chip, restText);
    this.refreshIcons(this.input);
  }

  private removeModeChip(): void {
    const chip = this.input.querySelector('.mode-chip[data-mode]');
    if (!chip) return;
    removeChipWithSeparator(chip);
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
    chip.innerHTML = `${icon ? `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i>` : ""}<span>${cmd.title}</span>`;
    wireChipClose(chip, () => this.deactivateCommandChip());
    // Chip + separator space + caret after the separator — see ui/chip.ts.
    appendChipWithCaret(this.input, chip, restText);
    this.refreshIcons(this.input);
  }

  private removeCommandChip(): void {
    const chip = this.input.querySelector('.command-chip[data-command]');
    if (!chip) return;
    removeChipWithSeparator(chip);
  }

  /** Resolve a Lucide icon name to something the bundled icon set actually
   *  has, or to the empty string.  lucide's registry is keyed by PascalCase
   *  (e.g. ``ListChecks``), while commands store kebab-case names — normalize
   *  before checking.  ``command`` (macOS ⌘) is a legacy stored value and is
   *  normalized to the skills glyph.  A command that declares no icon — or one
   *  we cannot resolve — gets no glyph: the app does not pick an identity for
   *  somebody else's command.  Callers skip the icon element when this is empty. */
  private resolveIcon(name: string): string {
    const candidateRaw = (name && name.trim()) || "";
    // ``command`` is the macOS ⌘ glyph — legacy default stored in old
    // configs.  Normalize it to the app-wide skills glyph.
    const candidate = candidateRaw === "command" ? "wand-2" : candidateRaw;
    if ((window as any).lucide && (window as any).lucide.icons) {
      const known = (window as any).lucide.icons as Record<string, unknown>;
      const pascal = candidate
        .split("-")
        .map(p => p.charAt(0).toUpperCase() + p.slice(1))
        .join("");
      if (!(candidate in known) && !(pascal in known)) return "";
    }
    return candidate;
  }

  /**
   * Re-run Lucide inside `root` only.
   *
   * `root` is required on purpose — an unscoped `createIcons()` walks the whole
   * document and replaces every `[data-lucide]` node, which during streaming
   * swallows clicks (the node under the cursor changes between mousedown and
   * mouseup), resets open UI back to its default state and thrashes layout.
   */
  private refreshIcons(root: HTMLElement | null | undefined): void {
    if (!root) return;
    if (!(window as any).lucide) return;
    (window as any).lucide.createIcons({ root });
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
    if (icon) {
      const resolved = this.resolveIcon(def?.icon || cmd.icon || "");
      icon.setAttribute("data-lucide", resolved);
      icon.classList.toggle("hidden", !resolved);
    }
    if (label) label.textContent = def?.title || cmd.title || cmd.name;
    this.refreshIcons(chip);
  }

  private handleModeEnd(): void {
    this.removeModeChip();
    this._currentChipMode = "";
    setInputMode("");
    this.updateChipState();
    this.updatePlaceholder();
  }

  /** Draft-level close for the inline chip ×: removes the chip only.
   *
   *  The persistent mode (toolbar chip + border) is intentionally left
   *  alone — ending the mode globally (clear _persistentMode + notify the
   *  backend) is the toolbar chip ×'s job (deactivateModeChip).  When no
   *  persistent mode exists the border simply drops back to neutral, which
   *  is visually identical to a full close. */
  public removeInlineModeChip(): void {
    this.removeModeChip();
    // The chipObserver clears _currentChipMode on removal; updateChipState is
    // idempotent so run it here too for the case the observer skips.
    this._currentChipMode = this.getCurrentMode() || "";
    this.updateChipState();
    this.updatePlaceholder();
    this.resizeInput();
    this.updateSendButton();
  }

  /** Rebuild chips mangled by native editing.
   *
   *  Chromium can split or strip contenteditable=false elements when the
   *  user backspaces at (or inside) their boundary — the chip loses its icon
   *  or label children and renders broken (see the "Plan Mode" corruption
   *  report).  The beforeinput guard prevents the common case; this pass is
   *  the safety net: any chip whose icon or label is missing is rebuilt from
   *  its data attributes (mode/command id, attachment path). */
  private repairChips(): void {
    const chips = this.input.querySelectorAll<HTMLElement>(".mode-chip, .command-chip");
    chips.forEach((chip) => {
      if (chip.querySelector(".chip-icon") && chip.querySelector("span")) return; // intact

      // ── Attachment chip: rebuild from state, or drop if unresolvable.
      if (chip.getAttribute("data-attach") === "true") {
        const path = chip.getAttribute("data-path");
        const att = path ? getState().attachments.find((a) => a.path === path) : undefined;
        if (!att) { removeChipWithSeparator(chip); return; }
        const fresh = buildAttachChip(att);
        chip.replaceWith(fresh);
        this.refreshIcons(this.input);
        return;
      }

      // ── Command chip: rebuild from the command catalog.
      if (chip.classList.contains("command-chip")) {
        const cmd = SLASH_COMMANDS.find((c) => c.id === chip.getAttribute("data-command"));
        if (!cmd) { removeChipWithSeparator(chip); return; }
        const fresh = this.buildChipElement("command-chip", cmd.id, cmd.title, this.resolveIcon(cmd.icon), () => this.deactivateCommandChip());
        chip.replaceWith(fresh);
        this.refreshIcons(this.input);
        return;
      }

      // ── Mode chip: rebuild from the command catalog (steer included).
      const mode = chip.getAttribute("data-mode") ?? "";
      const cmd = SLASH_COMMANDS.find((c) => c.id === mode);
      const fresh = this.buildChipElement("mode-chip", mode, cmd ? cmd.title : mode, this.resolveIcon(cmd ? cmd.icon : "list-checks"), () => this.removeInlineModeChip());
      chip.replaceWith(fresh);
      this.refreshIcons(this.input);
    });
  }

  /** Shared DOM builder for chip rebuilds: creates the span, wires the close
   *  affordance and guarantees a separator space after the chip so the caret
   *  stays off the border (re-inserts one if the mangled chip lost it). */
  private buildChipElement(cls: string, key: string, label: string, icon: string, onClose: () => void): HTMLElement {
    const chip = document.createElement("span");
    chip.contentEditable = "false";
    chip.className = cls;
    if (cls === "mode-chip") chip.setAttribute("data-mode", key);
    else chip.setAttribute("data-command", key);
    chip.innerHTML = `${icon ? `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i>` : ""}<span>${label}</span>`;
    wireChipClose(chip, onClose);
    if (!isChipSeparator(chip.nextSibling)) chip.after(document.createTextNode(" "));
    return chip;
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
      (window as any).lucide.createIcons({ root: chip });
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
   * small vertical settle rather than a horizontal push: entering iWork
   * drops in from above, returning to normal rises from below — the same
   * language every other surface uses when it appears, so a mode switch
   * reads as content arriving instead of a page sliding past. The two
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
   *  The motion is already carried by the chat-container entrance
   *  (`#chat-container.mode-in-forward/back`), so no separate title animation
   *  runs here — stacking a second animation on the title made it look like it
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
   * pending panel, no status bar, and no leftover dropdowns — exactly like
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
   * Declare a transient popup to the scope host so any view switch dismisses
   * it. Registration happens on show; the host's dismiss path just hides the
   * node, which is what the click-away handlers do anyway.
   */
  private ownEphemeral(el: HTMLElement): void {
    registerSurface(el, {
      scope: "ephemeral",
      onClose: () => hideAnimated(el),
    });
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

    // Plugin market occupies the same content slot — close it so a session
    // switch / new task never repaints "underneath" the open market view.
    this.market?.close();

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
      hideAnimated(mentionDropdown);
      mentionDropdown.closest(".input-wrapper")?.classList.remove("slash-menu-open");
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
    if (modelDropdown) hideAnimated(modelDropdown);

    // ── 6. Chat status bar + pending panel ─────────────────────────
    const statusBar = document.getElementById("chat-status-bar");
    if (statusBar) {
      statusBar.classList.add("hidden");
      statusBar.innerHTML = "";
      statusBar.style.maxWidth = "";
    }
    resetPendingPanel();

    // ── 7. Input area — clear leftover text/chip/attachments ───────
    // Capture the current composer content against the *outgoing* session
    // before it is wiped, so restoreDraft() can bring it back when the user
    // returns to this session.
    this.saveCurrentDraft();
    // Snap the composer back out of its expanded overlay before the next
    // session's draft is restored: the flag survived every switch, so a
    // conversation entered after an expand kept the vacated-slot placeholder
    // and the floating input box of the previous one.
    this.setInputExpanded(false);
    if (this.input) {
      this.input.innerHTML = "";
      this.input.style.height = "56px";
    }
    this._currentChipMode = "";
    this._persistentMode = "";  // Clear — the new session's mode_changed will set the correct value
    setInputMode("");
    clearAttachments();
    // Clear the local queue mirror; the freshly selected session's
    // authoritative snapshot arrives via queue_list → queued_submits.
    clearQueuedPrompts();

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

    // ── 10. Floating popups ─────────────────────────────────────────
    // Every click-away surface declared itself to the scope host when it
    // opened (context menus, dropdowns, the tab-add popups), so one call
    // dismisses them all — including any popup added later that nobody
    // remembered to list here. Tooltips are a hover artefact of the view that
    // is being repainted, so they are simply un-marked.
    closeEphemeralSurfaces();
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

    // ── 14. Leave #message-list + welcome screen alone ──────
    // chat.render() will repaint #message-list, but it does not always
    // run synchronously with this cleanup.  Clear it eagerly so we never
    // show a stale message bubble for one frame after switching modes.

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
      hideViewAnimated(childView);
      childView.innerHTML = "";
    }

    // ── 18. Reset session-bar text + btn-session-menu state ───────
    // When sub-agent view is active the session-bar shows a breadcrumb
    // trail; after cleanup we want the plain "New Session" label back.
    const sessionBarName = document.getElementById("session-bar-name");
    if (sessionBarName) {
      this.setSessionBarName(sessionBarName, t("session.newSession"));
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

    // ── 22. Scroll progress indicator reset ────────────────────────
    // The right-edge turn ruler is owned by ChatScrollIndicator. This step
    // used to empty `.chat-scroll-track` and put `.hidden` on the progress
    // thumb from the outside, which left the indicator's own bookkeeping
    // stale: the next update() saw an unchanged turn signature and skipped
    // the rebuild, so the ruler stayed blank until a new user turn arrived,
    // and the thumb -- `.hidden` is `display:none !important` -- never came
    // back at all. Hand the reset to the component instead.
    this.chat.resetScrollIndicator();
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
      // 5-line cap — must match the CSS `max-height: 121px` on .prompt-input
      // (5 × 14.5px × 1.55 line-height + 8px vertical padding).
      const h = Math.min(Math.max(contentH, 56), 121);
      this.input.style.height = `${h}px`;
      this.updateInputExpandButton();
      // Second frame: pin the caret line AFTER the browser's own caret
      // scroll-into-view pass has run against the committed height. Doing it
      // in the same frame lost the race — Chrome's selection scrolling fires
      // after this rAF's layout and reset the offset back, which is why line
      // 6 stayed hidden while typing. By the next frame the height is real,
      // the browser has done whatever it wanted to scrollTop, and this final
      // correction is the last word before paint.
      requestAnimationFrame(() => this.scrollCaretIntoView());
    });
  }

  /**
   * Keeps the line the caret is on visible inside the composer once the box
   * is height-capped and internally scrollable.  Only corrects the offset by
   * the actual overshoot, so a user scrolling around a long draft with the
   * mouse is left alone until they type again.
   */
  private scrollCaretIntoView(): void {
    if (this._inputExpanded) return; // expanded mode pins to the end instead
    const box = this.input;
    if (box.scrollHeight <= box.clientHeight) return; // nothing overflows yet
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || !sel.isCollapsed) return;
    const range = sel.getRangeAt(0).cloneRange();
    if (!box.contains(range.startContainer)) return;
    // The collapsed range's own rect IS the caret position — regardless of
    // how the line was produced (typed <div> vs soft-wrapped bare text).  The
    // previous parentElement approach returned the whole input box for bare
    // text nodes, whose rect equals the viewport box, so the overshoot was
    // always 0 and the caret line never scrolled into view.
    let caret = range.getBoundingClientRect();
    if (!caret || (caret.top === 0 && caret.bottom === 0)) {
      // Some engines return a zero rect for collapsed ranges at line starts;
      // fall back to the caret's containing element.
      const node = range.startContainer;
      const el = node.nodeType === Node.TEXT_NODE
        ? node.parentElement
        : (node as Element);
      if (!el || !box.contains(el)) return;
      caret = el.getBoundingClientRect();
    }
    const view = box.getBoundingClientRect();
    // Half a visual line of headroom (14.5px × 1.55) so the caret line sits
    // fully inside the box instead of hugging its very edge.
    const headroom = 11;
    if (caret.bottom > view.bottom) {
      box.scrollTop += caret.bottom - view.bottom + headroom;
    } else if (caret.top < view.top) {
      box.scrollTop -= view.top - caret.top + headroom;
    }
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
      // pending panel that is visible this very instant.  Trusting the
      // ResizeObserver cache instead is racy: the callback only runs at the
      // END of a frame, so a status-bar/pending-panel that just appeared or
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
   * same `[56, 121]` window `resizeInput()` uses for the non-expanded state
   * (5-line cap, see the CSS on .prompt-input).
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
    return Math.min(Math.max(contentH, 56), 121);
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
    const visible = matchingSlashCommands("", mode);

    // Mode commands (Plan / Spec) live on the dropdown's main page as
    // first-class entries; the slash sub-page only lists actions and
    // user/project commands.  Palette labels are resolved at render time —
    // freezing them at module load would bake in one locale.  The chip and
    // the payload keep the command's own title / id, so only what the
    // palette shows changes here.
    const paletteModeLabel = (cmd: SlashCommand): string => {
      if (cmd.id === "plan") return t("slashCommands.planEntry");
      if (cmd.id === "spec") return t("slashCommands.specEntry");
      return cmd.title;
    };
    const modesPage = document.getElementById("mention-modes");
    if (modesPage) {
      modesPage.innerHTML = visible.filter(c => c.kind === "mode").map(cmd => `
        <button class="mention-dropdown-item" data-action="${cmd.id}">
          ${this.resolveIcon(cmd.icon) ? `<i data-lucide="${this.resolveIcon(cmd.icon)}" class="lucide mention-icon-${cmd.id}"></i>` : ""}
          <span>${this.esc(paletteModeLabel(cmd))}</span>
        </button>`).join("");
      this.refreshIcons(modesPage);
    }

    const items = visible.filter(c => c.kind !== "mode").map(cmd => {
      return `
      <button class="mention-dropdown-item" data-action="${cmd.id}" data-slash="${cmd.name}" data-kind="${cmd.kind}">
        ${this.resolveIcon(cmd.icon) ? `<i data-lucide="${this.resolveIcon(cmd.icon)}" class="lucide mention-icon-${cmd.id}"></i>` : ""}
        <span>${cmd.title}  <span class="mention-hint">/${cmd.name}</span></span>
      </button>`;
    }).join("");

    slashPage.innerHTML = backBtn + items;

    this.refreshIcons(slashPage as HTMLElement);
  }

  /** Fills one resource sub-page of the "+" dropdown.  Pages mirror the
   *  plugin-market naming (智能体 / 技能 / 连接器 / 工具) plus rules and
   *  library documents, and are always rendered — an empty group shows a
   *  muted empty row instead of hiding the page.  Plugin rows render the
   *  plugin's own declared icon through pluginIconHtml (the market's one
   *  icon path); no generic substitute glyph is ever invented here. */
  private renderResPage(page: HTMLElement, kind: string): void {
    const st = getState();

    const hint = (s: string | undefined, max = 26): string => {
      const v = (s || "").replace(/\s+/g, " ").trim();
      if (!v) return "";
      return v.length > max ? `${v.slice(0, max)}…` : v;
    };
    const lucide = (name: string) => `<i data-lucide="${name}" class="lucide"></i>`;
    const entry = (resKind: string, name: string, title: string, iconHtml: string, h = "") => `
      <button class="mention-dropdown-item" data-resource-kind="${resKind}" data-resource-name="${this.esc(name)}">
        ${iconHtml}
        <span>${this.esc(title)}${h ? ` <span class="mention-hint">${this.esc(h)}</span>` : ""}</span>
      </button>`;
    const empty = `<button class="mention-dropdown-item muted" disabled>${this.esc(t("mention.emptyGroup"))}</button>`;

    const enabledPlugins = (pluginStates() || []).filter(p => p.enabled && p.state !== "failed");
    // The app-wide sub-agent glyph is the settled sa-loader chevron (settings
    // sidebar, agent panel, chat strip) — mirrored here, not a second icon.
    const saAgentGlyph = '<span class="sa-loader" data-running="false"><i></i><i></i><i class="sa-chevron"></i><i></i><i class="sa-chevron"></i><i></i><i></i><i></i><i class="sa-chevron"></i></span>';
    // Library node glyphs mirror the settings library tree's per-type icons.
    const libIcon = (type: string): string =>
      type === "md" ? "file-code"
      : type === "sheet" ? "table"
      : type === "clip" ? "globe"
      : type === "attachment" ? "paperclip"
      : "file-text";
    let items = "";
    if (kind === "agents") {
      const list = (st.subAgents || []).filter(a => !a.hidden);
      items = list.length
        ? list.map(a => entry("subagent", a.name, a.name, saAgentGlyph, hint(a.description))).join("")
        : empty;
    } else if (kind === "skills") {
      // The exact list the settings skills panel shows — every registered
      // skill, including the ones shipped by installed plugins and the ones
      // the user installed by hand. No extra filter: a skill is a skill.
      const list = st.skillsList || [];
      items = list.length
        ? list.map(s => entry("skill", s.name, s.name, lucide("wand-2"), hint(s.description))).join("")
        : empty;
    } else if (kind === "connectors") {
      // Only plugins that actually provide an MCP server are connectors;
      // a plugin's skills surface as skills, its agents as agents — the
      // plugin itself is never filed as a "tool" here.
      const isConnector = (p: PluginStateEntry): boolean => ((p.provides || {}).mcp || []).length > 0;
      const list = enabledPlugins.filter(isConnector);
      items = list.length
        ? list.map(p => entry("plugin", p.name, p.display_name || p.name, pluginIconHtml(p.icon, p.name), hint(p.description))).join("")
        : empty;
    } else if (kind === "rules") {
      const list = [...(st.globalRules || []), ...(st.projectRules || [])];
      items = list.length
        ? list.map(r => entry("rule", r.name, r.name, lucide("list-checks"))).join("")
        : empty;
    } else if (kind === "library") {
      const docTypes = new Set(["doc", "sheet", "clip", "md", "attachment"]);
      const list = (st.libraryTree || []).filter(n => docTypes.has(n.type));
      items = list.length
        ? list.map(n => entry("doc", n.name, n.name, lucide(libIcon(n.type)))).join("")
        : empty;
    } else if (kind === "attach") {
      // The merged attachment entry hosts the two native pickers; their
      // buttons reuse the existing upload-file / select-folder actions.
      items = `
        <button class="mention-dropdown-item" data-action="upload-file">
          ${lucide("file")}
          <span>${this.esc(t("mention.uploadFile"))}</span>
        </button>
        <button class="mention-dropdown-item" data-action="select-folder">
          ${lucide("folder")}
          <span>${this.esc(t("mention.selectFolder"))}</span>
        </button>`;
    } else {
      return;
    }

    // Sub-page titles reuse the app's own terminology keys — sidebar and
    // market already name these surfaces, so this menu never coins a second
    // word for the same thing.
    const title: Record<string, string> = {
      agents: t("sidebar.agent"),
      skills: t("market.surfaceSkills"),
      connectors: t("market.surfaceMcp"),
      rules: t("sidebar.rules"),
      library: t("sidebar.library"),
      attach: t("mention.addAttachment"),
    };
    page.innerHTML = `
      <button class="mention-dropdown-item" data-action="back-main">
        ${lucide("arrow-left")}
        <span>${this.esc(title[kind] || "")}</span>
      </button>
      <div class="mention-dropdown-divider"></div>
      ${items}`;

    this.refreshIcons(page);
  }

  /** Activates a user-defined resource through the shared chip pipeline.
   *  Skills and sub-agents carry a prompt body, so they persist on the
   *  backend via set_command (sticky, re-injected every turn); rules,
   *  library documents and plugins have no prompt body and render a
   *  local-only chip whose `<command>` tag reaches the model on submit. */
  private activateResourceChip(kind: string, name: string): void {
    const st = getState();
    // Chip icons follow the mention entries' unified mapping (settings
    // sidebar / library tree). Chips render through resolveIcon, which only
    // accepts lucide names, so the sub-agent keeps the app's lucide agent
    // glyph instead of the sa-loader span.
    const libIcon = (type: string): string =>
      type === "md" ? "file-code"
      : type === "sheet" ? "table"
      : type === "clip" ? "globe"
      : type === "attachment" ? "paperclip"
      : "file-text";
    let cmd: SlashCommand | undefined;
    if (kind === "skill") {
      const skill = (st.skillsList || []).find(s => s.name === name);
      if (!skill) return;
      cmd = { id: `skill:${skill.name}`, name: skill.name, title: skill.name, description: skill.description, icon: "wand-2", kind: "action", prompt: skill.body || undefined };
    } else if (kind === "subagent") {
      const agent = (st.subAgents || []).find(a => a.name === name);
      if (!agent) return;
      cmd = { id: `subagent:${agent.name}`, name: agent.name, title: agent.name, description: agent.description, icon: "bot", kind: "action", prompt: agent.description || agent.system_prompt || undefined };
    } else if (kind === "plugin") {
      const plugin = (pluginStates() || []).find(p => p.name === name);
      if (!plugin) return;
      // Only connector plugins are reachable here (see renderResPage), so the
      // chip carries the connector glyph throughout.
      cmd = { id: `plugin:${plugin.name}`, name: plugin.name, title: plugin.display_name || plugin.name, description: plugin.description || "", icon: "cable", kind: "action" };
    } else if (kind === "rule") {
      cmd = { id: `rule:${name}`, name, title: name, description: "", icon: "list-checks", kind: "action" };
    } else if (kind === "doc") {
      const node = (st.libraryTree || []).find(n => n.name === name);
      cmd = { id: `doc:${name}`, name, title: name, description: "", icon: node ? libIcon(node.type) : "file-text", kind: "action" };
    }
    if (cmd) this.activateSlashCommand(cmd);
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

    const fileRefs = st.attachments.length > 0 ? st.attachments.map(a => ({ name: a.name, size: a.size, icon: a.mime_type === "text/x-terminal" ? getTerminalIcon() : a.mime_type === "text/x-directory" ? getFolderIcon(a.name) : getFileIcon(a.name), path: a.path, mime_type: a.mime_type })) : undefined;

    // /steer: mid-run injection - don't add a user message or start a new turn
    if (this._currentChipMode === "steer") {
      if (!text.trim()) return;
      send({ type: "steer", session_id: st.sessionId || undefined, prompt: text } as any);
      this.deleteDraft(st.sessionId || "");
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

    // Busy sessions send immediately: the backend queues the prompt behind
    // the running turn (persisted, auto-drained FIFO) and mirrors it back
    // via the queued_submits broadcast.  No local bubble here — the
    // queued_run_started event renders it when the queued turn starts.
    if (!isQueued) {
      addUserMessage(text, sendMode, fileRefs);
      startAssistantMessage();
      setSessionState("running");
      this.updateUIState(true);
    }

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
    // Record the sent prompt in the recall history (dedup consecutive
    // duplicates, cap at 100) and persist it so it survives app restarts.
    if (text.trim() && text !== this._inputHistory[this._inputHistory.length - 1]) {
      this._inputHistory.push(text);
      if (this._inputHistory.length > 100) this._inputHistory.splice(0, this._inputHistory.length - 100);
    }
    this._inputHistoryIdx = this._inputHistory.length;
    this.persistInputHistory();
    // Force scroll to bottom so the user immediately sees the message they sent
    const _chatContainer = document.getElementById("chat-container");
    if (_chatContainer) _chatContainer.scrollTop = _chatContainer.scrollHeight;

    // Clear input — mode chip is hidden during agent execution.
    // A sticky *command* (active_command) is one-shot: it fires on submit
    // and then is cleared.  Unlike plan/spec it does NOT persist across
    // turns in the input chip (the backend re-injects its instructions
    // every turn until the next message).
    this.deleteDraft(st.sessionId || "");
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

  /** 「继续」 on an interrupted turn: sends the resume prompt exactly as if the
   *  user had typed it and pressed send — same mode/chip resolution and the
   *  same run payload as submit(), but without touching whatever the composer
   *  holds. The timeline stops rendering the button as soon as this lands. */
  private resumeInterrupted(): void {
    const st = getState();
    if (st.running) return;
    const sendMode = this.effectiveMode() || undefined;
    const payload: Record<string, any> = {
      type: "run",
      prompt: CONTINUE_PROMPT,
      session_id: st.sessionId || undefined,
      temp_chat: st.tempChat || undefined,
    };
    if (sendMode) {
      payload.mode = sendMode;
      const cmd = SLASH_COMMANDS.find(c => c.id === sendMode);
      if (cmd?.prompt) payload.mode_prompt = cmd.prompt;
    }
    addUserMessage(CONTINUE_PROMPT, sendMode);
    startAssistantMessage();
    setSessionState("running");
    this.updateUIState(true);
    send(payload as import("./core/types.js").ClientMessage);
  }

  private cancel(): void {
    // Close @ dropdown if open
    const dd = document.getElementById("mention-dropdown");
    const bm = document.getElementById("btn-mention");
    if (dd && !dd.classList.contains("hidden")) {
      hideAnimated(dd);
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
    // Pull the freshly selected session's queue snapshot for the panel.
    send({ type: "queue_list", session_id: sid });
  }

  /**
   * Exits the currently open session (if any) exactly like the "New task"
   * button does, so the automation page / plugin market never opens on top
   * of a live session and closing them later lands on the fresh new-task
   * state. No-op when the welcome screen is already up (not inside a
   * session) — that also avoids firing redundant `new_session` round-trips.
   */
  private exitSessionForOverlay(): void {
    const welcome = document.getElementById("welcome-screen");
    if (!welcome || !welcome.classList.contains("hidden")) return;
    this.exitTempChat();
    this.cleanupContentArea({ keepAutomationFlag: false });
    resetChat();
    const requestId = crypto.randomUUID();
    setRequestedSessionId("", requestId);
    send({ type: "new_session", request_id: requestId });
  }

  /**
   * Single entry for opening/closing the automation PAGE (sidebar entry,
   * keyboard shortcuts, tray). Exits an open session first so the page —
   * like the plugin market — always reveals the new-task state on close.
   * NOT used by the startup-session restore path, which must not kill an
   * existing session.
   */
  private toggleAutomationPage(): void {
    if (!this.automationPanel.isActive) this.exitSessionForOverlay();
    this.modeTransition.toggleAutomation();
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
      // detail panel, pending panel, model dropdown, …) before requesting
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
      const ok = await Dialog.confirm(t("session.confirmDeleteTitle"), t("session.confirmDelete", { name }), { scope: "app" });
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
      // 0-based among REAL user turns: the notice modes (compact_marker /
      // compact_notice / model_switch) are user-role but consume no
      // data-user-idx, so counting them drifted the retry one turn early.
      const userMsgIdx = realUserMessages(msgs.slice(0, lastAssistantIdx)).length - 1;
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
    a["settings_library"] = () => openSetting("library");
    // The four auxiliary surfaces merged into the 辅助面板 settings page;
    // the dedicated keybind actions land there too.
    a["settings_terminal"] = () => openSetting("assist");
    a["settings_editor"] = () => openSetting("assist");
    a["settings_review"] = () => openSetting("assist");
    a["settings_browser"] = () => openSetting("assist");
    a["settings_memory"] = () => openSetting("memory");
    a["settings_usage"] = () => openSetting("usage");
    a["settings_about"] = () => openSetting("about");
    a["settings_gateway"] = () => openSetting("gateway");
    a["settings_plugins"] = () => openSetting("plugins");
    a["settings_shortcuts"] = () => openSetting("shortcuts");
    a["settings_storage"] = () => openSetting("storage");
    a["settings_developer"] = () => {
      // The developer nav entry only exists in dev mode.
      if (!document.getElementById("settings-nav-developer")?.offsetParent) return;
      openSetting("developer");
    };

    // ── Navigation / Sidebar ─────────────────────────────────────────
    a["toggle_sidebar"] = () => {
      this.userToggledSidebar = true;
      document.getElementById("app")?.classList.toggle("sidebar-collapsed");
    };
    a["view_chat"] = () => this.viewManager.switchTo("chat");
    a["view_automation"] = () => this.toggleAutomationPage();

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
      Dialog.confirm(t("theme.switchTitle"), t("theme.switchConfirm", { label: t(`theme.${next}`) }), { priority: "low", scope: "app" }).then(ok => {
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
      Dialog.confirm(t("language.switchTitle"), t("language.switchConfirm", { label: t(`language.${next}`) }), { priority: "low", scope: "app" }).then(ok => {
        if (!ok) return;
        const settings = { ...getState().settings, language: next };
        setSettings(settings);
        setLocale(next);
        send({ type: "configure", config: { language: next } });
      });
    };
    // ── Automation ───────────────────────────────────────────────────
    a["automation_open"] = () => this.toggleAutomationPage();
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
      if (!this._shortcutSub) this._shortcutSub = subscribe(() => this.applyShortcutHints(), ["settings"]);
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

    const categories = new Map<string, Array<{ id: string; keys: string[]; desc: string }>>();
    for (const b of binds) {
      const cat = b.category || "general";
      if (!categories.has(cat)) categories.set(cat, []);
      categories.get(cat)!.push({ id: b.id, keys: b.keys, desc: b.description || b.id });
    }

    for (const [cat, items] of categories) {
      const cell = document.createElement("div");
      cell.className = "sc-category";
      const catKey = "settings.shortcutCategory" + cat.charAt(0).toUpperCase() + cat.slice(1);
      cell.textContent = t(catKey) || cat.charAt(0).toUpperCase() + cat.slice(1);
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
    Dialog.showHtmlDialog(t("settings.keyboardShortcuts"), scroll, { priority: "low", scope: "app" });
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
        // Escape has two roles: dismiss an open overlay (settings / search),
        // or — when nothing is open — fall through to the keybind table so
        // the `cancel` action can stop generation.  Previously this branch
        // always returned, which made the `cancel` binding dead code.
        const wasSettingsOpen = document.getElementById("app")?.classList.contains("settings-mode") ?? false;
        const wasSearchOpen = !document.getElementById("search-overlay")?.classList.contains("hidden");

        this.settings.close();
        setActiveToolId(null);
        this.search.close();

        if (wasSettingsOpen || wasSearchOpen) return;
        // Otherwise fall through — the dispatch loop below will fire
        // `cancel` (bound to `escape`) if its scope is satisfied.
      }

      // Never intercept Shift-only + printable character (e.g. shift+? = typing ?)
      if (shift && !mod && !alt && key.length === 1) {
        return;
      }

      const keybindsCfg = (getState().settings.keybinds as any);
      const binds: any[] = keybindsCfg?.keybinds || [];

      // Block native browser handling only for keys that will actually fire.
      // A key whose scope is unsatisfied (e.g. `Ctrl+Z` pressed while the
      // settings panel is foregrounded) must fall through to the browser's
      // own handler so a settings form field can still undo.
      if (mappedKey.match(/^f(1[0-2]|[1-9])$/) || binds.some((kb) => kb.keys && kb.keys.includes(pattern) && scopeAllows(kb.id))) {
        // Don't intercept Ctrl+Z when the composer is focused — let the
        // browser handle the native undo for the contenteditable.
        if (!(mappedKey === 'z' && mod && this.input.contains(document.activeElement))) {
          e.preventDefault();
        }
      }

      for (const kb of binds) {
        if (!kb.keys || !kb.keys.includes(pattern) || !scopeAllows(kb.id)) continue;
        // Skip undo_message when the composer is focused (native browser undo)
        if (kb.id === "undo_message" && this.input.contains(document.activeElement)) {
          return;
        }
        const action = this._keybindActions[kb.id];
        if (action) {
          e.preventDefault();
          action();
          return;
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

    showAnchoredDropdown(menu, btn);
    // Body-level portal: it belongs to no conversation, so a view switch has
    // to dismiss it through the scope host rather than be remembered here.
    registerSurface(menu, { scope: "ephemeral", onClose: () => this.closeEncreMenu() });

    const backdrop = document.createElement("div");
    backdrop.id = "encre-menu-backdrop";
    document.body.appendChild(backdrop);
    backdrop.addEventListener("click", () => this.closeEncreMenu());

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
    const backdrop = document.getElementById("encre-menu-backdrop");
    const cleanup = (): void => {
      this.notifications.detach();
      menu.remove();
      backdrop?.remove();
    };
    menu.classList.add("closing");
    backdrop?.classList.add("closing");
    menu.addEventListener("animationend", cleanup, { once: true });
    // Safety timeout in case animationend never fires
    setTimeout(cleanup, 600);
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
          <div class="encre-menu-item-title">
            <i data-lucide="bell" class="lucide"></i>
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
          <div class="encre-menu-item-title">
            <i data-lucide="settings" class="lucide"></i>
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
    // Colour skin: the pre-paint script in index.html already put the
    // stored skin on <html>; sync the state field so the settings panel
    // shows the active one. Unknown/missing values keep the ink default.
    const storedSkin = localStorage.getItem("encre-theme-skin");
    if (storedSkin && (THEME_SKINS as readonly string[]).includes(storedSkin)) {
      setThemeSkin(storedSkin);
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

/** Last-resort screen: boot failed before the UI existed. */
function showFatalBootError(error: unknown): void {
  try {
    const target = document.getElementById("splash-screen") || document.body;
    target.innerHTML = `
      <div style="display:flex;height:100vh;align-items:center;justify-content:center;
                  font-family:system-ui,sans-serif;color:var(--text-primary,#222);padding:24px;text-align:center">
        <div>
          <div style="font-size:16px;font-weight:600;margin-bottom:8px">Encre Agent failed to start</div>
          <div style="font-size:12px;opacity:.7">${String((error as Error)?.message || error).slice(0, 300)}</div>
        </div>
      </div>`;
  } catch {
    /* nothing else we can do */
  }
}

installUiErrorBoundaries();
try {
  const app = new App();
  void app.start().catch((e) => {
    reportUiFault("app", "start", e);
    showFatalBootError(e);
  });
} catch (e) {
  reportUiFault("app", "construct", e);
  showFatalBootError(e);
}

/** Normalized dedupe key for a summary-panel reference.  Memory-file
 *  references collapse onto their file name so create/read/update/delete on
 *  the same memory render as a single row.  Web-fetch URLs are normalized
 *  (protocol/www/trailing-slash stripped) so the same page fetched via
 *  slightly different URLs still collapses.  Web-search and memory-search
 *  queries are whitespace-normalized so repeated identical queries dedupe.
 *  MCP calls collapse onto the tool name so the same MCP tool invoked
 *  repeatedly renders as a single row. */
function refDedupeKey(r: { tool: string; summary: string }): string {
  const t = (r.tool || "").toLowerCase();
  if (t.startsWith("memory_") && t !== "memory_search" && t !== "memory_profile") {
    const text = stripRefTypePrefix(r.tool, r.summary).trim().toLowerCase();
    if (text) return "memory:" + (text.endsWith(".md") ? text : text + ".md");
  }
  if (t === "web_fetch") {
    const url = extractRefUrl(r.summary);
    if (url) return "url:" + normalizeUrlForDedup(url);
  }
  if (t === "web_search" || t === "memory_search") {
    const q = stripRefTypePrefix(r.tool, r.summary).trim().toLowerCase().replace(/\s+/g, " ");
    if (q) return (t === "web_search" ? "search:" : "memsearch:") + q;
  }
  if (t.startsWith("mcp__")) return "mcp:" + t;
  return `${r.tool}\u0000${r.summary}`;
}

/** Normalize a URL for deduplication: strip protocol, leading www, and
 *  trailing slash so near-identical URLs collapse onto the same key. */
function normalizeUrlForDedup(url: string): string {
  try {
    const u = new URL(url);
    return (
      u.hostname.toLowerCase().replace(/^www\./, "") +
      u.pathname.replace(/\/+$/, "") +
      u.search
    );
  } catch {
    return url
      .toLowerCase()
      .replace(/^https?:\/\/(www\.)?/, "")
      .replace(/\/+$/, "");
  }
}

/** Truncate reference display text for single-line display.  The full text
 *  stays available via the element's title attribute. */
function truncateRefText(text: string, max = 64): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1) + "\u2026";
}

/** Extract the first http(s) URL from a reference summary (e.g. a
 *  "Fetched: https://..." web-fetch reference).  Returns "" when the summary
 *  carries no URL (e.g. a web-search query), in which case the row stays
 *  non-interactive text. */
function extractRefUrl(summary: string): string {
  const m = (summary || "").match(/https?:\/\/[^\s"'<>]+/);
  if (!m) return "";
  // Strip trailing punctuation that likely belongs to the sentence.
  return m[0].replace(/[.,;:!)\]}\u3002\uff0c\uff09\uff1b\uff1a]+$/, "");
}

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
