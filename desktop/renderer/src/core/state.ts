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
 * Global application state store.
 *
 * A tiny reactive singleton holding the entire renderer-side `AppState`
 * (connection, active session, messages, tool calls, plans, telemetry,
 * notifications, settings, …). Components read via `getState()` and subscribe
 * for re-render via `subscribe()`; state is mutated only through the exported
 * `set*`/`add*`/`update*` helpers, which mirror changes into per-session
 * snapshots and notify listeners on a microtask.
 */

import { AppState, createEmptyState, createEmptySessionSnapshot, Message, ToolCallState, TelemetryData, UsageStatsData, TokenUsage, PlanItem, PlanProposal, NotificationItem, AttachmentMeta, TimelineSegment, BranchMeta, SessionSnapshot, SearchFilter, defaultSearchFilter, SessionState } from "./types.js";
import { t } from "../features/i18n.js";
import { findSlashCommand } from "../features/slash_commands.js";
import { buildTraySessionData, dedupeSessions } from "../chat/session-projection.js";
import { Dialog } from "../ui/dialog.js";

type Listener = () => void;

function loadSearchFilter(): SearchFilter {
  try {
    const raw = localStorage.getItem("encre-search-filter");
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<SearchFilter>;
      return { ...defaultSearchFilter(), ...parsed };
    }
  } catch {
    /* ignore corrupt value */
  }
  return defaultSearchFilter();
}

let state = createEmptyState();
state.searchFilter = loadSearchFilter();
const listeners = new Set<Listener>();
let pendingRollbackEdit: { serverId?: string; userIdx: number; content: string } | null = null;

function getSessionKey(sessionId?: string): string {
  return sessionId ?? state.sessionId ?? "";
}

function getOrCreateSessionSnapshot(sessionId?: string): SessionSnapshot {
  const key = getSessionKey(sessionId);
  if (!state.sessionStore[key]) {
    state.sessionStore[key] = createEmptySessionSnapshot();
  }
  return state.sessionStore[key];
}

function getSessionSnapshot(sessionId?: string): SessionSnapshot {
  return state.sessionStore[getSessionKey(sessionId)] ?? createEmptySessionSnapshot();
}

function syncActiveSessionState(): void {
  const snapshot = getSessionSnapshot(state.sessionId);
  state.messages = snapshot.messages;
  state.tokenUsage = snapshot.tokenUsage;
  state.telemetry = snapshot.telemetry;
  state.planItems = snapshot.planItems;
  state.planModeActive = snapshot.planModeActive;
  state.planProposals = snapshot.planProposals;
  state.artifacts = snapshot.artifacts;
  state.references = snapshot.references;
state.compactEvents = snapshot.compactEvents;
  state.compactStartingEvents = snapshot.compactStartingEvents || [];
  state.systemMessages = snapshot.systemMessages || [];
  state.branches = snapshot.branches;
  state.activeBranchId = snapshot.activeBranchId;
  state.running = snapshot.running;
  state.agentState = snapshot.agentState ?? null;
  state.planReview = snapshot.planReview;
}

function syncSessionState(sessionId?: string): void {
  if (getSessionKey(sessionId) === getSessionKey(state.sessionId)) {
    syncActiveSessionState();
  }
}

/** Returns the current (read-only) application state. */
export function getState(): Readonly<AppState> {
  return state;
}

/** Subscribes a listener to state changes; returns an unsubscribe function. */
export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

let _emitPending = false;

function emit(): void {
  if (_emitPending) return;
  _emitPending = true;
  queueMicrotask(() => {
    _emitPending = false;
    for (const fn of listeners) {
      fn();
    }
  });
}

function update(partial: Partial<AppState>): void {
  state = { ...state, ...partial };
  emit();
}

/** Sets the WebSocket connected flag. */
export function setConnected(v: boolean): void {
  update({ connected: v });
}

/** Switches the active session id, resetting scoped fields and syncing its snapshot. */
export function setSessionId(id: string): void {
  if (id !== state.sessionId) {
    Dialog.swapSession(state.sessionId, id);
  }
  state.sessionId = id;
  getOrCreateSessionSnapshot(id);
  // Reset session-scoped global fields so the sidebar canvas does not
  // display stale data from a previously active session. The fields are
  // repopulated by the next context_usage events. Per-session fields
  // (telemetry, tokenUsage, ...) are restored by syncActiveSessionState().
  state.contextTokens = 0;
  state.contextWindow = 0;
  syncActiveSessionState();
  emit();
}

/** Appends a message to the (active) session snapshot. */
export function addMessage(msg: Message, sessionId = state.sessionId): void {
  // Defense-in-depth: refuse to write into a different session's snapshot
  // than the one currently active.
  if (sessionId && state.sessionId && sessionId !== state.sessionId) return;
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.messages.push(msg);
  syncSessionState(sessionId);
  emit();
}

function extractMessageText(content: string | Array<{ type: string; text: string }>): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((b: any) => b.type === "text")
      .map((b: any) => b.text)
      .join("");
  }
  return "";
}

/** A renderer tool-call id may differ from the backend protocol id after resume. */
export function toolCallMatchesId(tc: ToolCallState, id: string | undefined): boolean {
  return !!id && (tc.id === id || tc.backendId === id);
}

type ParallelTaskGroup = {
  index: number;
  divider: Message;
  messages: Message[];
};

function splitParallelTaskGroups(messages: Message[]): ParallelTaskGroup[] {
  const groups = new Map<number, ParallelTaskGroup>();
  let fallbackIndex = 0;
  let current: ParallelTaskGroup | null = null;

  for (const message of messages) {
    if (message.mode === "task_divider") {
      const index = Number.isInteger(message.taskIndex) ? message.taskIndex! : fallbackIndex;
      fallbackIndex = Math.max(fallbackIndex, index + 1);
      current = { index, divider: message, messages: [] };
      // A resumed stream can replay a divider. The newest divider has the
      // current task status/name, so it deliberately replaces earlier metadata.
      groups.set(index, current);
    } else if (current) {
      current.messages.push(message);
    }
  }

  return [...groups.values()].sort((a, b) => a.index - b.index);
}

/**
 * Merges partial parallel-sub-agent snapshots without letting one task erase
 * its siblings. Non-parallel calls keep the normal replace-on-update behavior.
 */
export function mergeSubAgentMessages(existing: Message[] | undefined, incoming: Message[]): Message[] {
  if (!existing || existing.length === 0) return incoming;

  const existingHasDividers = existing.some((message) => message.mode === "task_divider");
  const incomingHasDividers = incoming.some((message) => message.mode === "task_divider");
  if (!existingHasDividers && !incomingHasDividers) return incoming;
  if (!incomingHasDividers) return existing;
  if (!existingHasDividers) return incoming;

  const merged = new Map<number, ParallelTaskGroup>();
  for (const group of splitParallelTaskGroups(existing)) {
    merged.set(group.index, group);
  }
  for (const group of splitParallelTaskGroups(incoming)) {
    const previous = merged.get(group.index);
    // Some reconnect/progress packets contain just a refreshed divider. Keep
    // already-rendered transcript data until this task sends a real snapshot.
    merged.set(group.index, {
      ...group,
      messages: group.messages.length > 0 || !previous ? group.messages : previous.messages,
    });
  }

  return [...merged.values()]
    .sort((a, b) => a.index - b.index)
    .flatMap((group) => [group.divider, ...group.messages]);
}

function restoreToolCalls(rawToolCalls: any[] | undefined): ToolCallState[] {
  const restoredToolCalls: ToolCallState[] = [];
  if (!rawToolCalls || !Array.isArray(rawToolCalls)) return restoredToolCalls;

  for (const tc of rawToolCalls) {
    const func = tc.function || {};
    let params: Record<string, unknown> = {};
    try {
      params = JSON.parse(func.arguments || "{}");
    } catch {
      params = { arguments: func.arguments };
    }
    // Prefer the synthetic client-facing id (``_client_id``) so that
    // streaming tool_result events delivered via client_id can be
    // correlated with the right tc.  The backend persists both the
    // protocol-mandated ``id`` (the real backend id used by the LLM)
    // and ``_client_id`` (the synthetic id used for renderer events).
    // They may differ when the backend id is something like
    // ``toolu_xxx``; matching on ``_client_id`` makes the renderer's
    // streaming update path work after a session restore.
    const clientId = tc._client_id;
    restoredToolCalls.push({
      id: clientId || tc.id || crypto.randomUUID(),
      backendId: tc.id && tc.id !== clientId ? tc.id : undefined,
      name: func.name || "unknown",
      params,
      result: undefined,
      isError: false,
      status: "done",
    });
  }
  return restoredToolCalls;
}

function indexToolCalls(toolCallsById: Map<string, ToolCallState>, toolCalls: ToolCallState[]): void {
  for (const toolCall of toolCalls) {
    toolCallsById.set(toolCall.id, toolCall);
    if (toolCall.backendId) toolCallsById.set(toolCall.backendId, toolCall);
  }
}

