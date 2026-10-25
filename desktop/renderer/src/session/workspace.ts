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
 * Workspace (iWork) sidebar & mode management.
 *
 * The iWork mode reuses the normal session sidebar (#session-section) and
 * only toggles the backend session context: entering iWork mode opens a
 * workspace (and lists its sessions via `list_sessions`, channel "iwork"),
 * exiting restores the normal session list. This module owns the mode state
 * transitions and the external entry points (tray, mode switch, keyboard
 * shortcuts). Also exposes `syncFirstNavActive` for sidebar nav state.
 */

import { getState, setActiveWorkspace, setWorkspaceMode, subscribe, removeArchivedSessionById, clearMessages, setSessionId, setSessionState, getTraySessions, setSessionsList } from "../core/state.js";
import { send } from "../core/ws.js";
import { setRequestedSessionId } from "../core/stream.js";
import { t } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";
import { showContextMenu, hideContextMenu, renderContextMenuItems } from "../ui/context-menu.js";
import { pluginMenuSpecs } from "../features/plugin_contributions.js";
import { hideAnimated, showAnimated } from "../ui/animate.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { guardRender } from "../core/ui_boundary.js";
import { registerSurface, unregisterSurface } from "../core/scope.js";
import { workspaceIconHtml } from "../chat/session-projection.js";
import { bindTargetModelSelection, readTargetModelSelection, readTargetModelSelectionEnabled, renderTargetModelSelection } from "../settings/model-selection.js";
import type { WorkspaceEntry, WorkspaceConfig, SessionEntryData } from "../core/types.js";

/**
 * Manages iWork mode transitions (enter / exit / force-exit).
 */
export class Workspace {
  private isInWorkspaceMode = false;
  private _exiting = false;
  private _transitioning = false;
  private pendingWorkspacePath = "";

  /** Public read-only access to the workspace mode flag. */
  public get isInWorkspaceModePublic(): boolean {
    return this.isInWorkspaceMode;
  }

  /** Callback fired after entering/exiting iWork mode so the app can refresh the content area. */
  public onModeChange: (() => void) | null = null;

  constructor() {
    this.syncFirstNavActive();
    // The sidebar workspace section keeps only its header + ">" nav button;
    // the actual workspace list lives in the WorkspaceManager popup.
    // Reconcile the internal mode flag with the shared state: some entry
    // points (e.g. WorkspaceManager "new workspace" -> openFolder) send
    // open_workspace directly, and the shared workspaceMode is only flipped
    // to "iwork" when the backend's workspace_opened confirmation arrives.
    // Without this reconciliation the internal flag would stay false, which
    // broke exit() and toggleWorkspaceMode() afterwards.
    subscribe(() => {
      if (this._transitioning || this._exiting) return;
      const mode = getState().workspaceMode;
      if (mode === "iwork" && !this.isInWorkspaceMode) {
        this.isInWorkspaceMode = true;
      } else if (mode !== "iwork" && this.isInWorkspaceMode) {
        this.isInWorkspaceMode = false;
      }
    }, ["workspaceMode"]);
  }

  private esc(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }

  /** Toggle between normal and iWork mode. */
  private async toggleWorkspaceMode(): Promise<void> {
    if (this._transitioning) return;
    if (this.isInWorkspaceMode) {
      await this.exitWorkspaceMode();
    } else {
      await this.enterWorkspaceMode();
    }
  }

  /** Enter iWork mode (no-op if already in it or mid-transition). */
  async enter(): Promise<void> {
    if (this._transitioning) return;
    if (!this.isInWorkspaceMode) {
      await this.enterWorkspaceMode();
    } else if (getState().workspaceMode !== "iwork") {
      // Reconcile a desync: the internal flag says we are in workspace mode
      // but the shared state was reset to normal (e.g. a late config/close
      // snapshot). Without this the tab click would silently no-op and leave
      // the user stuck in normal mode. Re-assert the mode so the seg and the
      // content agree again.
      setWorkspaceMode("iwork");
      this.onModeChange?.();
    }
  }

  /** Opens a workspace from an external entry point, such as the tray. */
  public async open(path: string): Promise<void> {
    if (!path || this._transitioning) return;
    if (!this.isInWorkspaceMode) {
      this.pendingWorkspacePath = path;
      await this.enterWorkspaceMode();
      return;
    }
    this.activate(path);
  }

  /** Public exit — used by the header mode switch to leave workspace mode. */
  async exit(): Promise<void> {
    if (this.isInWorkspaceMode && !this._transitioning) {
      await this.exitWorkspaceMode();
    }
  }

  /**
   * Force-exit workspace mode without any visual transitions.
   */
  public forceExit(): void {
    if (!this.isInWorkspaceMode) return;
    this._exiting = true;
    this.isInWorkspaceMode = false;

    const oldWs = getState().activeWorkspace;
    setActiveWorkspace("");
    setWorkspaceMode("normal");
    if (oldWs) {
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "close_workspace", path: oldWs, request_id: requestId });
    } else {
      // No active workspace to close, so no backend state change happens and
      // the existing snapshot is still current — no refill needed.
    }
    this.syncFirstNavActive();
    setTimeout(() => { this._exiting = false; }, 100);
  }

  private async enterWorkspaceMode(): Promise<void> {
    if (this.isInWorkspaceMode || this._transitioning) return;
    this._transitioning = true;
    this.isInWorkspaceMode = true;
    this._exiting = false;

    // Set workspace mode BEFORE sending requests so the sessions_list
    // handler's channel check (workspaceMode === "iwork") passes when the
    // response arrives.
    setWorkspaceMode("iwork");

    // Drop the outgoing (normal) session right away. onModeChange() clears
    // the DOM but then calls chat.render(), which is RAF-throttled and could
    // fire before this function finishes — re-reading the normal session's
    // messages from state and leaving them on screen until workspace_opened
    // lands. Force an immediate render here so the welcome screen shows
    // straight away. Clearing just the messages (but not the sessionId)
    // keeps the draft / channel tracking intact.
    clearMessages();
    (window as any).__chatForceRender?.();

    const workspaces = getState().workspaces;
    const activeWs = getState().activeWorkspace;
    const pendingWorkspacePath = this.pendingWorkspacePath;
    this.pendingWorkspacePath = "";
    if (pendingWorkspacePath) {
      setActiveWorkspace(pendingWorkspacePath);
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "open_workspace", path: pendingWorkspacePath, request_id: requestId });
    } else if ((window as any).__pendingTrayResume) {
      // Do nothing — onSwitchSession already handles opening the correct workspace.
      // Avoid overwriting _requestedSessionRequestId set by that handler.
    } else if (!activeWs && workspaces.length > 0 && workspaces[0].path) {
      setActiveWorkspace(workspaces[0].path);
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "open_workspace", path: workspaces[0].path, request_id: requestId });
    } else {
      // Already have an active workspace (or empty): no backend state change
      // happens on this path (no open_workspace is sent, so the backend does
      // not push a new snapshot), and the existing snapshot is still current
      // — it was pushed when this workspace was opened and kept fresh by
      // per-mutation broadcasts. Seed the sidebar below and render.
    }

    // Seed the sidebar with the cached iWork sessions for this workspace
    // immediately, so the list never keeps showing the previous (normal)
    // scope's rows while the async open_workspace round-trip is in flight.
    // The authoritative sessions_list response replaces this a beat later.
    const targetWs = getState().activeWorkspace;
    const cachedIwork = getTraySessions().iwork.filter((s: any) => {
      const wsPath = s?.metadata?.workspace_path || s?.metadata?.workspace || "";
      return wsPath === targetWs;
    });
    setSessionsList(cachedIwork);

    this.syncFirstNavActive();
    this._transitioning = false;
    this.onModeChange?.();
  }

  private async exitWorkspaceMode(): Promise<void> {
    if (this._transitioning) return;
    this._transitioning = true;
    this._exiting = true;
    this.isInWorkspaceMode = false;

    const oldWs = getState().activeWorkspace;
    setActiveWorkspace("");
    if (oldWs) {
      const requestId = crypto.randomUUID();
      setRequestedSessionId("", requestId);
      send({ type: "close_workspace", path: oldWs, request_id: requestId });
    }
    // No close sent and no backend state change: the existing snapshot is
    // still current (pushed at boot / on the last workspace transition and
    // kept fresh by per-mutation broadcasts), so there is nothing to refill.
    // Seed the cached normal sessions right away instead of
    // keeping the workspace's rows until the close_workspace round-trip lands.
    setSessionsList(getTraySessions().normal);
    this.syncFirstNavActive();
    this._transitioning = false;
    // Symmetric to enterWorkspaceMode(): drop the outgoing workspace session's
    // messages so its conversation cannot linger while normal mode is re-entered.
    clearMessages();
    (window as any).__chatForceRender?.();
    setWorkspaceMode("normal");
    this.onModeChange?.();
    setTimeout(() => { this._exiting = false; }, 100);
  }

  private syncFirstNavActive(): void {
    syncFirstNavActive();
  }

  /** Switch the active workspace (used when already in iWork mode). */
  private activate(path: string): void {
    if (this._exiting || this._transitioning || !this.isInWorkspaceMode) return;
    setActiveWorkspace(path);
    // Seed the sidebar with cached sessions of the TARGET workspace (same
    // pattern as enterWorkspaceMode) so the previous workspace's rows don't
    // linger until the authoritative sessions_list lands with the
    // workspace_opened snapshot.  The seed is replaced in one step — never
    // cleared — so there is no empty flash in between.
    const cached = getTraySessions().iwork.filter((s: any) => {
      const wsPath = s?.metadata?.workspace_path || s?.metadata?.workspace || "";
      return wsPath === path;
    });
    setSessionsList(cached);
    const requestId = crypto.randomUUID();
    setRequestedSessionId("", requestId);
    send({ type: "open_workspace", path, request_id: requestId });
  }

  /** Opens a folder picker and requests the backend to open it as a workspace. */
  async openFolder(): Promise<void> {
    const folderPath = await window.electronAPI?.pickDirectory();
    if (!folderPath) return;
    const requestId = crypto.randomUUID();
    setRequestedSessionId("", requestId);
    send({ type: "open_workspace", path: folderPath, request_id: requestId });
  }
}

