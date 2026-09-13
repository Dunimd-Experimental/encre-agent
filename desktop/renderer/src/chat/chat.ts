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
 * Chat view & markdown rendering.
 *
 * Renders the conversation message list (markdown + syntax-highlighted code
 * blocks, tool calls, artifacts and sub-agent views) and owns the composer
 * send flow. Also exports the shared `renderMarkdown` and `formatAgentLabel`
 * helpers used across the renderer.
 */

import { getState, subscribe, showToast, addUserMessage, addAttachments, startAssistantMessage, setSessionState, removeBranchMessages, restoreInputModeChip, truncateToUserMessage, setSubAgentView, pushSubAgentBreadcrumb, popSubAgentBreadcrumb, clearSubAgentBreadcrumb, resetToSubAgentBreadcrumbIndex, rememberRollbackEditTarget, isEnabled, toolCallMatchesId } from "../core/state.js";
import { send } from "../core/ws.js";
import { setRequestedSessionId } from "../core/stream.js";
import type { Message, ToolCallState, BranchMeta, AttachmentMeta } from "../core/types.js";
import { t, onLocaleChange, getLocale } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";
import { findSlashCommand } from "../features/slash_commands.js";
import { EALoader } from "../features/ealoader.js";
import { MediaViewer } from "./media-viewer.js";
import { renderMarkdown, escapeHtml } from "./chat_markdown.js";
import { isHiddenTool, isFileMutationTool, isFileReadTool, isToolItemTool, getToolIcon, formatToolName, getToolSummary, getToolInlineSummary, getToolBodyText, _toolStatusHtml, getAgentName, formatAgentLabel, createLucideIcons, saveVideoPlayback, restoreVideoPlayback, flashCopyButton } from "./chat_tool_display.js";
import { buildTimeline, buildRenderKey, buildSubAgentTimeline, selectSubAgentTaskMessages, pickRandomQuote, _fmtTokens, fmtSize } from "./timeline.js";
import type { TimelineItem, StatusQuote } from "./timeline.js";
import { ChatScrollIndicator } from "./chat_scroll_indicator.js";
import { renderSpecCardImpl, renderPlanCardImpl, parsePlanSectionsImpl, renderWorkflowCardImpl } from "./chat_review_cards.js";
import { renderItemHTMLImpl, renderAIHeaderImpl, renderModeCardImpl, renderUserItemImpl, renderThinkingStripImpl, renderToolCallImpl, renderQuestionCardImpl, renderInfoCardImpl, renderFileCardImpl, renderTerminalCardImpl, renderExpandableStripImpl, renderToolItemSimpleImpl, renderAgentImpl, _agentTaskDividersImpl, renderAssistantTextImpl, renderErrorCardImpl, renderWarningCardImpl, renderBranchSwitcherImpl, renderCompactStartingCardImpl, renderCompactCardImpl, renderSystemMessageImpl } from "./chat_item_render.js";
import { handleDelegateClickImpl, toggleItemImpl, openActionMenuImpl, closeActionMenuImpl, performRetryImpl, autoScrollImpl, toggleWelcomeImpl, scrollToBottomImpl, scrollToBottomAfterMediaImpl, syncScrollButtonImpl, resetScrollButtonImpl, _updateStatusBarImpl } from "./chat_actions.js";
import { requestRenderImpl, renderForceImpl, renderImpl, renderSubAgentIntoImpl, computeExpandedImpl, activeThinkingIdImpl, applyAutoExpandImpl, buildTimelineHTMLImpl, fullRenderParallelImpl, fullRenderImpl, incrementalTextUpdateImpl } from "./chat_render_engine.js";

export { renderMarkdown } from "./chat_markdown.js";
export { formatAgentLabel, getAgentName } from "./chat_tool_display.js";

const _openedHtmlCards = new Set<string>();

/**
 * The chat view controller: message list rendering and composer send flow.
 */