function findRestoredToolCall(
  raw: { tool_call_id?: string; _client_id?: string },
  toolCallsById: Map<string, ToolCallState>,
  fallbackAssistant?: Message,
): ToolCallState | undefined {
  const clientId = raw._client_id;
  const toolCallId = raw.tool_call_id;
  const target = (clientId ? toolCallsById.get(clientId) : undefined)
    || (toolCallId ? toolCallsById.get(toolCallId) : undefined);

  if (target) return target;
  if (clientId || toolCallId) {
    console.warn("[state] Unable to restore tool result", {
      clientId,
      toolCallId,
      knownToolCallIds: [...toolCallsById.keys()],
    });
    return undefined;
  }

  // Legacy history without a tool-call id can only be paired by position.
  return fallbackAssistant?.toolCalls[fallbackAssistant.toolCalls.length - 1];
}

/** Reconciles a raw message list into `Message` objects (used on resume). */
export function restoreMessages(rawMessages: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string; segments?: Array<{kind: string; text?: string; tool_id?: string}>; created_at?: number; mode?: string }>): Message[] {
  const messages: Message[] = [];
  const toolCallsById = new Map<string, ToolCallState>();
  let latestAssistantWithToolCalls: Message | undefined;
  for (const raw of rawMessages || []) {
    if (raw.role === "system") continue;
    if (raw.role === "compact_marker") {
      // Backend compaction boundary: renders as an inline "history
      // compacted" divider, never as a user bubble.
      messages.push({
        id: (raw as any).id ? `m:${(raw as any).id}` : crypto.randomUUID(),
        role: "user",
        content: "",
        isStreaming: false,
        toolCalls: [],
        segments: [],
        timestamp: (raw as any).created_at || Date.now(),
        mode: "compact_marker",
      });
      continue;
    }
    if (raw.role === "tool") {
      const target = findRestoredToolCall(raw as any, toolCallsById, latestAssistantWithToolCalls);
      if (target) {
        const toolText = extractMessageText(raw.content);
        target.result = toolText;
        target.isError = /^error:/i.test(toolText) || /^permission denied/i.test(toolText);
        target.status = "done";
        const subAgentMessages = (raw as any).sub_agent_messages;
        if (Array.isArray(subAgentMessages)) {
          target.subAgentMessages = restoreMessages(subAgentMessages);
        }
      }
      continue;
    }
    const toolCalls = restoreToolCalls(raw.tool_calls);
    const rawSegments = raw.segments;
    const segments: TimelineSegment[] = rawSegments?.length
      ? rawSegments.reduce<TimelineSegment[]>((acc, s) => {
          const next = s.kind === "thinking"
            ? { kind: "thinking" as const, text: s.text ?? "" }
            : s.kind === "tool"
              ? {
                  kind: "tool" as const,
                  toolId: toolCalls.find((toolCall) => toolCallMatchesId(toolCall, s.tool_id))?.id || s.tool_id || "",
                }
              : { kind: "text" as const, text: s.text ?? "" };
          const prev = acc[acc.length - 1];
          if (prev?.kind === "thinking" && next.kind === "thinking") {
            prev.text = (prev.text || "") + (next.text || "");
          } else if (prev?.kind === "text" && next.kind === "text") {
            prev.text = (prev.text || "") + (next.text || "");
          } else {
            acc.push(next);
          }
          return acc;
        }, [])
      : [
          ...((raw.reasoning_content ? [{ kind: "thinking" as const, text: raw.reasoning_content }] : [])),
          ...((extractMessageText(raw.content).trim() ? [{ kind: "text" as const, text: extractMessageText(raw.content) }] : [])),
          ...toolCalls.map((tc) => ({ kind: "tool" as const, toolId: tc.id })),
        ];
    const message: Message = {
      id: crypto.randomUUID(),
      role: raw.role === "assistant" ? "assistant" : "user",
      content: extractMessageText(raw.content),
      isStreaming: false,
      toolCalls,
      segments,
      timestamp: raw.created_at || Date.now(),
      thinking: raw.reasoning_content,
      mode: raw.mode,
      taskIndex: (raw as any).task_index,
      taskName: (raw as any).task_name,
      taskStatus: (raw as any).task_status,
      serverId: (raw as any).id,
      errorMessage: (raw as any).errorMessage,
      errorCode: (raw as any).errorCode,
      interruptedReason: (raw as any).interruptedReason,
      turnStatusText: (raw as any).turnStatusText,
      cancelledText: (raw as any).cancelledText,
      hasError: !!(raw as any).hasError,
      errorCategory: (raw as any).errorCategory,
    };
    messages.push(message);
    if (message.role === "assistant" && toolCalls.length > 0) {
      indexToolCalls(toolCallsById, toolCalls);
      latestAssistantWithToolCalls = message;
    }
  }
  if (rawMessages && rawMessages.length > 0) { console.log("[restoreMessages] input=" + rawMessages.length + " output=" + messages.length + " roles=" + JSON.stringify(messages.map(m => m.role))); }
  return messages;
}