/**
 * Marks the first visible sidebar nav item as active.
 */
export function syncFirstNavActive(): void {
  const nav = document.getElementById("sidebar-main-nav");
  if (!nav) return;
  nav.querySelectorAll(".nav-item").forEach((el) => el.classList.remove("active"));
  const first = nav.querySelector<HTMLElement>(".nav-item:not(.hidden)");
  first?.classList.add("active");
}

/** Deterministic hue from the workspace name (mirrors the backend generator). */
export function nameHue(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = (((h * 31) + name.charCodeAt(i)) | 0) >>> 0;
  }
  return h % 360;
}

/**
 * Workspace management overlay.
 *
 * A toast-overlay dialog listing all registered workspaces (real data from
 * `state.workspaces`). The header carries the "New workspace" and "Archive"
 * actions; each list item opens a right-click context menu with Settings /
 * Delete. Settings and Archive render as internal sub-views with a back
 * button, mirroring the notification panel's drill-down navigation.
 */
/** Unsaved form state captured from the settings DOM before a re-render. */
interface SettingsFormState {
  name: string;
  ctx: Record<string, boolean>;
  modelToggle: boolean;
  models: string[];
}

export class WorkspaceManager {
  private workspace: Workspace;
  private overlay: HTMLElement | null = null;
  private contextMenu: HTMLElement | null = null;
  private view: "list" | "details" | "settings" | "archive" = "list";
  private editingPath = "";
  private unsub: (() => void) | null = null;
  private wsConfigPending = new Set<string>();
  /** Signature of the state slices the settings view renders; used to skip
   *  needless re-renders that would wipe the user's unsaved edits. */
  private settingsSig = "";

