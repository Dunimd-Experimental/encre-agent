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

import { AppState, createEmptyState, createEmptySessionSnapshot, Message, ToolCallState, TelemetryData, UsageStatsData, TokenUsage, PlanItem, PlanProposal, NotificationItem, AttachmentMeta, TimelineSegment, BranchMeta, SessionSnapshot, SearchFilter, defaultSearchFilter, SessionState, QueuedPrompt } from "./types.js";
import { t } from "../features/i18n.js";
import { findSlashCommand } from "../ui/slash-commands.js";
import { wireChipClose, removeChipWithSeparator } from "../ui/chip.js";
import { buildTraySessionData, dedupeSessions } from "../chat/session-projection.js";
import { reportUiFault } from "./ui_boundary.js";
import {
  evictScope,
  setSurfaceSession,
  swapSession as swapSurfaceSession,
} from "./scope.js";

type Key = keyof AppState;
type Listener = (changed: ReadonlySet<Key>) => void;
type KeyList = "*" | ReadonlyArray<Key>;

interface Subscription {
  fn: Listener;
  keys: Set<Key> | null;
}

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
const listeners = new Set<Subscription>();
let pendingRollbackEdit: { sid: string; serverId?: string; userIdx: number; content: string; at: number } | null = null;

/** Pending rollback edits are only meaningful for the operation that started
 *  them. Beyond this age the confirming event has not arrived, so applying
 *  the marker would cut whichever user message merely happens to sit at the
 *  remembered ordinal (typically the user's own re-sent message). */
const ROLLBACK_EDIT_TTL_MS = 30_000;

/** Sessions/turns already warned about for a collapsed duplicate user turn.
 *  ``loadSessionMessages`` re-runs on every open/resume, so the warning must
 *  be throttled per dropped turn instead of re-toasting each restore. */
const _duplicateTurnWarned = new Set<string>();

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
  state.retryNotice = snapshot.retryNotice ?? null;
  state.systemMessages = snapshot.systemMessages || [];
  state.branches = snapshot.branches;
  state.activeBranchId = snapshot.activeBranchId;
  state.running = snapshot.running;
  state.agentState = snapshot.agentState ?? null;
  state.planReview = snapshot.planReview;
  // The spec card reads `state.spec` from the timeline; mirroring only
  // planReview left the previous conversation's spec floating over a new one.
  state.spec = snapshot.spec;
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

/** Subscribes a listener to state changes; returns an unsubscribe function.
 *  When `keys` is omitted or `"*"`, the listener fires on every change.
 *  When `keys` is an array of top-level AppState fields, the listener fires
 *  only when at least one of those fields was mutated. */
export function subscribe(fn: Listener, keys?: KeyList): () => void {
  const sub: Subscription = { fn, keys: !keys || keys === "*" ? null : new Set(keys) };
  listeners.add(sub);
  return () => listeners.delete(sub);
}

let _emitPending = false;
const _pendingChanged: Set<Key> = new Set();
let _allChanged = false;

function _scheduleFlush(): void {
  _emitPending = true;
  queueMicrotask(() => {
    _emitPending = false;
    const all = _allChanged;
    _allChanged = false;
    const snapshot = new Set(_pendingChanged);
    _pendingChanged.clear();
    if (!all && snapshot.size === 0) return;
    const started = performance.now();
    for (const sub of listeners) {
      if (all || sub.keys === null) {
        try { sub.fn(snapshot); } catch (e) { reportUiFault("subscriber", "state", e); }
      } else {
        let hit = false;
        for (const k of sub.keys) {
          if (snapshot.has(k)) { hit = true; break; }
        }
        if (hit) {
          try { sub.fn(snapshot); } catch (e) { reportUiFault("subscriber", "state", e); }
        }
      }
    }
    // Attribution for boot/plane long tasks: which state keys woke which
    // subscribers for how long. Synchronous subscriber work is the one part
    // of the pipeline the render scheduler cannot split.
    const elapsed = performance.now() - started;
    if (elapsed >= 30) {
      console.warn(
        `[perf] state flush ${Math.round(elapsed)}ms keys=${all ? "*" : Array.from(snapshot).join(",")}`,
      );
    }
  });
}

let _batchDepth = 0;
let _batchChanged: Set<Key> | null = null;

/** Batches multiple state mutations so subscribers fire exactly once at the
 *  end instead of once per `update()` call. Must wrap synchronous mutation
 *  sequences — any `await` inside defers the flush. */
export function batch<T>(fn: () => T): T {
  _batchDepth++;
  try {
    return fn();
  } finally {
    _batchDepth--;
    if (_batchDepth === 0 && _batchChanged && _batchChanged.size > 0) {
      const changed = _batchChanged;
      _batchChanged = null;
      for (const k of changed) _pendingChanged.add(k);
      if (!_emitPending) _scheduleFlush();
    }
  }
}

function emit(...changedKeys: Key[]): void {
  if (_batchDepth > 0) {
    if (!_batchChanged) _batchChanged = new Set();
    if (changedKeys.length === 0) _allChanged = true;
    else for (const k of changedKeys) _batchChanged.add(k);
    return;
  }
  if (changedKeys.length === 0) {
    _allChanged = true;
  } else {
    for (const k of changedKeys) _pendingChanged.add(k);
  }
  if (!_emitPending) _scheduleFlush();
}

function update(partial: Partial<AppState>): void {
  state = { ...state, ...partial };
  const keys = Object.keys(partial) as Key[];
  emit(...keys);
}

/** Sets the WebSocket connected flag. */
export function setConnected(v: boolean): void {
  update({ connected: v });
}

/** True while the backend still owes session `sid` an answer — a floating
 *  surface of that session may only be resurrected while it is live. */
function isSessionLive(sid: string): boolean {
  const snapshot = state.sessionStore[sid];
  if (!snapshot) return false;
  return snapshot.running || snapshot.sessionState === "awaiting_approval";
}