/** Loads a session's raw messages into the active session snapshot. */
export function loadSessionMessages(rawMessages: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string }>, sessionId = state.sessionId): void {
  const messages: Message[] = [];
  const toolCallsById = new Map<string, ToolCallState>();
  let latestAssistantWithToolCalls: Message | undefined;
  let totalInput = 0;
  let totalOutput = 0;

  for (const raw of rawMessages) {
    if (raw.role === "system") continue;

    if (raw.role === "compact_marker") {
      // Backend compaction boundary: renders as an inline "history
      // compacted" divider, never as a user bubble.
      messages.push({
        id: (raw as any).id ? `m:${(raw as any).id}` : crypto.randomUUID(),
        role: "user",
        content: "",
        isStreaming: false,
        toolCalls: [],
        segments: [],
        timestamp: (raw as any).created_at || Date.now(),
        mode: "compact_marker",
      });
      continue;
    }

    if (raw.role === "tool") {
      const target = findRestoredToolCall(raw as any, toolCallsById, latestAssistantWithToolCalls);
      if (target) {
        const toolText = extractMessageText(raw.content);
        target.result = toolText;
        target.isError = /^error:/i.test(toolText) || /^permission denied/i.test(toolText);
        target.status = "done";
        const subAgentMessages = (raw as any).sub_agent_messages;
        if (Array.isArray(subAgentMessages)) {
          target.subAgentMessages = restoreMessages(subAgentMessages);
        }
        target.subAgentSessionId = (raw as any).sub_agent_session_id;
      }
      continue;
    }

    const content = extractMessageText(raw.content);
    const cleanContent = (content.includes("<attach ") || content.includes("<terminal>") || content.includes("<mode>")) ? "" : content;
    const usage = (raw as any).usage || (raw as any).token_usage;
    const tu = usage ? {
      input_tokens: usage.input_tokens ?? 0,
      output_tokens: usage.output_tokens ?? 0,
      total_tokens: usage.total_tokens ?? 0,
    } : undefined;
    if (tu) {
      totalInput += tu.input_tokens;
      totalOutput += tu.output_tokens;
    }

    const toolCalls = restoreToolCalls(raw.tool_calls);
    // Use server-provided segments if available (preserves original streaming order),
    // otherwise reconstruct as thinking → text → tools.
    const rawSegments = (raw as any).segments as Array<{kind: string; text?: string; tool_id?: string}> | undefined;
    let segments: TimelineSegment[];
    if (rawSegments && rawSegments.length > 0) {
      segments = rawSegments.map(s => {
        if (s.kind === "thinking") return { kind: "thinking" as const, text: s.text ?? "" };
        if (s.kind === "text") return { kind: "text" as const, text: s.text ?? "" };
        if (s.kind === "tool") {
          const tc = toolCalls.find(t => toolCallMatchesId(t, s.tool_id));
          return { kind: "tool" as const, toolId: tc?.id || s.tool_id || "" };
        }
        return { kind: "text" as const, text: "" };
      });
    } else {
      segments = [];
      const thinkingText = (raw as any).reasoning_content as string | undefined;
      if (thinkingText) segments.push({ kind: "thinking", text: thinkingText });
      if (cleanContent) segments.push({ kind: "text", text: cleanContent });
      for (const tc of toolCalls) {
        segments.push({ kind: "tool", toolId: tc.id });
      }
    }

    const msgMode = (raw as any).mode;
    let rawFileRefs = (raw as any).fileRefs as { name: string; size: number; icon: string; path?: string; mime_type?: string }[] | undefined;
    if (!rawFileRefs) {
      const atts = (raw as any).attachments as any[] | undefined;
      if (atts) {
        rawFileRefs = atts.map(a => ({ name: a.name, size: a.size, icon: a.mime_type === "text/x-terminal" ? "terminal" : a.mime_type === "text/x-directory" ? "folder" : "paperclip", path: a.path, mime_type: a.mime_type }));
      }
    }
    if (!rawFileRefs) {
      const sid = sessionId;
      // Compact markers ride along as user-role messages but are not real
      // user turns -- exclude them so the per-user-message storage index
      // stays aligned with the backend's own user-message count.
      const idx = messages.filter(m => m.role === "user" && m.mode !== "compact_marker").length;
      try {
        const stored = localStorage.getItem(`fr:${sid}:${idx}`);
        if (stored) rawFileRefs = JSON.parse(stored);
      } catch {}
    }

    const message: Message = {
      // Derive a DETERMINISTIC id from the server message id so that
      // repeated restores of the same logical message (e.g. the automation
      // detail re-restores on every live update) keep a stable timeline id.
      // A churning random id would reset the user's expand/collapse state on
      // every re-render. Only fall back to a random id for transient,
      // not-yet-persisted messages (which are force-expanded while running).
      id: (raw as any).id ? `m:${(raw as any).id}` : crypto.randomUUID(),
      role: raw.role === "assistant" ? "assistant" : "user",
      content: cleanContent,
      isStreaming: false,
      toolCalls,
      segments,
      timestamp: (raw as any).created_at || Date.now(),
      thinking: (raw as any).reasoning_content,
      tokenUsage: tu,
      serverId: (raw as any).id,
      mode: msgMode,
      taskIndex: (raw as any).task_index,
      taskName: (raw as any).task_name,
      taskStatus: (raw as any).task_status,
      fileRefs: rawFileRefs,
      errorMessage: (raw as any).errorMessage,
      errorCode: (raw as any).errorCode,
      interruptedReason: (raw as any).interruptedReason,
      turnStatusText: (raw as any).turnStatusText,
      cancelledText: (raw as any).cancelledText,
      hasError: !!(raw as any).hasError,
      errorCategory: (raw as any).errorCategory,
    };
    messages.push(message);
    if (message.role === "assistant" && toolCalls.length > 0) {
      indexToolCalls(toolCallsById, toolCalls);
      latestAssistantWithToolCalls = message;
    }
  }
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  
  // Preserve previously-streamed sub-agent messages from the old snapshot.
  // When the backend serialises a running session the tool messages may not
  // carry ``sub_agent_messages`` (they are only persisted on ``tool_result``
  // delivery), so a fresh ``loadSessionMessages`` would otherwise lose every
  // sub-agent message already delivered through ``tool_progress``.
  const oldSubAgentData = new Map<string, { messages?: Message[]; sessionId?: string }>();
  for (const msg of snapshot.messages) {
    for (const tc of msg.toolCalls) {
      if (tc.subAgentMessages && tc.subAgentMessages.length > 0) {
        const old = {
          messages: tc.subAgentMessages,
          sessionId: tc.subAgentSessionId,
        };
        oldSubAgentData.set(tc.id, old);
        if (tc.backendId) oldSubAgentData.set(tc.backendId, old);
        console.log("[loadSessionMessages] Saved old subAgentMessages for tc.id:", tc.id, "backendId:", tc.backendId, "count:", tc.subAgentMessages.length);
      }
    }
  }
  console.log("[loadSessionMessages] Total old subAgentData saved:", oldSubAgentData.size);

  const hadTokenUsage = snapshot.tokenUsage;

  // A running session can occasionally return a partial assistant snapshot
  // while sibling agent calls are still executing. Keep those live cards in
  // the matching assistant message until the backend sends their result.
  if (snapshot.running) {
    const incomingToolCallIds = new Set<string>();
    for (const message of messages) {
      for (const toolCall of message.toolCalls) {
        incomingToolCallIds.add(toolCall.id);
        if (toolCall.backendId) incomingToolCallIds.add(toolCall.backendId);
      }
    }

    for (const oldMessage of snapshot.messages) {
      if (oldMessage.role !== "assistant" || !oldMessage.serverId) continue;
      const restoredMessage = messages.find(
        (message) => message.role === "assistant" && message.serverId === oldMessage.serverId,
      );
      if (!restoredMessage) continue;

      for (const oldToolCall of oldMessage.toolCalls) {
        const isMissing = !incomingToolCallIds.has(oldToolCall.id)
          && (!oldToolCall.backendId || !incomingToolCallIds.has(oldToolCall.backendId));
        if (oldToolCall.name !== "agent" || oldToolCall.result !== undefined || !isMissing) continue;

        restoredMessage.toolCalls.push({ ...oldToolCall });
        restoredMessage.segments.push({ kind: "tool", toolId: oldToolCall.id });
        incomingToolCallIds.add(oldToolCall.id);
        if (oldToolCall.backendId) incomingToolCallIds.add(oldToolCall.backendId);
        console.warn("[loadSessionMessages] Restored missing live agent tool call", {
          id: oldToolCall.id,
          backendId: oldToolCall.backendId,
          assistantId: oldMessage.serverId,
        });
      }
    }
  }

  // Preserve the in-progress streaming assistant turn across a resume/reload of
  // a *running* session. The backend only serialises *committed* messages: the
  // currently-streaming assistant (partial text/thinking and any tool calls that
  // have not completed yet) is absent from ``rawMessages``, so a wholesale
  // replacement would wipe everything the user saw before switching sessions or
  // reconnecting. Re-attach that message to its turn — or fold the still-pending
  // parts into the last committed assistant — so the resuming stream continues
  // to render into it and the "Encre Agent" header / partial content survives.
  const oldStreamingAssistants = snapshot.messages.filter(m => m.role === "assistant" && m.isStreaming);
  if (oldStreamingAssistants.length > 0) {
    for (const old of oldStreamingAssistants) {
      const oldIdx = snapshot.messages.indexOf(old);
      let prevUser: Message | undefined;
      for (let i = oldIdx - 1; i >= 0; i--) {
        if (snapshot.messages[i].role === "user") { prevUser = snapshot.messages[i]; break; }
      }
      const anchorId = prevUser ? (prevUser.serverId ? `m:${prevUser.serverId}` : prevUser.id) : undefined;
      const anchorIdx = anchorId ? messages.findIndex(m => m.role === "user" && m.id === anchorId) : -1;
      const followUp = anchorIdx >= 0 ? messages.slice(anchorIdx + 1) : [];
      const committed = [...followUp].reverse().find(m => m.role === "assistant");
      if (committed) {
        // The turn's text block was committed server-side. Fold in only what
        // has not been persisted yet: uncommitted thinking, an uncommitted text
        // tail, and any tool call that is still running.
        if (old.thinking && !committed.thinking) {
          committed.thinking = old.thinking;
          committed.segments.unshift({ kind: "thinking", text: old.thinking });
        }
        if (old.content
          && old.content !== committed.content
          && old.content.length > committed.content.length
          && old.content.startsWith(committed.content)) {
          const delta = old.content.slice(committed.content.length);
          committed.content = old.content;
          const lastTextSeg = [...committed.segments].reverse().find(s => s.kind === "text");
          if (lastTextSeg) lastTextSeg.text = (lastTextSeg.text ?? "") + delta;
          else committed.segments.push({ kind: "text", text: delta });
        }
        for (const oldTc of old.toolCalls) {
          const exists = committed.toolCalls.some(t =>
            toolCallMatchesId(t, oldTc.id)
            || (oldTc.backendId ? t.id === oldTc.backendId : false)
            || t.id === oldTc.id,
          );
          if (!exists && oldTc.result === undefined) {
            committed.toolCalls.push({ ...oldTc });
            committed.segments.push({ kind: "tool", toolId: oldTc.id });
          }
        }
        if (snapshot.running) committed.isStreaming = true;
      } else if (snapshot.running) {
        // Turn still uncommitted server-side: re-attach the preserved message
        // so the resuming deltas continue into it instead of creating a blank
        // assistant (which loses the ai_header and previously-streamed content).
        if (anchorIdx >= 0) {
          messages.splice(anchorIdx + 1, 0, old);
        } else if (messages.length > 0 && messages[messages.length - 1].role === "user") {
          messages.push(old);
        }
      }
    }
  }

  snapshot.messages = messages;

  // Re-apply sub-agent messages from the old snapshot to the new tool calls.
  // A running session can return a partial parallel snapshot (for example,
  // only the task that most recently emitted progress), so merge by task
  // index instead of replacing a complete in-memory snapshot.
  for (const msg of snapshot.messages) {
    for (const tc of msg.toolCalls) {
      const old = oldSubAgentData.get(tc.id) || (tc.backendId ? oldSubAgentData.get(tc.backendId) : undefined);
      if (old) {
        tc.subAgentMessages = mergeSubAgentMessages(old.messages, tc.subAgentMessages ?? []);
        tc.subAgentSessionId = tc.subAgentSessionId || old.sessionId;
        console.log("[loadSessionMessages] Merged subAgentMessages for tc.id:", tc.id, "count:", tc.subAgentMessages?.length ?? 0);
      }
      // Tool calls have no explicit in-progress record in persisted history.
      // A running session plus no tool result means this call is still active.
      if (snapshot.running && tc.result === undefined) tc.status = "running";
    }
  }
  snapshot.artifacts = [];
  snapshot.compactEvents = [];
  snapshot.compactStartingEvents = [];
  if (totalInput > 0 || totalOutput > 0) {
    snapshot.tokenUsage = { input_tokens: totalInput, output_tokens: totalOutput, total_tokens: totalInput + totalOutput };
    state.contextTokens = totalInput + totalOutput;
  } else if (hadTokenUsage) {
    // preserve existing token usage if messages don't carry per-message data
    state.contextTokens = hadTokenUsage.total_tokens;
  } else if (messages.length > 0) {
    // fallback: estimate token count from content length
    const estimated = Math.round(messages.reduce((sum, m) => sum + m.content.length, 0) / 4);
    snapshot.tokenUsage = { input_tokens: estimated, output_tokens: 0, total_tokens: estimated };
    state.contextTokens = estimated;
  } else {
    snapshot.tokenUsage = null;
  }
  syncSessionState(sessionId);
  emit();
}

/** Remembers which user message should receive a pending rollback edit. */
export function rememberRollbackEditTarget(message: Message, userIdx: number): void {
  pendingRollbackEdit = {
    serverId: message.serverId,
    userIdx,
    content: message.content,
  };
}

