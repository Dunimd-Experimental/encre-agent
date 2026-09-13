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

import type { Message, ToolCallState } from "../core/types.js";
import { getState, toolCallMatchesId } from "../core/state.js";
import { isHiddenTool } from "./chat_tool_display.js";

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
  | { kind: "user"; id: string; content: string; index: number; showBranchSwitcher?: boolean; mode?: string; fileRefs?: { name: string; size: number; icon: string }[] }
  | { kind: "ai_header"; id: string; time: string }
  | { kind: "thinking"; id: string; text: string; elapsed?: number; messageId?: string }
  | { kind: "tool"; id: string; tc: ToolCallState; messageId?: string; showActions?: boolean; showBranchSwitcher?: boolean }
  | { kind: "assistant_text"; id: string; content: string; isStreaming: boolean; hasError?: boolean; messageId?: string; showActions?: boolean; showBranchSwitcher?: boolean }
  | { kind: "error_card"; id: string; messageId: string; errorMessage: string; errorCode: string; errorCategory?: string; showActions?: boolean; showBranchSwitcher?: boolean }
  | { kind: "warning_card"; id: string; messageId: string; interruptedReason: string; showActions?: boolean; showBranchSwitcher?: boolean }
  | { kind: "inline_success"; id: string; messageId: string; turnStatusText: string; showActions?: boolean; showBranchSwitcher?: boolean }
  | { kind: "inline_cancelled"; id: string; messageId: string; text: string; showActions?: boolean; showBranchSwitcher?: boolean }
  | { kind: "compact_starting"; id: string }
  | { kind: "compact"; id: string }
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

