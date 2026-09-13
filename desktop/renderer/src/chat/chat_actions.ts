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
 * Event delegation, action menus and scroll/status helpers for the Chat view.
 *
 * Each helper was extracted from the `Chat` class and is invoked with
 * `impl.call(this, ...)` from a thin class delegate, so `this` still refers
 * to the owning `Chat` instance and every private member stays reachable.
 */

// @ts-nocheck -- helpers relocated from the Chat class; called via
// `XImpl.call(this, ...)` so the instance context is preserved.

import type { Chat } from "./chat.js";
import type { AttachmentMeta } from "../core/types.js";
import { getState, showToast, addAttachments, removeBranchMessages, startAssistantMessage, setSessionState, truncateToUserMessage, rememberRollbackEditTarget, restoreInputModeChip, restoreInputCommandChip } from "../core/state.js";
import { send } from "../core/ws.js";
import { t } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";
import { clampElLeft } from "../ui/context-menu.js";
import { createLucideIcons, flashCopyButton } from "./chat_tool_display.js";
import { pickRandomQuote } from "./timeline.js";

  export function handleDelegateClickImpl(this: Chat, e: MouseEvent): void {
    const target = e.target as HTMLElement;

    // Expandable strip / terminal card toggle (delegated).
    const strip = target.closest(".strip, .terminal-card-header") as HTMLElement | null;
    if (strip) {
      const item = strip.closest("[data-id]") as HTMLElement | null;
      if (item) {
        const id = item.getAttribute("data-id");
        if (id) {
          this.toggleItem(id, item);
          return;
        }
      }
    }

    // Review card section toggle: clicking a file row expands/collapses
    // that section's body. Uses data-review-toggle on the row.
    const reviewToggle = target.closest("[data-review-toggle]") as HTMLElement | null;
    if (reviewToggle) {
      const tid = reviewToggle.getAttribute("data-review-toggle");
      if (tid) {
        const expanded = this.expandedItems.has(tid);
        if (expanded) {
          this.expandedItems.delete(tid);
          reviewToggle.classList.remove("expanded");
        } else {
          this.expandedItems.add(tid);
          reviewToggle.classList.add("expanded");
        }
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons();
        }
        e.stopPropagation();
        return;
      }
    }

    // Link click handling — route based on default_link_behavior setting
    const link = target.closest("a");
    if (link && link.href) {
      const behavior = (getState().settings.default_link_behavior as string) || "system";
      let url = link.href;
      // Only intercept http/https/www links
      if (url.startsWith("http://") || url.startsWith("https://") || url.startsWith("www.")) {
        e.preventDefault();
        e.stopPropagation();
        if (url.startsWith("www.")) url = "https://" + url;
        if (behavior === "in_app") {
          const api = (window as any).electronAPI;
          if (api?.openChildWindow) {
            api.openChildWindow(url, url);
          } else {
            window.open(url, "_blank");
          }
        } else {
          // Default: open in system browser
          const api = (window as any).electronAPI;
          if (api?.openExternal) {
            api.openExternal(url);
          } else {
            window.open(url, "_blank");
          }
        }
        return;
      }
    }

    const codeCopyBtn = target.closest(".code-copy");
    if (codeCopyBtn) {
      e.stopPropagation();
      const code = codeCopyBtn.getAttribute("data-code") || "";
      navigator.clipboard.writeText(code)
        .then(() => { codeCopyBtn.textContent = t("chat.copied"); setTimeout(() => { codeCopyBtn.textContent = t("chat.copy"); }, 2000); })
        .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
      return;
    }

    const copyBtn = target.closest(".msg-copy-btn");
    if (copyBtn) {
      e.stopPropagation();
      const userItem = copyBtn.closest(".user-item") as HTMLElement | null;
      const idx = userItem ? parseInt(userItem.getAttribute("data-user-idx") ?? "") : NaN;
      const st = getState();
      const userMsgs = st.messages.filter(m => m.role === "user");
      if (!isNaN(idx) && userMsgs[idx]) {
        navigator.clipboard.writeText(userMsgs[idx].content)
          .then(() => flashCopyButton(copyBtn as HTMLElement))
          .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
      }
      return;
    }

    const editBtn = target.closest(".msg-rollback-btn");
    if (editBtn) {
      e.stopPropagation();
      const userItem = editBtn.closest(".user-item") as HTMLElement | null;
      const idx = userItem ? parseInt(userItem.getAttribute("data-user-idx") ?? "") : NaN;
      const st = getState();
      const userMsgs = st.messages.filter(m => m.role === "user");
      if (isNaN(idx) || !userMsgs[idx]) return;
      const targetMsg = userMsgs[idx];
      let origContent = targetMsg.content;
      // A message submitted with a command chip and no text is stored as
      // ``<command>name</command>`` — strip it from the restored text and
      // re-render it as a chip instead of raw markup.
      const cmdTagMatch = origContent.match(/^<command>(\w[\w-]*)<\/command>$/s);
      if (origContent.includes("<terminal>") || origContent.includes("<attach ") || origContent.includes("<mode>") || cmdTagMatch) origContent = "";
      Dialog.confirm(t("chat.rollbackEdit"), t("chat.rollbackEditDesc"), { container: document.getElementById("main-content") ?? undefined }).then((confirmed) => {
        if (confirmed) {
          const sid = targetMsg.serverId;
          rememberRollbackEditTarget(targetMsg, idx);
          if (sid && st.activeBranchId) {
            send({ type: "rollback", branch_id: st.activeBranchId, message_id: sid });
          } else {
            send({ type: "delete_message", message_index: idx, session_id: st.sessionId });
          }
          // Immediately remove this message and all after from local state
          truncateToUserMessage(idx);
          this.renderForce();
          const input = document.getElementById("prompt-input") as HTMLElement | null;
          if (input) { input.textContent = origContent; input.focus(); }
          const msgMode = (targetMsg as any).mode;
          if (msgMode) {
            restoreInputModeChip(msgMode);
          }
          // Restore the command chip for ``<command>name</command>`` messages
          // so it renders as a chip, not raw text.
          if (cmdTagMatch) {
            restoreInputCommandChip(cmdTagMatch[1]);
          }
          // Restore attachment chips from fileRefs
          const refs = targetMsg.fileRefs;
          if (refs && refs.length > 0) {
            const atts: AttachmentMeta[] = [];
            for (const r of refs) {
              if (r.mime_type === "text/x-terminal") continue;
              if (r.path) {
                atts.push({ name: r.name, path: r.path, content: "", mime_type: r.mime_type || "", size: r.size, is_binary: false });
              }
            }
            if (atts.length > 0) addAttachments(atts);
          }
        }
      });
      return;
    }

    const deleteBtn = target.closest(".msg-delete-btn");
    if (deleteBtn) {
      e.stopPropagation();
      const userItem = deleteBtn.closest(".user-item") as HTMLElement | null;
      const idx = userItem ? parseInt(userItem.getAttribute("data-user-idx") ?? "") : NaN;
      const st = getState();
      const userMsgs = st.messages.filter(m => m.role === "user");
      if (isNaN(idx) || !userMsgs[idx]) return;
      Dialog.confirm(t("chat.deleteMessage"), t("chat.deleteMessageDesc"), { container: document.getElementById("main-content") ?? undefined }).then((confirmed) => {
        if (confirmed) send({ type: "delete_message", message_index: idx });
      });
      return;
    }

    const assistantCopyBtn = target.closest(".assistant-copy-btn");
    if (assistantCopyBtn) {
      e.stopPropagation();
      const turn = assistantCopyBtn.closest(".turn") as HTMLElement | null;
      const assistantItem = turn?.querySelector(".assistant-text") as HTMLElement | null;
      const msgId = assistantItem?.dataset.messageId;
      if (!msgId) return;
      const st = getState();
      const msg = st.messages.find(m => m.id === msgId);
      const btn = assistantCopyBtn as HTMLElement;
      this.openActionMenu(btn, [
        {
          icon: "copy",
          label: t("chat.copy"),
          onClick: () => {
            const text = assistantItem?.innerText ?? msg?.content ?? "";
            navigator.clipboard.writeText(text)
              .then(() => flashCopyButton(btn))
              .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
          },
        },
        {
          icon: "code",
          label: t("chat.copyMarkdown"),
          onClick: () => {
            if (!msg) return;
            navigator.clipboard.writeText(msg.content)
              .then(() => flashCopyButton(btn))
              .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
          },
        },
      ]);
      return;
    }

    const assistantRetryBtn = target.closest(".assistant-retry-btn");
    if (assistantRetryBtn) {
      e.stopPropagation();
      const turn = assistantRetryBtn.closest(".turn") as HTMLElement | null;
      const assistantItem = turn?.querySelector(".assistant-text") as HTMLElement | null;
      const msgId = assistantItem?.dataset.messageId;
      if (!msgId) return;
      const btn = assistantRetryBtn as HTMLElement;
      this.openActionMenu(btn, [
        { icon: "refresh-cw", label: t("chat.retryNormal"), onClick: () => this.performRetry(msgId, "normal") },
        { icon: "stretch-horizontal", label: t("chat.retryDetailed"), onClick: () => this.performRetry(msgId, "detailed") },
        { icon: "minimize-2", label: t("chat.retryConcise"), onClick: () => this.performRetry(msgId, "concise") },
      ]);
      return;
    }

    const branchPrev = target.closest(".branch-prev");
    if (branchPrev) {
      e.stopPropagation();
      const switcher = branchPrev.closest(".branch-switcher") as HTMLElement;
      if (!switcher) return;
      const ids = switcher.dataset.branchIds?.split(",") || [];
      const idx = parseInt(switcher.dataset.activeIdx || "0");
      const newIdx = idx > 0 ? idx - 1 : ids.length - 1;
      const branchId = ids[newIdx];
      if (branchId && branchId !== getState().activeBranchId) {
        if (typeof (window as any).sendSwitchBranch === "function") {
          (window as any).sendSwitchBranch(branchId);
        }
      }
      return;
    }

    const branchNext = target.closest(".branch-next");
    if (branchNext) {
      e.stopPropagation();
      const switcher = branchNext.closest(".branch-switcher") as HTMLElement;
      if (!switcher) return;
      const ids = switcher.dataset.branchIds?.split(",") || [];
      const idx = parseInt(switcher.dataset.activeIdx || "0");
      const newIdx = idx < ids.length - 1 ? idx + 1 : 0;
      const branchId = ids[newIdx];
      if (branchId && branchId !== getState().activeBranchId) {
        if (typeof (window as any).sendSwitchBranch === "function") {
          (window as any).sendSwitchBranch(branchId);
        }
      }
      return;
    }

    // Spec approve/reject buttons
    const approveBtn = target.closest("[data-spec-approve]") as HTMLElement | null;
    if (approveBtn) {
      e.stopPropagation();
      const sessionId = approveBtn.getAttribute("data-spec-approve") || "";
      send({ type: "spec_approve", session_id: sessionId } as any);
      return;
    }
    const rejectBtn = target.closest("[data-spec-reject]") as HTMLElement | null;
    if (rejectBtn) {
      e.stopPropagation();
      const sessionId = rejectBtn.getAttribute("data-spec-reject") || "";
      const feedback = prompt("Feedback (optional):") || "";
      send({ type: "spec_reject", session_id: sessionId, feedback } as any);
      return;
    }

    // Plan file rows: open in sidebar markdown tab
    const openMdBtn = target.closest("[data-open-md]") as HTMLElement | null;
    if (openMdBtn) {
      e.stopPropagation();
      const fileKey = openMdBtn.getAttribute("data-open-md") || "";
      const title = openMdBtn.getAttribute("data-md-title") || "";
      const content = this._planFileLookup.get(fileKey) || "";
      (window as any).__sessionInner?.openMarkdownPreview(content, title);
      return;
    }

    // Plan approve/reject buttons
    const planApproveBtn = target.closest("[data-plan-approve]") as HTMLElement | null;
    if (planApproveBtn) {
      e.stopPropagation();
      const sessionId = planApproveBtn.getAttribute("data-plan-approve") || "";
      send({ type: "plan_approve", session_id: sessionId } as any);
      return;
    }
    const planRejectBtn = target.closest("[data-plan-reject]") as HTMLElement | null;
    if (planRejectBtn) {
      e.stopPropagation();
      const sessionId = planRejectBtn.getAttribute("data-plan-reject") || "";
      send({ type: "plan_reject", session_id: sessionId } as any);
      return;
    }
  }

  export function toggleItemImpl(this: Chat, id: string, el: HTMLElement): void {
    if (this.expandedItems.has(id)) {
      this.expandedItems.delete(id);
      el.classList.remove("expanded");
      this.userCollapsedItems.add(id);
      this.userExpandedItems.delete(id);
    } else {
      this.expandedItems.add(id);
      el.classList.add("expanded");
      this.userCollapsedItems.delete(id);
      this.userExpandedItems.add(id);
    }
  }

  /** Opens a unified dropdown menu anchored to the given action button.
   *  Reuses the same visual language as the model selector / @-command
   *  menus (`.mention-dropdown` / `.mention-dropdown-item`). */
  export function openActionMenuImpl(
    this: Chat,
    btn: HTMLElement,
    items: Array<{ icon: string; label: string; onClick: () => void }>,
  ): void {
    // If the same menu is already open on this button, just close it.
    if (btn.classList.contains("action-menu-open")) {
      this.closeActionMenu();
      return;
    }
    // Close any other action menu first.
    this.closeActionMenu();

    const rect = btn.getBoundingClientRect();
    const menu = document.createElement("div");
    menu.className = "mention-dropdown action-menu";
    menu.style.position = "fixed";
    menu.style.top = `${rect.bottom + 4}px`;
    menu.style.left = `${rect.left}px`;
    menu.style.marginBottom = "0";
    menu.style.bottom = "auto";
    menu.style.minWidth = "160px";

    for (const item of items) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "mention-dropdown-item";
      row.innerHTML = `<i data-lucide="${item.icon}" class="lucide"></i><span>${item.label}</span>`;
      row.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this.closeActionMenu();
        item.onClick();
      });
      menu.appendChild(row);
    }

    document.body.appendChild(menu);
    // Position uses a physical `left`, which never mirrors. Clamp it now
    // that the menu is in the DOM and its width is measurable, otherwise
    // an action button near the inline-end edge opens it off-window (rtl).
    menu.style.left = `${clampElLeft(menu, rect.left)}px`;
    createLucideIcons(menu);
    btn.classList.add("action-menu-open");
    this._actionMenu = menu;
    this._actionMenuBtn = btn;

    const closeOnOutside = (ev: MouseEvent) => {
      if (this._actionMenu && !this._actionMenu.contains(ev.target as Node)) {
        this.closeActionMenu();
      }
    };
    this._actionMenuClose = closeOnOutside;
    setTimeout(() => document.addEventListener("click", closeOnOutside), 0);

    // The action buttons are only visible while the turn is hovered. When
    // the mouse leaves the turn the buttons disappear, so the menu must
    // follow suit — but with a short grace period so the pointer has time
    // to travel from the button to the menu (which lives in document.body,
    // outside the turn). Entering the menu cancels the pending close.
    const turn = btn.closest(".turn") as HTMLElement | null;
    const scheduleClose = () => {
      if (this._actionMenuCloseTimer !== null) return;
      this._actionMenuCloseTimer = window.setTimeout(() => {
        this._actionMenuCloseTimer = null;
        this.closeActionMenu();
      }, 180);
    };
    const cancelClose = () => {
      if (this._actionMenuCloseTimer !== null) {
        window.clearTimeout(this._actionMenuCloseTimer);
        this._actionMenuCloseTimer = null;
      }
    };
    if (turn) {
      const onTurnLeave = (ev: MouseEvent) => {
        if (!this._actionMenu) return;
        const rel = ev.relatedTarget as Node | null;
        if (rel && this._actionMenu.contains(rel)) return;
        scheduleClose();
      };
      turn.addEventListener("mouseleave", onTurnLeave);
      this._actionMenuTurnLeave = { el: turn, fn: onTurnLeave };
    }
    menu.addEventListener("mouseenter", cancelClose);
    menu.addEventListener("mouseleave", scheduleClose);
    this._actionMenuEnter = { el: menu, fn: cancelClose };
    this._actionMenuLeave = { el: menu, fn: scheduleClose };
  }

  export function closeActionMenuImpl(this: Chat): void {
    if (this._actionMenu) {
      this._actionMenu.remove();
      this._actionMenu = null;
    }
    if (this._actionMenuBtn) {
      this._actionMenuBtn.classList.remove("action-menu-open");
      this._actionMenuBtn = null;
    }
    if (this._actionMenuClose) {
      document.removeEventListener("click", this._actionMenuClose);
      this._actionMenuClose = null;
    }
    if (this._actionMenuTurnLeave) {
      this._actionMenuTurnLeave.el.removeEventListener(
        "mouseleave",
        this._actionMenuTurnLeave.fn,
      );
      this._actionMenuTurnLeave = null;
    }
    if (this._actionMenuEnter) {
      this._actionMenuEnter.el.removeEventListener(
        "mouseenter",
        this._actionMenuEnter.fn,
      );
      this._actionMenuEnter = null;
    }
    if (this._actionMenuLeave) {
      this._actionMenuLeave.el.removeEventListener(
        "mouseleave",
        this._actionMenuLeave.fn,
      );
      this._actionMenuLeave = null;
    }
    if (this._actionMenuCloseTimer !== null) {
      window.clearTimeout(this._actionMenuCloseTimer);
      this._actionMenuCloseTimer = null;
    }
  }

  /** Re-generates the assistant reply starting from the preceding user
   *  message, forking the branch with the given retry mode. */
  export function performRetryImpl(this: Chat, msgId: string, mode: string): void {
    const st = getState();
    const assistantIdx = st.messages.findIndex(m => m.id === msgId);
    if (assistantIdx < 0) return;
    let userContent = "";
    for (let i = assistantIdx - 1; i >= 0; i--) {
      if (st.messages[i].role === "user" && st.messages[i].mode !== "compact_marker") {
        userContent = st.messages[i].content;
        break;
      }
    }
    if (!userContent) return;

    // Compute user message index (0-based among all user messages in the session)
    let userMsgIdx = -1;
    for (let i = 0; i < assistantIdx; i++) {
      if (st.messages[i].role === "user" && st.messages[i].mode !== "compact_marker") userMsgIdx++;
    }

    // Remove old assistant messages after the fork point; the preceding
    // user message stays in place (no duplicate needed).
    const removedIds = new Set<string>();
    for (let i = assistantIdx; i < st.messages.length; i++) {
      removedIds.add(st.messages[i].id);
    }
    removeBranchMessages(removedIds);
    // Create assistant placeholder for new streaming output
    startAssistantMessage();
    setSessionState("running");

    const branchId = st.activeBranchId;
    if (typeof (window as any).sendRetry === "function") {
      (window as any).sendRetry(branchId, userMsgIdx, mode);
    }
  }

  export function autoScrollImpl(this: Chat): void {
    const st = getState();
    if (st.messages.length === 0) return;
    if (st.messages[st.messages.length - 1].role === "user") { this.scrollToBottom(); return; }
    if (!st.running) return;
    if (!this.userScrolledUp) this.scrollToBottom();
  }

  export function toggleWelcomeImpl(this: Chat, show: boolean): void {
    if (show) { this.welcomeScreen.classList.remove("hidden"); this.ml.classList.add("hidden"); }
    else { this.welcomeScreen.classList.add("hidden"); this.ml.classList.remove("hidden"); }
  }

  /** Scrolls the message list to the bottom (unless the user scrolled up). */
  export function scrollToBottomImpl(this: Chat): void {
    const c = document.getElementById("chat-container");
    if (c) c.scrollTop = c.scrollHeight;
  }

  /** Scrolls to the bottom and keeps re-pinning until any async media
   *  (images / videos inside the message list) has finished loading.
   *  A plain scrollToBottom() on session entry runs before images resolve,
   *  so the container grows afterwards and leaves unread content below the
   *  fold.  This variant waits for those elements to settle. */
  export function scrollToBottomAfterMediaImpl(this: Chat): void {
    const c = document.getElementById("chat-container");
    if (!c) return;
    const pin = () => {
      if (this.userScrolledUp) return;
      c.scrollTop = c.scrollHeight;
    };
    pin();
    // Re-pin on the next frame so layout shifts from the same tick apply.
    requestAnimationFrame(pin);
    const media = Array.from(this.ml.querySelectorAll("img, video")).filter(
      (el) => el instanceof HTMLImageElement && !el.complete
        || el instanceof HTMLVideoElement && el.readyState < 2,
    );
    if (media.length === 0) return;
    const done = () => {
      pin();
      media.forEach((el) => {
        el.removeEventListener("load", done);
        el.removeEventListener("loadeddata", done);
        el.removeEventListener("error", done);
      });
    };
    media.forEach((el) => {
      el.addEventListener("load", done, { once: true });
      el.addEventListener("loadeddata", done, { once: true });
      el.addEventListener("error", done, { once: true });
    });
    // Safety net: never wait longer than this for slow media.
    window.setTimeout(() => {
      media.forEach((el) => {
        el.removeEventListener("load", done);
        el.removeEventListener("loadeddata", done);
        el.removeEventListener("error", done);
      });
      pin();
    }, 800);
  }

  /** Keeps the floating scroll-to-bottom button in sync with the real state
   *  of the chat container. The button may only appear when the content
   *  actually overflows the viewport AND the user has scrolled away from
   *  the bottom; when a session is cleared or switched this re-evaluates
   *  the metrics instead of trusting a stale scroll event. */
  export function syncScrollButtonImpl(this: Chat): void {
    const btn = document.getElementById("chat-scroll-bottom-btn");
    if (!btn) return;
    const { scrollTop, scrollHeight, clientHeight } = this.container;
    const scrollable = scrollHeight - clientHeight > 8;
    this.userScrolledUp = scrollable && scrollHeight - scrollTop - clientHeight > 8;
    btn.classList.toggle("hidden", !this.userScrolledUp);
  }

  /** Public reset so the view-switch cleanup can hide the button eagerly. */
  export function resetScrollButtonImpl(this: Chat): void {
    this.userScrolledUp = false;
    const btn = document.getElementById("chat-scroll-bottom-btn");
    if (btn) btn.classList.add("hidden");
  }

  export function _updateStatusBarImpl(this: Chat, running: boolean): void {
    if (running) {
      if (!this._currentQuote) {
        this._currentQuote = pickRandomQuote();
      }
      const q = this._currentQuote;
      this.statusBar.innerHTML = `<span class="status-shimmer">${t(`chat.${q.textKey}`)}</span>`;
      this.statusBar.classList.remove("hidden");
    } else {
      this.statusBar.classList.add("hidden");
      this._currentQuote = null;
    }
  }