/** Applies the pending rollback edit onto a raw message list, if any. */
export function applyPendingRollbackEdit<T extends { role?: string; content?: string | Array<{ type: string; text: string }>; id?: string }>(rawMessages: T[]): T[] {
  if (!pendingRollbackEdit) return rawMessages;

  let userOrdinal = -1;
  let cutIdx = -1;
  for (let i = 0; i < rawMessages.length; i++) {
    const raw = rawMessages[i];
    if (raw.role !== "user") continue;
    userOrdinal++;

    const rawId = raw.id || "";
    const idMatches = pendingRollbackEdit.serverId
      ? rawId === pendingRollbackEdit.serverId || rawId.endsWith(`:M:${pendingRollbackEdit.serverId}`)
      : false;
    const contentMatches = userOrdinal === pendingRollbackEdit.userIdx
      && extractMessageText(raw.content || "") === pendingRollbackEdit.content;

    if (idMatches || contentMatches) {
      cutIdx = i;
      break;
    }
  }

  if (cutIdx < 0) return rawMessages;
  pendingRollbackEdit = null;
  return rawMessages.slice(0, cutIdx);
}

/** Clears all messages for the (active) session. */
export function clearMessages(sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.messages = [];
  snapshot.tokenUsage = null;
  snapshot.telemetry = null;
  snapshot.planItems = [];
  snapshot.artifacts = [];
  snapshot.references = [];
  snapshot.compactEvents = [];
  snapshot.compactStartingEvents = [];
  snapshot.branches = [];
  snapshot.activeBranchId = "";
  snapshot.running = false;
  snapshot.sessionState = "idle";
  if (getSessionKey(sessionId) === getSessionKey(state.sessionId)) {
    state.activeToolId = null;
    state.telemetry = null;
    state.subAgentView = null;
    state.subAgentBreadcrumb = [];
  }
  syncSessionState(sessionId);
  emit();
}

// ── Thinking duration tracking ──────────────────────────────────────

let _thinkingStartTime: number | null = null;

/** Marks the assistant as starting a "thinking" (reasoning) phase. */
export function beginThinking(): void {
  if (_thinkingStartTime === null) {
    _thinkingStartTime = Date.now();
  }
}

/** Marks the assistant "thinking" phase as finished. */
export function finishThinking(): void {
  if (_thinkingStartTime !== null) {
    const msg = getLastAssistantMessage();
    if (msg) {
      msg.thinkingElapsed = Math.round((Date.now() - _thinkingStartTime) / 1000);
    }
    _thinkingStartTime = null;
  }
}

/** Adds a user message (optionally with file references) and returns it. */
export function addUserMessage(content: string, mode?: string, fileRefs?: { name: string; size: number; icon: string; path?: string; mime_type?: string }[]): Message {
  const msg: Message = {
    id: crypto.randomUUID(),
    role: "user",
    content,
    isStreaming: false,
    toolCalls: [],
    segments: [],
    timestamp: Date.now(),
    mode,
    fileRefs,
  };
  const sid = state.sessionId;
  addMessage(msg, sid);
  if (sid && fileRefs && fileRefs.length > 0) {
    const snapshot = getOrCreateSessionSnapshot(sid);
    const idx = snapshot.messages.filter(m => m.role === "user").length - 1;
    try { localStorage.setItem(`fr:${sid}:${idx}`, JSON.stringify(fileRefs)); } catch {}
  }
  return msg;
}

/** Starts a new (empty) assistant message and returns it. */
export function startAssistantMessage(sessionId = state.sessionId): Message {
  const msg: Message = {
    id: crypto.randomUUID(),
    role: "assistant",
    content: "",
    isStreaming: true,
    toolCalls: [],
    segments: [],
    timestamp: Date.now(),
  };
  addMessage(msg, sessionId);
  return msg;
}

/**
 * Record a segment in the current assistant message's timeline, preserving the
 * order of thinking / text / tool call events as they arrive from the server.
 * Skips duplicate consecutive segments of the same kind.
 */
/** Records a timeline segment (text/thinking/tool) for the active message. */
export function recordSegment(kind: TimelineSegment["kind"], toolId?: string, sessionId = state.sessionId): void {
  const msg = getLastAssistantMessage(sessionId);
  if (!msg) return;
  // Check entire segment list for existing tool segment with same toolId.
  // This prevents duplicates when tool_call_delta arrives before tool_call_start
  // and a text/thinking delta interleaves between the two recordSegment calls.
  if (kind === "tool" && toolId) {
    if (msg.segments.some(s => s.kind === "tool" && s.toolId === toolId)) return;
  }
  // Skip duplicate continuous deltas of same kind for non-tool segments.
  const last = msg.segments[msg.segments.length - 1];
  if (last && last.kind === kind && kind !== "tool") return;
  msg.segments.push({ kind, toolId });
  syncSessionState(sessionId);
  emit();
}

/** Returns the last assistant message in the (active) session, if any. */
export function getLastAssistantMessage(sessionId = state.sessionId): Message | undefined {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  for (let i = snapshot.messages.length - 1; i >= 0; i--) {
    if (snapshot.messages[i].role === "assistant") {
      return snapshot.messages[i];
    }
  }
  return undefined;
}

/** Appends raw content to the last assistant message. */
export function appendContent(content: string): void {
  if (!content) return;
  finishThinking();
  const msg = getLastAssistantMessage();
  if (msg) {
    msg.content += content;
    syncSessionState();
    emit();
  }
}

/**
 * Atomic append of a text delta — records the "text" segment and appends
 * content in a single emit(). Use this from stream.ts for text_delta events.
 */
/** Appends a text delta to the last assistant message. */
export function appendTextDelta(text: string, sessionId = state.sessionId): void {
  if (!text) return;
  // Defense-in-depth: never write a token to a different session's snapshot
  // than the one the event claims to belong to.  Prevents cross-session
  // contamination if the outer stream filter is bypassed.
  if (sessionId && state.sessionId && sessionId !== state.sessionId) return;
  finishThinking();
  const msg = getLastAssistantMessage(sessionId);
  if (!msg) return;
  const last = msg.segments[msg.segments.length - 1];
  if (!last || last.kind !== "text") {
    msg.segments.push({ kind: "text", text: text });
  } else {
    last.text = (last.text || "") + text;
  }
  msg.content += text;
  syncSessionState(sessionId);
  emit();
}

/**
 * Atomic append of a thinking delta — records the "thinking" segment and
 * appends content in a single emit(). Use this from stream.ts for thinking_delta events.
 */
/** Appends a reasoning/thinking delta to the last assistant message. */
export function appendThinkingDelta(text: string, sessionId = state.sessionId): void {
  if (!text) return;
  if (sessionId && state.sessionId && sessionId !== state.sessionId) return;
  const msg = getLastAssistantMessage(sessionId);
  if (!msg) return;
  if (!msg.thinking) {
    beginThinking();
  }
  const last = msg.segments[msg.segments.length - 1];
  if (!last || last.kind !== "thinking") {
    msg.segments.push({ kind: "thinking", text: text });
  } else {
    last.text = (last.text || "") + text;
  }
  msg.thinking = (msg.thinking ?? "") + text;
  syncSessionState(sessionId);
  emit();
}

/** Appends a complete reasoning/thinking block to the last assistant message. */
export function appendThinking(text: string): void {
  if (!text) return;
  const msg = getLastAssistantMessage();
  if (msg) {
    if (!msg.thinking) {
      beginThinking();
    }
    msg.thinking = (msg.thinking ?? "") + text;
    syncSessionState();
    emit();
  }
}

/** Finalizes the assistant message, recording token usage if provided. */
export function finishAssistantMessage(tokenUsage?: { input_tokens: number; output_tokens: number; total_tokens: number }, sessionId = state.sessionId): void {
  const msg = getLastAssistantMessage(sessionId);
  if (msg) {
    msg.isStreaming = false;
    if (tokenUsage) {
      msg.tokenUsage = tokenUsage;
    }
    // Sweep any tool calls that never received a tool_result (crashed/cancelled)
    // so the UI doesn't sit in "waiting" forever.
    for (const tc of msg.toolCalls) {
      if (tc.status !== "done") {
        tc.status = "done";
        if (tc.result === undefined) {
          tc.result = t("general.noResult");
          tc.isError = true;
        }
      }
    }
    syncSessionState(sessionId);
    emit();
  }
}

/** Adds a tool-call record to the (active) session. */
export function addToolCall(tc: ToolCallState, sessionId = state.sessionId): void {
  if (sessionId && state.sessionId && sessionId !== state.sessionId) return;
  const msg = getLastAssistantMessage(sessionId);
  if (msg) {
    msg.toolCalls.push(tc);
    syncSessionState(sessionId);
    emit();
  }
}

/** Updates an existing tool call by id (status/result/params). */
export function updateToolCall(
  id: string,
  patch: Partial<ToolCallState>,
  sessionId = state.sessionId
): void {
  if (sessionId && state.sessionId && sessionId !== state.sessionId) return;
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  for (const msg of snapshot.messages) {
    const tc = msg.toolCalls.find((t) => toolCallMatchesId(t, id));
    if (tc) {
      Object.assign(tc, patch);
      syncSessionState(sessionId);
      emit();
      return;
    }
  }
}

/** Sets the session state ("idle" | "running" | "awaiting_approval") for the
 *  (active) session, keeping the derived running flag in sync. */