  constructor(workspace: Workspace) {
    this.workspace = workspace;
    this.contextMenu = document.getElementById("workspace-mgr-context-menu");
    document.addEventListener("click", () => this.hideContextMenu());
    // Refresh the overlay whenever workspace state changes so the list
    // stays in sync with the backend in real time.
    this.unsub = subscribe(() => {
      if (!this.overlay || this.overlay.classList.contains("hidden")) return;
      // While editing settings, only re-render when the data this view
      // actually shows changes. Unrelated background updates (usage stats,
      // session lists, a late workspace_config response, …) would otherwise
      // rebuild the form and snap unsaved toggles back to their saved value.
      if (this.view === "settings" && this.computeSettingsSig() === this.settingsSig) return;
      // Coalesced paint + shared boundary, like every other surface.
      scheduleRender("workspace-manager", () => {
        guardRender("workspace-manager", "body", () => this.renderBody());
      }, 60);
    }, ["workspaces", "activeWorkspace", "workspaceConfigs", "usageStats"]);
  }

  open(): void {
    this.view = "list";
    this.editingPath = "";
    if (!this.overlay) this.buildOverlay();
    showAnimated(this.overlay);
    this.renderBody();
    window.addEventListener("keydown", this.onKeyDown);
  }

  close(): void {
    this.hideContextMenu();
    window.removeEventListener("keydown", this.onKeyDown);
    if (this.overlay) unregisterSurface(this.overlay);
    hideAnimated(this.overlay);
  }

  /** Re-render the open overlay (used when async config responses arrive). */
  refresh(): void {
    if (!this.overlay || this.overlay.classList.contains("hidden")) return;
    // Same guard as the state subscription: don't rebuild the settings form
    // when nothing it displays changed (the edits are preserved anyway, but
    // skipping avoids needless focus loss while typing).
    if (this.view === "settings" && this.computeSettingsSig() === this.settingsSig) return;
    this.renderBody();
  }

