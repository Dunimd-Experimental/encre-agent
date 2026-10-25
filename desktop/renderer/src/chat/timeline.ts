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
 * Timeline construction for the Chat view.
 *
 * Converts raw session messages into the ordered `TimelineItem` list that
 * the chat renderer displays, including sub-agent timelines, status cards
 * and structural render keys used for incremental updates.
 */

import type { Message, ToolCallState, BranchMeta } from "../core/types.js";
import { getState, toolCallMatchesId } from "../core/state.js";
import { isToolHidden } from "./tool_meta.js";

/** Per-fork-point branch switcher payload: the sibling branch ids that
 *  alternate at one fork point, and the active branch's index among them. */
export interface BranchSwitcherGroup {
  ids: string[];
  activeIdx: number;
}

/**
 * Select the messages that belong to a single parallel task.
 * When ``taskIndex`` is provided, the flat ``sub_agent_messages`` list is
 * split at ``task_divider`` markers and only the chosen task's body is
 * returned. Otherwise the full list is returned unchanged.
 */
export function selectSubAgentTaskMessages(msgs: Message[], taskIndex?: number): Message[] {
  if (taskIndex === undefined) return msgs;
  const groups = new Map<number, Message[]>();
  let fallbackIndex = 0;
  let current: Message[] | null = null;
  for (const m of msgs) {
    if (m.mode === "task_divider") {
      const index = Number.isInteger(m.taskIndex) ? m.taskIndex! : fallbackIndex;
      fallbackIndex = Math.max(fallbackIndex, index + 1);
      current = [];
      groups.set(index, current);
    } else if (current) {
      current.push(m);
    }
  }
  return groups.get(taskIndex) ?? [];
}

export function buildSubAgentTimeline(msgs: Message[], isRunning?: boolean): TimelineItem[] {
  // task_divider markers are only structural metadata used to split parallel
  // runs in the parent transcript; they should never render inside a sub-agent
  // view.
  const clean = msgs.filter((m) => m.mode !== "task_divider");
  const timeline = buildTimeline(clean);
  // When the sub-agent view is running, suppress action buttons (copy, retry)
  // on all messages so they only appear after the turn is truly finished.
  const subView = getState().subAgentView;
  const subRunning = isRunning ?? !!(subView && (subView.status === "running" || subView.status === "pending"));
  if (subRunning) {
    for (const item of timeline) {
      (item as any).showActions = false;
    }
  }
  // Sub-agent views render a single focused turn inline. The ai_header
  // ("Encre Agent") shows at the top so the user knows which agent is
  // producing the output -- only one header exists per task.
  return timeline;
}

export type TimelineItem =
  | { kind: "user"; id: string; content: string; index: number; showBranchSwitcher?: boolean; mode?: string; fileRefs?: { name: string; size: number; icon: string; path?: string; mime_type?: string }[] }
  | { kind: "ai_header"; id: string; time: string }
  | { kind: "thinking"; id: string; text: string; elapsed?: number; messageId?: string }
  | { kind: "tool"; id: string; tc: ToolCallState; messageId?: string; showActions?: boolean; showBranchSwitcher?: boolean; branchSwitcher?: BranchSwitcherGroup }
  | { kind: "assistant_text"; id: string; content: string; isStreaming: boolean; hasError?: boolean; messageId?: string; showActions?: boolean; showBranchSwitcher?: boolean; branchSwitcher?: BranchSwitcherGroup }
  | { kind: "error_card"; id: string; messageId: string; errorMessage: string; errorCode: string; errorCategory?: string; showActions?: boolean; showBranchSwitcher?: boolean; branchSwitcher?: BranchSwitcherGroup }
  | { kind: "warning_card"; id: string; messageId: string; interruptedReason: string; showContinue?: boolean; showActions?: boolean; showBranchSwitcher?: boolean; branchSwitcher?: BranchSwitcherGroup }
  | { kind: "inline_success"; id: string; messageId: string; turnStatusText: string; showActions?: boolean; showBranchSwitcher?: boolean; branchSwitcher?: BranchSwitcherGroup }
  | { kind: "inline_cancelled"; id: string; messageId: string; text: string; showActions?: boolean; showBranchSwitcher?: boolean; branchSwitcher?: BranchSwitcherGroup }
  | { kind: "compact_starting"; id: string; noticeId: string; anchorId: string }
  | { kind: "compact"; id: string }
  | { kind: "compact_notice"; id: string; oldTokens: number; newTokens: number; oldCount: number; newCount: number }
  | { kind: "model_switch"; id: string; fromModel: string; toModel: string; reason: string; source: string }
  | { kind: "retry_notice"; id: string; scope: string; attempt: number; maxAttempts: number; deadline: number; message: string }
  | { kind: "system_message"; id: string; content: string; kindTag: string }
  | { kind: "spec_card"; id: string; spec: import("../core/types.js").SpecData }
  | { kind: "plan_card"; id: string; review: import("../core/types.js").PlanReviewData }
  | { kind: "workflow"; id: string };