export function buildTimeline(msgs: Message[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let userIndex = 0;
  // Status cards from consecutive assistant messages are deferred and
  // flushed at the end of the group so the error/success banner always
  // sits below every text block, thinking strip, and tool card in the
  // visible turn — even when the backend split the turn into multiple
  // assistant messages.
  let pendingStatusCards: TimelineItem[] = [];

  const st = getState();
  if (st.branches.length > 1) {
    console.log("[buildTimeline] branches=%d active=%s msgs=%d", st.branches.length, st.activeBranchId?.slice(-8), msgs.length);
  }
  // NOTE: compact dividers are rendered INLINE at their compaction position
  // (from compact_marker messages) — not stacked at the top. The
  // "compressing" indicator is appended at the END of the timeline below,
  // matching where the paused turn actually is.

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

  // Find the fork-point user message (where branches diverge) so we can
  // render the branch switcher there instead of at the last assistant.
  // If serverId matching fails (e.g. locally-added messages), fall back
  // to the last user message position.
  let forkMsgIdx = -1;
  if (st.branches.length > 1) {
    const cur = st.branches.find(b => b.id === st.activeBranchId);
    const fpId = cur?.fork_point_message_id;
    if (fpId) {
      forkMsgIdx = msgs.findIndex(m => m.serverId === fpId);
      if (forkMsgIdx < 0) {
        // Fallback: last user message (no serverId — locally added)
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === "user") { forkMsgIdx = i; break; }
        }
      }
    } else {
      // Root branch — find first child branch's fork point
      for (const b of st.branches) {
        if (b.parent_branch_id === st.activeBranchId && b.fork_point_message_id) {
          const idx = msgs.findIndex(m => m.serverId === b.fork_point_message_id);
          if (idx >= 0) { forkMsgIdx = idx; break; }
        }
      }
    }
  }

  // Find the first assistant message after the fork point — the branch
  // switcher renders below this message rather than below the user bubble.
  let firstAssistantAfterForkIdx = -1;
  if (forkMsgIdx >= 0 && st.branches.length > 1) {
    for (let i = forkMsgIdx + 1; i < msgs.length; i++) {
      if (msgs[i].role === "assistant") {
        firstAssistantAfterForkIdx = i;
        break;
      }
    }
  }

  for (let i = 0; i < msgs.length; i++) {
    const msg = msgs[i];
    if (msg.role === "user") {
      if (msg.mode === "compact_marker") {
        // Backend compaction boundary — inline "history compacted" divider.
        items.push({ kind: "compact", id: `cp-${msg.id}` });
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
        let thinkingFallbackUsed = false;
        for (let si = 0; si < msg.segments.length; si++) {
          const seg = msg.segments[si];
          if (seg.kind === "thinking") {
            // Each thinking segment renders at its actual position with its own text.
            // When a segment has no per-segment text (legacy history), use msg.thinking
            // only on the first occurrence to avoid duplication.
            let thinkingText = seg.text || "";
            if (!thinkingText && !thinkingFallbackUsed && msg.thinking) {
              thinkingText = msg.thinking;
              thinkingFallbackUsed = true;
            }
            if (thinkingText) {
              items.push({ kind: "thinking", id: `th-${msg.id}-seg-${si}`, text: thinkingText, elapsed: msg.thinkingElapsed, messageId: msg.id });
            }
          } else if (seg.kind === "text") {
            const segText = (seg.text || "").trim();
            const isLast = textSegIndex === lastTextSegIndex;
            const isErrorOnly = msg.errorMessage && segText.startsWith("[Backend API Error]");
            if ((segText.length > 0 || msg.isStreaming) && !isErrorOnly) {
              items.push({
                kind: "assistant_text",
                id: `a-${msg.id}-seg-${textSegIndex}`,
                content: seg.text || "",
                isStreaming: msg.isStreaming,
                hasError: msg.hasError,
                messageId: msg.id,
              });
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
            if (tc && tc.name && !isHiddenTool(tc.name)) {
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
          if (!tc.name || isHiddenTool(tc.name)) continue;
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
      // Branch switcher belongs to the first assistant after a fork; it is
      // rendered at the end of the turn, so attach the flag to this message's
      // last rendered item.
      if (i === firstAssistantAfterForkIdx && assistantStartIdx < items.length) {
        const lastItem = items[items.length - 1];
        (lastItem as any).showBranchSwitcher = true;
      }
      // After the LAST assistant message in the timeline, push the spec/plan
      // review card (if any) so it sits at the natural review position —
      // right after the assistant that triggered it, before the next user
      // message.
      if (i === msgs.length - 1 || (i + 1 < msgs.length && msgs[i + 1].role !== "assistant")) {
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

  // "Compressing" indicator: shown at the bottom (where the paused turn
  // actually is) while a context compaction is in progress. Cleared when
  // the compact event arrives and the inline divider takes over.
  for (let ci = 0; ci < st.compactStartingEvents.length; ci++) {
    items.push({ kind: "compact_starting", id: `compact-starting-${ci}` });
  }

  // Fallback: if there are no assistant messages but a review/spec is
  // available (e.g. session just loaded), still surface the card at the end.
  if (items.every(it => it.kind !== "assistant_text" && it.kind !== "ai_header" && it.kind !== "thinking" && it.kind !== "tool")) {
    if (st.planReview) {
      items.push({ kind: "plan_card", id: `plan-card-${st.planReview.review_id}`, review: st.planReview });
    }
    if (st.spec) {
      items.push({ kind: "spec_card", id: `spec-card-${getState().sessionId}`, spec: st.spec });
    }
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
  return JSON.stringify(timeline.map(i => {
    if (i.kind === "user") return { k: "u", c: i.content };
    if (i.kind === "ai_header") return { k: "ah", t: i.time };
    if (i.kind === "thinking") return { k: "th", id: i.id };
    if (i.kind === "tool") {
      // The result transition is structural (a body slot gets filled);
      // status/params/error changes are handled incrementally.  For
      // agent tool calls, the task-divider count is structural too:
      // when subAgentMessages arrive with new dividers (e.g. after a
      // session switch restores empty state) the agent card layout
      // must be rebuilt from scratch.
      const taskDividers = i.tc.subAgentMessages
        ? i.tc.subAgentMessages
          .filter((message) => message.mode === "task_divider")
          .map((message, index) => ({
            index: message.taskIndex ?? index,
            name: message.taskName || "",
            status: message.taskStatus || "",
          }))
        : [];
      return { k: "tc", id: i.id, n: i.tc.name, r: i.tc.result ? 1 : 0, d: taskDividers, sa: i.showActions ? 1 : 0, sb: i.showBranchSwitcher ? 1 : 0 };
    }
    if (i.kind === "compact") return { k: "cp", id: i.id };
    if (i.kind === "compact_starting") return { k: "cs", id: i.id };
    if (i.kind === "workflow") {
      const wf = getState().workflowState;
      if (wf) return { k: "wf", id: wf.workflowId, n: wf.tasks.length, a: wf.active ? 1 : 0 };
      return { k: "wf" };
    }
    if (i.kind === "assistant_text") {
      // Streaming/finished transition is structural (turn actions appear,
      // streaming class toggles). Content stays incremental.
      return { k: "a", id: i.id, st: i.isStreaming ? 1 : 0, sa: i.showActions ? 1 : 0, sb: i.showBranchSwitcher ? 1 : 0 };
    }
    if (i.kind === "error_card") return { k: "ec", id: i.id, sa: i.showActions ? 1 : 0, sb: i.showBranchSwitcher ? 1 : 0 };
    if (i.kind === "warning_card") return { k: "wc", id: i.id, sa: i.showActions ? 1 : 0, sb: i.showBranchSwitcher ? 1 : 0 };
    if (i.kind === "inline_success") return { k: "is", id: i.id, sa: i.showActions ? 1 : 0, sb: i.showBranchSwitcher ? 1 : 0 };
    if (i.kind === "inline_cancelled") return { k: "ic", id: i.id, sa: i.showActions ? 1 : 0, sb: i.showBranchSwitcher ? 1 : 0 };
    // All TimelineItem kinds handled above — this fallback keeps TS happy.
    return { k: "" };
  }));
}

export interface StatusQuote {
  icon: string;
  textKey: string;
}

const STATUS_QUOTES: StatusQuote[] = [
  { icon: "waves", textKey: "statusQuoteWaves" },
  { icon: "cpu", textKey: "statusQuoteCpu" },
  { icon: "brain", textKey: "statusQuoteBrain" },
  { icon: "zap", textKey: "statusQuoteZap" },
  { icon: "dices", textKey: "statusQuoteDices" },
  { icon: "network", textKey: "statusQuoteNetwork" },
  { icon: "rocket", textKey: "statusQuoteRocket" },
  { icon: "target", textKey: "statusQuoteTarget" },
  { icon: "puzzle", textKey: "statusQuotePuzzle" },
  { icon: "crystal-ball", textKey: "statusQuoteCrystalBall" },
  { icon: "palette", textKey: "statusQuotePalette" },
  { icon: "egg", textKey: "statusQuoteEgg" },
  { icon: "guitar", textKey: "statusQuoteGuitar" },
  { icon: "binary", textKey: "statusQuoteBinary" },
  { icon: "battery-charging", textKey: "statusQuoteBattery" },
  { icon: "brush", textKey: "statusQuoteBrush" },
  { icon: "flask-conical", textKey: "statusQuoteFlask" },
];

export function pickRandomQuote(): StatusQuote {
  return STATUS_QUOTES[Math.floor(Math.random() * STATUS_QUOTES.length)];
}