export function setSessionState(state_val: string, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.running = state_val !== "idle";
  snapshot.sessionState = state_val as SessionState;
  syncSessionState(sessionId);
  emit();
  const sid = sessionId;
  if (sid) {
    state.sessionsList = state.sessionsList.map(e =>
      e.session_id === sid ? { ...e, state: state_val as SessionState } : e
    );
    traySessionsCache.normal = traySessionsCache.normal.map(e =>
      e.session_id === sid ? { ...e, state: state_val as SessionState } : e
    );
    traySessionsCache.iwork = traySessionsCache.iwork.map(e =>
      e.session_id === sid ? { ...e, state: state_val as SessionState } : e
    );
    publishTraySessions();
  }
}

/** Sets the currently active (expanded) tool-call id for the detail panel. */
export function setActiveToolId(id: string | null): void {
  update({ activeToolId: id });
}

/** Sets the sub-agent view tool call (currently opened sub-agent panel). */
export function setSubAgentView(tc: import("./types.js").ToolCallState | null): void {
  update({ subAgentView: tc });
}

/** Returns the current sub-agent breadcrumb trail. */
export function getSubAgentBreadcrumb(): Array<{sessionId: string; name: string; toolCallId: string; parentToolCallId: string | null}> {
  return state.subAgentBreadcrumb;
}

/** Pushes a sub-agent breadcrumb entry. */
export function pushSubAgentBreadcrumb(entry: {sessionId: string; name: string; toolCallId: string; parentToolCallId: string | null}): void {
  const MAX_DEPTH = 4;
  // Dedupe: if a crumb with the same toolCallId already exists, replace it
  // rather than appending. This prevents the breadcrumb from accumulating
  // duplicate entries when the user re-opens the same sub-agent.
  const existing_idx = state.subAgentBreadcrumb.findIndex(
    (c) => c.toolCallId === entry.toolCallId
  );
  if (existing_idx >= 0) {
    const next = state.subAgentBreadcrumb.slice();
    next[existing_idx] = entry;
    update({ subAgentBreadcrumb: next });
    return;
  }
  const crumb = [...state.subAgentBreadcrumb, entry].slice(-MAX_DEPTH);
  update({ subAgentBreadcrumb: crumb });
}

/** Pops the top sub-agent breadcrumb entry, or `null` if empty. */
export function popSubAgentBreadcrumb(): {sessionId: string; name: string; toolCallId: string; parentToolCallId: string | null} | null {
  if (state.subAgentBreadcrumb.length === 0) return null;
  const popped = state.subAgentBreadcrumb[state.subAgentBreadcrumb.length - 1];
  update({ subAgentBreadcrumb: state.subAgentBreadcrumb.slice(0, -1) });
  return popped;
}

/** Clears the entire sub-agent breadcrumb trail. */
export function clearSubAgentBreadcrumb(): void {
  update({ subAgentBreadcrumb: [] });
}

/** Resets to a specific breadcrumb index, returning the popped entry. */
export function resetToSubAgentBreadcrumbIndex(index: number): {sessionId: string; name: string; toolCallId: string; parentToolCallId: string | null} | null {
  if (index < 0 || index >= state.subAgentBreadcrumb.length) return null;
  const target = state.subAgentBreadcrumb[index];
  // Truncate to [0..index] -- the user is now "at" stack[index], so the
  // stack above it is discarded. This matches the on-screen order: clicking
  // the N-th crumb should not leave crumbs N+1..end visible.
  update({ subAgentBreadcrumb: state.subAgentBreadcrumb.slice(0, index + 1) });
  return target;
}

/** Replaces the user settings object. */
export function setSettings(settings: Record<string, unknown>): void {
  update({ settings });
}

const SEARCH_FILTER_KEY = "encre-search-filter";
/** Persists the search-filter toggle set and notifies subscribers. */
export function setSearchFilter(f: SearchFilter): void {
  try {
    localStorage.setItem(SEARCH_FILTER_KEY, JSON.stringify(f));
  } catch {
    /* storage may be unavailable; in-memory value still updates */
  }
  update({ searchFilter: f });
}

/**
 * Normalizes a persisted on/off setting to a boolean.
 *
 * Settings are persisted as the strings `"true"`/`"false"` (sent by the
 * settings panel toggles) but may also arrive as real booleans from the
 * backend `config_data` payload.  This helper treats both `"true"` and
 * `true` as enabled so callers don't have to special-case the type.
 */
export function isEnabled(value: unknown): boolean {
  return value === true || value === "true";
}

/** Replaces the tool-permission policies. */
export function setPermissionPolicies(policies: import("./types.js").PermissionPolicies): void {
  update({ permissionPolicies: policies });
}

/** Sets the concrete resolved theme (`dark`/`light`). */
export function setTheme(theme: "dark" | "light"): void {
  update({ theme });
  document.documentElement.setAttribute("data-theme", theme);
}

/** Sets the user's theme preference (`system`/`dark`/`light`). */
export function setThemePreference(pref: "system" | "dark" | "light"): void {
  update({ themePreference: pref });
}

/** Generates a random unique id. */
export function generateId(): string {
  return crypto.randomUUID();
}

/** Finds a tool call by id in the (active) session. */
export function findToolCall(id: string, sessionId = state.sessionId): ToolCallState | null {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  for (const msg of snapshot.messages) {
    for (const tc of msg.toolCalls) {
      if (toolCallMatchesId(tc, id)) return tc;
    }
  }
  return null;
}

/** Sets the telemetry summary data for the (active) session. */
export function setTelemetry(data: TelemetryData, sessionId = state.sessionId): void {
  if (!sessionId) return;
  // Persist to per-session snapshot so the canvas panel survives
  // session switches and refreshes.
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.telemetry = data;
  if (sessionId === state.sessionId) {
    state.telemetry = data;
    emit();
  }
}

/** Sets the usage-stats data (or `null` to clear). */
export function setUsageStats(data: UsageStatsData | null): void {
  update({ usageStats: data });
}

/** Sets the token usage for the (active) session. */
export function setTokenUsage(data: TokenUsage, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  const prev = snapshot.tokenUsage ?? { input_tokens: 0, output_tokens: 0, total_tokens: 0 };
  snapshot.tokenUsage = {
    input_tokens: prev.input_tokens + data.input_tokens,
    output_tokens: prev.output_tokens + data.output_tokens,
    total_tokens: prev.total_tokens + data.total_tokens,
  };
  syncSessionState(sessionId);
  emit();
}

// ── Attachments ──────────────────────────────────────────────────────────

/** Adds attachment records to the composer state. */
export function addAttachments(items: AttachmentMeta[]): void {
  state.attachments.push(...items);
  emit();
}

/** Clears all attachments. */
export function clearAttachments(): void {
  state.attachments = [];
  emit();
}

/** Removes a single attachment by path. */
export function removeAttachment(path: string): void {
  state.attachments = state.attachments.filter((a) => a.path !== path);
  emit();
}

// ── Plan ──────────────────────────────────────────────────────────────────

/** Sets the plan items for the (active) session. */
export function setPlanItems(items: PlanItem[], sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.planItems = items;
  syncSessionState(sessionId);
  emit();
}

/** Toggles plan mode for the (active) session. */
export function setPlanModeActive(active: boolean, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.planModeActive = active;
  if (!active) {
    snapshot.planProposals = [];
  }
  syncSessionState(sessionId);
  emit();
}

/** Adds a plan proposal to the (active) session. */
export function addPlanProposal(proposal: PlanProposal, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.planProposals = [...snapshot.planProposals, proposal];
  syncSessionState(sessionId);
  emit();
}

/** Removes a plan proposal by id. */
export function removePlanProposal(proposalId: string, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.planProposals = snapshot.planProposals.filter((p) => p.proposal_id !== proposalId);
  syncSessionState(sessionId);
  emit();
}

/** Patches a single plan item by id. */
export function updatePlanItem(id: string, patch: Partial<PlanItem>): void {
  const updated = state.planItems.map((item) =>
    item.id === id ? { ...item, ...patch } : item
  );
  setPlanItems(updated);
}

/** Replaces the artifacts list for the (active) session. */
export function setArtifacts(artifacts: import("./types.js").ArtifactItem[], sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.artifacts = artifacts;
  syncSessionState(sessionId);
  emit();
}

/** Append new artifacts, deduplicating by path. Used for streaming artifacts_update events. */
/** Appends artifacts to the (active) session's list. */
export function appendArtifacts(newArtifacts: import("./types.js").ArtifactItem[], sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  const existing = new Set(snapshot.artifacts.map(a => a.path));
  const toAdd = newArtifacts.filter(a => !existing.has(a.path));
  if (toAdd.length > 0) {
    snapshot.artifacts = [...snapshot.artifacts, ...toAdd];
    syncSessionState(sessionId);
    emit();
  }
}

/** Replaces the references list for the (active) session. */
export function setReferences(references: import("./types.js").ReferenceItem[], sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.references = references;
  syncSessionState(sessionId);
  emit();
}

/** Appends references to the (active) session's list. */
export function appendReferences(newRefs: import("./types.js").ReferenceItem[], sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.references = [...(snapshot.references || []), ...newRefs];
  syncSessionState(sessionId);
  emit();
}

/** Records a context-compaction-starting event for the (active) session. */
export function addCompactStartingEvent(sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.compactStartingEvents = [...snapshot.compactStartingEvents, { timestamp: Date.now() }];
  syncSessionState(sessionId);
  emit();
}