export class Chat {
  private ml: HTMLElement;
  private welcomeScreen: HTMLElement;
  private container: HTMLElement;
  private statusBar: HTMLElement;
  private userScrolledUp = false;
  private renderedKey = "";
  private expandedItems = new Set<string>();
  private userCollapsedItems = new Set<string>();
  private userExpandedItems = new Set<string>();
  private lastAssistantMsgId = "";
  private _lastRunning = false;
  private _inRenderForce = false;
  private rafPending = false;
  private liveLoader: EALoader | null = null;
  private scrollIndicator: ChatScrollIndicator;
  private _actionMenu: HTMLElement | null = null;
  private _actionMenuBtn: HTMLElement | null = null;
  private _actionMenuClose: ((ev: MouseEvent) => void) | null = null;
  private _actionMenuTurnLeave: { el: HTMLElement; fn: (ev: MouseEvent) => void } | null = null;
  private _actionMenuEnter: { el: HTMLElement; fn: () => void } | null = null;
  private _actionMenuLeave: { el: HTMLElement; fn: () => void } | null = null;
  private _actionMenuCloseTimer: number | null = null;
  /** Map of file keys to markdown content for plan file rows (avoids attribute length limits). */
  private _planFileLookup = new Map<string, string>();
  /** Callback invoked when the user clicks "View Changes" on an artifact file. */
  public onViewChanges: ((path: string) => void) | null = null;