  private onKeyDown = (e: KeyboardEvent): void => {
    if (e.key === "Escape") this.close();
  };

  private buildOverlay(): void {
    const overlay = document.createElement("div");
    overlay.className = "toast-overlay";
    overlay.id = "workspace-mgr-overlay";
    overlay.innerHTML = `<div class="toast-dialog workspace-mgr-dialog">
      <div class="workspace-mgr-header">
        <button class="btn-icon btn-sm" id="wsmgr-back" title="${t("workspace.back")}"><i data-lucide="arrow-left" class="lucide"></i></button>
        <span class="workspace-mgr-title" id="wsmgr-title"></span>
        <div class="workspace-mgr-actions">
          <button class="btn-icon btn-sm" id="wsmgr-archive" title="${t("workspace.archive")}"><i data-lucide="archive" class="lucide"></i></button>
          <button class="btn-icon btn-sm" id="wsmgr-new" title="${t("workspace.newWorkspace")}"><i data-lucide="plus" class="lucide"></i></button>
        </div>
      </div>
      <div class="workspace-mgr-body" id="wsmgr-body"></div>
      <div class="dialog-footer workspace-mgr-footer" id="wsmgr-footer" style="display:none"></div>
    </div>`;
    // Mount into the content area (not <body>) so the dimmed backdrop covers
    // the region the manager belongs to. It is WORKSPACE-wide — it manages
    // every conversation, not the open one — so it declares itself to the
    // scope host as an APP surface: switching conversations must not shelf it
    // or leave it repainted over somebody else's chat.
    const mount = document.getElementById("main-content") ?? document.body;
    mount.appendChild(overlay);
    registerSurface(overlay, {
      scope: "app",
      parent: mount,
      onClose: () => this.close(),
    });
    this.overlay = overlay;
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) this.close();
    });
    overlay.querySelector("#wsmgr-back")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.handleBack();
    });
    overlay.querySelector("#wsmgr-new")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.handleNewWorkspace();
    });
    overlay.querySelector("#wsmgr-archive")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.view = "archive";
      // Render purely from state: the archived list is filled by the boot
      // pull and re-pushed by the backend after every archive/unarchive/
      // delete mutation, and the state subscription re-renders this view —
      // entering the tab must never fire a per-entry refill request.
      this.renderBody();
    });
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: overlay });
    }
  }

  private handleBack(): void {
    if (this.view === "list") {
      this.close();
    } else {
      this.view = "list";
      this.editingPath = "";
      this.renderBody();
    }
  }

  private renderBody(): void {
    if (!this.overlay) return;
    const body = this.overlay.querySelector<HTMLElement>("#wsmgr-body");
    const titleEl = this.overlay.querySelector<HTMLElement>("#wsmgr-title");
    if (!body || !titleEl) return;
    // New-workspace / archive actions only belong to the workspace list; hide
    // them while drilling into a workspace's details/settings view.
    const showActions = this.view === "list";
    const archiveBtn = this.overlay.querySelector<HTMLElement>("#wsmgr-archive");
    const newBtn = this.overlay.querySelector<HTMLElement>("#wsmgr-new");
    if (archiveBtn) archiveBtn.style.display = showActions ? "" : "none";
    if (newBtn) newBtn.style.display = showActions ? "" : "none";
    const footer = this.overlay.querySelector<HTMLElement>("#wsmgr-footer");
    if (footer) footer.style.display = this.view === "settings" ? "" : "none";
    if (this.view === "details") {
      const ws = getState().workspaces.find((w) => w.path === this.editingPath);
      if (!ws) {
        this.view = "list";
        this.editingPath = "";
        this.renderBody();
        return;
      }
      titleEl.textContent = ws.name;
      body.innerHTML = this.renderDetails(ws);
    } else if (this.view === "settings") {
      const ws = getState().workspaces.find((w) => w.path === this.editingPath);
      if (!ws) {
        this.view = "list";
        this.editingPath = "";
        this.renderBody();
        return;
      }
      titleEl.textContent = t("workspace.settingsTitle");
      // Preserve the user's unsaved edits (name, context-file toggles, model
      // selection) across async re-renders, so a late workspace_config
      // response or any background state update cannot reset the form.
       const prior = this.captureSettingsFormState(body, ws);
       body.innerHTML = this.renderSettings(ws, prior);
       this.bindSettings(body, ws);
       this.renderSettingsFooter(body, ws);
       this.settingsSig = this.computeSettingsSig();
    } else if (this.view === "archive") {
      titleEl.textContent = t("workspace.archive");
      body.innerHTML = this.renderArchive();
      this.bindArchive(body);
    } else {
      titleEl.textContent = t("workspace.manageTitle");
      body.innerHTML = this.renderList();
      this.bindList(body);
    }
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: body });
    }
  }

  private renderList(): string {
    const workspaces = getState().workspaces;
    if (workspaces.length === 0) {
      return `<div class="si-empty-center"><i data-lucide="folder-open" class="lucide"></i>
        <span class="si-empty-title">${t("workspace.noWorkspaces")}</span>
        <span class="si-empty-sub">${t("workspace.empty")}</span></div>`;
    }
    return `<div class="workspace-mgr-list">${workspaces.map((w) => this.renderListItem(w)).join("")}</div>`;
  }

  private renderListItem(w: WorkspaceEntry): string {
    const active = w.path === getState().activeWorkspace;
    const initial = (w.name.trim()[0] || "?").toUpperCase();
    const iconInner = w.icon_data
      ? `<img class="workspace-mgr-icon-img" src="${w.icon_data}" alt="" draggable="false">`
      : `<span class="workspace-mgr-icon-letter">${this.esc(initial)}</span>`;
    const bgAttr = w.icon_data ? "" : ` style="background:hsl(${nameHue(w.name)}, 62%, 46%)"`;
    return `<div class="workspace-mgr-item${active ? " active" : ""}" data-path="${this.esc(w.path)}">
      <div class="workspace-mgr-item-icon"${bgAttr}>${iconInner}</div>
      <span class="workspace-mgr-item-name">${this.esc(w.name)}</span>
      <span class="workspace-mgr-item-count">${t("workspace.sessions", { count: w.session_count ?? 0 })}</span>
    </div>`;
  }

  private bindList(container: HTMLElement): void {
    container.querySelectorAll(".workspace-mgr-item").forEach((el) => {
      const item = el as HTMLElement;
      const path = item.dataset.path || "";
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!path) return;
        // Left-click drills into the workspace's details view.
        this.view = "details";
        this.editingPath = path;
        this.renderBody();
      });
      item.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.showItemMenu(path, e.clientX, e.clientY, item);
      });
    });
  }

  private showItemMenu(path: string, x: number, y: number, target?: HTMLElement | null): void {
    if (!this.contextMenu) return;
    renderContextMenuItems(
      [
        {
          id: "wsmgr-ctx-settings",
          icon: "settings",
          label: t("workspace.settingsTitle"),
          onClick: () => {
            this.view = "settings";
            this.editingPath = path;
            this.renderBody();
          },
        },
        ...pluginMenuSpecs("workspace_manager", { path }),
        {
          id: "wsmgr-ctx-delete",
          icon: "trash-2",
          danger: true,
          label: t("workspace.delete"),
          onClick: () => void this.handleDelete(path),
        },
      ],
      { container: this.contextMenu },
    );
    showContextMenu(this.contextMenu, x, y, { target });
  }

  private hideContextMenu(): void {
    hideAnimated(this.contextMenu);
  }

  private renderDetails(w: WorkspaceEntry): string {
    const createdAt = w.created_at ? new Date(w.created_at * 1000).toLocaleString() : "";
    return `<div class="workspace-mgr-settings">
      <div class="workspace-mgr-form-row"><span class="workspace-mgr-form-label">${t("workspace.workspaceName")}</span><span class="workspace-mgr-form-value">${this.esc(w.name)}</span></div>
      <div class="workspace-mgr-form-row"><span class="workspace-mgr-form-label">${t("workspace.path")}</span><span class="workspace-mgr-form-value workspace-mgr-form-value-path">${this.esc(w.path)}</span></div>
      <div class="workspace-mgr-form-row"><span class="workspace-mgr-form-label">${t("workspace.sessionCount")}</span><span class="workspace-mgr-form-value">${w.session_count ?? 0}</span></div>
      <div class="workspace-mgr-form-row"><span class="workspace-mgr-form-label">${t("workspace.workspaceId")}</span><span class="workspace-mgr-form-value">${this.esc(w.id)}</span></div>
      ${createdAt ? `<div class="workspace-mgr-form-row"><span class="workspace-mgr-form-label">${t("workspace.createdAt")}</span><span class="workspace-mgr-form-value">${this.esc(createdAt)}</span></div>` : ""}
    </div>`;
  }

  private renderSettings(w: WorkspaceEntry, prior?: SettingsFormState | null): string {
    const initial = (w.name.trim()[0] || "?").toUpperCase();
    const iconInner = w.icon_data
      ? `<img class="workspace-mgr-icon-img" src="${w.icon_data}" alt="" draggable="false">`
      : `<span class="workspace-mgr-icon-letter">${this.esc(initial)}</span>`;
    const bgAttr = w.icon_data ? "" : ` style="background:hsl(${nameHue(w.name)}, 62%, 46%)"`;
    // Pull the per-workspace config (target models, etc.) once; the
    // workspace_config response re-renders this view with the saved values.
    const cached = getState().workspaceConfigs[w.path];
    if (!cached && !this.wsConfigPending.has(w.path)) {
      this.wsConfigPending.add(w.path);
      send({ type: "get_workspace_config", path: w.path });
    }
    const savedSelected = Array.isArray(cached?.models) ? (cached.models as string[]) : [];
    // The enable toggle is persisted separately from the picked model ids so
    // that switching it on (even before any model is picked) survives the
    // round-trip re-render instead of snapping back to off.
    const savedEnabled = cached?.models_enabled === true || (cached?.models_enabled == null && savedSelected.length > 0);
    // Prefer the user's in-progress edits over the saved values when the view
    // is being re-rendered underneath them.
    const selected = prior ? prior.models : savedSelected;
    const modelSelEnabled = prior ? prior.modelToggle : savedEnabled;
    // Context-file toggles (AGENTS.md / CLAUDE.md), same card style as the
    // multimodal toggle in model config. A file only shows a toggle when it
    // exists in the workspace; it is enabled by default.
    const filesNow = getState().workspaceFiles[w.path] || {};
    const ctxConfig = (cached?.context_files as Record<string, boolean> | undefined) || {};
    const ctxToggles = ["AGENTS.md", "CLAUDE.md"]
      .filter((nm) => filesNow[nm])
      .map((nm) => {
        const enabled = prior && nm in prior.ctx ? prior.ctx[nm] : ctxConfig[nm] !== false;
        return `<div class="model-form-thinking-card" data-ctx-card="${this.esc(nm)}">
      <div class="model-form-thinking-info">
        <div class="model-form-thinking-title">${this.esc(nm)}</div>
        <div class="model-form-thinking-desc">${t("workspace.contextFileHint")}</div>
      </div>
      <label class="toggle-switch">
        <input type="checkbox" class="wsmgr-ctx-toggle" data-ctx-file="${this.esc(nm)}" ${enabled ? "checked" : ""} />
        <span class="toggle-slider"></span>
      </label>
    </div>`;
      })
      .join("");
    return `<div class="workspace-mgr-settings">
      <div class="workspace-mgr-icon-row">
        <div class="workspace-mgr-icon-wrap">
          <div class="workspace-mgr-icon"${bgAttr}>${iconInner}</div>
          <button class="workspace-mgr-icon-upload" id="wsmgr-icon-upload" title="${t("workspace.uploadIcon")}"><i data-lucide="image-plus" class="lucide"></i></button>
        </div>
      </div>
      <div class="model-form-row">
        <label class="model-form-label">${t("workspace.workspaceName")}</label>
        <input type="text" class="model-form-input" id="wsmgr-name-input" value="${this.esc(prior ? prior.name : w.name)}" />
      </div>
      ${ctxToggles}
      ${renderTargetModelSelection({
        id: w.id,
        models: getState().modelConfigs || [],
        selected,
        checked: modelSelEnabled,
        disabled: getState().settings.model_pool_enabled === false,
        t,
        titleKey: "workspace.modelSelectionTitle",
        hintKey: "workspace.modelSelectionHint",
      })}
    </div>`;
  }


  /**
   * Snapshot the user's live, unsaved settings-form values from the DOM.
   * Returns null when the settings form is not currently on screen (first
   * render after navigating in), so the fresh render can use saved values.
   */
  private captureSettingsFormState(body: HTMLElement, w: WorkspaceEntry): SettingsFormState | null {
    const nameInput = body.querySelector<HTMLInputElement>("#wsmgr-name-input");
    if (!nameInput) return null;
    const ctx: Record<string, boolean> = {};
    body.querySelectorAll<HTMLInputElement>(".wsmgr-ctx-toggle").forEach((cb) => {
      const fname = cb.dataset.ctxFile;
      if (fname) ctx[fname] = cb.checked;
    });
    return {
      name: nameInput.value,
      ctx,
      modelToggle: readTargetModelSelectionEnabled(body, w.id),
      models: readTargetModelSelection(body, w.id),
    };
  }

  /**
   * Signature of the state slices the settings view depends on. When it is
   * unchanged, a state update is irrelevant to the open form and the view is
   * left untouched (preserving focus and unsaved edits).
   */
  private computeSettingsSig(): string {
    const st = getState();
    const ws = st.workspaces.find((x) => x.path === this.editingPath);
    const models = (st.modelConfigs || [])
      .map((m) => `${m.model_id}:${m.enabled === false ? 0 : 1}`)
      .join(",");
    return JSON.stringify([
      ws ? ws.name : "",
      ws ? (ws.icon_data || "").length : 0,
      st.workspaceConfigs[this.editingPath] ?? null,
      st.workspaceFiles[this.editingPath] ?? null,
      models,
    ]);
  }

  private bindSettings(container: HTMLElement, w: WorkspaceEntry): void {
    // Rename-while-editing: Enter in the name field is an implicit save.
    const nameInput = container.querySelector("#wsmgr-name-input") as HTMLInputElement | null;
    nameInput?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        this.saveWorkspaceSettings(container, w);
      }
    });

    container.querySelector("#wsmgr-icon-upload")?.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!this.editingPath) return;
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/*";
      input.addEventListener("change", () => {
        const file = input.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
          if (typeof reader.result === "string" && reader.result.startsWith("data:image/")) {
            send({ type: "upload_workspace_icon", path: this.editingPath, icon_data: reader.result });
          }
        };
        reader.readAsDataURL(file);
      });
      input.click();
    });

    // Target-model selection: wire up the widget, but do NOT persist on
    // change — the Save button collects the final toggle + selection state.
    bindTargetModelSelection(container, w.id, () => {});
  }

  /** Fixed footer for the settings view (mirrors the model-edit dialog). */
  private renderSettingsFooter(container: HTMLElement, w: WorkspaceEntry): void {
    const footer = this.overlay?.querySelector<HTMLElement>("#wsmgr-footer");
    if (!footer) return;
    footer.style.display = "flex";
    footer.innerHTML = `
      <button class="btn" id="wsmgr-settings-cancel">${this.esc(t("common.cancel"))}</button>
      <button class="btn btn--primary" id="wsmgr-settings-save">${this.esc(t("common.save"))}</button>`;
    footer.querySelector("#wsmgr-settings-cancel")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.backToWorkspaceList();
    });
    footer.querySelector("#wsmgr-settings-save")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.saveWorkspaceSettings(container, w);
      this.backToWorkspaceList();
    });
  }

  /** Persist the staged settings, then return to the workspace home list. */
  private saveWorkspaceSettings(container: HTMLElement, w: WorkspaceEntry): void {
    if (!this.editingPath) return;
    const nameInput = container.querySelector<HTMLInputElement>("#wsmgr-name-input");
    const newName = nameInput?.value.trim() || "";
    if (newName && newName !== w.name) {
      send({ type: "rename_workspace", path: this.editingPath, name: newName });
    }
    const on = readTargetModelSelectionEnabled(container, w.id);
    const selected = readTargetModelSelection(container, w.id);
    send({
      type: "save_workspace_config",
      path: this.editingPath,
      config: { models: selected.length ? selected : null, models_enabled: on ? true : false },
    });
    const current: Record<string, boolean> = {};
    container.querySelectorAll<HTMLInputElement>(".wsmgr-ctx-toggle").forEach((cb) => {
      const fname = cb.dataset.ctxFile;
      if (fname) current[fname] = cb.checked;
    });
    send({
      type: "save_workspace_config",
      path: this.editingPath,
      config: { context_files: current },
    });
  }

  /** Navigate back from a sub-view (settings/archive/details) to the list. */
  private backToWorkspaceList(): void {
    this.view = "list";
    this.editingPath = "";
    this.renderBody();
  }

  /**
   * Archive manager view.
   *
   * Lists every archived conversation (not workspaces) using the exact same
   * row UI as the sidebar session list ("全部任务" rows): the same
   * `ws-tree-session-item` / `session-item-top` / `session-preview` markup
   * and styles. Each row carries two trailing actions — restore (移出归档)
   * and delete (identical to deleting a normal conversation) — plus a
   * right-click context menu offering the same two actions.
   */
  private renderArchive(): string {
    const archived = getState().archivedSessions;
    if (archived.length === 0) {
      return `<div class="si-empty-center"><i data-lucide="archive" class="lucide"></i>
        <span class="si-empty-title">${t("workspace.archiveEmpty")}</span></div>`;
    }
    return `<div class="workspace-mgr-list">${archived.map((s) => this.renderArchiveItem(s)).join("")}</div>`;
  }

  private renderArchiveItem(s: SessionEntryData): string {
    const preview = s.preview || t("general.emptySessionName");
    const displayName = s.name || preview;
    // Archived conversations are workspace sessions — always show the owning
    // workspace icon (forced, like the sidebar history rows).
    const wsPath = (s.metadata as any)?.workspace_path || (s.metadata as any)?.workspace || "";
    const wsIcon = workspaceIconHtml(wsPath, getState().workspaces, true);
    return `<div class="ws-tree-session-item" data-sid="${this.esc(s.session_id)}">
      <div class="session-item-top">
        ${wsIcon}
        <span class="session-preview">${this.esc(displayName)}</span>
        <span class="archive-item-actions">
          <button class="btn-icon btn-sm archive-action-restore" title="${t("workspace.unarchive")}"><i data-lucide="archive-restore" class="lucide"></i></button>
          <button class="btn-icon btn-sm archive-action-delete" title="${t("workspace.delete")}"><i data-lucide="trash-2" class="lucide"></i></button>
        </span>
      </div>
    </div>`;
  }

  private bindArchive(container: HTMLElement): void {
    container.querySelectorAll(".ws-tree-session-item").forEach((el) => {
      const item = el as HTMLElement;
      const sid = item.dataset.sid || "";
      if (!sid) return;
      item.querySelector(".archive-action-restore")?.addEventListener("click", (e) => {
        e.stopPropagation();
        void this.handleUnarchive(sid);
      });
      item.querySelector(".archive-action-delete")?.addEventListener("click", (e) => {
        e.stopPropagation();
        void this.handleArchiveDelete(sid);
      });
      item.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.showArchiveMenu(sid, e.clientX, e.clientY, item);
      });
    });
  }

  /** Context menu for an archived conversation: restore + delete. */
  private showArchiveMenu(sid: string, x: number, y: number, target?: HTMLElement | null): void {
    if (!this.contextMenu) return;
    renderContextMenuItems(
      [
        {
          id: "wsmgr-ctx-unarchive",
          icon: "archive-restore",
          label: t("workspace.unarchive"),
          onClick: () => void this.handleUnarchive(sid),
        },
        ...pluginMenuSpecs("archived_sessions", { sid }),
        { divider: true },
        {
          id: "wsmgr-ctx-delete-archived",
          icon: "trash-2",
          danger: true,
          label: t("workspace.delete"),
          onClick: () => void this.handleArchiveDelete(sid),
        },
      ],
      { container: this.contextMenu },
    );
    showContextMenu(this.contextMenu, x, y, { target });
  }

  /** Restore an archived conversation back into the workspace history —
   *  with the same confirmation UX as archiving/deleting. */
  private async handleUnarchive(sid: string): Promise<void> {
    const s = getState().archivedSessions.find((x) => x.session_id === sid);
    const name = s?.name || s?.preview || sid.slice(0, 8);
    if (!(await Dialog.confirm(t("workspace.confirmUnarchiveTitle"), t("workspace.confirmUnarchive", { name }), { scope: "app" }))) return;
    // Optimistically drop the row from the archive view; the backend
    // broadcast re-inserts the session into the workspace history list.
    removeArchivedSessionById(sid);
    send({ type: "unarchive_session", session_id: sid });
  }

  /** Delete an archived conversation — identical to deleting a normal one:
   *  same confirmation copy, same delete_session message, optimistic removal. */
  private async handleArchiveDelete(sid: string): Promise<void> {
    const s = getState().archivedSessions.find((x) => x.session_id === sid);
    const name = s?.name || s?.preview || sid.slice(0, 8);
    if (!(await Dialog.confirm(t("session.confirmDeleteTitle"), t("session.confirmDelete", { name }), { scope: "app" }))) return;
    // Optimistically drop the row from the archive view.
    removeArchivedSessionById(sid);
    // If the deleted session is the one currently being viewed, clear the
    // chat area to show the "new task" welcome screen immediately.
    if (getState().sessionId === sid) {
      clearMessages();
      setSessionId("");
      setSessionState("idle", "");
    }
    send({ type: "delete_session", session_id: sid });
  }

  private async handleDelete(path: string): Promise<void> {
    const ws = getState().workspaces.find((w) => w.path === path);
    const name = ws?.name || path;
    const ok = await Dialog.confirm(t("workspace.confirmDeleteOneTitle"), t("workspace.confirmDeleteOne", { name }), { scope: "app" });
    if (!ok) return;
    send({ type: "remove_workspace", path });
    // Backend replies workspace_removed; the state subscription refreshes the list.
  }

  private handleNewWorkspace(): void {
    void this.workspace.openFolder();
  }

  private esc(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }
}