/** Clears all compact-starting events. */
export function clearCompactStartingEvents(): void {
  const snapshot = getOrCreateSessionSnapshot();
  snapshot.compactStartingEvents = [];
  syncSessionState();
  emit();
}

/** Records a context-compaction event for the (active) session. */
export function addCompactEvent(evt: import("./types.js").CompactInfo, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.compactEvents = [...snapshot.compactEvents, evt];
  syncSessionState(sessionId);
  emit();
}

/** Sets the agent-state snapshot for the (active) session. */
export function setAgentState(agentState: import("./types.js").AgentStateSnapshot, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.agentState = agentState;
  syncSessionState(sessionId);
  emit();
}

/** Clears all context-compaction events. */
export function clearCompactEvents(): void {
  const snapshot = getOrCreateSessionSnapshot();
  snapshot.compactEvents = [];
  syncSessionState();
  emit();
}

export function addSystemMessage(info: { content: string; kind: string; sessionId: string }): void {
  const sessionId = info.sessionId || state.sessionId;
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.systemMessages = [...(snapshot.systemMessages || []), {
    content: info.content,
    kind: info.kind,
    timestamp: Date.now(),
  }];
  syncSessionState(sessionId);
  emit();
}

// ── Spec / Spec Update ──────────────────────────────────────────────────────

export function updateSpec(spec: any, sessionId?: string): void {
  const sid = sessionId || state.sessionId;
  const snapshot = getOrCreateSessionSnapshot(sid);
  snapshot.spec = spec;
  syncSessionState(sid);
  emit();
}

export function updatePlanReview(data: import("./types.js").PlanReviewData | null, sessionId?: string): void {
  const sid = sessionId || state.sessionId;
  const snapshot = getOrCreateSessionSnapshot(sid);
  snapshot.planReview = data;
  syncSessionState(sid);
  emit();
}

// ── Notifications ─────────────────────────────────────────────────────────

/** Resets per-session chat fields (messages, plans, telemetry, …) for the active session. */
export function resetChat(): void {
  state.sessionId = "";
  state.sessionStore[""] = createEmptySessionSnapshot();
  syncActiveSessionState();
  state.activeToolId = null;
  state.telemetry = null;
  state.inputMode = "";
  state.tempChat = false;
  state.pendingQueueCount = 0;
  state.queuedPrompts = [];
  state.subAgentView = null;
  state.subAgentBreadcrumb = [];
  // Clear automation sub-agent flags so the session bar label resets
  (window as any).__isAutomationView = false;
  emit();
}

/** Adds a notification to the store. */
export function addNotification(item: NotificationItem): void {
  state.notifications.push(item);
  emit();
  scheduleNotificationSave();
}

/** Shows a transient toast for a notification (delegates to the controller). */
export function showToast(
  title: string,
  message: string,
  type: NotificationItem["type"] = "info",
  source?: string,
  media?: NotificationItem["media"]
): void {
  const displayMessage = message ? `${title}: ${message}` : title;
  addNotification({
    id: crypto.randomUUID(),
    type,
    title: displayMessage,
    message,
    source: source || t("general.sourceYim"),
    timestamp: Date.now(),
    read: false,
    media,
  });
}

/** Marks all notifications as read. */
export function markNotificationsRead(): void {
  const updated = state.notifications.map((n) => ({ ...n, read: true }));
  update({ notifications: updated });
  scheduleNotificationSave();
}

/**
 * Marks a single notification (by id) as read.
 * No-op when the id is unknown or the item is already read, so callers can
 * invoke it unconditionally (e.g. on open) without emitting/persisting churn.
 */
export function markOneNotificationRead(id: string): void {
  const target = state.notifications.find((n) => n.id === id);
  if (!target || target.read) return;
  const updated = state.notifications.map((n) =>
    n.id === id ? { ...n, read: true } : n
  );
  update({ notifications: updated });
  scheduleNotificationSave();
}

/** Dismisses (removes) a notification by id. */
export function dismissNotification(id: string): void {
  update({ notifications: state.notifications.filter((n) => n.id !== id) });
  scheduleNotificationSave();
}

/** Clears all notifications. */
export function clearAllNotifications(): void {
  update({ notifications: [] });
  scheduleNotificationSave();
}

/** Returns the number of unread notifications. */
export function getUnreadCount(): number {
  return state.notifications.filter((n) => !n.read).length;
}

// ── Notification Persistence ───────────────────────────────────────────────

let _notificationsFilePath: string | null = null;
let _persistReady = false;
let _persistDirty = false;

function scheduleNotificationSave(): void {
  _persistDirty = true;
  if (!_persistReady) return;
  debouncePersist();
}

let _saveTimer: ReturnType<typeof setTimeout> | null = null;
function debouncePersist(): void {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => persistNotifications(), 500);
}

async function persistNotifications(): Promise<void> {
  if (!_persistDirty || !_notificationsFilePath) return;
  _persistDirty = false;
  try {
    const { encrypt, isReady } = await import("./crypto.js");
    if (!isReady()) return; // crypto not ready — keep dirty flag to retry later
    const json = JSON.stringify(state.notifications);
    const data = await encrypt(json);
    await window.electronAPI?.writeFile(_notificationsFilePath, data);
  } catch {
    _persistDirty = true; // retry next time
  }
}

/** Loads persisted notifications from storage and seeds the store. */
export async function initNotificationPersistence(onLoaded?: () => void): Promise<void> {
  try {
    const appPath = await window.electronAPI?.getAppPath();
    if (appPath) {
      _notificationsFilePath = appPath + "/notifications.enc";
      const raw = await window.electronAPI?.readFile(_notificationsFilePath);
      if (raw) {
        const { decrypt } = await import("./crypto.js");
        const json = await decrypt(raw.content);
        const items = JSON.parse(json);
        if (Array.isArray(items)) {
          state.notifications = items;
          emit();
          onLoaded?.();
        }
      }
    }
  } catch {
    // Corrupted file or first run — start fresh
  } finally {
    _persistReady = true;
    if (_persistDirty && _notificationsFilePath) debouncePersist();
  }
}

// ── Models ──────────────────────────────────────────────────────────────────

/** Sets the list of available model ids. */
export function setAvailableModels(models: string[]): void {
  update({ availableModels: models });
}

/** Sets the current composer input mode. */
export function setInputMode(mode: string): void {
  update({ inputMode: mode });
}

/** Restore a mode chip into the prompt input and sync state. */
/** Restores the input-mode chip UI for a given mode. */
export function restoreInputModeChip(mode: string): void {
  setInputMode(mode);
  const input = document.getElementById("prompt-input") as HTMLElement | null;
  if (!input) return;

  const old = input.querySelector(".mode-chip");
  if (old) old.remove();

  const cmd = findSlashCommand(mode);
  const label = cmd ? cmd.title : mode;
  const icon = cmd ? cmd.icon : "list-checks";

  const chip = document.createElement("span");
  chip.contentEditable = "false";
  chip.className = "mode-chip";
  chip.setAttribute("data-mode", mode);
  chip.innerHTML = `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i><span>${label}</span>`;
  input.insertBefore(chip, input.firstChild);

  if ((window as any).lucide) {
    (window as any).lucide.createIcons();
  }

  // Sync toolbar mode chip and input area styling
  const area = document.getElementById("input-area");
  if (area) area.setAttribute("data-input-mode", mode);
  const tbChip = document.getElementById("toolbar-mode-chip");
  if (tbChip) {
    tbChip.classList.remove("hidden");
    tbChip.setAttribute("data-mode", mode);
    const tbIcon = tbChip.querySelector(".chip-icon") as HTMLElement;
    const tbLabel = tbChip.querySelector(".toolbar-mode-label") as HTMLElement;
    if (tbIcon) tbIcon.setAttribute("data-lucide", icon);
    if (tbLabel) tbLabel.textContent = label;
    if ((window as any).lucide) {
      (window as any).lucide.createIcons();
    }
  }
}

/** Restore a sticky command chip into the prompt input after a rollback.
 *
 *  Mirrors the inline chip produced by the App's insertCommandChip path so a
 *  rolled-back ``<command>name</command>`` message re-appears as a rendered
 *  chip (not raw text).  Also mirrors the App's ``_activeCommand`` slot so
 *  the toolbar command chip and input-area styling stay in sync — the same
 *  path the ``command_changed`` event uses. */
export function restoreInputCommandChip(name: string): void {
  const input = document.getElementById("prompt-input") as HTMLElement | null;
  if (!input) return;

  const old = input.querySelector(".command-chip");
  if (old) old.remove();

  const cmd = findSlashCommand(name);
  const label = cmd ? cmd.title : name;
  const icon = cmd ? cmd.icon : "wand-2";

  const chip = document.createElement("span");
  chip.contentEditable = "false";
  chip.className = "command-chip";
  chip.setAttribute("data-command", name);
  chip.innerHTML = `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i><span>${label}</span>`;
  input.insertBefore(chip, input.firstChild);

  if ((window as any).lucide) {
    (window as any).lucide.createIcons();
  }

  // Mirror into the App instance (same shape as command_changed handling).
  const app = (window as any).__app;
  if (app) {
    app._activeCommand = { name, icon: cmd?.icon, title: cmd?.title };
    app._currentChipMode = name;
    app.updateChipState?.();
  }
}

