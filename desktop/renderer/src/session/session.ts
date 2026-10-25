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
 * Session sidebar & management.
 *
 * Renders the conversation session list in the sidebar and provides
 * rename/export/delete via a context menu and a rename dialog. Also supports a
 * batch-selection mode for bulk export/delete. The standalone helpers
 * `showSessionContextMenu` / `showRenameDialogForSession` reuse the same DOM
 * overlays for sessions shown inside the workspace tree.
 */

import { getState, subscribe, setSessionId, clearMessages, setSessionState, setSubAgentView, clearSubAgentBreadcrumb, setTempChat, markTempChatSession, removeSessionById, setActiveWorkspace } from "../core/state.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { guardHtml, guardRender } from "../core/ui_boundary.js";
import { send } from "../core/ws.js";
import { pluginMenuSpecs } from "../features/plugin_contributions.js";
import { renderSlot } from "../core/ui_slots.js";
import { setRequestedSessionId } from "../core/stream.js";
import { t, onLocaleChange } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";
import { showContextMenu, hideContextMenu, renderContextMenuItems, renderMenu, type ContextMenuItemSpec } from "../ui/context-menu.js";
import { nameHue, workspaceIconHtml } from "../chat/session-projection.js";
import type { SessionEntryData, SessionSource, WorkspaceEntry } from "../core/types.js";

/** Lightweight signature of the workspace list for change detection — avoids
 *  stringifying the full icon base64 blobs on every state emit.  The icon's
 *  trailing chars change whenever an icon is uploaded/replaced. */
function workspacesSig(ws: readonly WorkspaceEntry[]): string {
  return ws.map((w) => `${w.path}:${(w.icon_data || "").slice(-40)}`).join("|");
}

/**
 * Renders and manages the sidebar session list.
 */
export class Session {
  private el: HTMLElement;
  private contextMenuEl: HTMLElement;
  private contextTargetSid: string = "";
  private contextTargetEl: HTMLElement | null = null;
  private batchMode: boolean = false;
  private selectedIds: Set<string> = new Set();
  private batchBar: HTMLElement;
  /** Per-session-id cache of the last rendered row markup. Lets render()
   *  skip DOM writes for rows that did not change, so sidebar rows are reused
   *  instead of being rebuilt (which previously flickered during streaming). */
  private rowCache: Map<string, string> = new Map();
  /** How many sessions of the (recency-sorted) list are in the DOM. The
   *  sidebar used to materialize every session on first paint; a large
   *  history meant thousands of rows created in one synchronous task. The
   *  active session is always included, and scrolling near the bottom
   *  extends the window. */
  private renderLimit = 80;
  private static readonly RENDER_PAGE = 80;
  private scrollRaf = 0;
  /**
   * Constructor: grabs sidebar DOM nodes and subscribes to state/locale changes.
   */
  constructor() {
    this.el = document.getElementById("session-list")!;
    this.contextMenuEl = document.getElementById("session-context-menu")!;
    this.batchBar = document.getElementById("batch-action-bar")!;
    let lastSid = "";
    let lastListRef = getState().sessionsList;
    let lastWs = getState().activeWorkspace;
    let lastWsMode = getState().workspaceMode;
    // Workspace icon data lives in state.workspaces, NOT in sessionsList — so
    // a late-arriving workspaces_list (or an icon upload) must also re-render
    // the rows, otherwise every row rendered before the icons arrived stays
    // icon-less forever (only the row that later gets patched by a state
    // change shows its icon).
    let lastWsListSig = workspacesSig(getState().workspaces);
    subscribe(() => {
      const st = getState();
      const listChanged = st.sessionsList !== lastListRef;
      const wsListSig = workspacesSig(st.workspaces);
      const wsListChanged = wsListSig !== lastWsListSig;
      const sidChanged = st.sessionId !== lastSid;
      // The iWork sidebar filters by the ACTIVE workspace, so a workspace
      // switch (even a local setActiveWorkspace sync with no backend round
      // trip) must re-render; otherwise the list keeps showing the previous
      // workspace's sessions.
      const wsChanged = st.activeWorkspace !== lastWs || st.workspaceMode !== lastWsMode;
      if (listChanged || sidChanged || wsChanged || wsListChanged) {
        lastListRef = st.sessionsList;
        lastWsListSig = wsListSig;
        lastSid = st.sessionId;
        lastWs = st.activeWorkspace;
        lastWsMode = st.workspaceMode;
        // A different workspace/channel is a different list; start its
        // window small again instead of carrying an expanded one over.
        if (wsChanged) this.renderLimit = 80;
        this.requestRender();
      }
    }, ["sessionsList", "workspaces", "sessionId", "activeWorkspace", "workspaceMode"]);
    // Extend the rendered window as the user approaches the bottom. Rows are
    // reconciled by id, so this only appends; existing scroll position and
    // row nodes are untouched.
    this.el.addEventListener("scroll", () => {
      if (this.scrollRaf) return;
      this.scrollRaf = requestAnimationFrame(() => {
        this.scrollRaf = 0;
        const nearBottom = this.el.scrollTop + this.el.clientHeight >= this.el.scrollHeight - 240;
        if (!nearBottom) return;
        if (this.renderLimit >= getState().sessionsList.length) return;
        this.renderLimit += Session.RENDER_PAGE;
        this.requestRender();
      });
    }, { passive: true });
    onLocaleChange(() => {
      this.requestRender();
      this.updateBatchBarLabels();
    });
    document.addEventListener("click", () => this.hideContextMenu());
    this.bindBatchBar();
    this.bindEventDelegation();
  }

