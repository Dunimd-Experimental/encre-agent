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
 * Notification center (encre menu anchor, panel & toasts).
 *
 * Manages the in-app notifications surfaced through the Encre menu button: an
 * unread-count badge, a slide-out panel grouping unread/read items, and
 * transient toasts for newly-arrived notifications. The panel is opened from
 * the Encre menu ("Notifications" entry) — there is no dedicated bell button.
 * Reactive to global state via a subscription.
 */

import {
  getState,
  subscribe,
  markNotificationsRead,
  markOneNotificationRead,
  dismissNotification,
  clearAllNotifications,
  getUnreadCount,
  addNotification,
} from "../core/state.js";
import { MediaViewer } from "../chat/media-viewer.js";
import { t } from "../features/i18n.js";
import { TransitionHelper } from "./transition-helper.js";
import type { NotificationItem } from "../core/types.js";

/** Formats a timestamp as a localized relative time ("just now", "3m ago", …). */
function relativeTime(ts: number): string {
  const diff = Date.now() - ts;
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return t("notifications.justNow");
  const mins = Math.floor(secs / 60);
  if (mins < 60) return t("notifications.minutesAgo", { n: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t("notifications.hoursAgo", { n: hours });
  const days = Math.floor(hours / 24);
  return t("notifications.daysAgo", { n: days });
}

/** Returns the lucide icon name for a notification type. */
function typeIcon(type: string): string {
  switch (type) {
    case "error": return "alert-circle";
    case "warning": return "alert-triangle";
    case "success": return "check-circle";
    default: return "info";
  }
}

/**
 * Copies the notification's original text to the clipboard and flashes the button.
 */
function flashCopy(btn: HTMLElement): void {
  const orig = btn.getAttribute("data-original-icon");
  const original = orig || "copy";
  if (!orig) btn.setAttribute("data-original-icon", original);

  // Shared with the chat tool actions — see `.copy-flash` in styles.css.
  // The fade is CSS; these timings only decide when the glyph swaps.
  const fade = TransitionHelper.step("--duration-fast", 120);
  const swapGlyph = (name: string): void => {
    const i = btn.querySelector("[data-lucide]");
    if (i) i.setAttribute("data-lucide", name);
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: btn });
    }
  };

  btn.classList.add("copy-flash", "is-fading");
  setTimeout(() => {
    swapGlyph("check");
    btn.classList.remove("is-fading");
  }, fade);
  setTimeout(() => {
    btn.classList.add("is-fading");
    setTimeout(() => {
      swapGlyph(original);
      btn.classList.remove("is-fading", "copy-flash");
    }, fade);
  }, 2000);
}

/**
 * The notifications controller: badge, panel and toast lifecycle.
 */
export class Notifications {
  private anchor: HTMLElement | null;
  private host: HTMLElement | null = null;
  private _onBack: (() => void) | null = null;
  private toastContainer: HTMLElement | null = null;
  private lastNotificationIds: Set<string> = new Set();
  private _mediaViewer: MediaViewer | null = null;
  private _detailId: string | null = null;
  private _listClickHandler: ((e: MouseEvent) => void) | null = null;
  private _lastUnreadCount = -1;

  /**
   * Fired whenever the unread count changes (new arrival, item viewed, item
   * dismissed, cleared) so hosts such as the Encre menu can refresh their own
   * counter instead of rendering it once and going stale.
   */
  onUnreadCountChange: ((count: number) => void) | null = null;

  /**
   * Constructor: grabs the Encre menu button for the unread badge, wires the
   * state subscription. The list itself is rendered into a host container
   * inside the Encre menu (opened from the "Notifications" entry).
   */
  constructor() {
    this.anchor = document.getElementById("btn-encre-menu");

    this.ensureToastContainer();
    subscribe(() => this.render(), ["notifications"]);

    for (const n of getState().notifications) {
      this.lastNotificationIds.add(n.id);
    }
  }