/** Global session cache for search. Unlike `sessionsList` it is never cleared
 *  on mode switches, so session search stays global across all modes.
 *  `mode` "merge" (default) unions with the existing cache (mode-scoped
 *  updates must not drop sessions from other modes); "replace" overwrites
 *  with an authoritative full listing (e.g. the tray `sessions_all` event). */
export function setAllSessions(
  sessions: import("./types.js").SessionEntryData[],
  mode: "merge" | "replace" = "merge",
): void {
  // Filter out temp chat sessions from the global cache.
  const filtered = sessions.filter(
    (s) =>
      !(s.metadata as Record<string, unknown>)?.temp_chat &&
      !((s as unknown as Record<string, unknown>).temp_chat as boolean | undefined) &&
      !isTempChatSession(s.session_id),
  );
  if (mode === "replace") {
    update({ allSessions: filtered });
    return;
  }
  const existing = state.allSessions || [];
  const seen = new Set<string>();
  const merged = [...filtered, ...existing].filter((s) => {
    if (!s || !s.session_id || seen.has(s.session_id)) return false;
    seen.add(s.session_id);
    return true;
  });
  update({ allSessions: merged });
}

/** Replaces the sidebar session list. */
export function setSessionsList(sessions: import("./types.js").SessionEntryData[]): void {
  const visible = dedupeSessions(sessions).filter((session) => {
    // Temp chat sessions are ephemeral and must never appear in the sidebar.
    if ((session.metadata as Record<string, unknown>)?.temp_chat) return false;
    if (isTempChatSession(session.session_id)) return false;
    // External-channel sessions (iclaw, telegram, ...) never belong in the
    // normal/iwork sidebar lists.
    if (isExternalChannelSession(session.session_id)) return false;
    const ch = (session as { channel?: string }).channel || "normal";
    if (ch !== "normal" && ch !== "iwork") return false;
    return (session.message_count || 0) > 0;
  });
  update({ sessionsList: visible });
  // Keep the global search cache in sync (never cleared by mode switches).
  setAllSessions(sessions);
  // Note: do NOT update traySessionsCache here — it only has one mode's
  // data and would corrupt the dual-mode cache.  The tray cache is
  // maintained by setTraySessions() which is called from the
  // "sessions_all" response and updates both normal + iwork at once.
}

/** Replaces the archived session list (workspace manager archive view). */
export function setArchivedSessions(sessions: import("./types.js").SessionEntryData[]): void {
  update({ archivedSessions: sessions || [] });
}

/** Optimistically remove an archived session from the archive view (e.g. on
 *  delete or after the backend confirms an unarchive). */
export function removeArchivedSessionById(sessionId: string): void {
  const sessions = state.archivedSessions.filter((s) => s.session_id !== sessionId);
  update({ archivedSessions: sessions });
}

// Dual-channel session cache for the tray popup (normal + iwork).
const traySessionsCache: { normal: any[]; iwork: any[] } = { normal: [], iwork: [] };

export function setTraySessions(normal: any[], iwork: any[]): void {
  traySessionsCache.normal = normal;
  traySessionsCache.iwork = iwork;
  publishTraySessions();
}

/** Updates a single session's `state` field in the dual-mode tray cache
 *  (normal + iwork) and republishes to the tray popup.  Called from
 *  `_syncSessionEntry` during streaming so the tray's status lights stay
 *  in sync with the sidebar. */
export function syncTraySessionState(sessionId: string, newState: SessionState): void {
  if (!sessionId) return;
  let changed = false;
  traySessionsCache.normal = traySessionsCache.normal.map(e => {
    if (e.session_id === sessionId && e.state !== newState) { changed = true; return { ...e, state: newState }; }
    return e;
  });
  traySessionsCache.iwork = traySessionsCache.iwork.map(e => {
    if (e.session_id === sessionId && e.state !== newState) { changed = true; return { ...e, state: newState }; }
    return e;
  });
  if (changed) publishTraySessions();
}

export function getTraySessions(): { normal: any[]; iwork: any[] } {
  return traySessionsCache;
}

function publishTraySessions(): void {
  const projected = buildTraySessionData(
    traySessionsCache.normal,
    traySessionsCache.iwork,
    state.workspaces,
  );
  window.electronAPI?.traySessionsBothUpdate?.(projected as any);
}

/** Removes a session from the sidebar list by id. */
export function removeSessionById(sessionId: string): void {
  const sessions = state.sessionsList.filter((s) => s.session_id !== sessionId);
  const history = state.automationHistory.filter((h: any) => h.session_id !== sessionId);
  update({ sessionsList: sessions, automationHistory: history });
}

export function setAutomationHistory(history: any[]): void {
  update({ automationHistory: history });
}

/** Replaces the configured automation jobs list (unified push). */
export function setAutomationJobs(jobs: any[]): void {
  update({ automationJobs: jobs || [] });
}

/** Monotonic sequence guard for backend search responses (see applySearchResults). */
let _searchResultsSeq = -1;

/** Sequence of the newest backend search results applied to state. */
export function getAppliedSearchSeq(): number {
  return _searchResultsSeq;
}

/** Clear backend search results (also resets the staleness guard). */
export function setSearchResults(results: import("./types.js").SearchResultEntry[]): void {
  _searchResultsSeq = -1;
  update({ searchResults: results });
}

/**
 * Apply backend search results, dropping stale (out-of-order) responses:
 * the client increments `ClientSearch.seq` on every keystroke and the server
 * echoes it back; only the newest sequence is applied.
 */
export function applySearchResults(seq: number, results: import("./types.js").SearchResultEntry[]): void {
  if (seq < _searchResultsSeq) return;
  _searchResultsSeq = seq;
  update({ searchResults: results });
}

export function setModelConfigs(models: import("./types.js").ModelConfigMeta[], activeIndex: number): void {
  update({ modelConfigs: models, activeModelIndex: activeIndex });
}

export function setSkillsList(skills: import("./types.js").SkillInfo[]): void {
  update({ skillsList: skills });
}

export function setEnabledSkills(skills: string[]): void {
  update({ enabledSkills: skills });
}

export function setMcpServers(servers: import("./types.js").MCPServerConfig[]): void {
  update({ mcpServers: servers });
}

export function setCustomCommands(commands: import("./types.js").CustomCommand[]): void {
  update({ customCommands: commands });
}

export function setPendingQueueCount(n: number): void {
  update({ pendingQueueCount: n });
}

export function clearPendingQueueCount(): void {
  update({ pendingQueueCount: 0, queuedPrompts: [] });
}

export function pushQueuedPrompt(text: string, mode?: string): void {
  state.queuedPrompts.push({ text, mode });
  state.pendingQueueCount = state.queuedPrompts.length;
  emit();
}

/** Shifts the next queued prompt off the queue and returns it. */
export function shiftQueuedPrompt(): void {
  state.queuedPrompts.shift();
  emit();
}

/** Clears all queued prompts. */
export function clearQueuedPrompts(): void {
  state.queuedPrompts = [];
  emit();
}

/** Removes a queued prompt at the given index. */
export function removeQueuedPromptAt(index: number): void {
  state.queuedPrompts.splice(index, 1);
  emit();
}

/** Moves a queued prompt from one index to another (for reordering). */
export function moveQueuedPrompt(fromIndex: number, toIndex: number): void {
  if (fromIndex === toIndex) return;
  const [moved] = state.queuedPrompts.splice(fromIndex, 1);
  state.queuedPrompts.splice(toIndex, 0, moved);
  emit();
}

/** Removes the last (assistant) message from the (active) session. */
export function removeLastMessage(): void {
  const snapshot = getOrCreateSessionSnapshot();
  if (snapshot.messages.length > 0) {
    snapshot.messages.pop();
    syncSessionState();
    emit();
  }
}

/** Truncates the conversation back to a given user message; returns success. */
export function truncateToUserMessage(userIdx: number): boolean {
  const snapshot = getOrCreateSessionSnapshot();
  let ui = 0;
  let cutIdx = -1;
  for (let i = 0; i < snapshot.messages.length; i++) {
    if (snapshot.messages[i].role === "user") {
      if (ui === userIdx) {
        cutIdx = i;
        break;
      }
      ui++;
    }
  }
  if (cutIdx >= 0) {
    snapshot.messages = snapshot.messages.slice(0, cutIdx);
    syncSessionState();
    emit();
    return true;
  }
  return false;
}

/** Sets the configured sub-agents list. */
export function setSubAgents(agents: import("./types.js").SubAgentConfig[]): void {
  update({ subAgents: agents });
}

/** Sets the agent configuration object. */
export function setAgentConfig(config: {
  system_prompt: string;
  specialty: string;
  permission_mode: string;
  max_turns: number;
}): void {
  update({ agentConfig: config });
}

// ── Workspace ───────────────────────────────────────────────────────────────

/** Replaces the workspace list. */
export function setWorkspaces(workspaces: import("./types.js").WorkspaceEntry[]): void {
  // Filter out invalid entries
  const valid = workspaces.filter((w) => w.path && w.name);
  // Sort alphabetically (A→Z) so every list in the UI stays in a stable,
  // predictable order regardless of the backend's arrival order.
  valid.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true }));
  // Clear orphaned activeWorkspace if no longer in list
  if (state.activeWorkspace && !valid.some((w) => w.path === state.activeWorkspace)) {
    setActiveWorkspace("");
  }
  update({ workspaces: valid });
  publishTraySessions();
}

/** Sets the active workspace path. */
export function setActiveWorkspace(path: string): void {
  update({ activeWorkspace: path });
}