function buildStatusCards(msg: Message): TimelineItem[] {
  const cards: TimelineItem[] = [];
  if (msg.errorMessage) {
    cards.push({ kind: "error_card", id: `ec-${msg.id}`, messageId: msg.id, errorMessage: msg.errorMessage, errorCode: msg.errorCode || "", errorCategory: (msg as any).errorCategory || "" });
  } else if (msg.interruptedReason) {
    cards.push({ kind: "warning_card", id: `wc-${msg.id}`, messageId: msg.id, interruptedReason: msg.interruptedReason });
  }
  if (msg.turnStatusText) {
    cards.push({ kind: "inline_success", id: `is-${msg.id}`, messageId: msg.id, turnStatusText: msg.turnStatusText });
  }
  if (msg.cancelledText) {
    cards.push({ kind: "inline_cancelled", id: `ic-${msg.id}`, messageId: msg.id, text: msg.cancelledText });
  }
  return cards;
}

/**
 * Insertion index for an anchored notice: the position right AFTER the
 * last timeline item that belongs to the anchor message.  Falls back to
 * the end of the list when the anchor is unknown or no longer present
 * (rolled back, or a legacy event without an anchor), so the notice is
 * never dropped.
 */
function _anchorInsertIndex(items: TimelineItem[], anchorId: string): number {
  if (!anchorId) return items.length;
  const mid = `m:${anchorId}`;
  let lastIdx = -1;
  for (let i = 0; i < items.length; i++) {
    const it = items[i] as { id?: string; messageId?: string };
    if (it.id === `u-${mid}` || it.messageId === mid) lastIdx = i;
  }
  return lastIdx >= 0 ? lastIdx + 1 : items.length;
}

