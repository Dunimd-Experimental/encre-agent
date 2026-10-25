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

import type { Message, ToolCallState, BranchMeta } from "../core/types.js";
import { getState, showToast, removeBranchMessages, startAssistantMessage, setSessionState, truncateToUserMessage, rememberRollbackEditTarget, restoreInputModeChip, restoreInputCommandChip, openRoutedLink, isNoticeMode, realUserMessages } from "../core/state.js";
import { restoreComposerWithChips } from "../features/files.js";
import { send, sendRollback } from "../core/ws.js";
import { t } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";
import { flashCopyButton } from "./chat_tool_display.js";
import { pickRandomQuote } from "./timeline.js";
import { pinBodyTails } from "./chat_render_engine.js";

/**
 * Resolves a chat link target to a local filesystem path, or returns null
 * when the target is not a file (web URL, anchor, data URI, ...).
 *
 * Order matters: explicit absolute paths win, then workspace-relative paths,
 * then file:// URLs (written explicitly or produced by the browser resolving
 * a relative anchor against the app's file:// base).
 */
function resolveLocalFileTarget(rawHref: string, resolvedHref: string): string | null {
  const raw = (rawHref || "").trim();
  if (!raw || raw.startsWith("#")) return null;

  // Explicit scheme handling: file:// is local, everything else (http,
  // mailto, data, ...) is not a filesystem target. A single-letter "scheme"
  // like "D:" is a Windows drive, not a scheme.
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(raw)?.[1]?.toLowerCase();
  if (scheme && scheme !== "file" && scheme.length > 1) return null;
  if (/^www\./i.test(raw)) return null; // bare "www." link, not a file

  // 1. Absolute local path (Windows drive, UNC share, POSIX absolute).
  if (/^[A-Za-z]:[\\/]/.test(raw) || raw.startsWith("\\\\") || /^\/(?!\/)/.test(raw)) {
    return raw;
  }

  // 2. Workspace-relative path — resolve against the active workspace root
  //    so a model-mentioned "src/foo.ts" lands on the actual file.
  if (!scheme) {
    const ws = (getState().activeWorkspace || "").replace(/[\\/]+$/, "");
    if (ws) {
      let rel = raw.replace(/\\/g, "/");
      try { rel = decodeURIComponent(rel); } catch { /* keep encoded */ }
      rel = rel.replace(/^\.\//, "").replace(/^\/+/, "");
      if (rel) return ws + "/" + rel;
    }
  }

  // 3. Explicit file:// URL (raw or browser-resolved).
  const src = /^file:\/\//i.test(raw) ? raw : (/^file:\/\//i.test(resolvedHref || "") ? resolvedHref : "");
  if (src) {
    let p = src.replace(/^file:\/\/\/?/i, "");
    try { p = decodeURIComponent(p); } catch { /* keep encoded */ }
    p = p.replace(/^\/([A-Za-z]:)/, "$1"); // /D:/... -> D:/...
    if (p.startsWith("//")) p = p.slice(1); // UNC share
    return p || null;
  }
  return null;
}

  export function handleDelegateClickImpl(this: Chat, e: MouseEvent): void {
    const target = e.target as HTMLElement;

    // Stop tool (terminal) — kill the shell only, turn continues.
    const stopBtn = target.closest("[data-tool-stop]") as HTMLElement | null;
    if (stopBtn) {
      e.stopPropagation();
      send({ type: "tool_stop", session_id: getState().sessionId });
      return;
    }

    // 「继续」 on an interrupted turn — identical to the user typing the
    // resume prompt and pressing send. The timeline only renders the button
    // while the card is the tail of the transcript.
    const continueBtn = target.closest("[data-turn-continue]") as HTMLElement | null;
    if (continueBtn) {
      e.stopPropagation();
      if (this.onResume) this.onResume();
      return;
    }

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
        // Scoped to the row that just toggled. A document-wide pass here would
        // replace every icon in the app inside the very click that expanded
        // this section — wiping the section's own state and stealing clicks
        // landing anywhere else on screen.
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: reviewToggle });
        }
        e.stopPropagation();
        return;
      }
    }

    // Link click handling
    const link = target.closest("a");
    if (link) {
      const rawHref = link.getAttribute("href") || "";
      const href = link.href || "";
      // Files the model mentioned (absolute / workspace-relative / file://
      // targets) always open in the in-app file viewer — never the Encre Agent Desktop Subwindow (ESD)
      // sub-app or the system browser.
      const localPath = resolveLocalFileTarget(rawHref, href);
      if (localPath) {
        e.preventDefault();
        e.stopPropagation();
        if (this.onOpenFile) this.onOpenFile(localPath);
        return;
      }
      // Web/mailto URLs go through the shared router
      if (href && /^(https?:\/\/|www\.|mailto:)/i.test(href)) {
        e.preventDefault();
        e.stopPropagation();
        openRoutedLink(href);
        return;
      }
    }

    // Local images inside rendered markdown open in the file viewer too
    const msgImg = target.closest(".msg-text img") as HTMLImageElement | null;
    if (msgImg && msgImg.src && /^file:/i.test(msgImg.src)) {
      e.preventDefault();
      e.stopPropagation();
      let p = msgImg.src.replace(/^file:\/\/\/?/i, "");
      try { p = decodeURIComponent(p); } catch { /* keep encoded */ }
      p = p.replace(/^\/([A-Za-z]:)/, "$1");
      if (p && this.onOpenFile) this.onOpenFile(p);
      return;
    }

    const codeCopyBtn = target.closest(".code-copy");
    if (codeCopyBtn) {
      e.stopPropagation();
      const code = codeCopyBtn.getAttribute("data-code") || "";
      navigator.clipboard.writeText(code)
        .then(() => flashCopyButton(codeCopyBtn as HTMLElement))
        .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
      return;
    }

    const copyBtn = target.closest(".msg-copy-btn");
    if (copyBtn) {
      e.stopPropagation();
      const userItem = copyBtn.closest(".user-item") as HTMLElement | null;
      const idx = userItem ? parseInt(userItem.getAttribute("data-user-idx") ?? "") : NaN;
      const st = getState();
      const userMsgs = realUserMessages(st.messages);
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
      // data-user-idx is assigned by buildTimeline AFTER skipping the notice
      // modes (compact_marker / compact_notice / model_switch), so the lookup
      // must skip them too. A bare role === "user" filter counted the notices
      // and resolved one turn EARLIER than the one clicked -- which rolled
      // back the second-to-last turn and pre-filled the composer with the
      // wrong text.
      const userMsgs = realUserMessages(st.messages);
      if (isNaN(idx) || !userMsgs[idx]) return;
      const targetMsg = userMsgs[idx];
      let origContent = targetMsg.content;
      // A message submitted with a command chip and no text is stored as
      // ``<command>name</command>`` — strip it from the restored text and
      // re-render it as a chip instead of raw markup.
      const cmdTagMatch = origContent.match(/^<command>(\w[\w-]*)<\/command>$/s);
      if (origContent.includes("<terminal>") || origContent.includes("<attach ") || origContent.includes("<mode>") || cmdTagMatch) origContent = "";
      Dialog.confirm(t("chat.rollbackEdit"), t("chat.rollbackEditDesc"), { scope: "session" }).then((confirmed) => {
        if (confirmed) {
          // The dialog was open, so the user may have switched sessions in the
          // meantime. Re-read state and abort unless this is still the session
          // the click happened in -- otherwise the rollback lands in a
          // different conversation.
          const cur = getState();
          if (cur.sessionId !== st.sessionId) return;
          const sid = targetMsg.serverId;
          rememberRollbackEditTarget(targetMsg, idx);
          if (sid && cur.activeBranchId) {
            // session_id is mandatory: the backend binds an omitted one to the
            // connection's LAST session, not to this conversation.
            sendRollback(cur.activeBranchId, sid, cur.sessionId);
          } else {
            // No branch exists yet, so a branch rollback has nothing to roll
            // back against: delete the message outright instead. message_id is
            // the reliable locator -- a frontend-computed index can misalign
            // with the backend's raw list (synthetic loop plumbing, compact
            // markers) and delete the wrong turn.
            send({ type: "delete_message", message_index: idx, message_id: targetMsg.serverId || "", session_id: cur.sessionId });
          }
          // Immediately remove this message and all after from local state.
          // This is optimistic: if the server refuses the request, the error
          // handler in stream.ts re-syncs (resume) and clears the pending-edit
          // marker, so the UI returns to the real backend conversation instead
          // of staying truncated.
          truncateToUserMessage(idx);
          // The conversation just got truncated: pin the re-render to the
          // bottom instead of preserving the pre-rollback scroll offset.
          this.requestPinToBottom();
          this.renderForce();
          // Restore the composer with attachment chips rebuilt at their
          // inline positions (the content carries `<attach n="..." />`
          // placeholders where the chips originally sat).
          const input = document.getElementById("prompt-input") as HTMLElement | null;
          if (input) {
            restoreComposerWithChips(input, origContent, targetMsg.fileRefs);
            input.focus();
          }
          const msgMode = (targetMsg as any).mode;
          if (msgMode) {
            restoreInputModeChip(msgMode);
          }
          // Restore the command chip for ``<command>name</command>`` messages
          // so it renders as a chip, not raw text.
          if (cmdTagMatch) {
            restoreInputCommandChip(cmdTagMatch[1]);
          }
          // (Attachment chips are restored by restoreComposerWithChips above.)
        }
      }).catch((err) => {
        // The confirmation rejected instead of resolving: nothing was sent
        // and nothing was truncated, so the only duty is to keep the
        // rejection from going unhandled. A legitimate rollback in flight
        // keeps its own pending-edit marker.
        console.warn("[chat] rollback confirmation failed", err);
      });
      return;
    }

    const deleteBtn = target.closest(".msg-delete-btn");
    if (deleteBtn) {
      e.stopPropagation();
      const userItem = deleteBtn.closest(".user-item") as HTMLElement | null;
      const idx = userItem ? parseInt(userItem.getAttribute("data-user-idx") ?? "") : NaN;
      const st = getState();
      const userMsgs = realUserMessages(st.messages);
      if (isNaN(idx) || !userMsgs[idx]) return;
      const targetMsg = userMsgs[idx];
      Dialog.confirm(t("chat.deleteMessage"), t("chat.deleteMessageDesc"), { scope: "session" }).then((confirmed) => {
        if (!confirmed) return;
        // Re-read after the await: the session may have changed while the
        // dialog was open, and the delete must reach the session that owns
        // the message (an omitted session_id binds to the connection's last
        // session, i.e. a different conversation).
        const cur = getState();
        if (cur.sessionId !== st.sessionId) return;
        send({ type: "delete_message", message_index: idx, message_id: targetMsg.serverId || "", session_id: cur.sessionId });
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
      // Markdown is the only copy format — the previous dropdown
      // (plain text vs markdown) was removed by design.
      const text = msg ? msg.content : (assistantItem?.innerText ?? "");
      navigator.clipboard.writeText(text)
        .then(() => flashCopyButton(btn))
        .catch(() => showToast(t("chat.copyFailed"), "", "error", "Chat"));
      return;
    }

    const assistantRetryBtn = target.closest(".assistant-retry-btn");
    if (assistantRetryBtn) {
      e.stopPropagation();
      const turn = assistantRetryBtn.closest(".turn") as HTMLElement | null;
      const assistantItem = turn?.querySelector(".assistant-text") as HTMLElement | null;
      const msgId = assistantItem?.dataset.messageId;
      if (!msgId) return;
      // Regenerate runs immediately with the default mode — the detailed /
      // concise dropdown was removed by design.
      this.performRetry(msgId, "normal");
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

    // Plan file rows: open the artifact in the sidebar markdown tab
    const openMdBtn = target.closest("[data-md-path]") as HTMLElement | null;
    if (openMdBtn) {
      e.stopPropagation();
      const path = openMdBtn.getAttribute("data-md-path") || "";
      if (path) void (window as any).__sessionInner?.openMarkdownPreviewFile?.(path);
      return;
    }

    // Plan approve/reject are NOT handled here: the review card binds them
    // inline through `window.__reviewDecision` (see chat.ts), so there is one
    // path instead of a delegate plus an inline handler.
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
      // The class flip reveals a body that was laid out while collapsed, so
      // its scroll window has to be moved to the tail now — nothing else
      // re-renders this card on a manual expand.
      pinBodyTails(el);
    }
  }

  /** Re-generates the assistant reply starting from the preceding user
   *  message, forking the branch with the given retry mode. */
  export function performRetryImpl(this: Chat, msgId: string, mode: string): void {
    const st = getState();
    const assistantIdx = st.messages.findIndex(m => m.id === msgId);
    if (assistantIdx < 0) return;
    // Re-generate from the last REAL user turn before this card: the notice
    // modes (compact_marker / compact_notice / model_switch) ride along as
    // user-role messages but consume no data-user-idx, so they must not be
    // counted here either.
    let userContent = "";
    for (let i = assistantIdx - 1; i >= 0; i--) {
      const m = st.messages[i];
      if (m.role === "user" && !isNoticeMode(m.mode)) {
        userContent = m.content;
        break;
      }
    }
    if (!userContent) return;

    // 0-based among the real user turns, i.e. the same base buildTimeline
    // uses for data-user-idx. The turn found above is the last real one
    // before assistantIdx, so it is index (count - 1) of that prefix.
    const userMsgIdx = realUserMessages(st.messages.slice(0, assistantIdx)).length - 1;

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
    if (this.userScrolledUp) {
      const c = document.getElementById("chat-container");
      // Self-heal a flag poisoned by clamp scroll events during DOM swaps
      // (media collapse makes the old scrollTop clamp and fire a bogus
      // "scrolled up" event): only a real upward scroll -- well away from
      // the bottom -- stops the follow.
      if (!c || c.scrollHeight - c.scrollTop - c.clientHeight > 120) return;
    }
    this.scrollToBottom();
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
   *  fold.  This variant waits for those elements to settle.
   *  Force mode bypasses the userScrolledUp guard: session entry is a fresh
   *  navigation, so a stale "scrolled up" flag from the previous session
   *  (or from scroll events fired mid DOM-swap) must not keep the view
   *  anchored in the middle of the new history. */
  export function scrollToBottomAfterMediaImpl(this: Chat, force = false): void {
    const c = document.getElementById("chat-container");
    if (!c) return;
    // The userScrolledUp flag alone is not trustworthy: the innerHTML swap
    // collapses media heights, the browser clamps the old scrollTop and the
    // resulting scroll event marks the user as "scrolled up" even though
    // they never touched the wheel.  "Near the bottom" therefore counts as
    // pinned; only a real upward scroll keeps the view anchored.
    const atBottom = () => c.scrollHeight - c.scrollTop - c.clientHeight < 120;
    const pin = () => {
      if (!force && this.userScrolledUp && !atBottom()) return;
      c.scrollTop = c.scrollHeight;
    };
    pin();
    // Re-pin on the next frame so layout shifts from the same tick apply.
    requestAnimationFrame(pin);
    // Late layout growth (webfonts, KaTeX, code highlighting, lazy media
    // resolving after the settle window) can land after every media event
    // has fired.  Watch the message-list size for a grace period and
    // re-assert the bottom while the user has not scrolled away.
    const ro = new ResizeObserver(() => pin());
    ro.observe(this.ml);
    const media = Array.from(this.ml.querySelectorAll("img, video")).filter(
      (el) => el instanceof HTMLImageElement && !el.complete
        || el instanceof HTMLVideoElement && el.readyState < 2,
    );
    const done = () => {
      pin();
      media.forEach((el) => {
        el.removeEventListener("load", done);
        el.removeEventListener("loadeddata", done);
        el.removeEventListener("error", done);
      });
    };
    if (media.length > 0) {
      media.forEach((el) => {
        el.addEventListener("load", done, { once: true });
        el.addEventListener("loadeddata", done, { once: true });
        el.addEventListener("error", done, { once: true });
      });
    }
    // Safety net: never wait longer than this for slow media, then stop
    // watching for layout shifts.
    window.setTimeout(() => {
      media.forEach((el) => {
        el.removeEventListener("load", done);
        el.removeEventListener("loadeddata", done);
        el.removeEventListener("error", done);
      });
      ro.disconnect();
      pin();
    }, 1500);
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

  /**
   * Keeps the status bar OUT of #message-list on purpose: fullRender rebuilds
   * that list via innerHTML on every structural streaming change, and moving
   * the bar back in after each rebuild restarted its CSS animation (the
   * shimmer froze at its first frame).  The bar stays an absolute child of
   * the scroll container and is translated under the latest output by
   * syncStatusBarPosition — same visual spot, but the node (and its running
   * animation) is never touched by list rebuilds.
   */
  export function mountStatusBarImpl(this: Chat): void {
    const bar = this.statusBar;
    if (!bar) return;
    if (bar.parentElement !== this.container) this.container.appendChild(bar);
    if (!this.statusBarRO) {
      this.statusBarRO = new ResizeObserver(() => this.syncStatusBarPosition());
      this.statusBarRO.observe(this.ml);
    }
    this.syncStatusBarPosition();
    this.startStatusBarFollow();
  }

  /** Viewport-relative bottom of the last non-empty text line inside `root`,
   *  or null when the block paints no text at all (images, cards, a turn that
   *  only holds tool strips). */
  function lastTextLineBottom(root: HTMLElement): number | null {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node: Node | null = null;
    for (let n = walker.nextNode(); n; n = walker.nextNode()) node = n;
    // Step back over text nodes that paint nothing (they live in a
    // display:none subtree) until one reports a real line box. Bounded so a
    // pathological tail cannot turn a per-frame measure into a tree walk.
    for (let steps = 0; node && steps < 64; steps++) {
      if (node.nodeValue && node.nodeValue.trim()) {
        const range = document.createRange();
        range.selectNodeContents(node);
        const rect = range.getBoundingClientRect();
        if (rect.height || rect.width) return rect.bottom;
      }
      node = walker.previousNode();
    }
    return null;
  }

  /** Y, in the scroll container's CONTENT coordinates, of the transcript's
   *  last painted line.
   *
   *  Deliberately not `#message-list.scrollHeight`: the list is `flex: 1`
   *  inside the scroll container, so while a transcript is shorter than the
   *  pane the list is stretched to the full pane height and its scrollHeight
   *  marks the empty tail below the content.  The bar then floated a fixed
   *  distance above the input instead of sitting under the model's last line,
   *  and only snapped into place once the transcript outgrew the pane.
   *
   *  Nor the last block's BOX bottom: `.assistant-text` carries 12px of bottom
   *  padding plus the trailing paragraph's 6px margin, which pushed the line a
   *  plainly visible ~20px below the model's final sentence.  A range over the
   *  last text node measures the LINE, so the status reads as the next line of
   *  the reply.  Falls back to the block box, then to the list content bottom,
   *  for tails that paint no text. */
  function transcriptBottom(ml: HTMLElement, container: HTMLElement): number {
    let last = ml.lastElementChild as HTMLElement | null;
    // Trailing zero-height nodes (hidden action rows) never mark the bottom.
    while (last && last.offsetHeight === 0) {
      last = last.previousElementSibling as HTMLElement | null;
    }
    if (!last) {
      const padBottom = parseFloat(getComputedStyle(ml).paddingBottom) || 0;
      return Math.max(0, ml.scrollHeight - padBottom);
    }
    const lineBottom = lastTextLineBottom(last);
    if (lineBottom !== null) {
      return Math.max(0, lineBottom - container.getBoundingClientRect().top + container.scrollTop);
    }
    return Math.max(0, last.offsetTop + last.offsetHeight);
  }

  /** Pins the bar right below the transcript's last rendered line. The
   *  position rides on `transform`, which never restarts CSS animations and
   *  scrolls naturally with the container's content. Styles are only written
   *  when the value actually changed, so the per-frame caller stays cheap. */
  export function syncStatusBarPositionImpl(this: Chat): void {
    const bar = this.statusBar;
    if (!bar || bar.classList.contains("hidden")) return;
    const ml = this.ml;
    const cs = getComputedStyle(ml);
    // One list-gap keeps the bar's rhythm identical to the transcript's own
    // spacing (falls back to 10px if the gap is ever 0 or unparsable).
    const gap = parseFloat(cs.rowGap) || 10;
    const top = transcriptBottom(ml, this.container) + gap;
    const ty = `translateY(${top}px)`;
    if (bar.style.transform !== ty) bar.style.transform = ty;
    // The transcript's text column is the list's CONTENT box (the list carries
    // `padding: 28px 24px 42px`). Spanning the list's BORDER box instead put
    // the shimmer a full padding-left — 24px — to the left of the assistant
    // name and of every message, so the status line never read as the reply's
    // next line no matter how well its vertical offset was measured.
    const padLeft = parseFloat(cs.paddingLeft) || 0;
    const padRight = parseFloat(cs.paddingRight) || 0;
    const left = `${ml.offsetLeft + padLeft}px`;
    if (bar.style.left !== left) bar.style.left = left;
    const width = `${ml.clientWidth - padLeft - padRight}px`;
    if (bar.style.width !== width) bar.style.width = width;
  }

  /** Re-pins the bar on every frame for as long as it is visible.
   *
   *  The ResizeObserver alone is not enough: it fires when the LIST's box
   *  changes, and with a short transcript the stretched list keeps its height
   *  while the model streams text into the last block — no resize, no
   *  reposition, so the bar lagged behind the incoming text.  A per-frame
   *  measure costs one layout read (no writes unless the value changed) and
   *  makes the bar correct regardless of what moved it: streaming text,
   *  webfonts, KaTeX, lazy media, a resize, a transcript rebuild. */
  export function startStatusBarFollowImpl(this: Chat): void {
    if (this.statusBarRaf) return;
    const step = () => {
      this.statusBarRaf = 0;
      const bar = this.statusBar;
      // Hidden bar (turn finished, session switched) ends the loop.
      if (!bar || bar.classList.contains("hidden")) return;
      this.syncStatusBarPosition();
      this.statusBarRaf = requestAnimationFrame(step);
    };
    this.statusBarRaf = requestAnimationFrame(step);
  }

  export function _updateStatusBarImpl(this: Chat, running: boolean): void {
    if (running) {
      this.mountStatusBar();
      if (!this._currentQuote) {
        this._currentQuote = pickRandomQuote();
      }
      const q = this._currentQuote;
      // Rewrite the span only when the quote actually changes: rebuilding it
      // on every render frame restarted the shimmer animation over and over,
      // which read as the shimmer being stuck in place during streaming.
      const want = t(`chat.${q.textKey}`);
      const span = this.statusBar.querySelector(".status-shimmer");
      if (!span || span.textContent !== want) {
        this.statusBar.innerHTML = `<span class="status-shimmer"></span>`;
        this.statusBar.querySelector(".status-shimmer")!.textContent = want;
      }
      if (this.statusBar.classList.contains("hidden")) {
        this.statusBar.classList.remove("hidden");
        this.syncStatusBarPosition();
      }
      this.startStatusBarFollow();
    } else {
      if (this.statusBarRaf) {
        cancelAnimationFrame(this.statusBarRaf);
        this.statusBarRaf = 0;
      }
      this.statusBar.classList.add("hidden");
      this._currentQuote = null;
    }
  }