/** Switches the active session id, resetting scoped fields and syncing its snapshot. */
export function setSessionId(id: string): void {
  if (id !== state.sessionId) {
    const oldSid = state.sessionId;
    setSurfaceSession(id);
    // Hand the floating surfaces over: the outgoing conversation's own popups
    // and confirmations close, and anything the backend is still waiting on is
    // shelved — it returns only while that session is still live. Global
    // surfaces are left exactly where they are.
    swapSurfaceSession(oldSid, id, isSessionLive);
  }
  state.sessionId = id;
  getOrCreateSessionSnapshot(id);
  setSurfaceSession(id);
  // Reset session-scoped global fields so the sidebar canvas does not
  // display stale data from a previously active session. The fields are
  // repopulated by the next context_usage events. Per-session fields
  // (telemetry, tokenUsage, ...) are restored by syncActiveSessionState().
  state.contextTokens = 0;
  state.contextWindow = 0;
  state.contextUsageBreakdown = null;
  // Show the model this conversation used last time while the resume reply
  // is in flight. The backend remains authoritative — `session_ready` lands
  // moments later and overwrites this — but without the local restore the
  // picker briefly shows the PREVIOUS conversation's model.
  const cachedModel = getSessionSnapshot(id).activeModelIndex;
  if (typeof cachedModel === "number") state.activeModelIndex = cachedModel;
  syncActiveSessionState();
  // Per-session model isolation is backend-driven: the `session_ready` reply
  // carries the target session's own `active_model_index`, and stream.ts
  // applies it via setActiveModelIndex(). `modelIndexSessionId` still points
  // at the outgoing session until that reply lands, which keeps the
  // selector's auto-fallback from pushing a guessed model into the session
  // that is being opened.
  emit();
}

/** Applies the backend-authoritative active model for the CURRENT session and
 *  pins it into that session's snapshot.  Called from stream.ts when a
 *  `session_ready` frame arrives — the backend is the single source of truth
 *  for which model a conversation runs on. */
export function setActiveModelIndex(idx: number): void {
  if (idx !== state.activeModelIndex || state.modelIndexSessionId !== state.sessionId) {
    update({ activeModelIndex: idx, modelIndexSessionId: state.sessionId });
  }
  recordSessionModel(idx);
}

/** Marks the current model index as belonging to *sessionId*.
 *
 *  Called whenever a backend frame settles the active model (`session_ready`,
 *  `models_updated`, a config snapshot).  A frame for another session is
 *  ignored: only the conversation being viewed may unlock the picker's
 *  auto-fallback. */
export function markModelIndexSynced(sessionId?: string): void {
  const sid = sessionId || state.sessionId;
  if (!sid || sid !== state.sessionId) return;
  if (state.modelIndexSessionId !== sid) {
    update({ modelIndexSessionId: sid });
  }
}

/** Records which model the CURRENT session uses.  Called wherever the global
 *  active model settles (backend `models_updated` / `config_patch` apply), so
 *  the snapshot always mirrors what this conversation actually ran on —
 *  regardless of whether the change came from the picker, a settings edit or
 *  an auto-fallback. */
export function recordSessionModel(idx: number): void {
  const snapshot = getOrCreateSessionSnapshot(state.sessionId);
  snapshot.activeModelIndex = idx;
}

/**
 * Notice modes ride along as user-role messages but are NOT real user
 * turns: backend compaction boundaries (``compact_marker``) and the
 * persisted UI notices (``compact_notice`` / ``model_switch``).  Every
 * user-message count, duplicate guard and per-turn storage index must
 * exclude them so numbering stays aligned with the backend's own
 * user-message count.
 */
export function isNoticeMode(mode: string | undefined): boolean {
  return mode === "compact_marker" || mode === "compact_notice" || mode === "model_switch";
}

/**
 * Real user turns only. Mirrors exactly how ``chat/timeline.ts`` numbers the
 * timeline: ``data-user-idx`` is assigned only to genuine user bubbles, after
 * the three notice modes are skipped. Any consumer that resolves an ordinal
 * (rollback, delete, retry, truncate) must therefore filter the same way — a
 * bare ``role === "user"`` filter counts the notice records too and drifts by
 * one per notice, which is what made rollback hit the wrong turn.
 */
export function realUserMessages(messages: readonly Message[]): Message[] {
  return messages.filter(m => m.role === "user" && !isNoticeMode(m.mode));
}