  /**
   * Constructor: resolves DOM nodes and wires the scroll indicator + bridges.
   */
  constructor() {
    this.ml = document.getElementById("message-list")!;
    this.welcomeScreen = document.getElementById("welcome-screen")!;
    this.container = document.getElementById("chat-container")!;
    this.statusBar = document.getElementById("chat-status-bar")!;
    this.scrollIndicator = new ChatScrollIndicator(this.container);

    // Scroll-to-bottom floating button
    const scrollBtn = document.createElement("button");
    scrollBtn.id = "chat-scroll-bottom-btn";
    scrollBtn.className = "chat-scroll-bottom-btn hidden";
    scrollBtn.setAttribute("aria-label", "Scroll to bottom");
    scrollBtn.innerHTML = '<i data-lucide="chevron-down" style="width:18px;height:18px"></i>';
    this.container.parentElement?.appendChild(scrollBtn);
    scrollBtn.addEventListener("click", () => {
      this.container.scrollTo({ top: this.container.scrollHeight, behavior: "smooth" });
    });

    const w = window as any;
    w.__openSubAgentView = (toolCallId: string, taskIndex?: number) => {
      const st = getState();
      const MAX_DEPTH = 4;
      // Enforce max nesting depth using breadcrumb length
      if (st.subAgentBreadcrumb.length >= MAX_DEPTH) {
        showToast?.(t("chat.subAgent"), t("chat.maxDepthReached", { n: MAX_DEPTH }));
        return;
      }
      for (const msg of st.messages) {
        for (const tc of msg.toolCalls) {
          if (toolCallMatchesId(tc, toolCallId)) {
            // Explicit user navigation INTO a sub-agent re-enables auto-open.
            w.__subAgentAutoOpenDismissed = false;
            // The parent tool call id is the currently-active sub-agent
            // view's tc id (when navigating from inside another sub-agent)
            // or null when navigating from the root main session.
            const parentToolCallId = st.subAgentView ? st.subAgentView.id : null;
            // Record current session as parent in the breadcrumb stack
            const agentName = getAgentName(tc);
            const taskDividers = this._agentTaskDividers(tc);
            const selectedTask = taskIndex === undefined
              ? undefined
              : taskDividers.find((task) => task.index === taskIndex);
            const taskName = selectedTask
              ? selectedTask.name
              : "";
            const crumbName = taskName ? `${agentName} · ${taskName}` : agentName;
            // If the user already has a deeper view open and clicks
            // an ancestor's card, truncate the stack back to that level
            // so the breadcrumb reflects their actual navigation, not
            // a stale "deeper view" branch.
            const existingIdx = st.subAgentBreadcrumb.findIndex(
              (c) => c.toolCallId === toolCallId,
            );
            if (existingIdx >= 0) {
              resetToSubAgentBreadcrumbIndex(existingIdx);
            } else {
              pushSubAgentBreadcrumb({
                sessionId: st.sessionId,
                name: crumbName,
                toolCallId,
                parentToolCallId,
              });
            }
            if (tc.subAgentMessages && tc.subAgentMessages.length > 0) {
              // Viewing a single parallel task keeps the parent tool-call
              // identity but narrows the transcript to that task.
              const viewTc: ToolCallState = { ...tc, taskIndex };
              // Inline sub-agents use the tool-call snapshot, not a resumed
              // session. Clear any stale session-backed flag so the fallback
              // path does not accidentally render the parent session messages.
              w.__activeSubAgentSessionId = undefined;
              setSubAgentView(viewTc);
              this.render();
            } else if (tc.subAgentSessionId) {
              const requestId = crypto.randomUUID();
              // The sub-agent transcript is loaded by a session_ready reply.
              // Keep the originating tool-call identity until that reply
              // arrives so the renderer can re-enter the sub-agent view.
              w.__pendingSubAgentView = {
                toolCall: tc,
                sessionId: tc.subAgentSessionId,
                requestId,
              };
              setRequestedSessionId(tc.subAgentSessionId, requestId);
              send({ type: "resume", session_id: tc.subAgentSessionId, request_id: requestId });
            } else {
              setSubAgentView(tc);
              this.render();
            }
            return;
          }
        }
      }
    };
    w.__closeSubAgentView = () => {
      const crumb = popSubAgentBreadcrumb();
      w.__pendingSubAgentView = undefined;
      w.__activeSubAgentSessionId = undefined;
      // User deliberately stepped back toward the main agent: stop
      // auto-opening freshly spawned sub-agents for the rest of this run.
      if (!crumb) w.__subAgentAutoOpenDismissed = true;
      setSubAgentView(null);
      if (crumb) {
        const requestId = crypto.randomUUID();
        setRequestedSessionId(crumb.sessionId, requestId);
        send({ type: "resume", session_id: crumb.sessionId, request_id: requestId });
      } else {
        this.renderedKey = "";
        this.ml.innerHTML = "";
        requestAnimationFrame(() => this.render());
      }
    };
    w.__goToRootView = () => {
      const st = getState();
      // Leaving every sub-agent level: drop the session-backed marker so the
      // parent view is never mistaken for a sub-agent snapshot.
      w.__activeSubAgentSessionId = undefined;
      w.__pendingSubAgentView = undefined;
      // User explicitly returned to the main agent: suppress auto-open of
      // subsequently spawned sub-agents until they opt back in or a new turn.
      w.__subAgentAutoOpenDismissed = true;
      if (st.subAgentBreadcrumb.length === 0) {
        setSubAgentView(null);
        this.render();
        return;
      }
      const rootSessionId = st.subAgentBreadcrumb[0].sessionId;
      clearSubAgentBreadcrumb();
      setSubAgentView(null);
      if (rootSessionId !== st.sessionId) {
        const requestId = crypto.randomUUID();
        setRequestedSessionId(rootSessionId, requestId);
        send({ type: "resume", session_id: rootSessionId, request_id: requestId });
      }
      this.renderedKey = "";
      this.ml.innerHTML = "";
      this.render();
    };
    w.__navigateToBreadcrumb = (index: number) => {
      const st = getState();
      const target = resetToSubAgentBreadcrumbIndex(index);
      if (!target) return;
      // Navigate to the sub-agent identified by this breadcrumb entry's
      // toolCallId.  toolCallId is the id of the agent tool call that
      // spawned this sub-agent — the same id used when clicking the
      // agent card (__openSubAgentView).  Previously the code searched
      // for target.parentToolCallId, which is the *parent* entry's id
      // and caused clicking a breadcrumb to navigate to the wrong view.
      const stNow = getState();
      for (const msg of stNow.messages) {
        for (const tc of msg.toolCalls) {
          if (toolCallMatchesId(tc, target.toolCallId)) {
            // Breadcrumb navigation always stays inside the parent session
            // and reopens the inline sub-agent view. Resuming the sub-agent
            // session itself would break the breadcrumb stack.
            setSubAgentView(tc);
            this.render();
            return;
          }
        }
      }
      // Fallback: tool call not found — clear the sub-agent view
      // and resume the breadcrumb's stored session.
      setSubAgentView(null);
      const requestId = crypto.randomUUID();
      setRequestedSessionId(target.sessionId, requestId);
      send({ type: "resume", session_id: target.sessionId, request_id: requestId });
    };
    w.__answerQuestion = (event: Event, btn: HTMLElement, cardId: string) => {
      event.stopPropagation();
      // Resolve the card from the click source first: the same tc-* id can
      // exist in BOTH the main chat and the automation detail
      // (renderSubAgentInto reuses the timeline pipeline), so a global
      // getElementById would hit whichever card came first in the DOM and
      // answer the WRONG tool call.  closest() guarantees we operate on the
      // card the user actually clicked.
      const card = ((btn as HTMLElement).closest('[id^="tc-"]') as HTMLElement | null) ?? document.getElementById(cardId);
      if (!card) return;
      // Manual input wins over a previously selected option: clicking an
      // option fills its value into the input box, so reading the input box
      // covers both paths and never discards what the user typed by hand.
      const input = card.querySelector(".q-input") as HTMLInputElement;
      let answer = input ? input.value.trim() : "";
      if (!answer) {
        const selected = card.querySelector(".q-option-card.selected");
        if (selected) answer = selected.getAttribute("data-value") || "";
      }
      if (answer) {
        send({ type: "respond_question", tool_call_id: cardId.replace("tc-", ""), answers: answer });
      }
    };
    w.__hoverOn = (el: HTMLElement) => {
      const wrap = el.querySelector(".icon-wrap");
      if (wrap) wrap.classList.add("hover");
    };
    w.__hoverOff = (el: HTMLElement) => {
      const wrap = el.querySelector(".icon-wrap");
      if (wrap) wrap.classList.remove("hover");
    };
    w.__toggleStatusCard = (el: HTMLElement, id?: string) => {
      // Same-card-first resolution as __answerQuestion: the automation detail
      // may render the identical tc-* status card, and a global lookup would
      // toggle the main chat's copy instead of the one being clicked.
      const card = ((el as HTMLElement).closest('[id^="tc-"]') as HTMLElement | null) ?? (id ? document.getElementById(id) : null);
      if (card) card.classList.toggle("expanded");
    };
    w.__viewChanges = (el: HTMLElement) => {
      const card = el.closest(".file-card") as HTMLElement | null;
      if (card) {
        const path = card.getAttribute("data-path") || "";
        if (path && this.onViewChanges) this.onViewChanges(path);
      }
    };

    // Resize info-card iframes when the sandboxed content reports its height.
    const resizeInfoFrame = (iframe: HTMLIFrameElement) => {
      try {
        const doc = iframe.contentDocument;
        if (!doc) return;
        const height = Math.max(doc.body?.scrollHeight || 0, doc.documentElement?.scrollHeight || 0);
        if (height > 0) iframe.style.height = `${height}px`;
      } catch {
        // Cross-origin or otherwise inaccessible iframe; ignore.
      }
    };
    window.addEventListener("message", (event) => {
      const data = event.data;
      if (!data || typeof data.__encreInfoHeight !== "number" || !data.__encreInfoId) return;
      const iframe = document.querySelector(`#${data.__encreInfoId} .info-card-frame`) as HTMLIFrameElement | null;
      if (iframe) iframe.style.height = `${data.__encreInfoHeight}px`;
    });
    w.__resizeInfoFrame = resizeInfoFrame;

    // Info-card toolbar actions: toggle render/code view and copy source.
    w.__toggleInfoView = (cardId: string) => {
      const card = document.getElementById(cardId);
      if (!card) return;
      const frame = card.querySelector(".info-card-frame") as HTMLElement | null;
      const codeBlock = card.querySelector(".info-card-code") as HTMLElement | null;
      const btn = card.querySelector(".info-card-actions .info-btn[data-action='toggle-view']") as HTMLElement | null;
      if (!frame || !codeBlock) return;
      const showingCode = !codeBlock.classList.contains("hidden");
      const nextMode = showingCode ? "render" : "code";
      frame.classList.toggle("hidden", nextMode !== "render");
      codeBlock.classList.toggle("hidden", nextMode !== "code");
      if (btn) {
        // The button icon always represents the action that will happen on click:
        // preview mode -> show code icon (click to view code)
        // code mode -> show eye icon (click to view preview)
        const nextIcon = nextMode === "code" ? "code" : "eye";
        btn.dataset.mode = nextMode;
        btn.dataset.tooltip = t(nextMode === "code" ? "chat.infoCode" : "chat.infoRender");
        // Re-create the <i> element because Lucide replaces it with an SVG.
        btn.innerHTML = `<i data-lucide="${nextIcon}" class="info-btn-icon"></i>`;
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: btn });
        }
      }
    };
    w.__copyInfoSource = async (cardId: string) => {
      const card = document.getElementById(cardId);
      if (!card) return;
      const source = card.getAttribute("data-source") || "";
      try {
        await navigator.clipboard.writeText(source);
      } catch {
        showToast(t("chat.infoCopyFailed"), "", "error", "Chat");
      }
    };

    w.__initInfoCardMedia = (elOrId: HTMLElement | string) => {
      // Accept either the card element (from a scoped traversal) or an id
      // (legacy external callers).  Element-first resolution keeps the media
      // init inside the right container when the same card id exists in both
      // the main chat and the automation detail.
      const card = typeof elOrId === "string"
        ? document.getElementById(elOrId)
        : ((elOrId as HTMLElement).closest('[id^="tc-"]') as HTMLElement | null);
      if (!card) return;
      card.querySelectorAll(":scope > [data-type]").forEach((el) => {
        const type = el.getAttribute("data-type") as "image" | "video" || "image";
        const src = el.getAttribute("data-src") || "";
        if (src) new MediaViewer(el as HTMLElement, { type, src, controls: type === "video", openPreview: type === "image" });
      });
    };
    w.__initMediaCards = (scope?: HTMLElement) => {
      // Scoped traversal: renderSubAgentInto passes its own container so the
      // main chat's media cards are never re-initialized (or matched) from
      // inside the automation detail.
      ((scope ?? document) as HTMLElement).querySelectorAll(".info-card--media").forEach((card) => {
        w.__initInfoCardMedia(card as HTMLElement);
      });
    };
    w.__openInfoHtmlCard = async (el: HTMLElement, cardId?: string) => {
      // Element-first resolution (same rationale as __answerQuestion): the
      // automation detail reuses the tc-* ids, so operate on the card the
      // user clicked rather than the first match in the whole document.
      const card = ((el as HTMLElement).closest('[id^="tc-"]') as HTMLElement | null) ?? (cardId ? document.getElementById(cardId) : null);
      if (!card) return;
      const src = card.getAttribute("data-source");
      if (!src) return;
      const fileUrl = await window.electronAPI?.openInfoHtml(src).catch(() => null);
      if (fileUrl) {
        window.dispatchEvent(new CustomEvent("info-html-open", { detail: { url: fileUrl } }));
      }
    };
    w.__openInfoHtmlCards = (scope?: HTMLElement) => {
      // Scoped traversal so the auto-open pass never reaches into another
      // container (main chat <-> automation detail) for the same card ids.
      ((scope ?? document) as HTMLElement).querySelectorAll<HTMLElement>(".strip-item[data-source]").forEach((card) => {
        const id = card.id;
        if (!id || _openedHtmlCards.has(id)) return;
        _openedHtmlCards.add(id);
        w.__openInfoHtmlCard(card, id);
      });
    };
    window.__initMediaCards = w.__initMediaCards;
    window.__initInfoCardMedia = w.__initInfoCardMedia;
    window.__openInfoHtmlCards = w.__openInfoHtmlCards;
    this.container.addEventListener("scroll", () => this.syncScrollButton());

    // Hide the button whenever the chat is re-rendered (new/cleared session).
    const ro = new ResizeObserver(() => this.syncScrollButton());
    ro.observe(this.container);

    this.ml.addEventListener("click", (e) => this.handleDelegateClick(e));
    // Track session changes to prevent stale content bleeding
    let _lastSid = getState().sessionId;
    let _lastMsgLen = getState().messages.length;
    let _lastExpand = isEnabled(getState().settings.auto_expand);
    subscribe(() => {
      const st = getState();
      const sid = st.sessionId;
      const msgs = st.messages;
      // Session ID changed → force full reset and re-render
      if (sid && sid !== _lastSid) {
        _lastSid = sid;
        _lastMsgLen = msgs.length;
        this.renderedKey = "";
        this.expandedItems.clear();
        this.userCollapsedItems.clear();
        if (this.liveLoader) {
          this.liveLoader.destroy();
          this.liveLoader = null;
        }
        // Sub-agent views/breadcrumbs are scoped to a specific session.  When
        // the user switches sessions we must discard them so the new session
        // is not rendered with the previous session's nested agent content.
        if (st.subAgentView || st.subAgentBreadcrumb.length > 0) {
          setSubAgentView(null);
          clearSubAgentBreadcrumb();
        }
        this.ml.innerHTML = "";
        this.render();
        return;
      }
      if (msgs.length === 0 && _lastMsgLen > 0) {
        _lastMsgLen = 0;
        this.renderedKey = "";
        this.expandedItems.clear();
        this.userCollapsedItems.clear();
        if (this.liveLoader) {
          this.liveLoader.destroy();
          this.liveLoader = null;
        }
        if (st.subAgentView || st.subAgentBreadcrumb.length > 0) {
          setSubAgentView(null);
          clearSubAgentBreadcrumb();
        }
        this.ml.innerHTML = "";
        this.syncScrollButton();
        return;
      }
      if (msgs.length > 0 && _lastMsgLen === 0) {
        _lastMsgLen = msgs.length;
        this.renderedKey = "";
        this.render();
        return;
      }
      // Re-render when auto_expand setting changes (auto-expanded thinking
      // strips depend on it and they are not part of the render key).
      const curExpand = isEnabled(st.settings.auto_expand);
      if (curExpand !== _lastExpand) {
        _lastExpand = curExpand;
        this.requestRender();
        return;
      }
      _lastMsgLen = msgs.length;
      this.requestRender();
    });
    onLocaleChange(() => this.render());
  }

  /** RAF-throttled render — collapses bursts of stream deltas into one
   * frame so streaming never saturates the main thread and freezes
   * transitions/animations. */
  public requestRender(): void {
    requestRenderImpl.call(this);
  }

  private _parallelRenderTimer: number | null = null;
  private _pendingParallelRender = false;
  /** True while building marker-pipeline HTML for a sub-agent transcript
   *  (normal-chat sub-agent view, parallel tiles, or the automation detail
   *  panel).  renderUserItem reads this so user bubbles render their full
   *  content and hide rollback/delete actions even when the global
   *  `getState().subAgentView` is not set (the automation detail never sets
   *  it, because the automation panel is an independent view). */
  private _subAgentRender = false;

  renderForce(): void {
    renderForceImpl.call(this);
  }

  /** Re-renders the message list from current state (RAF-throttled). */
  render(): void {
    renderImpl.call(this);
  }

  /**
   * Build the timeline HTML for sub-agent messages and render it into
   * an arbitrary container element. Reuses the same rendering pipeline
   * as the main chat's fullRender, so the result looks identical to the
   * chat sub-agent view.
   *
   * Used by the automation panel to render execution results inside
   * #automation-detail-content without switching to chat mode.
   */
  public renderSubAgentInto(container: HTMLElement, messages: Message[], isRunning: boolean): void {
    renderSubAgentIntoImpl.call(this, container, messages, isRunning);
  }

  /**
   * Single source of truth for whether a timeline item should be expanded.
   * Delegated to computeExpandedImpl.
   */
  private computeExpanded(kind: string, id: string, autoExpand: boolean, thinkingActive: boolean): boolean {
    return computeExpandedImpl.call(this, kind, id, autoExpand, thinkingActive);
  }

  /**
   * Identify the thinking segment the model is *actively* generating.
   * Delegated to activeThinkingIdImpl.
   */
  private activeThinkingId(timeline: TimelineItem[], isRunning: boolean): string | null {
    return activeThinkingIdImpl.call(this, timeline, isRunning);
  }

  /**
   * Shared auto-expand pass used by both fullRender and renderSubAgentInto.
   * Delegated to applyAutoExpandImpl.
   */
  private applyAutoExpand(timeline: TimelineItem[], autoExpand: boolean, isRunning = false): void {
    applyAutoExpandImpl.call(this, timeline, autoExpand, isRunning);
  }

  /**
   * Build the timeline's inner HTML (turns + standalone cards) - the shared
   * body of fullRender. Extracted so renderSubAgentInto produces byte-identical
   * markup to the main chat's sub-agent view instead of a parallel copy.
   */
  private buildTimelineHTML(timeline: TimelineItem[], allMsgs?: Message[], treatAsSubAgent = false): string {
    return buildTimelineHTMLImpl.call(this, timeline, allMsgs, treatAsSubAgent);
  }

  /**
   * Render a parallel sub-agent run: one ``agent`` tool that executed several
   * tasks concurrently. The transcript is a flat list delimited by structured
   * ``task_divider`` markers (``mode === "task_divider"``); we split it back
   * into per-task groups and tile them into split regions -- like a window
   * snap layout -- so "one agent, many jobs" reads as distinct panes:
   *   1 task  -> fills the whole tape
   *   2 tasks -> left / right
   *   3 tasks -> one across the top, two side-by-side below
   *   4 tasks -> 2x2 grid
   * Regions are separated by faint dashed lines (CSS). At most 4 tiles are
   * drawn -- more would deform and can't be laid out cleanly.
   */
  private fullRenderParallel(subMsgs: Message[], isStreaming: boolean): void {
    fullRenderParallelImpl.call(this, subMsgs, isStreaming);
  }

  private fullRender(timeline: TimelineItem[], allMsgs?: Message[]): void {
    fullRenderImpl.call(this, timeline, allMsgs);
  }

  private incrementalTextUpdate(timeline: TimelineItem[]): void {
    incrementalTextUpdateImpl.call(this, timeline);
  }

  private renderItemHTML(item: TimelineItem): string {
    return renderItemHTMLImpl.call(this, item);
  }

  private renderAIHeader(item: Extract<TimelineItem, { kind: "ai_header" }>): string {
    return renderAIHeaderImpl.call(this, item);
  }

  private renderModeCard(icon: string, label: string, summary?: string): string {
    return renderModeCardImpl.call(this, icon, label, summary);
  }

  private renderUserItem(item: Extract<TimelineItem, { kind: "user" }>): string {
    return renderUserItemImpl.call(this, item);
  }

  private renderThinkingStrip(item: Extract<TimelineItem, { kind: "thinking" }>): string {
    return renderThinkingStripImpl.call(this, item);
  }

  private renderToolCall(item: Extract<TimelineItem, { kind: "tool" }>): string {
    return renderToolCallImpl.call(this, item);
  }

  private renderQuestionCard(tc: ToolCallState, itemId: string): string {
    return renderQuestionCardImpl.call(this, tc, itemId);
  }

  private renderInfoCard(tc: ToolCallState, itemId: string): string {
    return renderInfoCardImpl.call(this, tc, itemId);
  }

  private renderFileCard(tc: ToolCallState): string {
    return renderFileCardImpl.call(this, tc);
  }

  private renderTerminalCard(tc: ToolCallState, itemId: string): string {
    return renderTerminalCardImpl.call(this, tc, itemId);
  }

  private renderExpandableStrip(tc: ToolCallState, itemId: string): string {
    return renderExpandableStripImpl.call(this, tc, itemId);
  }

  private renderToolItemSimple(tc: ToolCallState): string {
    return renderToolItemSimpleImpl.call(this, tc);
  }

  private renderAgent(tc: ToolCallState, itemId: string): string {
    return renderAgentImpl.call(this, tc, itemId);
  }

  private _agentTaskDividers(tc: ToolCallState): Array<{ index: number; name: string; status?: string }> {
    return _agentTaskDividersImpl.call(this, tc);
  }

  private renderAssistantText(item: Extract<TimelineItem, { kind: "assistant_text" }>): string {
    return renderAssistantTextImpl.call(this, item);
  }

  private renderErrorCard(item: Extract<TimelineItem, { kind: "error_card" }>): string {
    return renderErrorCardImpl.call(this, item);
  }

  private renderWarningCard(item: Extract<TimelineItem, { kind: "warning_card" }>): string {
    return renderWarningCardImpl.call(this, item);
  }

  private renderBranchSwitcher(): string {
    return renderBranchSwitcherImpl.call(this);
  }

  private renderCompactStartingCard(item: Extract<TimelineItem, { kind: "compact_starting" }>): string {
    return renderCompactStartingCardImpl.call(this, item);
  }

  private renderCompactCard(item: Extract<TimelineItem, { kind: "compact" }>): string {
    return renderCompactCardImpl.call(this, item);
  }

  private renderSystemMessage(item: Extract<TimelineItem, { kind: "system_message" }>): string {
    return renderSystemMessageImpl.call(this, item);
  }

  private renderSpecCard(item: Extract<TimelineItem, { kind: "spec_card" }>): string {
    return renderSpecCardImpl(this, item);
  }

  private renderPlanCard(item: Extract<TimelineItem, { kind: "plan_card" }>): string {
    return renderPlanCardImpl(this, item);
  }

  private parsePlanSections(text: string): { plan: string; steps: string; checklist: string } {
    return parsePlanSectionsImpl(text);
  }

  private renderWorkflowCard(_item: Extract<TimelineItem, { kind: "workflow" }>): string {
    return renderWorkflowCardImpl(this, _item);
  }

  private handleDelegateClick(e: MouseEvent): void {
    handleDelegateClickImpl.call(this, e);
  }

  private toggleItem(id: string, el: HTMLElement): void {
    toggleItemImpl.call(this, id, el);
  }

  /** Opens a unified dropdown menu anchored to the given action button.
   *  Reuses the same visual language as the model selector / @-command
   *  menus (`.mention-dropdown` / `.mention-dropdown-item`). */
  private openActionMenu(
    btn: HTMLElement,
    items: Array<{ icon: string; label: string; onClick: () => void }>,
  ): void {
    openActionMenuImpl.call(this, btn, items);
  }

  private closeActionMenu(): void {
    closeActionMenuImpl.call(this);
  }

  /** Re-generates the assistant reply starting from the preceding user
   *  message, forking the branch with the given retry mode. */
  private performRetry(msgId: string, mode: string): void {
    performRetryImpl.call(this, msgId, mode);
  }

  private autoScroll(): void {
    autoScrollImpl.call(this);
  }

  private toggleWelcome(show: boolean): void {
    toggleWelcomeImpl.call(this, show);
  }

  /** Scrolls the message list to the bottom (unless the user scrolled up). */
  scrollToBottom(): void {
    scrollToBottomImpl.call(this);
  }

  /** Scrolls to the bottom and keeps re-pinning until any async media
   *  (images / videos inside the message list) has finished loading. */
  private scrollToBottomAfterMedia(): void {
    scrollToBottomAfterMediaImpl.call(this);
  }

  /** Keeps the floating scroll-to-bottom button in sync with the real state
   *  of the chat container. */
  private syncScrollButton(): void {
    syncScrollButtonImpl.call(this);
  }

  /** Public reset so the view-switch cleanup can hide the button eagerly. */
  resetScrollButton(): void {
    resetScrollButtonImpl.call(this);
  }

  private _currentQuote: StatusQuote | null = null;

  private _updateStatusBar(running: boolean): void {
    _updateStatusBarImpl.call(this, running);
  }
}