/** Sets the cached config for a workspace (keyed by its path). */
export function setWorkspaceConfig(path: string, config: import("./types.js").WorkspaceConfig): void {
  update({ workspaceConfigs: { ...state.workspaceConfigs, [path]: config } });
}

/** Sets the context-file availability map for a workspace (keyed by its path). */
export function setWorkspaceFiles(path: string, files: Record<string, boolean>): void {
  update({ workspaceFiles: { ...state.workspaceFiles, [path]: files } });
}/** Sets the workspace mode (`iwork`/`normal`). */
export function setWorkspaceMode(mode: "iwork" | "normal"): void {
  update({ workspaceMode: mode });
}

/** Sets the document index status. */
export function setIndexStatus(status: "idle" | "ready" | "indexing" | "error" | "no_workspace"): void {
  update({ indexStatus: status });
}

/** Sets the indexed file count. */
export function setIndexFiles(files: number): void {
  update({ indexFiles: files });
}

/** Sets the index progress (0-100). */
export function setIndexProgress(progress: number): void {
  update({ indexProgress: progress });
}

/** Updates context-token usage for the (active) session. */
export function updateContextUsage(tokens: number, window: number, sessionId = state.sessionId): void {
  if (!sessionId) return;
  // Ignore events from non-active sessions so background sessions cannot
  // overwrite the canvas panel currently shown in the sidebar.
  if (sessionId !== state.sessionId) return;
  state.contextTokens = tokens;
  state.contextWindow = window;
  emit();
}

/** Updates a workspace's index status/files/progress. */
export function updateWorkspaceIndex(wsId: string, status: string, files: number, progress?: number): void {
  if (!wsId) return;
  state.workspaces = state.workspaces.map(w =>
    w.id === wsId ? { ...w, index_status: status, index_files: files, index_progress: progress ?? w.index_progress } : w
  );
  // Also update global for session-inner panel
  state.indexStatus = status as any;
  state.indexFiles = files;
  if (progress !== undefined) state.indexProgress = progress;
  emit();
}

/** Sets the .gitignore content being viewed. */
export function setGitignoreContent(content: string): void {
  update({ gitignoreContent: content });
}

/** Sets the document list (indexed docs). */
export function setDocsList(docs: import("./types.js").DocumentEntry[]): void {
  update({ docsList: docs });
}

/** Sets the gateway (MCP gateway) status. */
export function setGatewayStatus(status: import("./types.js").GatewayStatusData | null): void {
  update({ gatewayStatus: status });
}

/** Sets the tools info (available tool metadata). */
export function setToolsInfo(info: import("./types.js").ToolsInfo): void {
  update({ toolsInfo: info });
}

/** Sets the model catalog. */
export function setModelCatalog(catalog: import("./types.js").ModelCatalog): void {
  update({ modelCatalog: catalog });
}

/** Sets the MCP server catalog. */
export function setMcpCatalog(catalog: import("./types.js").McpCatalog): void {
  update({ mcpCatalog: catalog });
}

/** Sets the memory entries list. */
export function setMemoryList(entries: import("./types.js").MemoryEntry[]): void {
  update({ memoryList: entries });
}

/** Sets the memory detail being viewed. */
export function setMemoryDetail(detail: { path: string; content: string; error?: string } | null): void {
  update({ memoryDetail: detail });
}

/** Sets the global rules list. */
export function setGlobalRulesList(rules: import("./types.js").GlobalRuleEntry[]): void {
  update({ globalRules: rules });
}

/** Sets the project rules list. */
export function setProjectRulesList(rules: import("./types.js").ProjectRuleEntry[]): void {
  update({ projectRules: rules });
}

/** Sets the project hooks list. */
export function setProjectHooksList(hooks: import("./types.js").ProjectHookEntry[]): void {
  update({ projectHooks: hooks });
}

/** Sets the global rule currently being viewed. */
export function setViewingGlobalRule(data: { name: string; content: string; error?: string } | null): void {
  update({ viewingGlobalRule: data });
}

/** Sets the user/profile data. */
export function setProfile(profile: import("./types.js").ProfileData | null): void {
  update({ profile });
}

/** Records the pending permission request awaiting user decision. */
export function setPendingPermission(
  toolName: string | null | { tool: string; reason: string },
): void {
  if (toolName == null) {
    update({ pendingPermission: null });
    return;
  }
  if (typeof toolName === "string") {
    update({ pendingPermission: toolName });
    return;
  }
  // Object form -- { tool, reason }.  The shared state only stores the
  // tool name; the dialog keeps the reason next to its own state.
  update({ pendingPermission: toolName.tool });
}

/** Toggles the temporary-chat flag. */
export function setTempChat(v: boolean): void {
  update({ tempChat: v });
}

/**
 * Session ids that were used as temp chats. Once recorded, the id stays
 * "poisoned" for the lifetime of the renderer: late streaming events or
 * broadcasts for it can never re-append it to the sidebar.
 */
const _tempChatSessionIds = new Set<string>();

/** Record a session id as ephemeral (called on temp-chat exit). */
export function markTempChatSession(sessionId: string): void {
  if (!sessionId) return;
  _tempChatSessionIds.add(sessionId);
  // Drop it from every list immediately.
  removeSessionById(sessionId);
}

/** True when the id belongs to (or belonged to) a temp chat. */
export function isTempChatSession(sessionId: string): boolean {
  return _tempChatSessionIds.has(sessionId);
}

/**
 * Sessions owned by an external channel (iclaw, telegram, qq, ...).
 * They are searchable and can be opened read-only from global search,
 * but must never appear in the normal/iwork sidebar lists.
 */
const _externalChannelSessionIds = new Set<string>();

/** Record a session id as belonging to an external channel. */
export function markExternalChannelSession(sessionId: string): void {
  if (!sessionId) return;
  _externalChannelSessionIds.add(sessionId);
  // Drop it from the sidebar lists immediately (it may have leaked in
  // via a broadcast that predates the channel fix).
  removeSessionById(sessionId);
}

/** True when the session belongs to an external channel (iclaw, ...). */
export function isExternalChannelSession(sessionId: string): boolean {
  return _externalChannelSessionIds.has(sessionId);
}

/** Switches the active branch for the (active) session. */
export function switchBranch(branchId: string, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.activeBranchId = branchId;
  syncSessionState(sessionId);
  emit();
}

/** Updates a branch's metadata/state. */
export function setBranchState(
  activeBranchId: string,
  branches: BranchMeta[],
  messages?: Message[],
  sessionId = state.sessionId
): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.activeBranchId = activeBranchId;
  snapshot.branches = branches;
  if (messages !== undefined) {
    snapshot.messages = messages;
  }
  syncSessionState(sessionId);
  emit();
}

/** Replaces the branch list and active branch id. */
export function setBranches(branches: BranchMeta[], activeBranchId: string, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.branches = branches;
  snapshot.activeBranchId = activeBranchId;
  syncSessionState(sessionId);
  emit();
}

/** Removes the given message ids from a branch. */
export function removeBranchMessages(removedIds: Set<string>, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.messages = snapshot.messages.filter(m => !removedIds.has(m.id));
  syncSessionState(sessionId);
  emit();
}

// ── Workflow state ──────────────────────────────────────────────────────

/** Merges a partial workflow-state update. */
export function setWorkflowState(partial: {
  workflowId: string;
  goal: string;
  totalTasks: number;
  taskIds: string[];
  active: boolean;
  completedCount: number;
  failedCount: number;
  skippedCount: number;
  success?: boolean;
  totalDuration?: number;
}): void {
  const existing = state.workflowState;
  if (!existing || existing.workflowId !== partial.workflowId) {
    // New workflow: create fresh task list
    state.workflowState = {
      workflowId: partial.workflowId,
      goal: partial.goal,
      totalTasks: partial.totalTasks,
      taskIds: partial.taskIds,
      active: partial.active,
      tasks: partial.taskIds.map(id => ({ taskId: id, taskName: "", status: "pending" })),
      completedCount: partial.completedCount,
      failedCount: partial.failedCount,
      skippedCount: partial.skippedCount,
      success: partial.success,
      totalDuration: partial.totalDuration,
    };
  } else {
    // Update existing workflow
    existing.active = partial.active;
    existing.completedCount = partial.completedCount;
    existing.failedCount = partial.failedCount;
    existing.skippedCount = partial.skippedCount;
    if (partial.success !== undefined) existing.success = partial.success;
    if (partial.totalDuration !== undefined) existing.totalDuration = partial.totalDuration;
  }
  emit();
}

/** Updates a single workflow task's info. */
export function updateWorkflowTask(info: {
  workflowId: string;
  taskId: string;
  taskName: string;
  status: string;
}): void {
  const wf = state.workflowState;
  if (!wf || wf.workflowId !== info.workflowId) return;
  const task = wf.tasks.find(t => t.taskId === info.taskId);
  if (task) {
    task.taskName = info.taskName;
    task.status = info.status;
  } else {
    wf.tasks.push({
      taskId: info.taskId,
      taskName: info.taskName,
      status: info.status,
    });
  }
  if (info.status === "completed") wf.completedCount = wf.tasks.filter(t => t.status === "completed").length;
  if (info.status === "failed") wf.failedCount = wf.tasks.filter(t => t.status === "failed").length;
  if (info.status === "skipped") wf.skippedCount = wf.tasks.filter(t => t.status === "skipped").length;
  emit();
}