  private bindBatchBar(): void {
    document.getElementById("btn-batch-manage")?.addEventListener("click", () => this.toggleBatchMode());
    document.getElementById("btn-batch-select-all")?.addEventListener("click", () => this.batchSelectAll());
    document.getElementById("btn-batch-export")?.addEventListener("click", () => this.batchExport());
    document.getElementById("btn-batch-delete")?.addEventListener("click", () => this.batchDelete());
    document.getElementById("btn-batch-cancel")?.addEventListener("click", () => this.exitBatchMode());
  }

  private updateBatchBarLabels(): void {
    const selAll = document.getElementById("btn-batch-select-all");
    const exp = document.getElementById("btn-batch-export");
    const del = document.getElementById("btn-batch-delete");
    const cancel = document.getElementById("btn-batch-cancel");
    if (selAll) selAll.dataset.tooltip = t("session.batchSelectAll");
    if (exp) exp.dataset.tooltip = t("session.batchExport");
    if (del) del.dataset.tooltip = t("session.batchDelete");
    if (cancel) cancel.dataset.tooltip = t("session.cancel");
  }

  private toggleBatchMode(): void {
    if (this.batchMode) {
      this.exitBatchMode();
    } else {
      this.enterBatchMode();
    }
  }

  private enterBatchMode(): void {
    const sessions = getState().sessionsList;
    if (sessions.length === 0) return;
    this.batchMode = true;
    this.selectedIds.clear();
    const manageBtn = document.getElementById("btn-batch-manage");
    if (manageBtn) manageBtn.style.display = "none";
    this.batchBar.classList.remove("hidden");
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.batchBar });
    }
    this.requestRender();
  }

  private exitBatchMode(): void {
    this.batchMode = false;
    this.selectedIds.clear();
    const manageBtn = document.getElementById("btn-batch-manage");
    if (manageBtn) manageBtn.style.display = "";
    this.batchBar.classList.add("hidden");
    this.requestRender();
  }

  /** Flips one row's selection in place.  Never calls render(): rebuilding the
   *  row would replay its fade-in, and `rowCache` diffs on the `checked`
   *  attribute while the live value lives on the `checked` property. */
  private toggleSelection(sid: string, row: HTMLElement): void {
    const checked = !this.selectedIds.has(sid);
    if (checked) this.selectedIds.add(sid);
    else this.selectedIds.delete(sid);
    const cb = row.querySelector<HTMLInputElement>(".session-checkbox");
    if (cb) cb.checked = checked;
  }

  private batchSelectAll(): void {
    const sessions = getState().sessionsList;
    if (this.selectedIds.size === sessions.length) {
      this.selectedIds.clear();
    } else {
      for (const s of sessions) {
        this.selectedIds.add(s.session_id);
      }
    }
    this.requestRender();
  }

  private async batchExport(): Promise<void> {
    const ids = Array.from(this.selectedIds);
    if (ids.length === 0) return;
    if (ids.length === 1) {
      send({ type: "export_session", session_id: ids[0] });
    } else {
      send({ type: "export_sessions_batch", session_ids: ids });
    }
    this.exitBatchMode();
  }

  private batchDelete(): void {
    const ids = Array.from(this.selectedIds);
    if (ids.length === 0) return;
    for (const sid of ids) {
      send({ type: "delete_session", session_id: sid });
    }
    this.exitBatchMode();
  }

  /** Renders the session list (or empty state) and (re)binds all interactions.
   *
   *  Rows are reconciled in place by session id: unchanged rows keep their DOM
   *  node (and therefore scroll position / hover / transition state), changed
   *  rows are rebuilt individually, and reordering is done by moving existing
   *  nodes.  This avoids the whole-list `innerHTML` teardown that previously
   *  made the sidebar flash while the model streamed or on session switches. */
  /** Coalesced paint: any number of pushes in one frame produce one render. */
  private requestRender(): void {
    scheduleRender("sidebar", () => this.render(), 20);
  }

  render(): void {
    const st = getState();
    const sessions = st.sessionsList;
    const currentSid = st.sessionId;

    // The backend scopes sessions_list to the current context (channel and,
    // in iwork mode, the active workspace), so the sidebar renders exactly
    // what it receives.  Only defence left here: temp-chat sessions are
    // ephemeral and must never appear in the list.
    const filteredSessions = sessions.filter(
      s => !(s.metadata as Record<string, unknown>)?.temp_chat,
    );

    const emptyHtml = `<div class="session-empty si-empty-center">
      <i data-lucide="message-square" class="lucide"></i>
      <span class="si-empty-title">${t("session.noHistory")}</span>
    </div>`;

    if (filteredSessions.length === 0) {
      this.rowCache.clear();
      if (this.el.innerHTML !== emptyHtml) {
        this.el.innerHTML = emptyHtml;
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: this.el });
        }
      }
      if (this.batchMode) this.exitBatchMode();
      return;
    }

    // Transitioning from an empty state → drop the empty-state markup once.
    if (this.el.querySelector(".session-empty")) this.el.innerHTML = "";

    // Materialize only a window of the recency-sorted list. The active
    // session is always included even when it sits outside the window.
    const activeIndex = filteredSessions.findIndex((s) => s.session_id === currentSid);
    const limit = Math.max(this.renderLimit, activeIndex + 1);
    const visibleSessions = filteredSessions.length > limit
      ? filteredSessions.slice(0, limit)
      : filteredSessions;

    const desiredIds: string[] = [];
    const htmlById = new Map<string, string>();
    for (const s of visibleSessions) {
      desiredIds.push(s.session_id);
      // One malformed/plugin-overridden row must render as a minimal row, not
      // abort the whole sidebar paint.
      htmlById.set(
        s.session_id,
        guardHtml(
          "session-row",
          s.session_id,
          () => this.rowHtml(s, s.session_id === currentSid),
          this.faultRowHtml(s),
        ),
      );
    }
    const desiredSet = new Set(desiredIds);
    const allIds = new Set(filteredSessions.map((s) => s.session_id));

    // Index existing rows by session id.
    const rowsById = new Map<string, HTMLElement>();
    for (const row of Array.from(this.el.children) as HTMLElement[]) {
      const sid = row.dataset.sid;
      if (sid && row.classList.contains("ws-tree-session-item")) rowsById.set(sid, row);
    }

    // Drop rows outside the window; keep their cached markup so scrolling
    // back repopulates them without rebuilding every row.
    for (const [sid, row] of rowsById) {
      if (desiredSet.has(sid)) continue;
      row.remove();
      rowsById.delete(sid);
      if (!allIds.has(sid)) this.rowCache.delete(sid);
    }

    // Create/update rows in place and keep them in the desired order.
    // Unchanged rows are left completely untouched, so the sidebar does not
    // flash while the model streams.
    let cursor: HTMLElement | null = null;
    for (const sid of desiredIds) {
      const html = htmlById.get(sid)!;
      let row = rowsById.get(sid);
      if (!row) {
        const tmp = document.createElement("div");
        tmp.innerHTML = html;
        row = tmp.firstElementChild as HTMLElement;
        if (!row) {
          // A plugin-provided row override can return anything (or nothing).
          // Keep a valid anchor so reconciliation never dereferences null.
          row = document.createElement("div");
          row.className = "ws-tree-session-item";
          row.dataset.sid = sid;
          row.textContent = sid;
        }
        rowsById.set(sid, row);
        this.rowCache.set(sid, html);
      } else if (this.rowCache.get(sid) !== html) {
        // Patch the existing row in place rather than swapping in a brand-new
        // element.  Replacing the element would replay the row's one-shot
        // CSS fade-in (`.ws-tree-session-item` → `sessionItemIn`), which looks
        // like a flash every time the active/state highlight changes.
        const target = row;
        if (target) {
          guardRender("session-row-patch", sid, () => this.patchRow(target, html));
          this.rowCache.set(sid, html);
        }
      }
      // Move the row into its desired slot (no-op when already in place).
      if (cursor === null) {
        if (this.el.firstElementChild !== row) this.el.insertBefore(row, this.el.firstChild ?? null);
      } else if (cursor.nextElementSibling !== row) {
        cursor.insertAdjacentElement("afterend", row);
      }
      cursor = row;
    }

    this.updateBatchBarLabels();
  }

  /** Last-resort row when a session's own markup cannot be produced. */
  private faultRowHtml(s: SessionEntryData): string {
    const label = s.name || s.preview || s.session_id;
    return `<div class="ws-tree-session-item" data-sid="${s.session_id}">
      <div class="session-item-top"><span class="session-preview">${this.esc(label)}</span></div>
    </div>`;
  }

  /** Builds a single session row's markup for the current (batch/normal) mode. */
  private rowHtml(s: SessionEntryData, active: boolean): string {
    const preview = s.preview || t("general.emptySessionName");
    const displayName = s.name || preview;
    const runningBadge = s.state && s.state !== "idle"
      ? `<span class="${s.state === "awaiting_approval" ? "session-waiting" : "session-running"}"></span>`
      : "";
    const wsPathRaw = (s.metadata as any)?.workspace_path || (s.metadata as any)?.workspace || "";
    const wsPath = this.esc(wsPathRaw);
    // Each iWork history row carries the icon of the workspace it belongs to —
    // forced on for workspace-channel sessions so it NEVER disappears.
    const wsIcon = this.wsIconHtml(wsPathRaw, s.channel === "iwork");
    const batchMode = this.batchMode;
    // Generic plugin override point ("session.row"): a plugin may replace the
    // row markup declaratively or via the JS runtime; the markup below is the
    // guarded fallback and MUST stay functionally identical to it (same
    // data-sid / checkbox / classes) for event delegation to keep working.
    return renderSlot(
      "session.row",
      { sid: s.session_id, name: displayName, preview, state: s.state || "idle", workspace: wsPathRaw, source: s.source?.platform || "" },
      () => {
        if (batchMode) {
          return `<div class="ws-tree-session-item" data-sid="${s.session_id}" data-ws="${wsPath}">
        <div class="session-item-top">
          ${wsIcon}
          <input type="checkbox" class="session-checkbox" data-sid="${s.session_id}" ${this.selectedIds.has(s.session_id) ? "checked" : ""} />
          <span class="session-preview">${this.esc(displayName)}</span>
          ${runningBadge}${this.sourceBadge(s.source)}
        </div>
      </div>`;
        }
        return `<div class="ws-tree-session-item${active ? " active" : ""}" data-sid="${s.session_id}" data-ws="${wsPath}">
      <div class="session-item-top">
        ${wsIcon}
        <span class="session-preview">${this.esc(displayName)}</span>
        ${runningBadge}${this.sourceBadge(s.source)}
      </div>
    </div>`;
      },
    );
  }

  /** Renders the owning workspace's icon for a session row.  iWork rows are
   *  FORCED to always show an icon (neutral avatar when the workspace path
   *  is missing) so it never disappears — shared with the workspace manager
   *  archive view. */
  private wsIconHtml(wsPath: string, force: boolean): string {
    return workspaceIconHtml(wsPath, getState().workspaces, force);
  }

  /** Syncs an existing row element's markup in place so the row's DOM node is
   *  preserved.  Keeping the element avoids restarting its fade-in animation
   *  and drops any transient state (hover highlight, scroll anchoring). */
  private patchRow(row: HTMLElement, html: string): void {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    const fresh = tmp.firstElementChild as HTMLElement;
    for (const attr of Array.from(fresh.attributes)) {
      if (row.getAttribute(attr.name) !== attr.value) row.setAttribute(attr.name, attr.value);
    }
    for (const attr of Array.from(row.attributes)) {
      if (!fresh.hasAttribute(attr.name)) row.removeAttribute(attr.name);
    }
    row.replaceChildren(...Array.from(fresh.childNodes));
  }

  /** Platform-origin badge for adapter-sourced sessions (Phase 5).
   *
   *  Renders a small pill showing the originating platform (and chat type)
   *  when the session carries a structured {@link SessionSource}.  Returns an
   *  empty string for desktop/normal sessions (no source) -- preserving the
   *  prior badgeless appearance.  Aligns with Hermes' platform_hint projection
   *  so a user can see at a glance which IM platform a conversation came from.
   */
  private sourceBadge(source?: SessionSource): string {
    if (!source || !source.platform) return "";
    const platformLabels: Record<string, string> = {
      qqbot: "QQ", telegram: "Telegram", webhook: "Webhook", discord: "Discord",
      slack: "Slack", feishu: "Feishu", dingtalk: "DingTalk", wecom: "WeCom",
      weixin: "WeChat", whatsapp: "WhatsApp", signal: "Signal", matrix: "Matrix",
      email: "Email", sms: "SMS",
      bluebubbles: "iMessage", homeassistant: "HomeAssistant",
      google_chat: "GoogleChat", irc: "IRC", line: "LINE",
      mattermost: "Mattermost", ntfy: "ntfy", photon: "Photon",
      raft: "Raft", simplex: "SimpleX", teams: "Teams",
    };
    const chatTypeLabels: Record<string, string> = {
      dm: "DM", group: "Group", channel: "Channel", thread: "Thread", forum: "Forum",
    };
    const plat = platformLabels[source.platform] || source.platform;
    const ct = source.chat_type ? (chatTypeLabels[source.chat_type] || source.chat_type) : "";
    const label = ct ? `${plat} · ${ct}` : plat;
    return `<span class="session-source-badge" data-platform="${this.esc(source.platform)}">${this.esc(label)}</span>`;
  }

  /** Binds row interactions once through event delegation, so reused row
   *  elements never accumulate duplicate listeners across renders. */
  private bindEventDelegation(): void {
    this.el.addEventListener("click", (e) => {
      const target = (e.target as HTMLElement).closest(".ws-tree-session-item") as HTMLElement | null;
      if (!target) return;
      const sid = target.dataset.sid;
      if (!sid) return;
      if (this.batchMode) {
        // The whole row toggles the selection, not just the 15px box. A click
        // that landed on the box itself already flipped `input.checked`, so it
        // is left to the `change` listener below to avoid double-flipping.
        if ((e.target as HTMLElement).tagName !== "INPUT") this.toggleSelection(sid, target);
        return;
      }
      if ((e.target as HTMLElement).tagName === "INPUT") return;
      if (sid !== getState().sessionId) {
        // Wipe the entire content area BEFORE flipping the session id so
        // every residual widget from the previous session is gone.  The
        // (window as any).__appCleanupContentArea() bridge is set by app.ts
        // at construction time; it nukes sub-agent view, tool detail
        // panel, mention dropdown, queue card, mode chip, attachments,
        // session-inner sidebar (and its terminal/editor tabs),
        // automation view, child view, and forces the welcome screen
        // back to its initial state.
        const cleanup = (window as any).__appCleanupContentArea as
          | ((opts?: { keepAutomationFlag?: boolean }) => void)
          | undefined;
        cleanup?.({ keepAutomationFlag: false });

        // Clear any active sub-agent view overlay (including automation
        // sub-agent views) when explicitly switching to a different session.
        setSubAgentView(null);
        clearSubAgentBreadcrumb();
        (window as any).__isAutomationView = false;
        (window as any).__activeAutomationJobId = "";
        const requestId = crypto.randomUUID();
        // Clean up temp chat before switching to another session
        if (getState().tempChat && getState().sessionId) {
          const oldSid = getState().sessionId;
          setTempChat(false);
          markTempChatSession(oldSid);
          removeSessionById(oldSid);
          send({ type: "delete_session", session_id: oldSid });
        }
        // In iWork mode the flat list mixes every workspace's sessions, so
        // the clicked session may live in a different workspace than the
        // active one. Sync the local active-workspace pointer (state only,
        // no backend round-trip) so the workspace selector and session
        // context stay consistent with the session being opened.
        const wsMeta = target.dataset.ws;
        if (getState().workspaceMode === "iwork" && wsMeta && wsMeta !== getState().activeWorkspace) {
          setActiveWorkspace(wsMeta);
        }
        setSessionId(sid);
        setRequestedSessionId(sid, requestId);
        send({ type: "resume", session_id: sid, request_id: requestId });
      }
    });

    this.el.addEventListener("contextmenu", (e) => {
      if (this.batchMode) return;
      const target = (e.target as HTMLElement).closest(".ws-tree-session-item") as HTMLElement | null;
      if (!target) return;
      e.preventDefault();
      const sid = target.dataset.sid;
      if (!sid) return;
      this.contextTargetSid = sid;
      this.contextTargetEl = target;
      this.showContextMenu(e.clientX, e.clientY);
    });

    this.el.addEventListener("change", (e) => {
      const cb = e.target as HTMLInputElement;
      if (!cb.classList.contains("session-checkbox")) return;
      const sid = cb.dataset.sid;
      if (!sid) return;
      if (cb.checked) {
        this.selectedIds.add(sid);
      } else {
        this.selectedIds.delete(sid);
      }
    });
  }

  private showContextMenu(x: number, y: number): void {
    // Archiving is a workspace (iWork) concept — it only appears in the
    // workspace-mode sidebar, not in the normal-mode conversation list.
    const archiveItems: ContextMenuItemSpec[] =
      getState().workspaceMode === "iwork"
        ? [
            { id: "ctx-archive", icon: "archive", label: this.esc(t("session.archive")), onClick: () => this.handleArchive() },
            { divider: true },
          ]
        : [];
    // Plugin-contributed items ("sessions" surface), spliced through the one
    // shared helper so escaping + the unified action round-trip live in a
    // single place.
    const pluginItems: ContextMenuItemSpec[] = pluginMenuSpecs("sessions", { sid: this.contextTargetSid });
    renderMenu(
      [
        { id: "ctx-rename", icon: "pencil", label: this.esc(t("session.rename")), onClick: () => this.handleRename() },
        { id: "ctx-export", icon: "arrow-up-right", label: this.esc(t("session.exportMd")), onClick: () => this.handleExport() },
        ...pluginItems,
        { divider: true },
        ...archiveItems,
        { id: "ctx-delete", icon: "trash-2", danger: true, label: this.esc(t("session.delete")), onClick: () => this.handleDelete() },
      ],
      {},
      { container: this.contextMenuEl },
    );
    showContextMenu(this.contextMenuEl, x, y, { target: this.contextTargetEl });
  }

  /** Archive the right-clicked conversation: hide it from the sidebar (data
   *  untouched). Asks for confirmation first (like delete), then hides the
   *  conversation optimistically; the backend broadcast refreshes both the
   *  sidebar and the archive manager view. */
  private async handleArchive(): Promise<void> {
    this.hideContextMenu();
    const sid = this.contextTargetSid;
    if (!sid) return;
    const s = getState().sessionsList.find((x) => x.session_id === sid);
    const name = s?.name || s?.preview || sid.slice(0, 8);
    if (!(await Dialog.confirm(t("session.confirmArchiveTitle"), t("session.confirmArchive", { name }), { scope: "app" }))) return;
    send({ type: "archive_session", session_id: sid });
    removeSessionById(sid);
    // If the archived session is currently open, clear the chat area so the
    // welcome screen shows again — the conversation still exists, just hidden.
    if (getState().sessionId === sid) {
      clearMessages();
      setSessionId("");
      setSessionState("idle", "");
    }
  }

  private hideContextMenu(): void {
    hideContextMenu(this.contextMenuEl);
    this.contextTargetEl = null;
  }

  private handleRename(): void {
    this.hideContextMenu();
    const sid = this.contextTargetSid;
    if (!sid) return;
    const sessions = getState().sessionsList;
    const s = sessions.find((x) => x.session_id === sid);
    const currentName = s?.name || s?.preview || "";
    Dialog.prompt(
      t("session.renameDialogTitle"),
      "",
      {
        primary: t("session.rename"),
        secondary: t("session.cancel"),
        placeholder: t("session.renamePlaceholder"),
        inputClass: "encre-dialog-input--text",
        defaultValue: currentName,
        scope: "app",
      },
    ).then((newName) => {
      const trimmed = newName?.trim();
      if (trimmed) {
        send({ type: "rename_session", session_id: sid, new_name: trimmed });
      }
    });
  }

  private handleExport(): void {
    this.hideContextMenu();
    const sid = this.contextTargetSid;
    if (!sid) return;
    send({ type: "export_session", session_id: sid });
  }

  private async handleDelete(): Promise<void> {
    this.hideContextMenu();
    const sid = this.contextTargetSid;
    if (!sid) return;
    const sessions = getState().sessionsList;
    const s = sessions.find((x) => x.session_id === sid);
    const name = s?.name || s?.preview || sid.slice(0, 8);
    if (await Dialog.confirm(t("session.confirmDeleteTitle"), t("session.confirmDelete", { name }), { scope: "app" })) {
      removeSessionById(sid);
      if (getState().sessionId === sid) {
        clearMessages();
        setSessionId("");
        setSessionState("idle", "");
      }
      send({ type: "delete_session", session_id: sid });
    }
  }

  private esc(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }
}