  /** Snapshots the current notification ids as "already seen". */
  syncSeenIds(): void {
    this.lastNotificationIds = new Set(getState().notifications.map((n) => n.id));
  }

  /** Copies a notification's original text to the clipboard with visual feedback. */
  copyNotification(item: NotificationItem, btn: HTMLElement): void {
    const text = [item.title, item.message, item.source && `${t("notifications.source")}: ${item.source}`]
      .filter(Boolean)
      .join("\n");
    navigator.clipboard.writeText(text)
      .then(() => flashCopy(btn))
      .catch(() => { /* clipboard unavailable */ });
  }

  /** Re-renders the badge/panel and pops toasts for any new notifications. */
  render(): void {
    const notifications = getState().notifications;

    for (const n of notifications) {
      if (!this.lastNotificationIds.has(n.id)) {
        this.lastNotificationIds.add(n.id);
        this.showToast(n);
      }
    }

    const currentIds = new Set(notifications.map((n) => n.id));
    for (const id of this.lastNotificationIds) {
      if (!currentIds.has(id)) this.lastNotificationIds.delete(id);
    }

    const count = getUnreadCount();
    if (count !== this._lastUnreadCount) {
      this._lastUnreadCount = count;
      this.onUnreadCountChange?.(count);
    }
    if (this.anchor) {
      let badge = this.anchor.querySelector(".notification-badge") as HTMLElement | null;
      if (count > 0) {
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "notification-badge";
          this.anchor.style.position = "relative";
          this.anchor.appendChild(badge);
        }
        badge.textContent = "";
      } else {
        badge?.remove();
      }
    }
    if (this.host) this.renderPanel();
  }

  private ensureToastContainer(): void {
    if (this.toastContainer) return;
    this.toastContainer = document.createElement("div");
    this.toastContainer.className = "notification-toast-container";
    document.body.appendChild(this.toastContainer);
  }

  /** Renders and shows a transient toast for a single notification. */
  showToast(item: NotificationItem): void {
    this.ensureToastContainer();
    const toast = document.createElement("div");
    toast.className = `notification-toast ${item.type}`;
    toast.setAttribute("data-id", item.id);

    const sourceHtml = item.source
      ? `<div class="notification-toast-source">${t("notifications.source")}: ${this.esc(item.source)}</div>`
      : "";

    toast.innerHTML = `
      <div class="notification-toast-message">${this.esc(item.message || item.title)}</div>
      ${sourceHtml}
      <div class="notification-toast-actions">
        <button class="notification-toast-copy" data-tooltip="${t("notifications.copy")}">
          <i data-lucide="copy" class="lucide lucide-sm"></i>
        </button>
        <button class="notification-toast-collapse" data-tooltip="${t("notifications.dismiss")}">
          <i data-lucide="chevron-down" class="lucide lucide-sm"></i>
        </button>
      </div>
    `;

    const copyBtn = toast.querySelector(".notification-toast-copy");
    copyBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      this.copyNotification(item, copyBtn as HTMLElement);
    });

    const collapseBtn = toast.querySelector(".notification-toast-collapse");
    collapseBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      markOneNotificationRead(item.id);
      dismissNotification(item.id);
      toast.classList.add("removing");
      setTimeout(() => toast.remove(), 300);
    });

    toast.addEventListener("click", () => {
      markOneNotificationRead(item.id);
    });

    this.toastContainer!.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("visible"));

    const toasts = this.toastContainer!.querySelectorAll(".notification-toast");
    if (toasts.length > 3) {
      const oldest = toasts[0] as HTMLElement;
      oldest.classList.add("removing");
      setTimeout(() => oldest.remove(), 300);
    }

    // Every toast owns its own dismissal timer — a single shared timer meant
    // only the newest toast ever expired and older ones stayed on screen.
    let dismissTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleDismiss = (): void => {
      if (dismissTimer) clearTimeout(dismissTimer);
      dismissTimer = setTimeout(() => {
        toast.classList.add("removing");
        setTimeout(() => toast.remove(), 300);
      }, 5000);
    };
    scheduleDismiss();
    // Hovering keeps the toast alive so its copy/dismiss buttons stay reachable.
    toast.addEventListener("mouseenter", () => {
      if (dismissTimer) clearTimeout(dismissTimer);
      dismissTimer = null;
    });
    toast.addEventListener("mouseleave", () => scheduleDismiss());

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: toast });
    }
  }

  /** Attaches the notification list to a host container inside the Encre menu.
 *  @param host   - Container element that receives the list/detail HTML.
 *  @param onBack - Callback invoked when the user taps the top-left back button. */
  attach(host: HTMLElement, onBack: () => void): void {
    this.host = host;
    this._onBack = onBack;
    this._detailId = null;
    this.renderPanel();
  }

  /** Detaches from the host (destroys media viewer). */
  detach(): void {
    this._destroyMediaViewer();
    this.host = null;
    this._onBack = null;
    this._detailId = null;
  }

  /** Resets the detail drill-down (returns to list). */
  resetDetail(): void {
    this._destroyMediaViewer();
    this._detailId = null;
  }

  /** Builds the panel's inner HTML from unread/read notification lists or detail view. */
  private renderPanel(): void {
    if (!this.host) return;

    // Detail view
    if (this._detailId) {
      this._renderDetailView();
      return;
    }

    const all = getState().notifications;
    const unread = all.filter((n) => !n.read);
    const read = all.filter((n) => n.read);

    const empty = all.length === 0
      ? `<div class="si-empty-center"><i data-lucide="bell" class="lucide"></i><span class="si-empty-title">${t("notifications.empty")}</span></div>`
      : "";

    const inMenu = this.host.closest(".encre-menu-dropdown") !== null;
    const header = `<div class="notification-panel-header">
      <div class="notification-panel-header-left">
        <button class="notification-panel-back" id="notif-list-back" data-tooltip="${t("notifications.back")}">
          <i data-lucide="arrow-left" class="lucide"></i>
        </button>
        ${inMenu ? "" : `<span class="notification-panel-title">${t("notifications.title")}</span>`}
      </div>
      ${all.length > 0 ? `<button class="notification-panel-clear" data-tooltip="${t("notifications.clearAll")}">
        <i data-lucide="trash-2" class="lucide"></i>
      </button>` : ""}
    </div>`;

    const unreadSection = unread.length > 0
      ? `<div class="notification-section-label">${t("notifications.new")} (${unread.length})</div>
         ${this.renderItems(unread)}`
      : "";

    const readSection = read.length > 0
      ? `<div class="notification-section-label">${t("notifications.read")}</div>
         ${this.renderItems(read.slice(0, 20))}`
      : "";

    this.host.innerHTML = `${header}${empty}${unreadSection}${readSection}`;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.host });
    }

    this._wireListEvents();
  }

  private _wireListEvents(): void {
    if (!this.host) return;

    // Remove previous handler to avoid duplicates
    if (this._listClickHandler) {
      this.host.removeEventListener("click", this._listClickHandler);
    }

    this.host.querySelector("#notif-list-back")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this._destroyMediaViewer();
      this._detailId = null;
      this._onBack?.();
    });

    this.host.querySelector(".notification-panel-clear")?.addEventListener("click", () => {
      clearAllNotifications();
      this.renderPanel();
    });

    this._listClickHandler = (e: MouseEvent) => {
      const item = (e.target as HTMLElement).closest<HTMLElement>(".notification-panel-item");
      if (!item) return;
      const id = item.getAttribute("data-id");
      if (!id) return;

      if ((e.target as HTMLElement).closest(".notification-panel-dismiss")) {
        e.stopPropagation();
        dismissNotification(id);
        return;
      }

      e.stopPropagation();
      this.openDetail(id);
    };

    this.host.addEventListener("click", this._listClickHandler);
  }

  /**
   * Opens a notification's detail view.
   *
   * Opening counts as "viewed": the item is marked read right away, so the
   * unread counter drops (3 → 2 → …) as items are inspected. Dismissing or
   * deleting is no longer the only way to reduce the count.
   */
  private openDetail(id: string): void {
    this._detailId = id;
    const n = getState().notifications.find((x) => x.id === id);
    if (n && !n.read) {
      // Emits state, which re-renders the panel straight into the detail view.
      markOneNotificationRead(id);
      return;
    }
    this.renderPanel();
  }

  /** Destroys the current media viewer instance (stops video playback). */
  private _destroyMediaViewer(): void {
    if (this._mediaViewer) {
      this._mediaViewer.destroy();
      this._mediaViewer = null;
    }
  }

  /** Renders the detail view for the currently selected notification. */
  private _renderDetailView(): void {
    if (!this.host) return;
    const n = getState().notifications.find((x) => x.id === this._detailId);
    if (!n) {
      this._detailId = null;
      this.renderPanel();
      return;
    }

    const absTime = new Date(n.timestamp).toLocaleString();

    this.host.innerHTML = `
      <div class="notification-detail">
        <div class="notification-detail-header">
          <button class="notification-detail-back" id="notif-detail-back" data-tooltip="${t("notifications.back")}">
            <i data-lucide="arrow-left" class="lucide"></i>
          </button>
          <button class="notification-detail-copy" data-tooltip="${t("notifications.copy")}">
            <i data-lucide="copy" class="lucide lucide-sm"></i>
          </button>
        </div>
        ${n.media ? `<div class="notification-detail-media" id="notif-detail-media"></div>` : ""}
        <div class="notification-detail-body">
          <div class="notification-detail-title">${this.esc(n.title)}</div>
          ${n.message ? `<div class="notification-detail-msg">${this.esc(n.message)}</div>` : ""}
          <div class="notification-detail-meta">
            ${n.source ? `<span class="notification-detail-source">${this.esc(n.source)}</span>` : ""}
            <span class="notification-detail-time">${absTime}</span>
          </div>
        </div>
      </div>`;

    // Render media via shared MediaViewer component
    if (n.media) {
      const mediaEl = this.host.querySelector<HTMLElement>("#notif-detail-media");
      if (mediaEl) {
        this._destroyMediaViewer();
        this._mediaViewer = new MediaViewer(mediaEl, { ...n.media, toolbar: false });
      }
    }

    this.host.querySelector("#notif-detail-back")?.addEventListener("click", (e) => {
      e.stopPropagation();
      this._destroyMediaViewer();
      this._detailId = null;
      this.renderPanel();
    });

    this.host.querySelector(".notification-detail-copy")?.addEventListener("click", (e) => {
      e.stopPropagation();
      const btn = this.host!.querySelector(".notification-detail-copy") as HTMLElement;
      this.copyNotification(n, btn);
    });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.host });
    }
  }

  private renderItems(items: NotificationItem[]): string {
    return items.map((n) => `
      <div class="notification-panel-item ${n.type}${n.read ? "" : " unread"}" data-id="${n.id}">
        <i data-lucide="${typeIcon(n.type)}" class="lucide notification-panel-icon"></i>
        <div class="notification-panel-body">
          <div class="notification-panel-item-title">${this.esc(n.title)}</div>
          <div class="notification-panel-item-msg">${this.esc(n.message || "")}</div>
          <div class="notification-panel-meta">
            ${n.source ? `<span class="notification-panel-item-source">${this.esc(n.source)}</span>` : ""}
            <span class="notification-panel-item-time">${relativeTime(n.timestamp)}</span>
          </div>
        </div>
        <div class="notification-panel-actions">
          <button class="notification-panel-dismiss" data-tooltip="${t("notifications.dismiss")}">
            <i data-lucide="x" class="lucide lucide-sm"></i>
          </button>
        </div>
      </div>
    `).join("");
  }

  private esc(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }
}