export function buildTimeline(msgs: Message[]): TimelineItem[] {
  let items: TimelineItem[] = [];
  let userIndex = 0;
  // Status cards from consecutive assistant messages are deferred and
  // flushed at the end of the group so the error/success banner always
  // sits below every text block, thinking strip, and tool card in the
  // visible turn — even when the backend split the turn into multiple
  // assistant messages.
  let pendingStatusCards: TimelineItem[] = [];

  const st = getState();
  // NOTE: compact dividers are rendered INLINE at their compaction position
  // (from compact_marker / persisted ui-notice messages). The live
  // "compressing" indicator is anchored right after the last compacted
  // message (see addCompactStartingEvent below), so it flips in place into
  // the completed notice when the compaction finishes.

  for (let si = 0; si < (st.systemMessages || []).length; si++) {
    const sm = st.systemMessages[si];
    items.push({ kind: "system_message", id: `sysmsg-${si}`, content: sm.content, kindTag: sm.kind });
  }

  // The spec/plan review cards are pushed at the bottom of the message
  // loop (see below) so they land right after the assistant that triggered
  // the review, instead of at the top of the timeline.

  // Insert workflow progress card when active
  if (st.workflowState && st.workflowState.active) {
    items.push({ kind: "workflow", id: `wf-${st.workflowState.workflowId}` });
  }

  // Branch switchers are PER FORK POINT (git-like). Each fork point groups
  // the parent branch (holding the original version) with every branch that
  // forked at that same message; the switcher on that turn lists only its own
  // sibling set. A session-global list instead showed unrelated branches on
  // the active branch's fork turn ("2/2" on the wrong turn) and let one click
  // jump the whole conversation to an unrelated tail.
  const switcherAnchors = new Map<number, BranchSwitcherGroup>();
  if (st.branches.length > 1) {
    const byFp = new Map<string, BranchMeta[]>();
    for (const b of st.branches) {
      if (!b.fork_point_message_id) continue;
      const list = byFp.get(b.fork_point_message_id) || [];
      list.push(b);
      byFp.set(b.fork_point_message_id, list);
    }
    for (const [fpId, children] of byFp) {
      // Sibling set = parent branch first (it carries the original turn),
      // then the children in creation order.
      const ids: string[] = [];
      const parentId = children[0]?.parent_branch_id || "";
      if (parentId) ids.push(parentId);
      for (const c of [...children].sort((a, b) => (a.created_at || 0) - (b.created_at || 0))) {
        if (!ids.includes(c.id)) ids.push(c.id);
      }
      if (ids.length < 2) continue;
      const activeIdx = ids.indexOf(st.activeBranchId);
      if (activeIdx < 0) continue;
      // Anchor on the LAST assistant message of the turn that follows the
      // fork point -- the turn whose reply the sibling set alternates.
      const fpIdx = msgs.findIndex(m => m.serverId === fpId);
      if (fpIdx < 0) continue;
      let anchor = -1;
      for (let i = fpIdx + 1; i < msgs.length; i++) {
        if (msgs[i].role === "assistant") { anchor = i; break; }
      }
      if (anchor < 0) continue;
      while (anchor + 1 < msgs.length && msgs[anchor + 1].role === "assistant") anchor++;
      switcherAnchors.set(anchor, { ids, activeIdx });
    }
  }

  // A review card exists only while a live turn is blocked on it, so it is
  // anchored on the submit_review call that opened it: no matching call in
  // this transcript means no card -- that is what keeps a stale "review"
  // record from popping up at the top of a fresh turn whose model has not
  // written or submitted anything yet. The card is pushed once, at the end of
  // the owning assistant group.
  let reviewAnchorIdx = -1;
  if (st.running && (st.planReview || st.spec)) {
    const cid = st.planReview?.call_id || "";
    if (cid) {
      for (let k = msgs.length - 1; k >= 0; k--) {
        if (msgs[k].role !== "assistant") continue;
        // The call must still be open: a `done` submit_review means the turn
        // already resolved (approved, rejected, cancelled, or tombstoned), so
        // the decision it asked for is no longer owed.
        const owner = (msgs[k].toolCalls || []).find(tc => tc.id === cid);
        if (owner && owner.status !== "done") { reviewAnchorIdx = k; break; }
      }
    } else if (st.spec) {
      for (let k = msgs.length - 1; k >= 0; k--) {
        if (msgs[k].role === "assistant") { reviewAnchorIdx = k; break; }
      }
    }
    while (reviewAnchorIdx >= 0 && reviewAnchorIdx + 1 < msgs.length
      && msgs[reviewAnchorIdx + 1].role === "assistant") {
      reviewAnchorIdx++;
    }
  }
  if (st.planReview && reviewAnchorIdx < 0) {
    console.log("[review] card suppressed: call=", st.planReview.call_id,
      "running=", st.running, "msgs=", msgs.length);
  }

  for (let i = 0; i < msgs.length; i++) {
    const msg = msgs[i];
    if (msg.role === "user") {
      if (msg.mode === "compact_marker") {
        // Backend compaction boundary — inline "history compacted" divider.
        items.push({ kind: "compact", id: `cp-${msg.id}` });
        continue;
      }
      if (msg.mode === "compact_notice") {
        // Persisted compaction record (backend session notice): inline
        // "context compacted" line with before/after stats in the tooltip.
        const s = msg.compactStats;
        items.push({
          kind: "compact_notice",
          id: `cn-${msg.id}`,
          oldTokens: s?.oldTokens ?? 0,
          newTokens: s?.newTokens ?? 0,
          oldCount: s?.oldCount ?? 0,
          newCount: s?.newCount ?? 0,
        });
        continue;
      }
      if (msg.mode === "model_switch") {
        // Persisted model-switch record (manual or pool cycling): inline
        // "模型已从 A 更改为 B" line with the trigger in the tooltip.
        const s = msg.switchInfo;
        items.push({
          kind: "model_switch",
          id: `ms-${msg.id}`,
          fromModel: s?.fromModel ?? "",
          toModel: s?.toModel ?? "",
          reason: s?.reason ?? "",
          source: s?.source ?? "manual",
        });
        continue;
      }
      items.push({
        kind: "user",
        id: `u-${msg.id}`,
        content: msg.content,
        index: userIndex++,
        mode: msg.mode,
        fileRefs: msg.fileRefs,
      });
    } else if (msg.role === "assistant") {
      const prevIsAssistant = i > 0 && msgs[i - 1].role === "assistant";
      if (!prevIsAssistant) {
        const d = new Date(msg.timestamp || Date.now());
        const y = d.getFullYear();
        const mo = String(d.getMonth() + 1).padStart(2, "0");
        const day = String(d.getDate()).padStart(2, "0");
        const h = String(d.getHours()).padStart(2, "0");
        const mi = String(d.getMinutes()).padStart(2, "0");
        const msgTime = `${y}-${mo}-${day} ${h}:${mi}`;
        items.push({ kind: "ai_header", id: `ah-${msg.id}`, time: msgTime });
      }
      // Use segments ordering to preserve thinking/text/tool interleaving
      // as the model outputs them. Each kind is rendered at its segment
      // position in the exact order they were streamed.
      // Status cards (error/warning/success/cancelled) are intentionally
      // pushed AFTER all segments so they always sit at the bottom of the
      // turn, below any tool calls that arrived later in the stream.
      const assistantStartIdx = items.length;
      if (msg.segments && msg.segments.length > 0) {
        let lastTextSegIndex = -1;
        {
          let segCount = 0;
          for (let si = 0; si < msg.segments.length; si++) {
            if (msg.segments[si].kind === "text") {
              lastTextSegIndex = segCount;
              segCount++;
            }
          }
        }
        let textSegIndex = 0;
        // Thinking segments whose `text` is empty (legacy or partially
        // persisted history) must still render, otherwise switching back to a
        // session silently drops every reasoning block but the first one.
        // `msg.thinking` is the concatenation of all of them, so hand each
        // empty segment the slice that belongs to it: the gap from the end of
        // the previous segment's text to the start of the next one that
        // carries text. Falls back to "everything not yet consumed" for the
        // trailing segment.
        let thinkingCursor = 0;
        for (let si = 0; si < msg.segments.length; si++) {
          const seg = msg.segments[si];
          if (seg.kind === "thinking") {
            // Each thinking segment renders at its actual position with its own text.
            let thinkingText = seg.text || "";
            if (!thinkingText && msg.thinking) {
              // Find the next non-empty thinking segment to bound this slice.
              let nextIdx = -1;
              let nextOffset = -1;
              for (let k = si + 1; k < msg.segments.length; k++) {
                const t = (msg.segments[k] as { kind: string; text?: string }).text || "";
                if (msg.segments[k].kind === "thinking" && t) {
                  nextIdx = k;
                  nextOffset = msg.thinking.indexOf(t, thinkingCursor);
                  if (nextOffset >= 0) break;
                  nextOffset = -1;
                }
              }
              if (nextIdx >= 0 && nextOffset >= 0) {
                thinkingText = msg.thinking.slice(thinkingCursor, nextOffset);
                thinkingCursor = nextOffset;
              } else {
                thinkingText = msg.thinking.slice(thinkingCursor);
                thinkingCursor = msg.thinking.length;
              }
            } else if (thinkingText && msg.thinking) {
              // Keep the cursor aligned so a later empty segment does not
              // re-consume text this one already rendered.
              const at = msg.thinking.indexOf(thinkingText, thinkingCursor);
              if (at >= 0) thinkingCursor = at + thinkingText.length;
            }
            if (thinkingText) {
              items.push({ kind: "thinking", id: `th-${msg.id}-seg-${si}`, text: thinkingText, elapsed: msg.thinkingElapsed, messageId: msg.id });
            }
          } else if (seg.kind === "text") {
            const segText = (seg.text || "").trim();
            const isLast = textSegIndex === lastTextSegIndex;
            const isErrorOnly = msg.errorMessage && segText.startsWith("[Backend API Error]");
            if ((segText.length > 0 || msg.isStreaming) && !isErrorOnly) {
              // A hidden tool between two text pieces leaves no item of its
              // own, so the two pieces would land as sibling text blocks and
              // the turn's flex `gap` would open a blank seam where the tool
              // ran. Re-fold them into the block already on the tape (the
              // segments are the model's own deltas, so plain concatenation
              // restores the exact text flow) and the seam disappears.
              const prev = items[items.length - 1];
              if (prev && prev.kind === "assistant_text" && prev.messageId === msg.id) {
                prev.content += seg.text || "";
                prev.isStreaming = msg.isStreaming;
              } else {
                items.push({
                  kind: "assistant_text",
                  id: `a-${msg.id}-seg-${textSegIndex}`,
                  content: seg.text || "",
                  isStreaming: msg.isStreaming,
                  hasError: msg.hasError,
                  messageId: msg.id,
                });
              }
            }
            textSegIndex++;
          } else if (seg.kind === "tool") {
            const tc = seg.toolId ? msg.toolCalls.find(t => toolCallMatchesId(t, seg.toolId)) : undefined;
            // Skip tools that must never render (hidden), and tools whose
            // name has not streamed in yet (empty name). Rendering an
            // unnamed tool briefly shows a blank generic strip that then
            // vanishes once the name resolves (e.g. to a hidden tool),
            // which reads as a flicker. Keeping them out of the timeline
            // also keeps them out of the render key entirely.
            if (tc && tc.name && !isToolHidden(tc.name)) {
              items.push({ kind: "tool", id: `tc-${tc.id}`, tc, messageId: msg.id });
            }
          }
        }
      } else {
        // Legacy fallback for messages without segments
        if (msg.thinking) {
          items.push({ kind: "thinking", id: `th-${msg.id}`, text: msg.thinking, elapsed: msg.thinkingElapsed, messageId: msg.id });
        }
        for (const tc of msg.toolCalls) {
          // Mirror the segment path: never surface hidden or not-yet-named
          // tools so they cannot flash in and out during streaming.
          if (!tc.name || isToolHidden(tc.name)) continue;
          items.push({ kind: "tool", id: `tc-${tc.id}`, tc, messageId: msg.id });
        }
        if (msg.content.trim().length > 0 || msg.isStreaming) {
          items.push({ kind: "assistant_text", id: `a-${msg.id}`, content: msg.content, isStreaming: msg.isStreaming, hasError: msg.hasError, messageId: msg.id });
        }
      }
      // Status cards for this assistant message. If more assistant messages
      // follow consecutively, defer the cards so they all render at the end
      // of the combined turn.
      const statusCards = buildStatusCards(msg);
      const isLastAssistantInGroup = i === msgs.length - 1 || msgs[i + 1].role !== "assistant";
      if (isLastAssistantInGroup) {
        if (pendingStatusCards.length > 0) {
          items.push(...pendingStatusCards);
          pendingStatusCards = [];
        }
        items.push(...statusCards);
      } else {
        pendingStatusCards.push(...statusCards);
      }
      // Place copy/retry/branch-switcher actions on the very last rendered
      // item of the *last* assistant message in the current group. That
      // guarantees the action bar sits below every text block, thinking
      // strip, tool card, and status card in the visible turn.
      if (isLastAssistantInGroup && assistantStartIdx < items.length) {
        const lastItem = items[items.length - 1];
        if (!msg.isStreaming && !st.running) {
          (lastItem as any).showActions = true;
        }
      }
      // Branch switcher belongs to the turn anchored by a fork point; it is
      // rendered in the turn's action bar, so attach the flag and the
      // per-fork-point sibling group to this message's last rendered item.
      const swGroup = switcherAnchors.get(i);
      if (swGroup && assistantStartIdx < items.length) {
        const lastItem = items[items.length - 1];
        (lastItem as any).showBranchSwitcher = true;
        (lastItem as any).branchSwitcher = swGroup;
      }
      // Push the review card once, at the end of the turn that asked for the
      // decision (see reviewAnchorIdx above).
      if (i === reviewAnchorIdx) {
        if (st.planReview) {
          items.push({ kind: "plan_card", id: `plan-card-${st.planReview.review_id}`, review: st.planReview });
        }
        if (st.spec) {
          items.push({ kind: "spec_card", id: `spec-card-${getState().sessionId}`, spec: st.spec });
        }
      }
    }
  }

  // Safety flush: any deferred status cards that weren't flushed inside the
  // loop (should only happen in edge cases) are appended at the very end.
  if (pendingStatusCards.length > 0) {
    items.push(...pendingStatusCards);
    pendingStatusCards = [];
  }

  // "Compressing" indicator: while a context compaction is in progress the
  // line is anchored right after the message the compaction consumed (the
  // same position the completed notice lands at), so the SAME line flips
  // from "正在压缩上下文" to "上下文已压缩" instead of jumping.  When the
  // anchor is unknown (legacy event) it falls back to the live edge.
  for (const ev of st.compactStartingEvents) {
    const at = _anchorInsertIndex(items, ev.anchorId);
    items.splice(at, 0, {
      kind: "compact_starting",
      id: `compact-starting-${ev.noticeId || ev.timestamp}`,
      noticeId: ev.noticeId,
      anchorId: ev.anchorId,
    });
  }

  // Transient retry indicator: a failed model call is being retried.  Shown
  // at the very bottom (the live edge of the turn) and dropped the moment the
  // retried attempt produces output — `state.clearRetryNotice` runs on the
  // first text/thinking/tool event, which changes the render key and removes
  // this item.
  if (st.retryNotice && st.running) {
    const rn = st.retryNotice;
    items.push({
      kind: "retry_notice",
      id: `retry-notice-${rn.timestamp}-${rn.attempt}`,
      scope: rn.scope,
      attempt: rn.attempt,
      maxAttempts: rn.maxAttempts,
      // Absolute instant the wait is due to end, so the line can tick the
      // remaining seconds down in place. 0 when there is no wait to count
      // (a model switch), which also means "render no countdown".
      deadline: rn.delayMs > 0 ? rn.timestamp + rn.delayMs : 0,
      message: rn.message,
    });
  }

  // Fallback: if there are no assistant messages but a legacy spec is
  // available (e.g. session just loaded), still surface that card at the end.
  // A plan review never lands here -- it has to be anchored on the
  // submit_review call that opened it, and this branch means there is none.
  if (st.running && !st.planReview && st.spec && items.every(it => it.kind !== "assistant_text" && it.kind !== "ai_header" && it.kind !== "thinking" && it.kind !== "tool")) {
    items.push({ kind: "spec_card", id: `spec-card-${getState().sessionId}`, spec: st.spec });
  }

  // A turn whose only content was a hidden tool call contributes no item of
  // its own, so its `ai_header` would stand alone as a bare "Encre Agent" line
  // floating above the next user bubble — a gap the user reads as the hidden
  // tool taking up space. Drop a header that heads no visible content so a
  // fully-hidden turn occupies nothing. The trailing group is the exception:
  // while a turn is live the header is the waiting state the user just
  // triggered, and it must be there before the first token arrives.
  const TURN_BREAK = new Set<TimelineItem["kind"]>(["user", "compact", "workflow", "ai_header"]);
  items = items.filter((it, idx) => {
    if (it.kind !== "ai_header") return true;
    const next = items[idx + 1];
    if (next) return !TURN_BREAK.has(next.kind);
    return st.running;
  });

  // The resume affordance on an interrupted turn belongs to the tail alone:
  // the moment anything else lands below it (a new user message, or the
  // resumed turn's own output), the card is no longer last in the transcript
  // and 「继续」 must disappear. Sub-agent threads never get it — resuming
  // there would send the prompt to the main session.
  const tailItem = items[items.length - 1];
  if (tailItem && tailItem.kind === "warning_card" && !st.subAgentView) {
    tailItem.showContinue = true;
  }

  return items;
}