/**
 * Shows the session context menu (rename/export/delete) at the given coordinates.
 *
 * @param sid     - The session id the menu acts on.
 * @param x       - Screen X for menu placement.
 * @param y       - Screen Y for menu placement.
 * @param noRename - When `true`, hide the rename item (e.g. for workspace sessions).
 */
export interface RenameConfirmCallback {
  (newName: string): void;
}

/**
 * Shows a generic rename dialog and calls `onConfirm` with the trimmed new name.
 * Uses the same unified `Dialog.prompt` component used across the app.
 *
 * @param currentName - The initial value in the input field.
 * @param onConfirm   - Called when the user confirms a non-empty name.
 */
export function showRenameDialog(currentName: string, onConfirm: RenameConfirmCallback): void {
  Dialog.prompt(
    t("session.renameDialogTitle"),
    "",
    {
      primary: t("session.rename"),
      secondary: t("session.cancel"),
      placeholder: t("session.renamePlaceholder"),
      inputClass: "encre-dialog-input--text",
      defaultValue: currentName,
      scope: "app",
    },
  ).then((newName) => {
    const trimmed = newName?.trim();
    if (trimmed) {
      onConfirm(trimmed);
    }
  });
}

export function showSessionContextMenu(
  sid: string,
  x: number,
  y: number,
  noRename?: boolean,
  noExport?: boolean,
  deleteOverride?: () => void,
  renameOverride?: () => void,
  noArchive?: boolean,
  target?: HTMLElement | null,
): void {
  const menuEl = document.getElementById("session-context-menu")!;
  const currentSid = sid;

  const items: ContextMenuItemSpec[] = [];
  if (!noRename) {
    items.push({
      id: "ctx-rename-ws",
      icon: "pencil",
      label: escHtml(t("session.rename")),
      onClick: () => {
        if (renameOverride) {
          renameOverride();
          return;
        }
        const sessions = getState().sessionsList;
        const s = sessions.find((v) => v.session_id === currentSid);
        const currentName = s?.name || s?.preview || "";
        showRenameDialog(currentName, (newName) => {
          send({ type: "rename_session", session_id: currentSid, new_name: newName });
        });
      },
    });
  }
  if (!noExport) {
    items.push({
      id: "ctx-export-ws",
      icon: "arrow-up-right",
      label: escHtml(t("session.exportMd")),
      onClick: () => send({ type: "export_session", session_id: currentSid }),
    });
  }
  if (!noRename || !noExport || !noArchive) {
    items.push({ divider: true });
  }
  if (!noArchive) {
    items.push({
      id: "ctx-archive-ws",
      icon: "archive",
      label: escHtml(t("session.archive")),
      onClick: async () => {
        const s = getState().sessionsList.find((v) => v.session_id === currentSid);
        const name = s?.name || s?.preview || currentSid.slice(0, 8);
        if (!(await Dialog.confirm(t("session.confirmArchiveTitle"), t("session.confirmArchive", { name }), { scope: "app" }))) return;
        // Hide the conversation from the sidebar immediately; the backend
        // broadcast refreshes the archive manager view.
        send({ type: "archive_session", session_id: currentSid });
        removeSessionById(currentSid);
        if (getState().sessionId === currentSid) {
          clearMessages();
          setSessionId("");
          setSessionState("idle", "");
        }
      },
    });
    items.push({ divider: true });
  }
  // Plugin-contributed items ("sessions" surface) before the destructive row.
  items.push(...pluginMenuSpecs("sessions", { sid: currentSid }));
  items.push({
    id: "ctx-delete-ws",
    icon: "trash-2",
    danger: true,
    label: escHtml(t("session.delete")),
    onClick: async () => {
      if (deleteOverride) {
        deleteOverride();
        return;
      }
      const sessions = getState().sessionsList;
      const s = sessions.find((v) => v.session_id === currentSid);
      const name = s?.name || s?.preview || currentSid.slice(0, 8);
      if (await Dialog.confirm(t("session.confirmDeleteTitle"), t("session.confirmDelete", { name }), { scope: "app" })) {
        // Optimistically remove the session from the local state immediately,
        // so the UI updates even if the server response is delayed or fails.
        removeSessionById(currentSid);
        // If the deleted session is the one currently being viewed, clear the
        // chat area to show the "new task" welcome screen immediately.
        if (getState().sessionId === currentSid) {
          clearMessages();
          setSessionId("");
          setSessionState("idle", "");
        }
        send({ type: "delete_session", session_id: currentSid });
      }
    },
  });

  renderContextMenuItems(items, { container: menuEl });
  showContextMenu(menuEl, x, y, { target });
}

/**
 * Shows the rename dialog for a session and wires confirm/cancel handlers.
 *
 * @param sid - The session id to rename.
 */
export function showRenameDialogForSession(sid: string): void {
  const sessions = getState().sessionsList;
  const s = sessions.find((x) => x.session_id === sid);
  const currentName = s?.name || s?.preview || "";
  Dialog.prompt(
    t("session.renameDialogTitle"),
    "",
    {
      primary: t("session.rename"),
      secondary: t("session.cancel"),
      placeholder: t("session.renamePlaceholder"),
      inputClass: "encre-dialog-input--text",
      defaultValue: currentName,
      scope: "app",
    },
  ).then((newName) => {
    const trimmed = newName?.trim();
    if (trimmed) {
      send({ type: "rename_session", session_id: sid, new_name: trimmed });
    }
  });
}

function escHtml(s: string): string {
  const el = document.createElement("span");
  el.textContent = s;
  return el.innerHTML;
}