/** Appends a message to the (active) session snapshot. */
export function addMessage(msg: Message, sessionId = state.sessionId): void {
  // Defense-in-depth: refuse to write into a different session's snapshot
  // than the one currently active.
  if (sessionId && state.sessionId && sessionId !== state.sessionId) return;
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  // Duplicate guard for optimistic user turns: rollback / delete
  // reconciliation or double event delivery can append the same user
  // message twice -- identical text, adjacent in the timeline, regardless
  // of whether the previous copy already carries a server id. Legitimate
  // repeated sends are separated by an assistant turn.
  if (msg.role === "user" && !isNoticeMode(msg.mode) && !msg.serverId) {
    const prev = snapshot.messages[snapshot.messages.length - 1];
    if (prev && prev.role === "user" && !isNoticeMode(prev.mode)
      && prev.content === msg.content) {
      return;
    }
  }
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

/**
 * Whether a restored tool result is a failure.
 *
 * The backend persists the authoritative flag (`is_error`), so it wins; the
 * text heuristics only cover sessions written before that flag existed.  The
 * prefix list must also cover the recovery engine's terminal markers
 * (`[Exhausted]`, `[Aborted]`, ...), otherwise a plugin fault silently loses
 * its card the moment the session is reloaded.
 */
function restoredToolError(raw: { is_error?: unknown }, text: string): boolean {
  if (typeof raw.is_error === "boolean") return raw.is_error;
  return /^\s*(error|blocked|permission denied|tool execution|\[exhausted\]|\[aborted\]|\[error)/i.test(text || "");
}

function indexToolCalls(toolCallsById: Map<string, ToolCallState>, toolCalls: ToolCallState[]): void {
  for (const toolCall of toolCalls) {
    toolCallsById.set(toolCall.id, toolCall);
    if (toolCall.backendId) toolCallsById.set(toolCall.backendId, toolCall);
  }
}

/** Warn once per unique orphaned tool result id -- a list reload re-runs
 *  restore for every message, so a damaged (historically truncated) session
 *  would otherwise spam the console with the same warning dozens of times. */
const orphanedToolResultWarned = new Set<string>();

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

  // Fall back to position-based pairing when IDs don't match
  // (e.g. stale or mismatched backend ids after compaction).
  const fallback = fallbackAssistant?.toolCalls[fallbackAssistant.toolCalls.length - 1];
  if (fallback) return fallback;

  const warnKey = clientId || toolCallId || "";
  if (warnKey && !orphanedToolResultWarned.has(warnKey)) {
    orphanedToolResultWarned.add(warnKey);
    console.warn("[state] Unable to restore tool result (orphaned -- its assistant tool_call is not in this list)", {
      clientId,
      toolCallId,
    });
  }

  return undefined;
}

/** Reconciles a raw message list into `Message` objects (used on resume). */
export function restoreMessages(rawMessages: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string; segments?: Array<{kind: string; text?: string; tool_id?: string}>; created_at?: number; mode?: string }>): Message[] {
  const messages: Message[] = [];
  const toolCallsById = new Map<string, ToolCallState>();
  let latestAssistantWithToolCalls: Message | undefined;
  for (const raw of rawMessages || []) {
    if (raw.role === "system") continue;
    // Backend-injected retry nudges (empty response, anti-stub,
    // anti-deliberation, grace, ...) arrive as role "user" with
    // is_synthetic=true.  They are loop plumbing, not user input --
    // never render them as chat bubbles.
    if (raw.role === "user" && (raw as any).is_synthetic) continue;
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
    if (raw.role === "compact_notice" || raw.role === "model_switch") {
      // Persisted UI notice spliced into the renderer stream by the
      // backend (recorded like a tool call; survives reload).
      messages.push({
        id: (raw as any).id ? `m:${(raw as any).id}` : crypto.randomUUID(),
        role: "user",
        content: "",
        isStreaming: false,
        toolCalls: [],
        segments: [],
        timestamp: (raw as any).created_at || Date.now(),
        mode: raw.role,
        serverId: (raw as any).id,
        ...(raw.role === "compact_notice"
          ? {
              compactStats: {
                oldTokens: Number((raw as any).old_tokens) || 0,
                newTokens: Number((raw as any).new_tokens) || 0,
                oldCount: Number((raw as any).old_count) || 0,
                newCount: Number((raw as any).new_count) || 0,
              },
            }
          : {
              switchInfo: {
                fromModel: String((raw as any).from_model || ""),
                toModel: String((raw as any).to_model || ""),
                reason: String((raw as any).reason || ""),
                source: String((raw as any).source || "manual"),
              },
            }),
      });
      continue;
    }
    if (raw.role === "tool") {
      const target = findRestoredToolCall(raw as any, toolCallsById, latestAssistantWithToolCalls);
      if (target) {
        const toolText = extractMessageText(raw.content);
        target.result = toolText;
        target.isError = restoredToolError(raw as any, toolText);
        if (target.isError && !target.errorAt) target.errorAt = (raw as any).created_at || Date.now();
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
      // Deterministic id from the server message id (same rule as
      // loadSessionMessages): sub-agent transcripts are re-restored on every
      // progress tick, and a churning random id would change every timeline
      // key -- forcing a full innerHTML rebuild of the WHOLE transcript
      // ~12x/second and resetting expand/collapse state each time.
      id: (raw as any).id ? `m:${(raw as any).id}` : crypto.randomUUID(),
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
export function loadSessionMessages(
  rawMessages: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string }>,
  sessionId = state.sessionId,
  opts?: { userIndexOffset?: number },
): void {
  // Tail-first open: the tail chunk resumes the absolute user-message
  // ordinal where the (incoming) pre-tail backfill chunk will leave off, so
  // per-user-message storage keys (fr:<sid>:<idx>) keep full-list numbering.
  const userIndexOffset = opts?.userIndexOffset ?? 0;
  const messages: Message[] = [];
  const toolCallsById = new Map<string, ToolCallState>();
  const seenUserServerIds = new Set<string>();
  let latestAssistantWithToolCalls: Message | undefined;
  let totalInput = 0;
  let totalOutput = 0;

  for (const raw of rawMessages) {
    if (raw.role === "system") continue;

    // Backend-injected loop plumbing (context updates, retry nudges,
    // grace prompts) arrives as role "user" with is_synthetic=true --
    // never render it as a chat bubble.  Mirrors restoreMessages().
    if (raw.role === "user" && (raw as any).is_synthetic) continue;

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
    if (raw.role === "compact_notice" || raw.role === "model_switch") {
      // Persisted UI notice spliced into the renderer stream by the
      // backend (recorded like a tool call; survives reload).
      messages.push({
        id: (raw as any).id ? `m:${(raw as any).id}` : crypto.randomUUID(),
        role: "user",
        content: "",
        isStreaming: false,
        toolCalls: [],
        segments: [],
        timestamp: (raw as any).created_at || Date.now(),
        mode: raw.role,
        serverId: (raw as any).id,
        ...(raw.role === "compact_notice"
          ? {
              compactStats: {
                oldTokens: Number((raw as any).old_tokens) || 0,
                newTokens: Number((raw as any).new_tokens) || 0,
                oldCount: Number((raw as any).old_count) || 0,
                newCount: Number((raw as any).new_count) || 0,
              },
            }
          : {
              switchInfo: {
                fromModel: String((raw as any).from_model || ""),
                toModel: String((raw as any).to_model || ""),
                reason: String((raw as any).reason || ""),
                source: String((raw as any).source || "manual"),
              },
            }),
      });
      continue;
    }

    if (raw.role === "tool") {
      const target = findRestoredToolCall(raw as any, toolCallsById, latestAssistantWithToolCalls);
      if (target) {
        const toolText = extractMessageText(raw.content);
        target.result = toolText;
        target.isError = restoredToolError(raw as any, toolText);
        if (target.isError && !target.errorAt) target.errorAt = (raw as any).created_at || Date.now();
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
        rawFileRefs = atts.map(a => ({ name: a.name, size: a.size, icon: "paperclip", path: a.path, mime_type: a.mime_type }));
      }
    }
    if (!rawFileRefs) {
      const sid = sessionId;
      // Notice entries ride along as user-role messages but are not real
      // user turns -- exclude them so the per-user-message storage index
      // stays aligned with the backend's own user-message count.
      const idx = userIndexOffset
        + messages.filter(m => m.role === "user" && !isNoticeMode(m.mode)).length;
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
    // Duplicate guard: reconciliation after rollback / delete can deliver
    // the same user turn twice -- with an identical server id, as adjacent
    // uncommitted copies, or as the resurrected pre-rollback message next
    // to its re-sent replacement (adjacent, identical text, different ids).
    // Collapse ALL adjacent identical user turns to the last one. Legitimate
    // repeated sends are separated by an assistant turn and are unaffected.
    if (message.role === "user" && !isNoticeMode(message.mode)) {
      const dupById = !!message.serverId && seenUserServerIds.has(message.serverId);
      const prev = messages[messages.length - 1];
      const dupAdjacent = !!prev && prev.role === "user" && !isNoticeMode(prev.mode)
        && prev.content === message.content;
      if (dupById || dupAdjacent) {
        console.warn("[loadSessionMessages] collapsed duplicate user turn", {
          droppedId: message.serverId || message.id,
          keptId: prev?.serverId || prev?.id,
          content: message.content.slice(0, 60),
        });
        // A dropped duplicate normally means the backend never actually
        // removed the rolled-back turn -- the pre-rollback message came back
        // next to its replacement. Silent de-duplication would make a failed
        // rollback look like a success, so surface it. Throttled per dropped
        // turn: this path also runs on plain reloads.
        const dupKey = `${getSessionKey(sessionId)}|${message.serverId || message.id}`;
        if (!_duplicateTurnWarned.has(dupKey)) {
          _duplicateTurnWarned.add(dupKey);
          if (_duplicateTurnWarned.size > 256) _duplicateTurnWarned.clear();
          showToast(t("chat.duplicateTurnCollapsed"), message.content.slice(0, 60), "warning", "Chat");
        }
        continue;
      }
      if (message.serverId) seenUserServerIds.add(message.serverId);
    }
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
  snapshot.retryNotice = null;
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

/**
 * Prepends the pre-tail history chunk onto an already-opened session.
 *
 * Tail-first opens render the most recent turns immediately; this lands the
 * older range in front of them moments later. The chunk is transformed by the
 * same loader (its tool calls/results are self-contained — the backend never
 * cuts between a call and its result) and spliced in front of the live tail,
 * keeping every streaming/sub-agent state the tail already restored.
 */
export function prependSessionMessages(
  rawMessages: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string }>,
  sessionId = state.sessionId,
): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  // Belt & braces: drop anything the tail already delivered so a duplicated
  // backfill can never render twice.
  const existingIds = new Set<string>();
  for (const m of snapshot.messages) if (m.serverId) existingIds.add(m.serverId);
  const fresh = rawMessages.filter(r => {
    const id = (r as any).id;
    return !id || !existingIds.has(String(id));
  });
  if (fresh.length === 0) return;

  // Transform the chunk in isolation. Stashing the live state keeps the
  // loader's running-session merge paths (sub-agent restore, streaming
  // re-attach, token usage overwrite) away from the live tail — the chunk is
  // pure history. The chunk's own user-message ordinal starts at 0, which is
  // exactly right: these are the FIRST user messages of the conversation.
  const savedMessages = snapshot.messages;
  const savedRunning = snapshot.running;
  const savedUsage = snapshot.tokenUsage;
  snapshot.messages = [];
  snapshot.running = false;
  try {
    loadSessionMessages(fresh, sessionId);
  } finally {
    const backfill = snapshot.messages;
    const backfillUsage = snapshot.tokenUsage;
    snapshot.messages = [...backfill, ...savedMessages];
    snapshot.running = savedRunning;
    // Combine token totals: the loader reported the chunk alone.
    if (savedUsage && backfillUsage) {
      const input = savedUsage.input_tokens + backfillUsage.input_tokens;
      const output = savedUsage.output_tokens + backfillUsage.output_tokens;
      snapshot.tokenUsage = { input_tokens: input, output_tokens: output, total_tokens: input + output };
      state.contextTokens = input + output;
    } else if (savedUsage) {
      snapshot.tokenUsage = savedUsage;
      state.contextTokens = savedUsage.total_tokens;
    }
  }

  // Viewport anchor: prepending grows the list ABOVE the visible range, and
  // a naive re-render would jump the user's view. Compensate the scrollTop
  // by the height delta after the flush has re-rendered (the flush itself is
  // a queueMicrotask, so one more microtask runs after it).
  const el = document.getElementById("chat-container");
  const prevHeight = el?.scrollHeight ?? 0;
  const prevTop = el?.scrollTop ?? 0;
  syncSessionState(sessionId);
  emit();
  queueMicrotask(() => {
    const el2 = document.getElementById("chat-container");
    if (el2 && prevHeight > 0) el2.scrollTop = prevTop + (el2.scrollHeight - prevHeight);
  });
}

/** Remembers which user message should receive a pending rollback edit. */
export function rememberRollbackEditTarget(message: Message, userIdx: number): void {
  pendingRollbackEdit = {
    sid: state.sessionId,
    serverId: message.serverId,
    userIdx,
    content: message.content,
    // The marker is only valid for the rollback the user just started. The
    // re-sent message lands at the SAME ordinal with the SAME text, so a
    // marker that outlives its operation cuts the user's own new message.
    at: Date.now(),
  };
}

/** Drops any pending rollback edit. Call it from paths that invalidate it:
 *  a refused request, a fresh session, or a branch switch that is not the
 *  rollback's own reply. */
export function clearPendingRollbackEdit(): void {
  pendingRollbackEdit = null;
}

/** True while a rollback edit awaits server confirmation. Belongs to the
 *  conversation it was started in, so a snapshot for any other session is
 *  never treated as the confirming one. */
export function hasPendingRollbackEdit(sessionId?: string): boolean {
  if (!pendingRollbackEdit) return false;
  const target = sessionId || state.sessionId;
  return pendingRollbackEdit.sid === target;
}

/** Applies the pending rollback edit onto a raw message list, if any. */
export function applyPendingRollbackEdit<T extends { role?: string; content?: string | Array<{ type: string; text: string }>; id?: string }>(rawMessages: T[], sessionId?: string): T[] {
  if (!pendingRollbackEdit) return rawMessages;
  // The edit belongs to the conversation it was started in. Loading another
  // session's list must not consume it — the old code cut the wrong session's
  // messages at whichever ordinal/content happened to match.
  const targetSid = sessionId || state.sessionId;
  if (!pendingRollbackEdit.sid || pendingRollbackEdit.sid !== targetSid) return rawMessages;
  // A marker whose operation has not been confirmed within the TTL is stale:
  // applying it would cut whichever message merely sits at the remembered
  // ordinal -- normally the user's own re-sent copy of the same text.
  if (Date.now() - pendingRollbackEdit.at > ROLLBACK_EDIT_TTL_MS) {
    pendingRollbackEdit = null;
    return rawMessages;
  }

  let userOrdinal = -1;
  let cutIdx = -1;
  for (let i = 0; i < rawMessages.length; i++) {
    const raw = rawMessages[i];
    // Align with buildTimeline's data-user-idx counting: only real user
    // turns consume an ordinal. compact_marker / compact_notice /
    // model_switch arrive as their own roles in a raw backend list, or as
    // mode-tagged user-role messages in a normalised one, and are skipped
    // either way; synthetic loop plumbing is skipped here too. The old rule
    // counted compact_marker IN and compact_notice/model_switch OUT, so the
    // ordinal base drifted from the stored userIdx after compaction.
    const isSynthetic = raw.role === "user" && (raw as any).is_synthetic;
    if (raw.role !== "user" || isSynthetic
        || isNoticeMode(raw.role)
        || isNoticeMode((raw as any).mode)) continue;
    userOrdinal++;

    const rawId = raw.id || "";
    const idMatches = pendingRollbackEdit.serverId
      ? rawId === pendingRollbackEdit.serverId || rawId.endsWith(`:M:${pendingRollbackEdit.serverId}`)
      : false;
    // Content matching is a fallback for targets without a server id only.
    // A re-sent rollback edit lands at the SAME ordinal with the SAME text,
    // so a content match would cut the NEW message out of the list. Guard it
    // with the marker timestamp: a message created after the rollback click
    // is the re-send, not the target.
    const contentMatches = !pendingRollbackEdit.serverId
      && userOrdinal === pendingRollbackEdit.userIdx
      && Number((raw as any).created_at || 0) <= pendingRollbackEdit.at
      && extractMessageText(raw.content || "") === pendingRollbackEdit.content;

    if (idMatches || contentMatches) {
      cutIdx = i;
      break;
    }
  }

  // Resolve the pending edit on the first list arrival after the rollback
  // operation: either the target is still present (git checkout path -- cut
  // it) or the server already removed it (branch path -- nothing to cut).
  // Letting it linger would content-match the re-sent message later.
  pendingRollbackEdit = null;
  if (cutIdx < 0) return rawMessages;
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
  snapshot.retryNotice = null;
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

/** Max per-session file-ref cache entries kept in localStorage. */
const FR_CACHE_MAX = 200;

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
    // Same base as buildTimeline's data-user-idx, which is also what
    // loadSessionMessages uses to read these keys back: the notice records
    // consume no ordinal, so counting them misaligned the fr:<sid>:<idx>
    // storage slot with the restored one.
    const idx = realUserMessages(snapshot.messages).length - 1;
    try {
      localStorage.setItem(`fr:${sid}:${idx}`, JSON.stringify(fileRefs));
      // localStorage has a hard quota; keep a per-session index list so the
      // oldest entries are evicted instead of accumulating for life.
      const listKey = `fr-idx:${sid}`;
      let list: number[] = [];
      const rawList = localStorage.getItem(listKey);
      if (rawList) { try { const parsed = JSON.parse(rawList); if (Array.isArray(parsed)) list = parsed; } catch {} }
      list.push(idx);
      while (list.length > FR_CACHE_MAX) {
        const drop = list.shift();
        if (drop !== undefined) localStorage.removeItem(`fr:${sid}:${drop}`);
      }
      localStorage.setItem(listKey, JSON.stringify(list));
    } catch {}
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

/**
 * Returns the assistant message of the CURRENT turn (the one after the last
 * user message), if any.
 *
 * The scan stops at the last user message: tool-call / text-delta events must
 * never attach to an assistant message from a PREVIOUS turn.  The old
 * "scan back for any assistant" behavior let a stale streaming assistant (e.g.
 * one left open by a suspended question tool or a missed finish event) swallow
 * the next turn's tool calls, which rendered new question cards up inside the
 * old turn instead of at the bottom.
 */
export function getLastAssistantMessage(sessionId = state.sessionId): Message | undefined {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  for (let i = snapshot.messages.length - 1; i >= 0; i--) {
    const msg = snapshot.messages[i];
    if (msg.role === "user") return undefined;
    if (msg.role === "assistant") return msg;
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

/** Resolves one tool call by id, preferring an exact renderer-id match over a
 *  backend-protocol-id match.  Several backends mint request-local protocol ids
 *  ("call_0", "call_1", "call_2") that repeat on every request, so a restored
 *  card's ``backendId`` can equal a later event's id; the exact pass keeps live
 *  events on their own card instead of rewriting the earliest match. */
function resolveToolCall(id: string, sessionId: string): ToolCallState | null {
  if (!id) return null;
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  for (const msg of snapshot.messages) {
    for (const tc of msg.toolCalls) if (tc.id === id) return tc;
  }
  for (const msg of snapshot.messages) {
    for (const tc of msg.toolCalls) if (tc.backendId === id) return tc;
  }
  return null;
}

/** Updates an existing tool call by id (status/result/params). */
export function updateToolCall(
  id: string,
  patch: Partial<ToolCallState>,
  sessionId = state.sessionId
): void {
  if (sessionId && state.sessionId && sessionId !== state.sessionId) return;
  const tc = resolveToolCall(id, sessionId);
  if (tc) {
    Object.assign(tc, patch);
    syncSessionState(sessionId);
    emit();
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

/**
 * Single funnel for opening any URL from the renderer UI.
 *
 * Every non-`file:` URL must go through here so the user's
 * `default_link_behavior` setting (Encre Agent Desktop Subwindow / ESD sub-app vs. system browser) is always
 * honored — regardless of which panel or view the link lives in. `file:` URLs
 * are deliberately ignored: they belong to dedicated internal flows (info
 * pages, markdown previews) and must never be re-routed. Non-web schemes
 * (e.g. `mailto:`) can only be handled by the OS, so they always go to the
 * system handler.
 */
export function openRoutedLink(rawUrl: string, label?: string): void {
  if (!rawUrl) return;
  let url = rawUrl.trim();
  if (!url || /^file:/i.test(url)) return;
  if (/^www\./i.test(url)) url = "https://" + url;
  let protocol = "";
  try {
    protocol = new URL(url).protocol;
  } catch {
    return; // Not a parseable URL — nothing sensible to open.
  }
  const api = (window as any).electronAPI;
  if (!api) {
    window.open(url, "_blank");
    return;
  }
  if (protocol !== "http:" && protocol !== "https:") {
    void api.openExternal?.(url);
    return;
  }
  const behavior = (state.settings.default_link_behavior as string) || "system";
  if (behavior === "in_app" && api.openChildWindow) {
    void api.openChildWindow(url, label || url);
  } else if (api.openExternal) {
    void api.openExternal(url);
  } else {
    window.open(url, "_blank");
  }
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

/** Colour skins shipped with the app — the keys of themes/<skin>.css. */
export const THEME_SKINS = [
  "ink", "spring", "autumn", "winter", "honey", "grass", "sky", "sunset",
] as const;

/**
 * Sets the active colour skin and mirrors it onto `<html data-skin>`.
 * The palettes themselves live in themes/<skin>.css; the light/dark mode
 * keeps riding on [data-theme], so every existing mode-level override
 * keeps working unchanged.
 */
export function setThemeSkin(skin: string): void {
  update({ themeSkin: skin });
  document.documentElement.setAttribute("data-skin", skin);
}

/** Generates a random unique id. */
export function generateId(): string {
  return crypto.randomUUID();
}

/** Finds a tool call by id in the (active) session. */
export function findToolCall(id: string, sessionId = state.sessionId): ToolCallState | null {
  return resolveToolCall(id, sessionId);
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

/** Adds attachment records to the composer state.
 *
 *  Duplicate paths are skipped: the rollback paths can fire twice for the
 *  same message (local edit restore + the async ``rollback_checkout``
 *  broadcast), and blindly pushing would stack identical chips and make
 *  the next send carry the attachment more than once. */
export function addAttachments(items: AttachmentMeta[]): void {
  for (const item of items) {
    if (item.path && state.attachments.some((a) => a.path === item.path)) continue;
    state.attachments.push(item);
  }
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
export function addCompactStartingEvent(
  noticeId: string,
  anchorId: string,
  sessionId = state.sessionId,
): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.compactStartingEvents = [
    ...snapshot.compactStartingEvents,
    { timestamp: Date.now(), noticeId, anchorId },
  ];
  syncSessionState(sessionId);
  emit();
}

/** Clears compact-starting events.
 *
 *  With a *noticeId* only that one indicator is dismissed (the compaction
 *  it describes finished or failed); without one every indicator goes --
 *  the session was reset or the turn ended.  Ids the session never saw are
 *  a no-op, so a late ``compact_failed`` cannot wipe a live indicator. */
export function clearCompactStartingEvents(noticeId = "", sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  if (!noticeId) {
    if (snapshot.compactStartingEvents.length === 0) return;
    snapshot.compactStartingEvents = [];
  } else {
    const before = snapshot.compactStartingEvents.length;
    snapshot.compactStartingEvents = snapshot.compactStartingEvents.filter(
      (e) => e.noticeId !== noticeId,
    );
    if (snapshot.compactStartingEvents.length === before) return;
  }
  syncSessionState(sessionId);
  emit();
}

/** Appends a persisted-style UI notice message to the (active) session.
 *
 *  Used for LIVE events (model switch) whose persisted twin the backend has
 *  already recorded: the message rides in ``st.messages`` exactly like the
 *  ``compact_marker`` entries restored from history, so the timeline renders
 *  it inline and a later full-history reload replaces it in place.
 *
 *  Model-switch COALESCING: the backend merges a burst of switches (no new
 *  round in between) into ONE persisted notice and re-emits it under the
 *  ORIGINAL id with the net change.  An event whose id is already on screen
 *  therefore UPDATES that line in place instead of stacking a duplicate;
 *  a net no-op (from == to, switched away and back) removes the line. */
export function appendNoticeMessage(
  kind: "compact_notice" | "model_switch",
  payload: Record<string, unknown>,
  sessionId = state.sessionId,
): string {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  const id = String(payload.id || `${kind}-${Date.now()}`);
  const ts = Number(payload.timestamp) || Date.now();
  if (kind === "model_switch") {
    const fromModel = String(payload.from_model || "");
    const toModel = String(payload.to_model || "");
    const existingIdx = snapshot.messages.findIndex((m) => m.serverId === id);
    if (existingIdx >= 0) {
      const existing = snapshot.messages[existingIdx];
      if (fromModel && fromModel === toModel) {
        // Switched away and back within the same round: net no change,
        // so the notice disappears entirely.
        snapshot.messages = snapshot.messages.filter((_, i) => i !== existingIdx);
      } else {
        const updated: import("./types.js").Message = {
          ...existing,
          timestamp: ts,
          switchInfo: {
            fromModel: fromModel || existing.switchInfo?.fromModel || "",
            toModel: toModel || existing.switchInfo?.toModel || "",
            reason: String(payload.reason || ""),
            source: String(payload.source || existing.switchInfo?.source || "manual"),
          },
        };
        snapshot.messages = snapshot.messages.map((m, i) => (i === existingIdx ? updated : m));
      }
      syncSessionState(sessionId);
      emit();
      return id;
    }
    // Net no-op with nothing on screen yet: nothing to show.
    if (fromModel && fromModel === toModel) return id;
  }
  // A model-switch line only makes sense inside a conversation. An empty
  // timeline (new chat, history still loading, cleared transcript) must not
  // show a lone divider floating on a blank page.
  if (kind === "model_switch") {
    const hasVisibleTurn = snapshot.messages.some(
      (m) =>
        m.role === "assistant" ||
        (m.role === "user" &&
          m.mode !== "model_switch" &&
          m.mode !== "compact_notice" &&
          m.mode !== "compact_marker"),
    );
    if (!hasVisibleTurn) return id;
  }
  // Dedupe: a reload that already carries the notice must not stack a
  // second copy on top of the live one.
  if (snapshot.messages.some((m) => m.serverId === id)) return id;
  const msg: import("./types.js").Message = {
    id: `m:${id}`,
    role: "user",
    content: "",
    isStreaming: false,
    toolCalls: [],
    segments: [],
    timestamp: ts,
    mode: kind,
    serverId: id,
    ...(kind === "compact_notice"
      ? {
          compactStats: {
            oldTokens: Number(payload.old_tokens) || 0,
            newTokens: Number(payload.new_tokens) || 0,
            oldCount: Number(payload.old_count) || 0,
            newCount: Number(payload.new_count) || 0,
          },
        }
      : {
          switchInfo: {
            fromModel: String(payload.from_model || ""),
            toModel: String(payload.to_model || ""),
            reason: String(payload.reason || ""),
            source: String(payload.source || "manual"),
          },
        }),
  };
  // Position: right after the message this notice belongs to — NEVER at the
  // very end while an in-flight streaming assistant bubble is sitting there
  // (that is what dragged every notice below the reply).  The backend's
  // anchor id wins when it resolves; otherwise the notice goes after the
  // last COMMITTED message (the one the switch/compaction followed).
  const anchorId = String(payload.anchor_id || "");
  let insertAfter = -1;
  if (anchorId) {
    insertAfter = snapshot.messages.findIndex((m) => m.serverId === anchorId);
  }
  if (insertAfter < 0) {
    for (let i = snapshot.messages.length - 1; i >= 0; i--) {
      if (!snapshot.messages[i].isStreaming) {
        insertAfter = i;
        break;
      }
    }
  }
  snapshot.messages = [
    ...snapshot.messages.slice(0, insertAfter + 1),
    msg,
    ...snapshot.messages.slice(insertAfter + 1),
  ];
  syncSessionState(sessionId);
  emit();
  return id;
}

/** Records the in-flight retry notice for the (active) session. */
export function setRetryNotice(info: import("./types.js").RetryNoticeInfo, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.retryNotice = info;
  syncSessionState(sessionId);
  emit();
}

/** Drops the in-flight retry notice once the retried attempt produces output
 *  (or the turn ends).  No-op when there is nothing to clear, so the
 *  per-token clear calls in the streaming handlers stay cheap. */
export function clearRetryNotice(sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  if (!snapshot.retryNotice) return;
  snapshot.retryNotice = null;
  syncSessionState(sessionId);
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
    source: source || t("general.sourceEncre"),
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

  const old = input.querySelector(".mode-chip[data-mode]");
  if (old) removeChipWithSeparator(old);

  const cmd = findSlashCommand(mode);
  const label = cmd ? cmd.title : mode;
  // A plugin command with no declared icon gets no glyph; "list-checks" is
  // only this app's own marker for a mode the command table does not know.
  const icon = cmd ? (cmd.icon || "") : "list-checks";

  const chip = document.createElement("span");
  chip.contentEditable = "false";
  chip.className = "mode-chip";
  chip.setAttribute("data-mode", mode);
  chip.innerHTML = `${icon ? `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i>` : ""}<span>${label}</span>`;
  // Draft-level close (removeInlineModeChip) — the restored chip is an
  // editable draft; ending the persistent mode globally is the toolbar ×.
  wireChipClose(chip, () => (window as any).__app?.removeInlineModeChip?.());
  input.insertBefore(chip, input.firstChild);

  if ((window as any).lucide) {
    (window as any).lucide.createIcons({ root: chip });
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
      (window as any).lucide.createIcons({ root: tbChip });
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

  const old = input.querySelector(".command-chip[data-command]");
  if (old) removeChipWithSeparator(old);

  const cmd = findSlashCommand(name);
  const label = cmd ? cmd.title : name;
  const icon = cmd ? (cmd.icon || "") : "wand-2";

  const chip = document.createElement("span");
  chip.contentEditable = "false";
  chip.className = "command-chip";
  chip.setAttribute("data-command", name);
  chip.innerHTML = `${icon ? `<i data-lucide="${icon}" class="chip-icon" style="width:12px;height:12px;"></i>` : ""}<span>${label}</span>`;
  // Shared delegation — see ui/chip.ts for why the icon element cannot carry
  // the listener (lucide swaps it for an <svg>).
  wireChipClose(chip, () => (window as any).__app?.deactivateCommandChip?.());
  input.insertBefore(chip, input.firstChild);

  if ((window as any).lucide) {
    (window as any).lucide.createIcons({ root: chip });
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
/** 32-bit FNV-1a over one string (no intermediate list allocation). */
function _hash32(text: string, seed: number): number {
  let h = seed >>> 0;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/**
 * Stable identity of a session list for change detection.
 *
 * Two independent 32-bit hashes (64 bits total) replace the previous
 * ~500KB concatenated string, which was rebuilt and compared on every
 * sessions push (boot, streaming, tray sync). Collision odds are negligible.
 */
function _sessionsSignature(list: import("./types.js").SessionEntryData[]): string {
  let h1 = 2166136261;
  let h2 = 0x811c9dc5 ^ 0x9e3779b9;
  for (const s of list) {
    const md = (s.metadata || {}) as Record<string, unknown>;
    const line = `${s.session_id}|${s.name || ""}|${(s.preview || "").slice(0, 80)}|${s.state || ""}|${s.last_active || 0}|${s.message_count || 0}|${s.channel || ""}|${s.source?.platform || ""}|${md.workspace_path || md.workspace || ""}`;
    h1 = _hash32(line, h1);
    h2 = _hash32(line, h2);
  }
  return `${list.length}:${h1}:${h2}`;
}

let _sessionsListSig = "";
let _allSessionsSig = "";

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
    const sig = _sessionsSignature(filtered);
    if (sig === _allSessionsSig) return;
    _allSessionsSig = sig;
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
  const sig = _sessionsSignature(merged);
  if (sig === _allSessionsSig) return;
  _allSessionsSig = sig;
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
    // A session only becomes "history" once it has at least one message:
    // brand-new empty sessions must not appear in "All tasks".
    return (session.message_count || 0) > 0;
  });
  // The backend broadcasts the same list on several edges (connect snapshot,
  // no-op refreshes). Re-storing an identical list would wake the sidebar,
  // the tray and the search cache for nothing.
  const sig = _sessionsSignature(visible);
  if (sig !== _sessionsListSig) {
    _sessionsListSig = sig;
    update({ sessionsList: visible });
  }
  // Keep the global search cache in sync (never cleared by mode switches);
  // it dedupes its own signature.
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

let _lastTraySig = "";

function publishTraySessions(): void {
  const projected = buildTraySessionData(
    traySessionsCache.normal,
    traySessionsCache.iwork,
    state.workspaces,
  );
  const sig =
    projected.normal.map((s) => `${s.session_id}:${s.state}:${s.name}:${s.last_active}`).join("|") +
    "#" +
    projected.iwork.map((s) => `${s.session_id}:${s.state}:${s.name}:${s.last_active}`).join("|") +
    "#" +
    projected.workspaces.map((w) => `${w.path}:${w.icon_data || ""}`).join("|");
  if (sig === _lastTraySig) return;
  _lastTraySig = sig;
  window.electronAPI?.traySessionsBothUpdate?.(projected as any);
}

/** Removes a session from the sidebar list by id. */
export function removeSessionById(sessionId: string): void {
  const sessions = state.sessionsList.filter((s) => s.session_id !== sessionId);
  const history = state.automationHistory.filter((h: any) => h.session_id !== sessionId);
  // A deleted conversation takes its floating surfaces with it: an unanswered
  // shelved prompt must never resurface, nor may a rollback intent cut a list
  // for an id that no longer exists.
  evictScope("session", sessionId);
  if (pendingRollbackEdit && pendingRollbackEdit.sid === sessionId) pendingRollbackEdit = null;
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

export function setProviders(providers: import("./types.js").ProviderConfigMeta[]): void {
  update({ providers });
}

/**
 * Maps a flat `modelConfigs` index onto its owning provider.
 *
 * The backend flattens providers in declaration order, so the flat list
 * indexes models provider-by-provider.  Returns null when the index is
 * out of range or the provider snapshot is out of sync.
 */
export function locateModelInProviders(flatIdx: number): { providerIdx: number; modelIdx: number } | null {
  const providers = (getState() as any).providers as import("./types.js").ProviderConfigMeta[] | undefined;
  if (!providers) return null;
  let n = 0;
  for (let p = 0; p < providers.length; p++) {
    const count = providers[p]?.models?.length || 0;
    if (flatIdx < n + count) return { providerIdx: p, modelIdx: flatIdx - n };
    n += count;
  }
  return null;
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

/** Replaces the local queue mirror with the backend's authoritative snapshot.
 *
 * The submit queue lives server-side (persisted across restarts), so the
 * renderer never mutates it locally: panel edits go out as ``queue_move`` /
 * ``queue_remove`` / ``queue_pop`` messages and the refreshed snapshot
 * arrives back as a ``queued_submits`` broadcast, which lands here.
 */
export function setQueuedPrompts(items: QueuedPrompt[]): void {
  update({ queuedPrompts: items, pendingQueueCount: items.length });
}

/** Clears the local queue mirror (session switch; the backend re-syncs). */
export function clearQueuedPrompts(): void {
  update({ queuedPrompts: [], pendingQueueCount: 0 });
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
    const msg = snapshot.messages[i];
    // Count exactly like buildTimeline assigns data-user-idx: the notice
    // records (compact_marker / compact_notice / model_switch) ride along as
    // user-role messages but never consume an ordinal, so they must not here
    // either. Counting them made rollback cut one turn earlier than clicked.
    if (msg.role !== "user" || isNoticeMode(msg.mode)) continue;
    if (ui === userIdx) {
      cutIdx = i;
      break;
    }
    ui++;
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

/** Records a session's backend-resolved files directory, keyed by session id
 *  so each session's editor tree root stays isolated from the others. */
export function setSessionFilesDir(sessionId: string, dir: string): void {
  if (!sessionId) return;
  update({ sessionFilesDirs: { ...state.sessionFilesDirs, [sessionId]: dir } });
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

/** Updates context-token usage for the (active) session. */
export function updateContextUsage(
  tokens: number,
  window: number,
  sessionId = state.sessionId,
  breakdown?: import("./types.js").ContextUsageBreakdown | null,
): void {
  if (!sessionId) return;
  // Ignore events from non-active sessions so background sessions cannot
  // overwrite the canvas panel currently shown in the sidebar.
  if (sessionId !== state.sessionId) return;
  state.contextTokens = tokens;
  state.contextWindow = window;
  state.contextUsageBreakdown = breakdown ?? null;
  emit();
}

/** Sets the .gitignore content being viewed. */
export function setGitignoreContent(content: string): void {
  update({ gitignoreContent: content });
}

/** Sets the knowledge-library tree (all nodes, flat). */
export function setLibraryTree(nodes: import("./types.js").LibraryNode[]): void {
  update({ libraryTree: nodes });
}

/** Sets the last library content read (or clears it with null). */
export function setLibraryContent(content: { id: string; content: string } | null): void {
  update({ libraryContent: content });
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

/** Sets the memory relationship graph (nodes + typed edges). */
export function setMemoryGraph(graph: { nodes: import("./types.js").MemoryGraphNode[]; edges: import("./types.js").MemoryGraphEdge[] } | null): void {
  update({ memoryGraph: graph });
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

/** Removes the given message ids from a branch.
 *
 *  Ids arrive in two forms: renderer ids are `m:<backend id>` (assigned in
 *  loadSessionMessages) while `branch_rolled_back` reports raw backend ids.
 *  Match both, otherwise a raw-id removal compares against `m:`-prefixed
 *  ids and silently removes nothing. */
export function removeBranchMessages(removedIds: Set<string>, sessionId = state.sessionId): void {
  const snapshot = getOrCreateSessionSnapshot(sessionId);
  snapshot.messages = snapshot.messages.filter(
    m => !removedIds.has(m.id) && !removedIds.has(m.id.replace(/^m:/, ""))
  );
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