export function _fmtTokens(n: number): string {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "K";
  return String(n);
}

export function fmtSize(bytes: number): string {
  if (!bytes || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function buildRenderKey(timeline: TimelineItem[]): string {
  // Key only encodes STRUCTURAL changes. Any per-item content (streaming
  // text, tool params, tool status, tool result body, workflow task
  // progress) is handled by incrementalTextUpdate. Including those here
  // forces a full innerHTML reset on every delta, which makes the chat
  // feel like it is "always rendering" and prevents the user from
  // scrolling up while the model is streaming.
  //
  // Encoded by hand rather than through JSON.stringify: the key is rebuilt
  // on every render frame, so JSON's per-item object allocation plus
  // string-escaping churn compounds over a long conversation. Free-form
  // fields are length-prefixed, so a field value can never be mistaken for
  // a separator.
  // The branch switcher's sibling group is structural too: the same item can
  // keep its flag while the group membership or active index changes (e.g.
  // after a new retry fork), and the switcher markup must be rebuilt then.
  const branchGroupKey = (item: TimelineItem): string => {
    const g = (item as { branchSwitcher?: BranchSwitcherGroup }).branchSwitcher;
    return g ? "B" + g.ids.join(",") + "@" + g.activeIdx : "";
  };
  const buf: Array<string | number> = [];
  for (let i = 0; i < timeline.length; i++) {
    const item = timeline[i];
    if (item.kind === "user") {
      buf.push("u", item.content.length, ":", item.content, ";");
    } else if (item.kind === "ai_header") {
      buf.push("ah", item.time, ";");
    } else if (item.kind === "thinking") {
      buf.push("th", item.id, ";");
    } else if (item.kind === "tool") {
      // The result transition is structural (a body slot gets filled);
      // status/params/error changes are handled incrementally.  For
      // agent tool calls, the task-divider count is structural too:
      // when subAgentMessages arrive with new dividers (e.g. after a
      // session switch restores empty state) the agent card layout
      // must be rebuilt from scratch.
      const tc = item.tc;
      let divs = "";
      if (tc.subAgentMessages) {
        for (let j = 0; j < tc.subAgentMessages.length; j++) {
          const sub = tc.subAgentMessages[j];
          if (sub.mode !== "task_divider") continue;
          const name = sub.taskName || "";
          const status = sub.taskStatus || "";
          divs += (sub.taskIndex ?? j) + "/" + name.length + ":" + name + "/" + status.length + ":" + status + ",";
        }
      }
      // Result presence is intentionally NOT part of the key: when a tool
      // finishes, only that one card changes (body/error/info-card markup) and
      // the stream handler patches it in place. Encoding it here forced a full
      // transcript rebuild — every turn's markdown and highlighting — per tool.
      buf.push(
        "tc", tc.id, ";",
        tc.name.length, ":", tc.name, ";",
        divs, ";",
        item.showActions ? "1" : "0", item.showBranchSwitcher ? "1" : "0", branchGroupKey(item), ";",
      );
    } else if (item.kind === "compact") {
      buf.push("cp", item.id, ";");
    } else if (item.kind === "compact_starting") {
      buf.push("cs", item.id, ";");
    } else if (item.kind === "retry_notice") {
      // Only the identity and the static fields belong here: the countdown
      // itself ticks in place (see syncRetryCountdown), so encoding a
      // per-second value would force a full transcript re-render every second.
      buf.push("rn", item.id, "/", item.deadline, "/", item.scope, ";");
    } else if (item.kind === "plan_card") {
      // The review record is session state, not message state, so its
      // transitions (review -> approved / rejected / cancelled, and the
      // feedback the user attached) must be part of the key or the card
      // keeps rendering the decision the user already made.
      const rv = item.review;
      buf.push(
        "pc", rv.review_id, ";", rv.call_id || "", ";", rv.status, ";", rv.mode, ";",
        (rv.content || "").length, ";", (rv.feedback || "").length, ";",
        (rv.files || []).length, ";",
      );
    } else if (item.kind === "spec_card") {
      const sp = item.spec;
      buf.push("sc", ";", sp.status, ";", (sp.sections || []).length, ";");
    } else if (item.kind === "workflow") {
      const wf = getState().workflowState;
      if (wf) buf.push("wf", wf.workflowId, ";", wf.tasks.length, ";", wf.active ? "1" : "0", ";");
      else buf.push("wfn;");
    } else if (item.kind === "assistant_text") {
      // Streaming/finished transition is structural (turn actions appear,
      // streaming class toggles). Content stays incremental.
      buf.push("a", item.id, ";", item.isStreaming ? "1" : "0", item.showActions ? "1" : "0", item.showBranchSwitcher ? "1" : "0", branchGroupKey(item), ";");
    } else if (item.kind === "error_card") {
      buf.push("ec", item.id, ";", item.showActions ? "1" : "0", item.showBranchSwitcher ? "1" : "0", branchGroupKey(item), ";");
    } else if (item.kind === "warning_card") {
      buf.push("wc", item.id, ";", item.showContinue ? "1" : "0", item.showActions ? "1" : "0", item.showBranchSwitcher ? "1" : "0", branchGroupKey(item), ";");
    } else if (item.kind === "inline_success") {
      buf.push("is", item.id, ";", item.showActions ? "1" : "0", item.showBranchSwitcher ? "1" : "0", branchGroupKey(item), ";");
    } else if (item.kind === "inline_cancelled") {
      buf.push("ic", item.id, ";", item.showActions ? "1" : "0", item.showBranchSwitcher ? "1" : "0", branchGroupKey(item), ";");
    } else if (item.kind === "compact_notice") {
      // Coalescing UPDATES the persisted notice in place (same id, new
      // stats) — the content fields must be structural or the line keeps
      // showing the first burst's stale values.
      buf.push("cn", item.id, ";", item.oldTokens, "/", item.newTokens, "/", item.oldCount, "/", item.newCount, ";");
    } else if (item.kind === "model_switch") {
      buf.push("ms", item.id, ";", item.fromModel, "->", item.toModel, "/", item.source, ";",
        item.reason.length, ":", item.reason, ";");
    } else {
      // All TimelineItem kinds handled above — this fallback keeps TS happy.
      buf.push(";");
    }
  }
  return buf.join("");
}

export interface StatusQuote {
  textKey: string;
}

const STATUS_QUOTES: StatusQuote[] = [
  { textKey: "statusQuoteWaves" },
  { textKey: "statusQuoteCpu" },
  { textKey: "statusQuoteBrain" },
  { textKey: "statusQuoteZap" },
  { textKey: "statusQuoteDices" },
  { textKey: "statusQuoteNetwork" },
  { textKey: "statusQuoteRocket" },
  { textKey: "statusQuoteTarget" },
  { textKey: "statusQuotePuzzle" },
  { textKey: "statusQuoteCrystalBall" },
  { textKey: "statusQuotePalette" },
  { textKey: "statusQuoteEgg" },
  { textKey: "statusQuoteGuitar" },
  { textKey: "statusQuoteBinary" },
  { textKey: "statusQuoteBattery" },
  { textKey: "statusQuoteBrush" },
  { textKey: "statusQuoteFlask" },
];

export function pickRandomQuote(): StatusQuote {
  return STATUS_QUOTES[Math.floor(Math.random() * STATUS_QUOTES.length)];
}
