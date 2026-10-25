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
import { findSlashCommand } from "../ui/slash-commands.js";
import { EALoader } from "../features/ealoader.js";
import { MediaViewer } from "./media-viewer.js";
import { renderMarkdown, escapeHtml } from "./chat_markdown.js";
import { getToolSummary, getToolInlineSummary, getToolBodyText, _toolStatusHtml, getAgentName, formatAgentLabel, createLucideIcons, saveVideoPlayback, restoreVideoPlayback, flashCopyButton } from "./chat_tool_display.js";
import { buildTimeline, buildRenderKey, buildSubAgentTimeline, selectSubAgentTaskMessages, pickRandomQuote, _fmtTokens, fmtSize } from "./timeline.js";
import { guardRender, reportUiFault } from "../core/ui_boundary.js";
import type { TimelineItem, StatusQuote } from "./timeline.js";
import { ChatScrollIndicator } from "./chat_scroll_indicator.js";
import { renderSpecCardImpl, renderPlanCardImpl, renderWorkflowCardImpl } from "./chat_review_cards.js";
import { renderItemHTMLImpl, renderAIHeaderImpl, renderModeCardImpl, renderUserItemImpl, renderThinkingStripImpl, renderToolCallImpl, renderQuestionCardImpl, renderInfoCardImpl, renderFileCardImpl, renderTerminalCardImpl, renderExpandableStripImpl, renderToolItemSimpleImpl, renderAgentImpl, _agentTaskDividersImpl, renderAssistantTextImpl, renderErrorCardImpl, renderWarningCardImpl, renderBranchSwitcherImpl, renderCompactStartingCardImpl, renderCompactCardImpl, renderCompactNoticeImpl, renderModelSwitchNoticeImpl, renderRetryNoticeImpl, renderSystemMessageImpl } from "./chat_item_render.js";
import { handleDelegateClickImpl, toggleItemImpl, performRetryImpl, autoScrollImpl, toggleWelcomeImpl, scrollToBottomImpl, scrollToBottomAfterMediaImpl, syncScrollButtonImpl, resetScrollButtonImpl, mountStatusBarImpl, syncStatusBarPositionImpl, startStatusBarFollowImpl, _updateStatusBarImpl } from "./chat_actions.js";
import { requestRenderImpl, renderForceImpl, renderImpl, renderSubAgentIntoImpl, computeExpandedImpl, activeThinkingIdImpl, applyAutoExpandImpl, buildTimelineHTMLImpl, fullRenderParallelImpl, fullRenderImpl, incrementalTextUpdateImpl, appendTimelineTailImpl, patchToolItemImpl, refreshToolCardsImpl, noteBodyScroll, buildDelegatedPromptBubbleImpl } from "./chat_render_engine.js";

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
  /** Picked-but-not-yet-submitted options per question ask (tcId -> question
   *  index -> option value). DOM classList dies on re-render; this map is
   *  what survives a session switch. */
  private questionSelections = new Map<string, Record<number, string>>();
  private lastAssistantMsgId = "";
  /** Streaming markdown cache. Re-rendering the whole accumulated answer on
   *  every animation frame is O(n^2) per turn and re-highlights every code
   *  block behind it; these fields let the incremental path skip unchanged
   *  text and cap the re-parse rate while the model is still writing. */
  private _streamMarkdownId = "";
  private _streamMarkdownText = "";
  private _streamMarkdownHtml = "";
  private _streamMarkdownAt = 0;
  /** Rendered HTML for finished assistant text blocks, keyed by timeline id.
   *  A structural re-render (tool result, turn boundary, session reopen) used
   *  to re-run markdown + highlight.js for every historical message in the
   *  transcript. Unchanged blocks now reuse their HTML, so only the block
   *  that actually changed is parsed. Bounded to keep memory flat. */
  public assistantHtmlCache = new Map<string, {
    content: string;
    isStreaming: boolean;
    hasError: boolean;
    messageId: string;
    locale: string;
    html: string;
    /** True when `html` is the escaped plain-text fallback, not markdown. */
    deferred: boolean;
  }>();
  /** Render historical assistant blocks as plain text until idle. Set before
   *  a boot/session render, cleared once the last block has been upgraded. */
  public deferMarkdown = false;
  private _markdownUpgradeGen = 0;
  private _markdownUpgradeStartedAt = 0;
  /** Scroll sweep: upgrade newly-visible raw blocks as the reader scrolls. */
  private _mdSweepBound = false;
  private _mdSweepTimer = 0;
  /** Per-item signature of the last incremental DOM sync. Unchanged items are
   *  skipped entirely, so a long transcript costs per-frame work proportional
   *  to what is actually streaming instead of to its total length. */
  public incrementalSigs = new Map<string, string>();
  private _lastRunning = false;
  /** Session id rendered by the last fullRender; detects session switches. */
  private _lastRenderedSessionId = "";
  /** One-shot flag: force-pin the next fullRender to the bottom (rollback,
   *  branch switch, session entry). Consumed by fullRenderImpl. */
  private _forcePinNextRender = false;
  private _scrollSig = "";
  private _inRenderForce = false;
  private liveLoader: EALoader | null = null;
  private scrollIndicator: ChatScrollIndicator;
  /** Watches #message-list growth while a turn runs; drives the status bar's
   *  transform so it follows the latest output without restarting its CSS
   *  animation. */
  public statusBarRO: ResizeObserver | null = null;
  /** Handle of the per-frame re-pin loop that keeps the status bar glued to
   *  the transcript's last line while a turn runs (0 = not running). */
  public statusBarRaf = 0;
  /** Number of timeline items the last fullRender rendered — lets the
   *  streaming tail-append path know which items are already in the DOM. */
  public renderedItemCount = 0;
  /** Callback invoked when the user clicks "View Changes" on an artifact file. */
  public onViewChanges: ((path: string) => void) | null = null;
  /** Callback invoked when the user clicks a file/image the model mentioned: opens the in-app file viewer. */
  public onOpenFile: ((path: string) => void) | null = null;
  /** Callback invoked when the user clicks 「继续」 on an interrupted turn. */
  public onResume: (() => void) | null = null;

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
    // Lucide only swaps `[data-lucide]` placeholders it is explicitly pointed
    // at.  The button is built here, after index.html's one global pass, so
    // without this the circle renders with an empty <i> inside.
    createLucideIcons(scrollBtn);
    this.container.parentElement?.appendChild(scrollBtn);
    scrollBtn.addEventListener("click", () => {
      this.container.scrollTo({ top: this.container.scrollHeight, behavior: "smooth" });
    });

    const w = window as any;
    // Review decisions are bound inline on the card's buttons (same mechanism
    // as __openSubAgentView). They are NOT in the message-list delegate: the
    // clicks always fired, but the server silently dropped them because
    // ClientPlanApprove/Reject (and ClientSteer) had no `from_dict`, so
    // `parse_client_message` raised and the receive loop answered with a
    // generic parse_error. See backend/tests/test_client_message_dispatch.py.
    w.__reviewDecision = (kind: string, reviewId: string) => {
      const id = (reviewId || "").trim();
      console.log("[review] decision clicked:", kind, id);
      if (!id) return;
      if (kind === "approve") {
        send({ type: "plan_approve", session_id: getState().sessionId, review_id: id } as any);
      } else {
        // No feedback is collected in the card: the loop hands the rejection
        // back to the model with an instruction to ask the user what has to
        // change, so the follow-up is a question in the transcript.
        send({ type: "plan_reject", session_id: getState().sessionId, review_id: id, feedback: "" } as any);
      }
    };
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
      // Stepping back from a sub-agent must land on the parent's latest turn
      // (the agent card itself). The userScrolledUp flag is stale here: it
      // carries whatever the sub-agent transcript's scrolling left it at,
      // which made the preserved-offset branch position the parent view a
      // few hundred pixels ABOVE the bottom. Force-pin the next render.
      this._forcePinNextRender = true;
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
        // Forced paint through the unified scheduler's immediate mode.
        this.renderForce();
      }
    };
    w.__goToRootView = () => {
      const st = getState();
      // Leaving every sub-agent level: drop the session-backed marker so the
      // parent view is never mistaken for a sub-agent snapshot.
      w.__activeSubAgentSessionId = undefined;
      w.__pendingSubAgentView = undefined;
      // Same stale-scroll fix as __closeSubAgentView: the root view must
      // land on its latest turn, not on the sub-agent's leftover offset.
      this._forcePinNextRender = true;
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
    // Mark a question card as answered: disable every control so the user
    // gets instant feedback before the backend's tool_result collapses the
    // card into a plain strip.
    const __markQuestionSubmitted = (card: HTMLElement) => {
      card.classList.add("q-answered");
      card.querySelectorAll<HTMLButtonElement | HTMLInputElement>("button,input").forEach(el => { el.disabled = true; });
    };
    // Collect one answer per rendered .q-item (manual input wins over the
    // selected option) and submit.  A batched ask sends an array — one entry
    // per question in ask order; a single-question ask sends a plain string.
    const __submitQuestionAnswers = (event: Event, btn: HTMLElement) => {
      event.stopPropagation();
      // Same-card-first resolution as the other __handlers: the automation
      // detail reuses the timeline pipeline, so closest() guarantees we
      // operate on the card the user actually clicked.
      const card = btn.closest('[id^="tc-"]') as HTMLElement | null;
      if (!card || card.classList.contains("q-answered")) return;
      const items = Array.from(card.querySelectorAll<HTMLElement>(".q-item"));
      if (items.length === 0) return;
      const answers: string[] = [];
      let firstMissing: HTMLElement | null = null;
      for (const item of items) {
        const input = item.querySelector<HTMLInputElement>(".q-input");
        const selected = item.querySelector<HTMLElement>(".q-option-card.selected");
        const answer = (input?.value ?? "").trim() || selected?.getAttribute("data-value") || "";
        if (!answer) {
          item.classList.add("q-missing");
          if (!firstMissing) firstMissing = item;
        }
        answers.push(answer);
      }
      if (firstMissing) {
        // Stepped cards show one question at a time — jump to the first
        // unanswered one before focusing its input, or the user would see no
        // reaction at all.
        __qShow(card, items.indexOf(firstMissing));
        firstMissing.querySelector<HTMLInputElement>(".q-input")?.focus();
        return;
      }
      send({ type: "respond_question", tool_call_id: card.id.replace("tc-", ""), answers: answers.length > 1 ? answers : answers[0] });
      __markQuestionSubmitted(card);
    };
    w.__answerQuestion = (event: Event, btn: HTMLElement, _cardId?: string) => {
      __submitQuestionAnswers(event, btn);
    };
    // Option click behavior.  The input row is reserved for hand-typed
    // answers — clicking an option never fills it.  A single-question card
    // answers immediately on click; a batched card toggles the option's
    // selected state (radio behavior within its question) and waits for the
    // shared submit button.
    // Stepped navigation for batched question cards.  All .q-item blocks are
    // rendered up front (answers survive switching back and forth); __qShow
    // toggles which one is visible and mirrors the position onto the footer
    // buttons: "previous" hides on the first question, "next" hides on the
    // last one, and "submit" appears ONLY on the last question.  __qNav moves
    // relative to the currently visible question (no wrap-around).
    const __qShow = (card: HTMLElement, index: number) => {
      const items = Array.from(card.querySelectorAll<HTMLElement>(".q-item"));
      if (items.length === 0) return;
      const clamped = Math.min(items.length - 1, Math.max(0, index));
      const last = items.length - 1;
      items.forEach((it, i) => { it.hidden = i !== clamped; });
      const prev = card.querySelector<HTMLElement>('[data-nav="-1"]');
      const next = card.querySelector<HTMLElement>('[data-nav="1"]');
      const submit = card.querySelector<HTMLElement>("[data-qsubmit]");
      if (prev) prev.hidden = clamped === 0;
      if (next) next.hidden = clamped === last;
      if (submit) submit.hidden = clamped !== last;
    };
    w.__qShow = __qShow;
    w.__qNav = (btn: HTMLElement, dir: number) => {
      const card = btn.closest('[id^="tc-"]') as HTMLElement | null;
      if (!card || card.classList.contains("q-answered")) return;
      const items = Array.from(card.querySelectorAll<HTMLElement>(".q-item"));
      const cur = items.findIndex(it => !it.hidden);
      __qShow(card, (cur < 0 ? 0 : cur) + dir);
    };
    w.__selectQuestionOption = (btn: HTMLElement) => {
      const card = btn.closest('[id^="tc-"]') as HTMLElement | null;
      if (!card || card.classList.contains("q-answered")) return;
      const item = btn.closest(".q-item") as HTMLElement | null;
      if (!item) return;
      item.classList.remove("q-missing");
      if (card.dataset.batch !== "1") {
        const value = btn.getAttribute("data-value") || "";
        send({ type: "respond_question", tool_call_id: card.id.replace("tc-", ""), answers: value });
        __markQuestionSubmitted(card);
        return;
      }
      const wasSelected = btn.classList.contains("selected");
      item.querySelectorAll(".q-option-card.selected").forEach(b => b.classList.remove("selected"));
      if (!wasSelected) btn.classList.add("selected");
      // Stepped flow: once this question is answered, jump to the next one.
      // The arrows stay available for reviewing / changing earlier answers.
      const items = Array.from(card.querySelectorAll<HTMLElement>(".q-item"));
      const idx = items.indexOf(item);
      // Persist the pick on the instance — the DOM class does not survive a
      // session switch (full re-render), this map does.
      const tcId = card.id.replace("tc-", "");
      const selMap = { ...(this.questionSelections.get(tcId) ?? {}) };
      if (wasSelected) delete selMap[idx];
      else selMap[idx] = btn.getAttribute("data-value") || "";
      this.questionSelections.set(tcId, selMap);
      if (idx >= 0 && idx < items.length - 1) __qShow(card, idx + 1);
    };
    w.__hoverOn = (el: HTMLElement) => {
      const wrap = el.querySelector(".icon-wrap");
      if (wrap) wrap.classList.add("hover");
    };
    w.__hoverOff = (el: HTMLElement) => {
      const wrap = el.querySelector(".icon-wrap");
      if (wrap) wrap.classList.remove("hover");
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
    // Card bodies follow their own tail as content streams in. A body the user
    // scrolled away from is remembered so the next update never yanks it back.
    // Scroll events do not bubble, so this has to listen in the capture phase.
    this.ml.addEventListener("scroll", (e) => noteBodyScroll(e.target), true);
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
        this.userExpandedItems.clear();
        _openedHtmlCards.clear();
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
        // rAF, not a synchronous render: several state writes land in the
        // same boot tick (session id, messages, settings). Rendering each one
        // eagerly blocked the UI thread for the whole restored transcript.
        this.requestRender();
        return;
      }
      if (msgs.length === 0 && _lastMsgLen > 0) {
        _lastMsgLen = 0;
        this.renderedKey = "";
        this.expandedItems.clear();
        this.userCollapsedItems.clear();
        this.userExpandedItems.clear();
        _openedHtmlCards.clear();
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
        this.requestRender();
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
    }, ["sessionId", "messages", "subAgentView", "subAgentBreadcrumb", "settings"]);
    // A locale switch must rebuild the transcript: every string in a timeline
    // item comes from `t()` at render time, and the render key is unchanged,
    // so a plain render() would keep showing the previous language.
    onLocaleChange(() => {
      this.renderedKey = "";
      this.requestRender();
    });
    // The tool-UI catalog arrives via `plugin_ui_sync`, typically after the
    // transcript was first painted (a client connecting during plugin warmup
    // gets an empty catalog first). Every tool card resolves its card shape /
    // icon / label from that catalog at render time, so a real catalog change
    // must rebuild the already-rendered items — otherwise plugin tool cards
    // (e.g. the agent strip) keep their neutral fallback look until an
    // unrelated view switch happens to force a full render.
    window.addEventListener("encre-tool-meta-updated", () => {
      // Only tool cards derive their markup from the catalog; rebuilding the
      // whole transcript (the old behaviour) made the answer text visibly
      // refresh when plugin metadata arrived after boot.
      guardRender("chat", "tool-refresh", () => this.refreshToolCards());
    });
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

  /** Escaped-text render for an assistant block awaiting markdown upgrade. */
  public renderAssistantTextRaw(item: Extract<TimelineItem, { kind: "assistant_text" }>): string {
    return renderAssistantTextImpl.call(this, item, true);
  }

  /** Store one assistant block's rendered HTML, keeping the cache bounded. */
  public cacheAssistantHtml(
    id: string,
    entry: {
      content: string;
      isStreaming: boolean;
      hasError: boolean;
      messageId: string;
      locale: string;
      html: string;
      deferred: boolean;
    },
  ): void {
    this.assistantHtmlCache.delete(id);
    this.assistantHtmlCache.set(id, entry);
    if (this.assistantHtmlCache.size > 600) {
      const oldest = this.assistantHtmlCache.keys().next().value;
      if (oldest !== undefined) this.assistantHtmlCache.delete(oldest);
    }
  }

  /**
   * Upgrade deferred assistant blocks to full markdown, newest first, in
   * idle slices. Each block is patched on its own, so the UI keeps handling
   * input between blocks and one heavy block cannot hold the rest back.
   */
  public beginMarkdownUpgrade(delayMs = 0): void {
    const gen = ++this._markdownUpgradeGen;
    this._markdownUpgradeStartedAt = performance.now();
    if (!this._mdSweepBound) {
      this._mdSweepBound = true;
      this.container.addEventListener("scroll", () => {
        if (!this.deferMarkdown || this._mdSweepTimer) return;
        // Coalesce scroll storms; only blocks near the viewport get parsed.
        this._mdSweepTimer = window.setTimeout(() => {
          this._mdSweepTimer = 0;
          if (this.deferMarkdown) this.beginMarkdownUpgrade(0);
        }, 140);
      }, { passive: true });
    }
    const idle = () => {
      if (gen !== this._markdownUpgradeGen) return;
      const ric = (window as any).requestIdleCallback as
        | ((cb: () => void, opts?: { timeout: number }) => number)
        | undefined;
      if (ric) ric(() => this._upgradeMarkdownChunk(gen), { timeout: 300 });
      else window.setTimeout(() => this._upgradeMarkdownChunk(gen), 0);
    };
    const waitFrames = () => {
      // Let the cheap raw render reach the screen before parsing the first
      // block; a session switch may have superseded this upgrade by then.
      if (gen !== this._markdownUpgradeGen) return;
      requestAnimationFrame(() => requestAnimationFrame(idle));
    };
    if (delayMs > 0) window.setTimeout(waitFrames, delayMs);
    else waitFrames();
  }

  private _upgradeMarkdownChunk(gen: number): void {
    if (gen !== this._markdownUpgradeGen) return;
    const nodes = Array.from(this.ml.querySelectorAll(".msg-text--raw")) as HTMLElement[];
    if (nodes.length === 0) {
      this.deferMarkdown = false;
      if (this._markdownUpgradeStartedAt) {
        console.log(`[perf] markdown upgrade ${Math.round(performance.now() - this._markdownUpgradeStartedAt)}ms`);
        this._markdownUpgradeStartedAt = 0;
      }
      return;
    }
    // Only blocks near the viewport are worth parsing. Upgrading the whole
    // restored transcript at boot made one 200ms+ long task; offscreen raw
    // text is invisible anyway and is upgraded by the scroll sweep.
    const containerRect = this.container.getBoundingClientRect();
    const nearViewport = (el: HTMLElement): boolean => {
      const r = el.getBoundingClientRect();
      return r.bottom >= containerRect.top - 600 && r.top <= containerRect.bottom + 600;
    };
    const candidates = nodes.filter(nearViewport);
    if (candidates.length === 0) return;
    const started = performance.now();
    const container = this.container;
    const prevTop = container.scrollTop;
    const prevHeight = container.scrollHeight;
    for (let i = candidates.length - 1; i >= 0; i--) {
      if (performance.now() - started >= 8) break;
      const contentEl = candidates[i];
      if (!contentEl.isConnected || !contentEl.classList.contains("msg-text--raw")) continue;
      const wrapper = contentEl.closest(".assistant-text") as HTMLElement | null;
      const itemId = wrapper?.getAttribute("data-id") || "";
      const text = contentEl.textContent || "";
      const _blockStart = performance.now();
      let html = "";
      try {
        html = renderMarkdown(text);
      } catch (e) {
        // One malformed block stays as plain text; the upgrade continues.
        reportUiFault("markdown", itemId || "block", e);
        continue;
      }
      const _blockMs = performance.now() - _blockStart;
      if (_blockMs >= 30) {
        console.warn(`[perf] markdown block ${Math.round(_blockMs)}ms chars=${text.length}`);
      }
      contentEl.innerHTML = html;
      contentEl.classList.remove("msg-text--raw");
      if (html.includes("data-lucide=")) createLucideIcons(contentEl);
      guardRender("media", "markdown-upgrade", () => window.__initMediaCards?.(contentEl));
      if (itemId && wrapper) {
        const hasError = wrapper.classList.contains("has-error");
        const messageId = wrapper.getAttribute("data-message-id") || "";
        this.cacheAssistantHtml(itemId, {
          content: text,
          isStreaming: false,
          hasError,
          messageId,
          locale: getLocale(),
          html: `<div class="assistant-text${hasError ? " has-error" : ""}" data-id="${itemId}" data-message-id="${messageId}"><div class="msg-text">${html}</div></div>`,
          deferred: false,
        });
      }
    }
    // Upgrading changes block heights above the viewport. Keep the reader's
    // visual position (or the bottom pin) stable across the swap.
    if (this.userScrolledUp) {
      const delta = container.scrollHeight - prevHeight;
      if (delta) container.scrollTop = prevTop + delta;
    } else {
      this.autoScroll();
    }
    // Continue only while raw blocks near the viewport remain; otherwise wait
    // for the scroll sweep instead of burning idle time on offscreen text.
    const remaining = (Array.from(this.ml.querySelectorAll(".msg-text--raw")) as HTMLElement[])
      .filter(nearViewport);
    if (remaining.length === 0) return;
    const ric = (window as any).requestIdleCallback as
      | ((cb: () => void, opts?: { timeout: number }) => number)
      | undefined;
    if (ric) ric(() => this._upgradeMarkdownChunk(gen), { timeout: 300 });
    else window.setTimeout(() => this._upgradeMarkdownChunk(gen), 0);
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

  /** Re-render a single tool card in place; false when it cannot be patched. */
  public patchToolItem(tcId: string): boolean {
    return patchToolItemImpl.call(this, tcId);
  }

  /** Re-render tool cards after a tool-UI catalog change (text untouched). */
  public refreshToolCards(): void {
    refreshToolCardsImpl.call(this);
  }

  /** Delegated task prompt as a plain user bubble (sub-agent view). */
  public buildDelegatedPromptBubble(tc: ToolCallState): string {
    return buildDelegatedPromptBubbleImpl.call(this, tc);
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

  private renderBranchSwitcher(group?: { ids: string[]; activeIdx: number }): string {
    return renderBranchSwitcherImpl.call(this, group);
  }

  private renderCompactStartingCard(item: Extract<TimelineItem, { kind: "compact_starting" }>): string {
    return renderCompactStartingCardImpl.call(this, item);
  }

  private renderRetryNotice(item: Extract<TimelineItem, { kind: "retry_notice" }>): string {
    return renderRetryNoticeImpl.call(this, item);
  }

  private renderCompactCard(item: Extract<TimelineItem, { kind: "compact" }>): string {
    return renderCompactCardImpl.call(this, item);
  }

  private renderCompactNotice(item: Extract<TimelineItem, { kind: "compact_notice" }>): string {
    return renderCompactNoticeImpl.call(this, item);
  }

  private renderModelSwitchNotice(item: Extract<TimelineItem, { kind: "model_switch" }>): string {
    return renderModelSwitchNoticeImpl.call(this, item);
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

  private renderWorkflowCard(_item: Extract<TimelineItem, { kind: "workflow" }>): string {
    return renderWorkflowCardImpl(this, _item);
  }

  private handleDelegateClick(e: MouseEvent): void {
    handleDelegateClickImpl.call(this, e);
  }

  private toggleItem(id: string, el: HTMLElement): void {
    toggleItemImpl.call(this, id, el);
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
   *  (images / videos inside the message list) has finished loading.
   *  When force is set the userScrolledUp guard is bypassed (session entry). */
  private scrollToBottomAfterMedia(force = false): void {
    scrollToBottomAfterMediaImpl.call(this, force);
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

  /** Public reset for the right-edge turn ruler, called by the view-switch
   *  cleanup so the next render rebuilds it from scratch. Clears the turn
   *  signature too: without that the incoming render takes the no-rebuild
   *  `update()` path and the ruler would stay empty. */
  resetScrollIndicator(): void {
    this.scrollIndicator.reset();
    this._scrollSig = "";
  }

  /** Requests the next full render to pin the view to the bottom regardless
   *  of the userScrolledUp state. Used after rollback / branch switches:
   *  the conversation just got truncated, so the pre-event scroll offset is
   *  meaningless and the user expects to land on the new last turn. */
  requestPinToBottom(): void {
    this._forcePinNextRender = true;
  }

  private _currentQuote: StatusQuote | null = null;

  private _updateStatusBar(running: boolean): void {
    _updateStatusBarImpl.call(this, running);
  }

  /** Re-mounts the status bar into #message-list after a fullRender rebuilt it. */
  mountStatusBar(): void {
    mountStatusBarImpl.call(this);
  }

  /** Repositions the status bar under the list's latest output (transform only). */
  syncStatusBarPosition(): void {
    syncStatusBarPositionImpl.call(this);
  }

  /** Starts the per-frame re-pin loop that keeps the status bar glued to the
   *  transcript's last line while a turn runs (idempotent). */
  startStatusBarFollow(): void {
    startStatusBarFollowImpl.call(this);
  }

  /** Streaming fast path: appends only newly arrived timeline items to the
   *  DOM instead of rebuilding the whole transcript. Returns false when the
   *  change is structural and a fullRender is required. */
  appendTimelineTail(timeline: TimelineItem[], prevCount: number): boolean {
    return appendTimelineTailImpl.call(this, timeline, prevCount);
  }
}
