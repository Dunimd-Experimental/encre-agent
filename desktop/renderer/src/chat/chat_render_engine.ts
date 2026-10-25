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
 * Render pipeline for the Chat view (full / parallel / incremental).
 *
 * Each pipeline stage was extracted from the `Chat` class and is invoked
 * with `impl.call(this, ...)` from a thin class delegate, so `this` still
 * refers to the owning `Chat` instance and every private member stays
 * reachable.
 */

// @ts-nocheck -- pipeline stages relocated from the Chat class; called via
// `XImpl.call(this, ...)` so the instance context is preserved.

import type { Chat } from "./chat.js";
import type { Message, ToolCallState } from "../core/types.js";
import type { TimelineItem, BranchSwitcherGroup } from "./timeline.js";
import { scheduleRender, cancelScheduledRender, runScheduledRenderNow, RENDER_PRIORITY } from "../core/render_scheduler.js";
import { guardHtml, guardRender, reportUiFault } from "../core/ui_boundary.js";
import { getState, setSubAgentView, isEnabled, toolCallMatchesId } from "../core/state.js";
import { t, getLocale } from "../features/i18n.js";
import { EALoader } from "../features/ealoader.js";
import { renderMarkdown, escapeHtml } from "./chat_markdown.js";
import { getToolSummary, getToolInlineSummary, getToolBodyText, _toolStatusHtml, createLucideIcons, saveVideoPlayback, restoreVideoPlayback } from "./chat_tool_display.js";
import { metaFor } from "./tool_meta.js";
import { buildTimeline, buildRenderKey, buildSubAgentTimeline, selectSubAgentTaskMessages } from "./timeline.js";

  /**
   * Replays the unified content-enter motion on an element that is already
   * visible.  A session switch swaps `#message-list`'s innerHTML in place —
   * the node never passes through `display:none`, so the `:not(.hidden)` CSS
   * rule never fires and the tape would snap straight to the new transcript.
   * Dropping the class, forcing a reflow, then re-adding it restarts the
   * keyframes.  (In the reverse direction — history arriving after the
   * welcome screen — the element really does unhide, so CSS handles it and
   * no JS call is needed.)
   */
  function playEnterAnim(el: HTMLElement | null | undefined): void {
    if (!el) return;
    el.classList.remove("session-enter");
    void el.offsetWidth;
    el.classList.add("session-enter");
  }

  // === Tail-following card bodies ==========================================
  //
  // Every expandable card body that clips its content (thinking, tool result,
  // terminal output) shows its NEWEST lines, not its first ones: the scrolling
  // window sits on the bottom and content growing below it pushes the window
  // down with it. The marker class lives on the body so the behaviour is
  // scoped to the cards the user asked for and cannot leak onto a review or
  // spec document, which is read from the top. A diff nests its own scroller,
  // so the inner `.diff-body` is tracked alongside the body that wraps it.
  const TAIL_BODY_SELECTOR = ".tail-follow, .tail-follow .diff-body";
  const TAIL_SLACK = 4;

  /**
   * Bodies the user scrolled away from their bottom. They are left where the
   * user put them until scrolled back down. Keyed by element identity: a
   * re-render builds fresh nodes, so a body re-arms for tailing on its own.
   */
  const _detachedBodies = new WeakSet<HTMLElement>();

  function isBodyAtBottom(el: HTMLElement): boolean {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= TAIL_SLACK;
  }

  /** Tracks a transcript scroll event's target; wired to the capture phase
   *  because scroll events do not bubble out of the body that fired them. */
  export function noteBodyScroll(target: EventTarget | null): void {
    const el = target as HTMLElement | null;
    if (!el || !el.classList || !el.matches?.(TAIL_BODY_SELECTOR)) return;
    if (isBodyAtBottom(el)) _detachedBodies.delete(el);
    else _detachedBodies.add(el);
  }

  /** Pins one body to its newest line unless the user scrolled it away. A
   *  body that does not overflow is left alone (nothing to scroll). */
  export function pinBodyTail(el: HTMLElement | null): void {
    if (!el || _detachedBodies.has(el)) return;
    if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;
  }

  /** Pins every tail-following body under `root` — the full-render pass.
   *  Measured first, moved second: interleaving `scrollTop` writes with
   *  `scrollHeight` reads would force a layout per card (layout thrashing). */
  export function pinBodyTails(root: ParentNode): void {
    const pending: Array<{ el: HTMLElement; top: number }> = [];
    root.querySelectorAll<HTMLElement>(TAIL_BODY_SELECTOR).forEach((el) => {
      if (_detachedBodies.has(el)) return;
      if (el.scrollHeight > el.clientHeight) pending.push({ el, top: el.scrollHeight });
    });
    for (const { el, top } of pending) el.scrollTop = top;
  }

  /**
   * Live countdown for the transient retry line ("3s 后重试" -> 2 -> 1).
   *
   * The remaining seconds deliberately are NOT part of the render key: a value
   * that changes every second would force a full transcript re-render every
   * second.  Instead the number is ticked in place.  The line carries
   * `data-retry-deadline` (epoch ms), re-read from the DOM on every tick, so a
   * re-render that replaces the node is picked up automatically.  The tick
   * stops itself the moment the countdown is over or the line is gone, so a
   * stale interval can never outlive its notice.
   */
  let _retryCountdownTimer: number | null = null;

  function _stopRetryCountdown(): void {
    if (_retryCountdownTimer !== null) {
      window.clearInterval(_retryCountdownTimer);
      _retryCountdownTimer = null;
    }
  }

  function _paintRetryCountdown(ml: HTMLElement | null): void {
    const span = ml?.querySelector("[data-retry-deadline] .turn-line__countdown") as HTMLElement | null;
    if (!span) {
      _stopRetryCountdown();
      return;
    }
    const line = span.closest("[data-retry-deadline]") as HTMLElement | null;
    const deadline = Number(line?.getAttribute("data-retry-deadline")) || 0;
    const remaining = Math.ceil((deadline - Date.now()) / 1000);
    if (remaining <= 0) {
      // The wait is over and the retried request is in flight; a frozen "0"
      // would be a lie, so drop the countdown clause entirely.
      if (span.textContent) span.textContent = "";
      _stopRetryCountdown();
      return;
    }
    const text = t("chat.retryCountdown", { seconds: remaining });
    if (span.textContent !== text) span.textContent = text;
  }

  function syncRetryCountdown(ml: HTMLElement | null): void {
    _paintRetryCountdown(ml);
    if (
      _retryCountdownTimer === null &&
      ml?.querySelector("[data-retry-deadline] .turn-line__countdown")
    ) {
      _retryCountdownTimer = window.setInterval(() => _paintRetryCountdown(ml), 1000);
    }
  }

  export function requestRenderImpl(this: Chat): void {
    // Keyed scheduler: any number of state writes in one tick produce a
    // single chat paint, ordered before the sidebar and side panels.
    scheduleRender("chat", () => this.render(), RENDER_PRIORITY.chat);
  }

  export function renderForceImpl(this: Chat): void {
    // A forced paint renders current state now through the scheduler's
    // immediate mode; a queued paint would be a duplicate of the same state.
    cancelScheduledRender("chat");
    this.renderedKey = "";
    this._inRenderForce = true;
    const paintNow = () =>
      runScheduledRenderNow("chat", () => this.render(), RENDER_PRIORITY.chat);
    try {
      const st = getState();
      const subTc = st.subAgentView;
      const isSubAgentStreaming = !!subTc &&
        (subTc.status === "running" || subTc.status === "pending");
      if (!isSubAgentStreaming) {
        paintNow();
        return;
      }
      // Throttle sub-agent streaming renders (single AND parallel) so the
      // main thread stays responsive enough for scrolling and window
      // dragging.  Without this, every tool_progress frame triggers a full
      // transcript re-render, which freezes the UI while a sub-agent runs.
      this._pendingParallelRender = true;
      if (this._parallelRenderTimer === null) {
        paintNow();
        this._parallelRenderTimer = window.setTimeout(() => {
          this._parallelRenderTimer = null;
          if (this._pendingParallelRender) {
            this._pendingParallelRender = false;
            paintNow();
          }
        }, 100);
      }
    } finally {
      this._inRenderForce = false;
    }
  }

  /** Re-renders the message list from current state (RAF-throttled). */
  export function renderImpl(this: Chat): void {
    if (this.liveLoader) {
      this.liveLoader.destroy();
      this.liveLoader = null;
    }
    const state = getState();
    // Breadcrumb restoration: if the user navigated back to an ancestor
    // session via the breadcrumb, the sub-agent view may have been cleared
    // while waiting for the parent session to load. Once the parent session
    // arrives, automatically reopen the sub-agent view that corresponds to
    // the last breadcrumb entry so the UI lands on the right level.
    if (!state.subAgentView && state.subAgentBreadcrumb.length > 0) {
      const crumb = state.subAgentBreadcrumb[state.subAgentBreadcrumb.length - 1];
      if (crumb.sessionId === state.sessionId) {
        for (const msg of state.messages) {
          for (const tc of msg.toolCalls) {
            if (toolCallMatchesId(tc, crumb.toolCallId)) {
              setSubAgentView(tc);
              break;
            }
          }
          if (state.subAgentView) break;
        }
      }
    }
    if (state.subAgentView) {
      this.toggleWelcome(false);
      const subTc = state.subAgentView;
      // A session-backed sub-agent streams directly into the active session
      // snapshot after its transcript has been resumed. Inline sub-agents use
      // the tool-call snapshot instead.
      const sessionBacked = (window as any).__activeSubAgentSessionId === state.sessionId;
      // Prefer the live session snapshot while session-backed — the sub-agent
      // session streams every text/thinking/tool delta into `state.messages`,
      // so rendering from it makes the sub-agent view update incrementally
      // instead of showing one frozen tool-call snapshot and then the whole
      // transcript at once. Fall back to the inline snapshot when no session
      // has been resumed (e.g. plain inline sub-agent, automation detail).
      let subMsgs: Message[];
      if (sessionBacked) {
        subMsgs = state.messages.length > 0
          ? state.messages
          : (subTc.subAgentMessages ?? []);
      } else {
        subMsgs = subTc.subAgentMessages?.length
          ? subTc.subAgentMessages
          : [];
      }

      // When the user clicked one card of a parallel agent run, show only
      // that single task's transcript instead of the combined run.
      const selectedMsgs = selectSubAgentTaskMessages(subMsgs, subTc.taskIndex);
      const hasInlineData = selectedMsgs.length > 0;
      const isStreaming = sessionBacked
        ? state.running
        : (subTc.status === "running" || subTc.status === "pending");
      this.renderedKey = "__subagent__";
      if (hasInlineData) {
        // Combined parallel run (no specific task picked, >= 2 task
        // dividers): tile every task into its own pane via
        // fullRenderParallel. Routing it through the flat fullRender below
        // interleaves the two transcripts into one column.
        const combinedParallel =
          subTc.taskIndex === undefined &&
          subMsgs.filter((m) => m.mode === "task_divider").length >= 2;
        if (combinedParallel) {
          this.fullRenderParallel(subMsgs, isStreaming);
        } else {
          // Sub-agent snapshot arrived: render the real user + assistant
          // bubbles together.  The loader was already torn down at the
          // top of render(), so the message area is clean by now.
          const timeline = buildSubAgentTimeline(selectedMsgs, isStreaming);
          this.fullRender(timeline, selectedMsgs);
        }
      } else if (isStreaming) {
        // Still waiting for the first snapshot: show the delegated task
        // prompt as a plain user bubble right away (same bubble as the main
        // chat) instead of a bare centered loader. Only fall back to the
        // loader when no prompt text is available yet.
        const promptBubble = this.buildDelegatedPromptBubble(subTc);
        if (promptBubble) {
          this.ml.innerHTML = promptBubble;
        } else {
          this.ml.innerHTML = "";
          this.liveLoader = new EALoader(this.ml, { maxWidth: "96px" });
        }
      } else if (subTc.isError) {
        // Map the raw tool result into something the user can act on:
        // bare sandbox exit codes (-1 timeout / -2 Docker missing) get a
        // readable label, "Error: …" text is shown verbatim instead of
        // being used as a cryptic error code.
        const rawResult = typeof subTc.result === "string" ? subTc.result.trim() : "";
        let errorCode = String(rawResult || "AUTOMATION_EXECUTION_FAILED");
        if (rawResult === "-1") {
          errorCode = t("automation.errorBashTimeout") || "Command timed out (-1)";
        } else if (rawResult === "-2") {
          errorCode = t("automation.errorDockerMissing") || "Sandbox unavailable: Docker not installed (-2)";
        } else if (rawResult.startsWith("Error:") && rawResult.length > 6) {
          errorCode = rawResult.length > 500 ? rawResult.slice(0, 500) + "…" : rawResult;
        }
        this.ml.innerHTML = `<div class="si-empty-center" style="flex:1;gap:14px;">
          <i data-lucide="ban" class="lucide"></i>
          <div class="si-empty-title">${t("automation.executionFailed") || "Automation execution error"}</div>
          <div class="si-empty-sub" style="word-break:break-word;">${escapeHtml(errorCode)}</div>
        </div>`;
      } else {
        // No inline transcript survived (e.g. reopened after switching
        // sessions before the snapshot was persisted). Fall back to the
        // aggregated tool result text so the user still sees the real
        // output instead of a bare "no output" placeholder.
        const resultText = typeof subTc.result === "string" ? subTc.result.trim() : "";
        if (resultText) {
          this.ml.innerHTML = `<div class="sub-agent-result-fallback">${renderMarkdown(resultText)}</div>`;
        } else {
          // Finished (or never started) without producing any output.
          this.ml.innerHTML = `<div class="si-empty-center"><i data-lucide="bot" class="lucide"></i><span class="si-empty-title">${t("chat.noSubAgentOutput")}</span></div>`;
        }
      }
      createLucideIcons(this.ml);
      this._updateStatusBar(false);
      this.autoScroll();
      return;
    }
     this.toggleWelcome(state.messages.length === 0);
     const msgs = state.messages;
     if (msgs.length === 0) {
       if (this.ml.children.length > 0) {
         // Keep the previous session's content visible until the new
         // session's content arrives via fullRender — avoids a
         // welcome-screen flash between sessions.
         this.syncScrollButton();
         return;
       }
       this.ml.innerHTML = "";
       this.renderedKey = "";
       this.syncScrollButton();
       return;
     }
    const timeline = buildTimeline(msgs);
    const key = buildRenderKey(timeline);
    const wasRunning = this._lastRunning;
    this._lastRunning = state.running;
    // Force full render when streaming completes so auto-expand/collapse
    // logic in fullRender is applied correctly (the render key may not
    // change when running transitions from true to false).
    if (key !== this.renderedKey || (wasRunning && !state.running)) {
      // Streaming fast path: when the key only grew by whole tail segments
      // (a new tool card / text block appended mid-run), patch just the tail
      // in place. A full innerHTML swap here is what made streaming feel
      // like the whole page refreshing and restarted every CSS animation.
      const tailOk = wasRunning && state.running
        && this.renderedKey !== ""
        && key.startsWith(this.renderedKey)
        && this.appendTimelineTail(timeline, this.renderedItemCount);
      if (tailOk) {
        this.autoScroll();
      } else {
        this.fullRender(timeline, msgs);
      }
      this.renderedKey = key;
      this.renderedItemCount = timeline.length;
    } else {
      this.incrementalTextUpdate(timeline);
    }
    // Keep the retry line's seconds ticking (or tear the ticker down once the
    // notice is gone).  Runs after the DOM update above so it sees the node
    // the current frame actually produced.
    syncRetryCountdown(this.ml);
    this._updateStatusBar(state.running);
    // Auto-open HTML info cards only when the model just finished
    if (wasRunning && !state.running) {
      // Scoped to the main chat container so the pass never reaches into the
      // automation detail (which can hold the same tc-* card ids).
      window.__openInfoHtmlCards?.(this.ml);
    }

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
  /**
   * Builds the delegated task prompt as a plain user bubble, identical to
   * the main chat's user bubble. Reads the prompt from the agent tool call
   * params (`prompt` / `description`). Used from the sub-agent view before
   * the first transcript snapshot lands so the prompt is visible
   * immediately instead of a bare centered loader.
   */
  export function buildDelegatedPromptBubbleImpl(this: Chat, tc: ToolCallState): string {
    const prompt =
      (typeof tc.params.prompt === "string" && tc.params.prompt.trim()) ||
      (typeof tc.params.task === "string" && tc.params.task.trim()) ||
      (typeof tc.params.description === "string" && tc.params.description.trim()) ||
      "";
    if (!prompt) return "";
    return `<div class="user-item" data-user-idx="0">
      <div class="user-bubble">${escapeHtml(prompt as string)}</div>
    </div>`;
  }

  export function renderSubAgentIntoImpl(this: Chat, container: HTMLElement, messages: Message[], isRunning: boolean): void {
    const timeline = buildSubAgentTimeline(messages, isRunning);
    const st = getState();
    const autoExpand = isEnabled(st.settings.auto_expand);
    this.applyAutoExpand(timeline, autoExpand, isRunning);
    // Treat the automation detail exactly like the tape's sub-agent view:
    // user bubbles belong to the current turn (in-turn), not standalone
    // blocks. Pass treatAsSubAgent=true so buildTimelineHTML matches what
    // chat.render() produces when state.subAgentView is set.
    this._subAgentRender = true;
    let html: string;
    try {
      html = this.buildTimelineHTML(timeline, messages, true);
    } finally {
      this._subAgentRender = false;
    }
    const _vs = saveVideoPlayback(container);
    window.__stopAllMedia?.();
    container.innerHTML = html;
    createLucideIcons(container);
    window.__initMediaCards?.(container);
    restoreVideoPlayback(_vs, container);
    // Fresh nodes start with scrollTop 0; an expanded card must still open on
    // its newest lines, so the tail pin runs after the rebuild.
    pinBodyTails(container);
    if (!container.dataset.subAgentClickBound) {
      container.addEventListener("click", (e) => this.handleDelegateClick(e));
      container.dataset.subAgentClickBound = "true";
    }
  }

  /**
   * Single source of truth for whether a timeline item should be expanded.
   * Honors explicit user overrides, then the special "thinking" rule, then
   * the auto-expand setting.
   *
   * Rules:
   *  - Thinking, while the model is still actively reasoning
   *    (thinkingActive): ALWAYS expanded so the user can watch the reasoning
   *    stream, regardless of the auto-expand setting (a user may still
   *    explicitly collapse it).
   *  - Otherwise: a manual collapse/expand wins; when the user hasn't touched
   *    it, follow the auto-expand setting. This applies uniformly to thinking
   *    (after it finishes) and to every expandable tool card, so the automation
   *    detail behaves exactly like the main chat.
   */
  export function computeExpandedImpl(this: Chat, kind: string, id: string, autoExpand: boolean, thinkingActive: boolean): boolean {
    const userCollapsed = this.userCollapsedItems.has(id);
    if (kind === "thinking" && thinkingActive) {
      return !userCollapsed;
    }
    if (userCollapsed) return false;
    if (this.userExpandedItems.has(id)) return true;
    return autoExpand;
  }

  /**
   * Identify the thinking segment the model is *actively* generating.
   *
   * "Still thinking" means the reasoning is the last content the model has
   * produced so far — the moment any text or tool segment appears after it,
   * the thinking is finished even though the overall turn keeps running (e.g.
   * while a tool executes). Only the actively-streaming thinking strip is
   * force-expanded; finished ones fall back to the auto-expand setting.
   */
  export function activeThinkingIdImpl(this: Chat, timeline: TimelineItem[], isRunning: boolean): string | null {
    if (!isRunning) return null;
    for (let i = timeline.length - 1; i >= 0; i--) {
      const it = timeline[i];
      if (it.kind === "thinking") return it.id;
      if (it.kind === "tool" || it.kind === "assistant_text") return null;
    }
    return null;
  }

  /**
   * Shared auto-expand pass used by both fullRender and renderSubAgentInto
   * so the two never drift. Auto-expands thinking strips and every expandable
   * tool card unless the user manually collapsed them.
   */
  export function applyAutoExpandImpl(this: Chat, timeline: TimelineItem[], autoExpand: boolean, isRunning = false): void {
    const activeThinkingId = this.activeThinkingId(timeline, isRunning);
    for (const item of timeline) {
      if (item.kind !== "thinking" && item.kind !== "tool") continue;
      const id = item.id;
      const thinkingActive = item.kind === "thinking" && id === activeThinkingId;
      if (this.computeExpanded(item.kind, id, autoExpand, thinkingActive)) {
        this.expandedItems.add(id);
      } else {
        this.expandedItems.delete(id);
      }
    }
  }

  /**
   * Build the timeline's inner HTML (turns + standalone cards) - the shared
   * body of fullRender. Extracted so renderSubAgentInto produces byte-identical
   * markup to the main chat's sub-agent view instead of a parallel copy.
   *
   * @param treatAsSubAgent When true, user bubbles are folded into the
   *   current turn (matching chat.render()'s sub-agent path). When false,
   *   the live subAgentView/__parentSessionId flags decide - the normal
   *   chat behaviour.
   */
  export function buildTimelineHTMLImpl(this: Chat, timeline: TimelineItem[], allMsgs?: Message[], treatAsSubAgent = false): string {
    let html = "";
    let turnMid = ""; // buffered items inside current turn
    let turnActions = false;
    let turnRetry = false;
    let turnBranchSwitcher = false;
    let turnBranchSwitcherGroup: BranchSwitcherGroup | undefined;
    const _this = this;
    // The newest few answers are what the reader is looking at. Render those
    // as markdown on the first pass and defer only older history, so the
    // visible text never reformats (raw → markdown) a moment after paint.
    const tailAssistantIds = new Set<string>();
    for (let i = timeline.length - 1; i >= 0 && tailAssistantIds.size < 6; i--) {
      if (timeline[i].kind === "assistant_text") tailAssistantIds.add(timeline[i].id);
    }

    // Isolation boundary for the timeline loop: one broken item renderer is
    // logged and skipped, it never aborts the whole transcript render. Arrow
    // form on purpose - it is called bare below, so a plain `function` would
    // see `this === undefined` and lose the Chat instance.
    const renderItemSafe = (item: TimelineItem): string => guardHtml(
      "chat-item",
      (item as { id?: string }).id || (item as { kind?: string }).kind || "item",
      () => {
        if (item.kind === "assistant_text") {
          // Markdown + syntax highlighting is the expensive part of a full
          // render. Reuse the previous HTML when the block's inputs are
          // unchanged; only the streaming block fails this check and is
          // re-parsed. Each item is independent, so one changing block never
          // forces its neighbours back through markdown-it.
          //
          // While boot deferral is active, historical blocks render as escaped
          // text (nearly free) and are upgraded to markdown in idle slices.
          // An already-upgraded block stays markdown even if deferral is on.
          const cache = _this.assistantHtmlCache;
          const locale = getLocale();
          const deferred =
            _this.deferMarkdown &&
            !treatAsSubAgent &&
            !item.isStreaming &&
            !tailAssistantIds.has(item.id);
          const cached = cache.get(item.id);
          if (
            cached &&
            cached.content === item.content &&
            cached.isStreaming === item.isStreaming &&
            cached.hasError === item.hasError &&
            cached.messageId === item.messageId &&
            cached.locale === locale &&
            (!cached.deferred || deferred)
          ) {
            return cached.html;
          }
          const html = deferred
            ? _this.renderAssistantTextRaw(item)
            : _this.renderItemHTML(item);
          _this.cacheAssistantHtml(item.id, {
            content: item.content,
            isStreaming: item.isStreaming,
            hasError: item.hasError,
            messageId: item.messageId,
            locale,
            html,
            deferred,
          });
          return html;
        }
        return _this.renderItemHTML(item);
      },
      "",
    );

    function closeTurn() {
      if (!turnMid) return;
      html += `<div class="turn">${turnMid}`;
      if (turnActions) {
        html += `<div class="assistant-actions turn-actions">`;
        html += `<button class="btn-icon btn-icon--msg assistant-copy-btn" data-tooltip="${t("chat.copy")}">
          <i data-lucide="copy" class="lucide"></i>
        </button>`;
        if (turnRetry) {
          html += `<button class="btn-icon btn-icon--msg assistant-retry-btn" data-tooltip="${t("chat.retry")}">
            <i data-lucide="refresh-cw" class="lucide"></i>
          </button>`;
        }
        if (turnBranchSwitcher) {
          try {
            html += _this.renderBranchSwitcher(turnBranchSwitcherGroup);
          } catch (e) {
            reportUiFault("chat-turn", "branch-switcher", e);
          }
        }
        html += `</div>`;
      }
      html += `</div>`;
      turnMid = "";
      turnActions = false;
      turnRetry = false;
      turnBranchSwitcher = false;
      turnBranchSwitcherGroup = undefined;
    }

    const inSubAgent = treatAsSubAgent || !!getState().subAgentView || !!(window as any).__parentSessionId;
    for (let i = 0; i < timeline.length; i++) {
      const item = timeline[i];

      if (item.kind === "compact" || item.kind === "workflow" || (item.kind === "user" && !inSubAgent)
        || item.kind === "compact_notice" || item.kind === "model_switch" || item.kind === "compact_starting") {
        closeTurn.call(_this);
        html += renderItemSafe(item);
      } else if (item.kind === "ai_header") {
        closeTurn.call(_this);
        turnMid += renderItemSafe(item);
      } else {
        // thinking / assistant_text / tool / status cards all belong to the current turn
        if ((item as any).showBranchSwitcher) {
          turnBranchSwitcher = true;
          turnBranchSwitcherGroup = (item as any).branchSwitcher;
        }
        if ((item as any).showActions) {
          // Every completed turn gets a copy button
          turnActions = true;
          // No retry button in sub-agent view
          if (!inSubAgent) {
            // Only show retry on the very last assistant message in the conversation
            if (allMsgs) {
              for (let mi = allMsgs.length - 1; mi >= 0; mi--) {
                if (allMsgs[mi].role === "assistant") {
                  if ((item as any).messageId === allMsgs[mi].id) turnRetry = true;
                  break;
                }
              }
            } else {
              turnRetry = true;
            }
          }
        }
        turnMid += renderItemSafe(item);
      }
    }
    closeTurn.call(_this);
    return html;
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
  export function fullRenderParallelImpl(this: Chat, subMsgs: Message[], isStreaming: boolean): void {
    // Split the flat transcript by its stable backend task index. A reconnect
    // may replay dividers out of order, so array position is not task identity.
    const groupsByIndex = new Map<number, { divider: Message; body: Message[] }>();
    let fallbackIndex = 0;
    let current: { divider: Message; body: Message[] } | null = null;
    for (const m of subMsgs) {
      if (m.mode === "task_divider") {
        const index = Number.isInteger(m.taskIndex) ? m.taskIndex! : fallbackIndex;
        fallbackIndex = Math.max(fallbackIndex, index + 1);
        current = { divider: m, body: [] };
        groupsByIndex.set(index, current);
      } else if (current) {
        current.body.push(m);
      }
    }
    const groups = [...groupsByIndex.entries()]
      .sort(([left], [right]) => left - right)
      .map(([, group]) => group);

    const statusMeta = (s: string | undefined): { icon: string; label: string; cls: string } => {
      if (s === "done") return { icon: "check", label: t("chat.taskDone") || "Done", cls: "is-done" };
      if (s === "error") return { icon: "x", label: t("chat.taskError") || "Failed", cls: "is-error" };
      if (s === "queued") return { icon: "clock", label: t("chat.taskQueued") || "Queued", cls: "is-queued" };
      return { icon: "loader", label: t("chat.taskRunning") || "Running", cls: "is-running" };
    };

    // Only the first 4 tasks get a region; the split grid can't draw more.
    const shown = groups.slice(0, 4);
    const count = shown.length;

    // Build each tile body HTML up-front so we can diff against the
    // existing DOM and only replace the tiles that actually changed.
    const tileBodies: string[] = [];
    for (const g of shown) {
      const isRunning = statusMeta(g.divider.taskStatus).cls === "is-running";
      const bodyTimeline = buildSubAgentTimeline(g.body, isRunning);
      this._subAgentRender = true;
      let bodyHtml: string;
      try {
        bodyHtml = g.body.length > 0
          ? this.buildTimelineHTML(bodyTimeline, g.body, true)
          : `<div class="parallel-task-pending">${t("chat.taskWaiting") || "Waiting for output…"}</div>`;
      } catch (e) {
        // One failed task pane must not blank every sibling pane.
        reportUiFault("chat-turn", `parallel-task:${g.divider.taskIndex ?? ""}`, e);
        bodyHtml = `<div class="parallel-task-pending">${escapeHtml(g.divider.taskName || "")}</div>`;
      } finally {
        this._subAgentRender = false;
      }
      tileBodies.push(bodyHtml);
    }

    // Windows-style snap layout: panes tile edge-to-edge with no outer frame.
    // 1 task fills the tape; 2 tasks split left/right; 3 tasks put one large
    // pane on the left and two stacked panes on the right; 4 tasks form a 2x2
    // grid. Split ratios are fixed (no drag resize).
    const wrapTile = (idx: number): string =>
      `<section class="parallel-tile"><div class="parallel-tile-body">${tileBodies[idx]}</div></section>`;
    let html = `<div class="parallel-sub-agent" data-count="${count}">`;
    if (count === 3) {
      html += wrapTile(0);
      html += `<div class="parallel-sub-agent-stack">`;
      html += wrapTile(1);
      html += wrapTile(2);
      html += `</div>`;
    } else {
      for (let i = 0; i < count; i++) {
        html += wrapTile(i);
      }
    }
    html += `</div>`;

    this.ml.classList.add("parallel-active");
    const existing = this.ml.querySelector(":scope > .parallel-sub-agent") as HTMLElement | null;
    const _vs = saveVideoPlayback(this.ml);
    window.__stopAllMedia?.();
    if (existing && existing.dataset.count === String(count)) {
      // Diff update: only swap tile bodies whose content changed. This keeps
      // scroll position, cursor selection, and expansion state intact for the
      // tiles that are not currently streaming.
      const bodies = existing.querySelectorAll(".parallel-tile-body");
      let idx = 0;
      for (const body of Array.from(bodies)) {
        if (idx < tileBodies.length && body.innerHTML !== tileBodies[idx]) {
          body.innerHTML = tileBodies[idx];
        }
        idx++;
      }
      window.__initMediaCards?.(this.ml);
      restoreVideoPlayback(_vs, this.ml);
    } else {
      this.ml.innerHTML = html;
      window.__initMediaCards?.(this.ml);
      restoreVideoPlayback(_vs, this.ml);

    }
    // The innerHTML rebuild dropped the status bar node — put it back so the
    // running turn's status line stays pinned under the latest output.
    this.mountStatusBar();
    pinBodyTails(this.ml);
  }

  /**
   * Re-render one tool card in place. A finished tool result only changes
   * that card's own markup (body, error state, info-card source); replacing
   * it avoids rebuilding every earlier turn's markdown and DOM. Returns
   * false when the card or its timeline item cannot be found, so the caller
   * can fall back to a full render.
   */
  /** Replace one tool card's DOM with fresh markup. No scroll handling. */
  function _replaceToolCard(this: Chat, item: Extract<TimelineItem, { kind: "tool" }>): boolean {
    // A multi-task agent card renders one sibling strip per task, all sharing
    // the same data-id. Anchor on the first and drop the rest, or replacing
    // one would leave duplicated siblings behind.
    const existing = Array.from(
      this.ml.querySelectorAll(`[data-id="tc-${item.tc.id}"]`)
    ) as HTMLElement[];
    const el = existing[0];
    if (!el) return false;
    let html = "";
    try {
      html = this.renderItemHTML(item).trim();
    } catch (e) {
      console.warn("[chat] tool item patch failed:", e);
      return false;
    }
    if (!html) return false;
    const tmp = document.createElement("template");
    tmp.innerHTML = html;
    const nodes = Array.from(tmp.content.children) as HTMLElement[];
    if (nodes.length === 0) return false;
    el.replaceWith(...nodes);
    for (const extra of existing.slice(1)) extra.remove();
    for (const node of nodes) {
      createLucideIcons(node);
      guardRender("media", "tool-patch", () => window.__initMediaCards?.(node));
      guardRender("scroll", "tool-patch-pin", () => pinBodyTails(node));
    }
    return true;
  }

  export function patchToolItemImpl(this: Chat, tcId: string): boolean {
    const timeline = buildTimeline(getState().messages);
    let item: Extract<TimelineItem, { kind: "tool" }> | undefined;
    for (const candidate of timeline) {
      if (candidate.kind === "tool" && candidate.tc.id === tcId) {
        item = candidate;
        break;
      }
    }
    if (!item) return false;
    // Replacing the card changes its height; keep the reader's anchor stable
    // instead of letting the viewport drift under them.
    const container = this.container;
    const prevTop = container.scrollTop;
    const prevHeight = container.scrollHeight;
    if (!_replaceToolCard.call(this, item)) return false;
    if (this.userScrolledUp) {
      const delta = container.scrollHeight - prevHeight;
      if (delta) container.scrollTop = prevTop + delta;
    } else {
      this.autoScroll();
    }
    return true;
  }

  /**
   * Re-render every tool card in place. Used when the tool-UI catalog
   * changes (plugin metadata arriving after boot): cards derive their shape,
   * icon and labels from it, while assistant text does not. This replaces
   * only the cards instead of rebuilding the whole transcript, so the answer
   * text never visually refreshes.
   */
  export function refreshToolCardsImpl(this: Chat): void {
    const timeline = buildTimeline(getState().messages);
    const container = this.container;
    const prevTop = container.scrollTop;
    const prevHeight = container.scrollHeight;
    let changed = false;
    for (const item of timeline) {
      if (item.kind !== "tool") continue;
      if (_replaceToolCard.call(this, item)) changed = true;
    }
    if (!changed) return;
    if (this.userScrolledUp) {
      const delta = container.scrollHeight - prevHeight;
      if (delta) container.scrollTop = prevTop + delta;
    } else {
      this.autoScroll();
    }
  }

  /**
   * Streaming fast path: the render key only GREW by whole segments — the
   * existing transcript prefix is byte-identical, so only the newly arrived
   * timeline items need DOM nodes. Appending them in place avoids the full
   * innerHTML swap that re-created every icon and replayed every CSS
   * animation on the page each time a tool card or text block appeared.
   * Returns false when the change is structural (unknown/new kind of item,
   * no viable insertion point) and the caller must fullRender instead.
   */
  export function appendTimelineTailImpl(this: Chat, timeline: TimelineItem[], prevCount: number): boolean {
    // Only content items that stream mid-turn may be appended; turn
    // boundaries (user bubble, ai header) and session-level cards rebuild.
    const appendable = new Set(["thinking", "tool", "assistant_text", "error_card", "warning_card", "inline_success", "inline_cancelled"]);
    for (let i = prevCount; i < timeline.length; i++) {
      if (!appendable.has(timeline[i].kind)) return false;
    }
    const appended: HTMLElement[] = [];
    for (let i = prevCount; i < timeline.length; i++) {
      const item = timeline[i];
      let html = "";
      try {
        html = this.renderItemHTML(item).trim();
      } catch (e) {
        console.warn(`[chat] tail append aborted kind=${item.kind}:`, e);
        return false;
      }
      if (!html) continue;
      const tmp = document.createElement("template");
      tmp.innerHTML = html;
      // A multi-task agent card renders one sibling strip per task. Insert
      // every top-level node, not just the first, or the remaining task cards
      // silently vanish from the tape.
      const nodes = Array.from(tmp.content.children) as HTMLElement[];
      if (nodes.length === 0) continue;
      // Streaming items always belong to the LAST turn in the DOM. The
      // action bar (copy / retry) sits at the very end of a closed turn, so
      // new items must slot in before it.
      const turn = this.ml.querySelector(".turn:last-of-type") as HTMLElement | null;
      if (turn) {
        const actions = turn.querySelector(":scope > .assistant-actions");
        for (const node of nodes) {
          if (actions) turn.insertBefore(node, actions);
          else turn.appendChild(node);
          appended.push(node);
        }
      } else {
        for (const node of nodes) {
          this.ml.appendChild(node);
          appended.push(node);
        }
      }
    }
    // Hydrate only the nodes that were just appended. A document/tape-wide
    // lucide pass replaces every existing icon node, which is O(history) per
    // appended card and can swap an icon out from under a click.
    for (const node of appended) {
      createLucideIcons(node);
      guardRender("media", "chat-append", () => window.__initMediaCards?.(node));
      guardRender("scroll", "chat-append-pin", () => pinBodyTails(node));
    }
    return true;
  }

  let _fullRenderCount = 0;

  export function fullRenderImpl(this: Chat, timeline: TimelineItem[], allMsgs?: Message[]): void {
    const _renderStarted = performance.now();
    this.ml.classList.remove("parallel-active");
    const st = getState();
    const autoExpand = isEnabled(st.settings.auto_expand);

    // Auto-expand based on setting (unless user manually collapsed).
    // During streaming, thinking is always expanded so the user sees the
    // reasoning process in real time. After streaming, follow the setting.
    // Delegated to the shared applyAutoExpand pass so fullRender, the
    // streaming incremental path and the automation detail never drift.
    const isRunning = st.running;
    this.applyAutoExpand(timeline, autoExpand, isRunning);

    // Reuse the shared turn-builder so fullRender and renderSubAgentInto
    // (and the automation detail) produce byte-identical markup.
    const html = buildTimelineHTMLImpl.call(this, timeline, allMsgs, false);

    // Capture the scroll snapshot BEFORE innerHTML resets, then restore
    // the visual anchor AFTER. Without this, a full re-render during
    // streaming collapses the user's scroll position and makes the
    // chat feel like it is "always rendering", preventing them from
    // scrolling up to inspect earlier turns.
    const container = this.container;
    // A session switch is a fresh navigation: the stale userScrolledUp flag
    // from the previous session (or from scroll events fired while the old
    // DOM was being swapped) must not preserve the old scroll offset — the
    // user expects to land on the last turn of the newly opened session.
    // The same applies to rollback / branch switches (one-shot flag): the
    // conversation just got truncated, so preserving the pre-rollback
    // scroll offset would leave the view stranded above the new bottom.
    const sessionChanged = this._lastRenderedSessionId !== st.sessionId;
    this._lastRenderedSessionId = st.sessionId;
    const forcePin = sessionChanged || this._forcePinNextRender;
    this._forcePinNextRender = false;
    const wasScrolledUp = this.userScrolledUp && !forcePin;
    if (forcePin) this.userScrolledUp = false;
    const prevScrollTop = container.scrollTop;
    const prevScrollHeight = container.scrollHeight;
    const _vs = saveVideoPlayback(this.ml);
    window.__stopAllMedia?.();
    this.ml.innerHTML = html;
    // The rebuild produced fresh nodes and classes; force the next incremental
    // pass to re-check every item once instead of trusting stale signatures.
    this.incrementalSigs.clear();
    // Post-swap decoration is individually bounded: one broken image/video/
    // icon must not leave the transcript without scroll restoration.
    createLucideIcons(this.ml);
    guardRender("media", "chat-full", () => window.__initMediaCards?.(this.ml));
    guardRender("media", "chat-video", () => restoreVideoPlayback(_vs, this.ml));
    // The innerHTML rebuild dropped the status bar node — put it back so the
    // running turn's status line stays pinned under the latest output.
    this.mountStatusBar();
    // Rebuilt bodies come back at scrollTop 0; re-pin each one to its newest
    // line so an expanded card never flashes its first lines before tailing.
    guardRender("scroll", "chat-pin", () => pinBodyTails(this.ml));

    if (wasScrolledUp) {
      // Preserve the user's visual position: when new content is appended
      // below, the previous bottom offset should still point at the same
      // pixel anchor in the re-rendered DOM.
      const newScrollHeight = container.scrollHeight;
      const delta = newScrollHeight - prevScrollHeight;
      container.scrollTop = prevScrollTop + delta;
      // Refresh the "scrolled up" flag — if the delta restored the user
      // to the bottom, treat them as pinned again.
      const dist = newScrollHeight - container.scrollTop - container.clientHeight;
      this.userScrolledUp = dist > 8;
    } else {
      // New render: always pin to the bottom so the latest message is
      // visible. This covers both streaming and static sessions -- when
      // the user opens a session, we want to land on the last turn,
      // not the middle of the history. Session switches and rollbacks
      // force the pin so the stale flag cannot block it.
      this.scrollToBottomAfterMedia(forcePin);
    }
    updateScrollIndicatorImpl.call(this);
    this.syncScrollButton();
    // The scroll anchor above must measure layout before anything transforms
    // the tape, so the enter motion is replayed last: switching sessions
    // fades and settles onto the new transcript instead of snapping to it.
    const _renderMs = performance.now() - _renderStarted;
    if (_renderMs >= 60 || _fullRenderCount < 3) {
      _fullRenderCount++;
      console.log(
        `[perf] fullRender ${Math.round(_renderMs)}ms items=${timeline.length} deferred=${!!this.deferMarkdown}`,
      );
    }
    if (sessionChanged) playEnterAnim(this.ml);
  }

  /** Keeps the scroll indicator in sync with the current user-turn set.
   *
   *  The signature is allocation-free (turn count + total user text
   *  length), so it is safe to compute every render frame. The tick texts
   *  themselves are only materialised when the signature actually changes,
   *  which was not the case before: a full `filter` + `map` + `join` of
   *  every user message's content ran per frame, copying the whole
   *  conversation's prompt text into one string just to detect a change.
   */
  export function updateScrollIndicatorImpl(this: Chat): void {
    const msgs = getState().messages;
    let turns = 0;
    let total = 0;
    for (let i = 0; i < msgs.length; i++) {
      if (msgs[i].role === "user") {
        turns++;
        total += msgs[i].content.length;
      }
    }
    const sig = `${turns}:${total}`;
    if (sig === this._scrollSig) {
      this.scrollIndicator.update();
      return;
    }
    this._scrollSig = sig;
    const texts: string[] = [];
    for (let i = 0; i < msgs.length; i++) {
      if (msgs[i].role === "user") texts.push(msgs[i].content);
    }
    this.scrollIndicator.update(turns, texts);
  }

  export function incrementalTextUpdateImpl(this: Chat, timeline: TimelineItem[]): void {
    // Find the last assistant message block in timeline.  This anchor is
    // only needed for the assistant-text delta updates below; the thinking,
    // tool, and workflow updates must still run during the pre-text thinking
    // phase (i.e. when only `thinking_delta` events have arrived so far) so
    // the user sees the reasoning stream in real time instead of having the
    // card freeze on the first delta and then jump to the full text when
    // the first text_delta triggers a fullRender.
    let lastAssistantIndex = -1;
    for (let i = timeline.length - 1; i >= 0; i--) {
      if (timeline[i].kind === "assistant_text") {
        lastAssistantIndex = i;
        break;
      }
    }

    if (lastAssistantIndex >= 0) {
      const assistantItem = timeline[lastAssistantIndex] as Extract<TimelineItem, { kind: "assistant_text" }>;

      // Detect new assistant message round — reset userCollapsedItems
      const currentAssistantMsgId = assistantItem.id;
      if (currentAssistantMsgId !== this.lastAssistantMsgId) {
        this.lastAssistantMsgId = currentAssistantMsgId;
      }
    }

    // Update thinking text if present.  This runs on every render frame
    // (including frames where only a thinking_delta arrived) so the thought
    // card streams in real time just like the assistant text card.
    const activeThinkingId = this.activeThinkingId(timeline, getState().running);
    for (let i = 0; i < timeline.length; i++) {
      const item = timeline[i];
      if (item.kind === "thinking") {
        // Unchanged reasoning blocks must not cost a DOM query per frame.
        const sig = `${item.id}|${item.text.length}|${item.id === activeThinkingId ? 1 : 0}|${this.expandedItems.has(item.id) ? 1 : 0}`;
        if (this.incrementalSigs.get(item.id) === sig) continue;
        this.incrementalSigs.set(item.id, sig);
        const el = this.ml.querySelector(`[data-id="${item.id}"]`) as HTMLElement | null;
        if (el) {
          const bodyEl = el.querySelector(".thought-body") as HTMLElement | null;
          if (bodyEl && bodyEl.textContent !== item.text) {
            bodyEl.textContent = item.text;
            // The thought grows past its window one token at a time; keep the
            // visible slice on the newest line instead of leaving it parked at
            // the top of the very first tokens.
            pinBodyTail(bodyEl);
          }
          // Auto-expand thinking: while the model is ACTIVELY thinking (this is
          // the last content it has produced) always expand unless the user
          // manually collapsed it. Once thinking finishes, follow the
          // auto-expand setting. User manual toggle is respected via
          // userCollapsedItems/userExpandedItems.
          const shouldExpand = isEnabled(getState().settings.auto_expand);
          const thinkingActive = item.id === activeThinkingId;
          const wantExpanded = this.computeExpanded("thinking", item.id, shouldExpand, thinkingActive);
          const isExpanded = el.classList.contains("expanded");
          if (wantExpanded && !isExpanded) {
            el.classList.add("expanded");
            this.expandedItems.add(item.id);
          } else if (!wantExpanded && isExpanded) {
            el.classList.remove("expanded");
            this.expandedItems.delete(item.id);
          }
        }
      }
    }

    // Update tool items incrementally. Covers: streaming params (summary
    // text), status transitions (spinner show/hide), result body, file
    // card diff badge / view button, and agent card preview.
    for (let i = 0; i < timeline.length; i++) {
      const toolItem = timeline[i];
      if (toolItem.kind !== "tool") continue;
      // Cheap signature covering everything this pass writes: status, result
      // growth, error state, expansion, streamed arguments and sub-agent
      // progress. Unchanged cards skip the DOM sweep entirely.
      const tcState = toolItem.tc;
      const subs = tcState.subAgentMessages;
      const subSig = subs && subs.length
        ? `${subs.length}:${subs[subs.length - 1]?.content?.length ?? 0}`
        : "0";
      const sig = `${toolItem.id}|${tcState.status}|${(tcState.result || "").length}|${tcState.isError ? 1 : 0}|${this.expandedItems.has(toolItem.id) ? 1 : 0}|${subSig}|${String(tcState.params?.arguments || "").length}`;
      if (this.incrementalSigs.get(toolItem.id) === sig) continue;
      this.incrementalSigs.set(toolItem.id, sig);
      // timeline item id is already `tc-${tc.id}`; do NOT prefix again
      // (the old selector emitted `data-id="tc-tc-..."` and never matched).
      const el = this.ml.querySelector(`[data-id="${toolItem.id}"]`) as HTMLElement | null;
      if (!el) continue;

      // 1) Status slot — toggle spinner on pending<->running<->done.
      const statusSlot = el.querySelector(".strip-status, .tool-item-status") as HTMLElement | null;
      if (statusSlot) {
        const curStatus = statusSlot.getAttribute("data-status") || "";
        if (curStatus !== toolItem.tc.status) {
          statusSlot.setAttribute("data-status", toolItem.tc.status);
          statusSlot.innerHTML = _toolStatusHtml(toolItem.tc.status);
        }
      }

      // 1.5) Stop button (terminal tools) — toggle visibility on status change.
      const strip = el.querySelector(".strip") as HTMLElement | null;
      if (strip) {
        const btn = strip.querySelector(".strip-stop-btn");
        if (btn) {
          const wantRunning = toolItem.tc.status === "running" && metaFor(toolItem.tc.name).body === "terminal";
          btn.setAttribute("data-running", wantRunning ? "true" : "false");
        }
        // 1.6) Sub-agent pixel loader — animate while running, freeze into the
        // static chevron the moment the agent settles. The agent card has no
        // `.strip-status` slot, so without this the grid would keep shimmering
        // forever after the run finished.
        const loader = strip.querySelector(".sa-loader");
        if (loader) {
          const wantRunning = toolItem.tc.status === "running";
          if (loader.getAttribute("data-running") !== String(wantRunning)) {
            loader.setAttribute("data-running", wantRunning ? "true" : "false");
            const delays = [90, 180, 270, 0, 90, 180, 90, 180, 270];
            const chevron = [2, 4, 8];
            loader.querySelectorAll(":scope > i").forEach((cell, i) => {
              cell.classList.toggle("sa-chevron", !wantRunning && chevron.includes(i));
              if (wantRunning) cell.setAttribute("style", `--sa-d:${delays[i]}ms`);
              else cell.removeAttribute("style");
            });
          }
        }
      }

      // 2) Summary text (strip-summary) — pick the right summary function
      //    per layout (inline vs expandable).
      const summaryEl = el.querySelector(".strip-summary") as HTMLElement | null;
      if (summaryEl) {
        const isExpandable = el.classList.contains("strip-item") || el.classList.contains("terminal-card");
        const newSummary = isExpandable ? getToolSummary(toolItem.tc) : getToolInlineSummary(toolItem.tc);
        if (summaryEl.textContent !== newSummary) {
          summaryEl.textContent = newSummary;
        }
      }

      // 3) Result body slot (expandable strip + terminal card). Filled here
      //    both when tc.result first arrives (tool_result patches the card, and
      //    streamed progress appends) because result presence is not part of
      //    the render key.
      const bodySlot = el.querySelector(".strip-body-slot, .terminal-body-slot") as HTMLElement | null;
      if (bodySlot) {
        const hasBody = !!toolItem.tc.result;
        const cur = bodySlot.getAttribute("data-has-body") === "1";
        // Streamed progress keeps appending to tc.result without a structural
        // key change; compare lengths so the body follows the output instead
        // of freezing on its first chunk.
        const resultLen = String((toolItem.tc.result || "").length);
        const curLen = bodySlot.getAttribute("data-result-len") || "";
        if (hasBody !== cur || (hasBody && resultLen !== curLen)) {
          bodySlot.setAttribute("data-has-body", hasBody ? "1" : "0");
          bodySlot.setAttribute("data-result-len", resultLen);
          if (hasBody) {
            bodySlot.innerHTML = `<div class="${el.classList.contains("terminal-card") ? "terminal-body" : "strip-body"} tail-follow">${getToolBodyText(toolItem.tc)}</div>`;
            // A result arriving fills the body for the first time; land on its
            // tail so a long output reads from its last line, like the run that
            // produced it.
            pinBodyTails(bodySlot);
          } else {
            bodySlot.innerHTML = "";
          }
        }
      }

      // 4) Auto-expand based on setting (unless user manually collapsed).
      //    Applies to all expandable tool cards. Mirrors the thinking-strip
      //    handling above so toggling auto_expand takes effect on already-
      //    rendered tool cards without a full re-render.
      const shouldExpandTool = isEnabled(getState().settings.auto_expand);
      const wantExpandedTool = this.computeExpanded("tool", toolItem.id, shouldExpandTool, false);
      const isExpandedTool = el.classList.contains("expanded");
      if (wantExpandedTool && !isExpandedTool) {
        el.classList.add("expanded");
        this.expandedItems.add(toolItem.id);
      } else if (!wantExpandedTool && isExpandedTool) {
        el.classList.remove("expanded");
        this.expandedItems.delete(toolItem.id);
      }
      
    }

    // Update workflow card progress bar and task list
    for (let i = 0; i < timeline.length; i++) {
      if (timeline[i].kind !== "workflow") continue;
      const wf = getState().workflowState;
      if (!wf) continue;
      const card = this.ml.querySelector(`#wf-${wf.workflowId}`) as HTMLElement | null;
      if (!card) continue;
      // Update progress bar
      const bar = card.querySelector(".workflow-progress-fill") as HTMLElement | null;
      if (bar) {
        const pct = wf.totalTasks > 0 ? Math.round((wf.completedCount + wf.failedCount + wf.skippedCount) / wf.totalTasks * 100) : 0;
        bar.style.width = pct + "%";
      }
      // Update progress text
      const textEl = card.querySelector(".workflow-progress-text") as HTMLElement | null;
      if (textEl) {
        const newText = `${wf.completedCount + wf.failedCount + wf.skippedCount}/${wf.totalTasks} tasks — ${wf.completedCount} done, ${wf.failedCount} failed, ${wf.skippedCount} skipped`;
        if (textEl.textContent !== newText) textEl.textContent = newText;
      }
      // Update header badge
      const badge = card.querySelector(".workflow-card-badge") as HTMLElement | null;
      if (badge && !wf.active) {
        const statusColor = wf.success ? "#22c55e" : "#ef4444";
        badge.textContent = wf.success ? (t("chat.workflowDone") || "Completed") : (t("chat.workflowFailed") || "Failed");
        badge.style.background = statusColor + "20";
        badge.style.color = statusColor;
      }
      // Update task status dots
      const taskEls = card.querySelectorAll(".wf-task") as NodeListOf<HTMLElement>;
      taskEls.forEach((taskEl, idx) => {
        if (idx >= wf.tasks.length) return;
        const task = wf.tasks[idx];
        const dot = taskEl.querySelector(".wf-dot") as HTMLElement | null;
        const statusEl = taskEl.querySelector(".wf-task-status") as HTMLElement | null;
        if (dot && dot.getAttribute("data-status") !== task.status) {
          dot.setAttribute("data-status", task.status);
          dot.className = `wf-dot wf-dot--${task.status}`;
        }
        if (statusEl && statusEl.textContent !== task.status) {
          statusEl.textContent = task.status;
        }
      });
    }

    // Update assistant text content.
    // The whole accumulated answer is re-parsed (and every fenced block
    // re-highlighted) here, so while streaming this is rate-limited instead of
    // running on every animation frame. The final markdown is written by the
    // full render that runs when the turn ends.
    if (lastAssistantIndex >= 0) {
      const assistantItem = timeline[lastAssistantIndex] as Extract<TimelineItem, { kind: "assistant_text" }>;
      const newText = assistantItem.content;
      const el = this.ml.querySelector(`[data-id="${assistantItem.id}"]`) as HTMLElement | null;
      if (!el) {
        // DOM and state are out of sync (e.g. the assistant message was
        // created for a tool call before any text segment existed). Force a
        // full render so the produced content actually appears.
        // Guard against re-entrancy: if we're already inside a renderForce
        // (which is what got us here), don't recurse infinitely.
        if (!this._inRenderForce) {
          this.renderForce();
        }
        return;
      }
      if (newText.trim().length > 0) {
        const contentEl = el.querySelector(".msg-text") as HTMLElement | null;
        if (contentEl) {
          const running = getState().running;
          const sameItem = this._streamMarkdownId === assistantItem.id;
          if (!sameItem || newText !== this._streamMarkdownText) {
            const now = performance.now();
            // markdown-it re-parses the entire message and highlight.js
            // re-highlights every fenced block, so running this on each frame
            // costs tens of ms for a long answer and starves the WebSocket
            // reader. Cap it while streaming; an open fence is slower still
            // because partial code re-highlights yield no stable cache.
            const fenceCount = (newText.match(/```/g) || []).length;
            const interval = fenceCount % 2 === 1 ? 160 : 90;
            // While the user has scrolled up to read history, the streaming
            // block is offscreen: skip the parse so scrolling stays smooth.
            const offscreen = running && this.userScrolledUp;
            if (!offscreen && (!running || !sameItem || now - this._streamMarkdownAt >= interval)) {
              const newHtml = renderMarkdown(newText);
              // Compare against the cached string, not innerHTML: reading
              // innerHTML serialises the whole subtree on every frame.
              if (!sameItem || newHtml !== this._streamMarkdownHtml) {
                // A block that started as the boot plain-text fallback is
                // now real markdown; drop the pre-wrap styling with it.
                contentEl.classList.remove("msg-text--raw");
                contentEl.innerHTML = newHtml;
                // Re-hydrate the code-block copy icons this write replaced
                // (scoped to this block only -- a document-wide pass would
                // churn every icon in the app on every stream tick).
                if (newHtml.includes("data-lucide=")) createLucideIcons(contentEl);
              }
              this._streamMarkdownId = assistantItem.id;
              this._streamMarkdownText = newText;
              this._streamMarkdownHtml = newHtml;
              this._streamMarkdownAt = now;
            }
          }
        }
      }
    }

    this.autoScroll();
    updateScrollIndicatorImpl.call(this);
    this.syncScrollButton();
  }

