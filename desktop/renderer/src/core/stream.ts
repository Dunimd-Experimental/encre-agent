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
 * Server-event stream handling.
 *
 * The bridge between the WebSocket transport and the renderer UI. Registers
 * per-event-type callbacks (adapter tests, automation jobs/history, …), routes
 * every decoded {@link ServerEvent} through `handleEvent` into the state store
 * and the chat view, and owns the "requested session" handshake used to match
 * resume/open responses to the right session.
 */

import { ServerEvent, WorkspaceEntry, BranchUpdated, BranchSwitched, BranchRolledBack, UsageStatsEvent, SessionState, QueuedSubmitsEvent, QueuedRunStartedEvent } from "./types.js";
import type { PluginPendingEntry, PluginStateEntry } from "./types.js";
import * as state from "./state.js";
import { send, getWorkspaceIntent } from "./ws.js";
import { Chat } from "../chat/chat.js";
import { Tools } from "../features/tools.js";
import { Permissions } from "../ui/permissions.js";
import { Settings } from "../settings/settings.js";
import { t } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";
import { pruneStaleKeybinds } from "../ui/keybind-scope.js";
import { handleEngineInstallRequest, handleEngineInstallProgress } from "../features/engine_install.js";
import type { WechatScanResultEvent } from "./types.js";

let _wechatScanCallback: ((event: WechatScanResultEvent) => void) | null = null;
let _automationJobsCallback: ((jobs: any[]) => void) | null = null;
let _automationHistoryCallback: ((history: any[]) => void) | null = null;
let _automationJobCreatedCallback: ((job: any) => void) | null = null;
let _automationJobCancelledCallback: ((jobId: string) => void) | null = null;
let _automationJobUpdatedCallback: (() => void) | null = null;
let _automationShowResultCallback: ((data: any) => void) | null = null;
let _automationStreamCallback: ((event: import("./types.js").AutomationStreamEvent) => void) | null = null;
/** Progress dialog handle for full data-dir export/import. */
let _migrationProgress: ReturnType<typeof Dialog.progress> | null = null;

/** Error codes that can leave the local transcript out of step with the
 *  backend, so the frontend must re-request the authoritative session state.
 *
 *  The rollback / branch / retry family is the direct case: the UI truncates
 *  optimistically before the server answers, so a refusal leaves a ghost
 *  user message on screen. The lookup and capacity codes mean the request
 *  was never applied at all, which is the same inconsistency. */
const RESYNC_ERROR_CODES: ReadonlySet<string> = new Set([
  "rollback_error",
  "branch_not_found",
  "retry_error",
  "no_session",
  "busy",
  "delete_error",
  "not_found",
  "invalid_request",
]);

/** Re-requests the authoritative state for the active session after the
 *  backend refused a request the UI applied optimistically. Drops the pending
 *  rollback edit too -- nothing was actually rolled back, so a lingering
 *  marker would cut whichever message merely sits at the remembered ordinal. */
function resyncActiveSession(): void {
  state.clearPendingRollbackEdit();
  const sid = state.getState().sessionId;
  if (!sid) return;
  const rid = crypto.randomUUID();
  setRequestedSessionId(sid, rid);
  send({ type: "resume", session_id: sid, request_id: rid } as any);
}
/** Registers a callback for WeChat QR code scan results. */
export function onWechatScanResult(cb: (event: WechatScanResultEvent) => void): void {
  _wechatScanCallback = cb;
}

/** Serialised pending-plugin confirmation queue (dynamic authorisation). */
let _pluginConfirmBusy = false;
const _pluginConfirmQueue: PluginPendingEntry[] = [];

/**
 * Shows one activation-confirmation dialog per pending third-party plugin,
 * strictly one at a time, and reports each decision back to the backend.
 *
 * The dialog reuses the standard confirm shell (same overlay/card/buttons
 * as the deletion confirmation) so the design language stays consistent.
 * Approve imports + activates the plugin; deny drops the inert shell and
 * the plugin's code is never imported.
 */
async function handlePluginConfirmQueue(plugins: PluginPendingEntry[]): Promise<void> {
  _pluginConfirmQueue.push(...plugins);
  if (_pluginConfirmBusy) return;
  _pluginConfirmBusy = true;
  try {
    while (_pluginConfirmQueue.length) {
      const plugin = _pluginConfirmQueue.shift()!;
      const description = plugin.description ? `\n\n${plugin.description}` : "";
      const perms = plugin.permissions && plugin.permissions.length
        ? `\n\n${t("permissions.pluginPerms", { perms: plugin.permissions.join(", ") })}`
        : "";
      let approved = false;
      try {
        approved = await Dialog.confirm(
          t("permissions.pluginTitle", { name: plugin.name }),
          t("permissions.pluginBody", { name: plugin.name, version: plugin.version })
            + description + perms,
          {
            priority: "critical",
            scope: "session",
            restore: true,
          },
        );
      } catch {
        approved = false;
      }
      send({ type: "plugin_confirm", name: plugin.name, approved });
    }
  } finally {
    _pluginConfirmBusy = false;
  }
}

/** Registers a callback invoked whenever the automation jobs list arrives. */
export function onAutomationJobs(cb: (jobs: any[]) => void): void {
  _automationJobsCallback = cb;
}

/** Registers a callback for automation run-history updates. */
export function onAutomationHistory(cb: (history: any[]) => void): void {
  _automationHistoryCallback = cb;
}

/** Registers a callback fired when a new automation job is created. */
export function onAutomationJobCreated(cb: (job: any) => void): void {
  _automationJobCreatedCallback = cb;
}

/** Registers a callback fired when an automation job is updated. */
export function onAutomationJobUpdated(cb: () => void): void {
  _automationJobUpdatedCallback = cb;
}

/** Registers a callback to show a job's result detail. */
export function onAutomationShowResult(cb: (data: any) => void): void {
  _automationShowResultCallback = cb;
}

/** Registers a callback fired when an automation job is cancelled. */
export function onAutomationJobCancelled(cb: (jobId: string) => void): void {
  _automationJobCancelledCallback = cb;
}

/** Registers a callback for live automation stream events. */
export function onAutomationStreamEvent(cb: (event: import("./types.js").AutomationStreamEvent) => void): void {
  _automationStreamCallback = cb;
}
import { applyServerCommands, applyProjectCommands } from "../ui/slash-commands.js";
import { resetPluginUi, syncPluginUiProviders } from "../features/plugin_ui.js";
import { resetPluginContributions, syncPluginContributions } from "../features/plugin_contributions.js";
import { setPluginInfo } from "./plugin_info.js";
import { setMarketIndex } from "./market_index.js";
import { setToolMetaEntries } from "../chat/tool_meta.js";
import { dispatchServerSignal } from "./server_signals.js";

let chat: Chat | null = null;
let tools: Tools | null = null;
let permissions: Permissions | null = null;
let _settings: Settings | null = null;
let permissionResolve: ((allowed: boolean) => void) | null = null;
let _sessionGeneration = 0;
let _requestedSessionId = "";
/**
 * Session whose pre-tail history is still owed by a tail-first open.
 *
 * Set when a `session_ready` carries `has_earlier_history`, drained when the
 * matching topic completes. Guards the prepend so a stale or duplicated
 * backfill can never land in the wrong conversation.
 */
let _pendingBackfillSid = "";

/**
 * Generic topic assembler for the sharded bulk-data channel
 * (data_begin -> data_shard* -> data_end). ONE reassembly path shared by
 * every bulk backend payload; consumers register by topic shape below.
 * A dropped or duplicated shard poisons only its own topic, never others.
 */
const _topicBuffers = new Map<string, {
  meta: Record<string, unknown>;
  items: any[];
  nextSeq: number;
  totalShards: number;
}>();

function handleDataBegin(topic: string, meta: Record<string, unknown>, totalShards: number): void {
  _topicBuffers.set(topic, { meta: meta || {}, items: [], nextSeq: 0, totalShards });
}

function handleDataShard(topic: string, seq: number, items: any[] | undefined): void {
  const buf = _topicBuffers.get(topic);
  if (!buf) return;
  if (seq !== buf.nextSeq) {
    // Gap: the topic is corrupt (reconnect raced a push). Drop it; the
    // producer's contract is strict sequential shards, so partial assembly
    // would silently lose history.
    _topicBuffers.delete(topic);
    console.warn("[stream] topic shard gap, dropping:", topic, "expected", buf.nextSeq, "got", seq);
    return;
  }
  if (items && items.length > 0) buf.items.push(...items);
  buf.nextSeq = seq + 1;
  // Bootstrap fill: the first shard of a session list is the newest ~80
  // rows, exactly what the sidebar window shows. Apply it immediately so the
  // sidebar populates without waiting for all 5k rows; the final assembled
  // list replaces it at data_end.
  if (seq === 0 && !_sessionsArrived) {
    const listTopic = /^sessions:list:(normal|iwork)$/.exec(topic);
    if (listTopic && buf.items.length > 0) {
      handleEvent({
        type: "sessions_list",
        sessions: buf.items.slice(),
        channel: listTopic[1],
      } as unknown as ServerEvent);
    }
  }
}

function handleDataEnd(topic: string): void {
  const buf = _topicBuffers.get(topic);
  if (!buf) return;
  _topicBuffers.delete(topic);
  // Route the completed topic. New bulk producers add one entry here.
  // Session lists arrive sharded: thousands of rows would otherwise parse
  // as one multi-megabyte JSON blob in a single long task.
  // Generic list-plane sharding: the backend sends any large list panel
  // under "plane:<frame type>" and names the payload field in meta.
  const planeMatch = /^plane:([a-z0-9_]+)$/.exec(topic);
  if (planeMatch) {
    const kwarg = String(buf.meta.kwarg || "");
    if (kwarg) {
      handleEvent({
        type: planeMatch[1],
        [kwarg]: buf.items,
      } as unknown as ServerEvent);
    }
    return;
  }
  const sessionsTopic = /^sessions:(list:(normal|iwork)|all)$/.exec(topic);
  if (sessionsTopic) {
    if (sessionsTopic[1] === "all") {
      const items = buf.items as Array<Record<string, unknown>>;
      const normal = items.filter((s) => (s.channel as string) !== "iwork");
      const iwork = items.filter((s) => (s.channel as string) === "iwork");
      handleEvent({ type: "sessions_all", normal, iwork } as unknown as ServerEvent);
    } else {
      handleEvent({
        type: "sessions_list",
        sessions: buf.items,
        channel: sessionsTopic[2],
      } as unknown as ServerEvent);
    }
    return;
  }
  const historyMatch = /^session:(.+):history-backfill$/.exec(topic);
  if (historyMatch) {
    const sid = historyMatch[1];
    if (_pendingBackfillSid === sid && sid === state.getState().sessionId) {
      _pendingBackfillSid = "";
      if (buf.items.length > 0) {
        // Older history arrives after the tail is on screen. Keep the same
        // deferred treatment for it and upgrade in idle slices; otherwise
        // prepending a long history re-parsed every block at once.
        if (chat) chat.deferMarkdown = true;
        state.prependSessionMessages(buf.items, sid);
        chat?.requestRender();
        chat?.beginMarkdownUpgrade();
        console.log("[stream] history-backfill topic assembled, total:", state.getState().messages.length);
      }
    }
  }
}
/**
 * Session ids with a `resume` request in flight.
 *
 * Populated by `setRequestedSessionId` (every resume path calls it right
 * before sending) and drained when the matching `session_ready` (or an
 * `error`) frame arrives.  The chat view reads it to distinguish "switched
 * to a session with no cached transcript, backend is loading it" from
 * "opened a genuinely empty new chat", so it can show a loading placeholder
 * instead of either the previous session's stale content or a welcome flash.
 */
const _pendingResumes = new Set<string>();
let _requestedSessionRequestId = "";
let _activeStreamSessionId = "";
let _toolCallGeneration: Record<string, number> = {};
/** Whether the agent-control lock was already announced for the current
 *  turn. Turn-scoped ON PURPOSE: consecutive browser calls in one turn
 *  (start → result → start → …) must announce ONCE, or every re-sent
 *  "active" would be treated by main as a fresh operation and silently
 *  strip a user takeover that happened in between (the "it steals control
 *  back" bug). Released only at the turn boundary (finish). */
let _browserControlAnnounced = false;
let _validationPending = new Map<string, { resolve: (v: any) => void; reject: (r: string) => void }>();

function _hasSessionId(event: { session_id?: string | null }): event is { session_id: string } {
  return typeof event.session_id === "string" && event.session_id.length > 0;
}

/** Lifecycle identity of a plugin snapshot for signal deduplication. */
function _pluginStatesSignature(plugins: readonly PluginStateEntry[]): string {
  let out = "";
  for (const p of plugins) {
    out += `${p.name}|${p.version || ""}|${p.state || ""}|${p.installed ? 1 : 0}|${p.enabled === false ? 0 : 1};`;
  }
  return out;
}

function _shouldRejectSessionScopedEvent(event: ServerEvent): boolean {
  if (event.type === "session_ready") return false;
  // Session lifecycle notifications must always be handled so the sidebar
  // stays in sync even when no session is currently active.
  if (event.type === "session_deleted" || event.type === "session_exported" || event.type === "sessions_exported_zip" || event.type === "session_renamed") return false;
  const activeSid = state.getState().sessionId;
  // Normalise: JSON null → undefined so falsy checks work correctly
  const eventSid = ((event as any).session_id as string | undefined) || undefined;

  // Any event carrying a session_id must belong to the currently active
  // session.  An explicit mismatch means it comes from a background session
  // and must not render in the active view.
  if (eventSid) {
    if (activeSid && eventSid !== activeSid) return true;
    // When the frontend has no active session (e.g. during workspace/mode
    // switches) drop session-scoped events entirely so a background session
    // cannot resurrect stale content in the empty content area.
    if (!activeSid) return true;
  }

  // For stream-bound events, REQUIRE a session_id.  The backend tags every
  // non-iClaw stream event with session_id, so an empty value means the
  // event originates from the iClaw/adapter pipeline which the desktop UI
  // must NOT render.  Also catch stale _activeStreamSessionId mismatches.
  if (_isSessionBoundStreamEvent(event.type)) {
    if (!eventSid) return true;
    if (_activeStreamSessionId && _activeStreamSessionId !== activeSid) return true;
  }

  return false;
}

function _isSessionBoundStreamEvent(type: ServerEvent["type"]): boolean {
  return type === "text_delta" ||
    type === "thinking_delta" ||
    type === "tool_call_start" ||
    type === "tool_call_delta" ||
    type === "tool_call_end" ||
    type === "question_request" ||
    type === "tool_progress" ||
    type === "tool_result" ||
    type === "permission_request" ||
    type === "engine_install_request" ||
    type === "engine_install_progress" ||
    type === "workflow_started" ||
    type === "workflow_task" ||
    type === "workflow_completed" ||
    type === "finish" ||
    type === "error" ||
    type === "plan_update" ||
    type === "plan_proposal" ||
    type === "plan_mode_changed" ||
    type === "command_changed" ||
    type === "plan_resolved" ||
    type === "assistant_boundary" ||
    type === "compact" ||
    type === "compact_starting" ||
    type === "retry" ||
    type === "run_started";
}

function _requiresExplicitSessionId(type: ServerEvent["type"]): boolean {
  return _isSessionBoundStreamEvent(type) ||
    type === "messages_updated" ||
    type === "rollback_checkout" ||
    type === "telemetry" ||
    type === "agent_state" ||
    type === "context_usage" ||
    type === "artifacts_update" ||
    type === "references_update";
}

/** Create an assistant message when a queued run starts streaming.
 *  The submit() call only adds the user message (without starting an assistant),
 *  so we detect the transition here: the last message is user and no streaming
 *  assistant exists yet.  The prompt queue itself is drained server-side —
 *  the ``queued_run_started`` event mirrors the transition for queued turns. */
function _ensureAssistantMessage(sessionId?: string): void {
  const st = state.getState();
  // Bail early if the event belongs to a different session than the one
  // being viewed — never mutate another session's messages from here.
  const resolvedSid = sessionId || st.sessionId;
  if (resolvedSid && resolvedSid !== st.sessionId) return;

  const msgs = st.messages;
  const last = msgs[msgs.length - 1];
  if (last?.role !== "user") return;
  // Create the turn's assistant message only when the boundary after the
  // last user message is still empty.  We deliberately do NOT reuse a
  // streaming assistant found anywhere earlier in the list: a stale one left
  // open by a suspended question tool (or a missed finish event) belongs to a
  // PREVIOUS turn, and appending to it rendered new tool cards up inside the
  // old turn.  getLastAssistantMessage already stops at the last user
  // message, so "undefined" here means this turn has no assistant yet.
  if (state.getLastAssistantMessage()) return;
  state.startAssistantMessage();
}

function _eventSessionId(event: { session_id?: string }): string {
  return event.session_id || state.getState().sessionId || "";
}

/** Records the requested session id and its matching request id. */
export function setRequestedSessionId(sid: string, requestId = ""): void {
  _requestedSessionId = sid;
  _requestedSessionRequestId = requestId;
  if (sid) _pendingResumes.add(sid);
}

/**
 * True while a `resume` for `sid` has been sent and not yet answered.
 *
 * Args:
 *   sid: The session id to check. An empty id is never pending.
 *
 * Returns:
 *   Whether the backend is still loading that session's transcript.
 */
export function isResumePending(sid: string): boolean {
  return !!sid && _pendingResumes.has(sid);
}

/**
 * Unified data-refresh entry point.
 *
 * Every mode switch / startup / reconnect goes through this single call so
 * all data planes (config+models, sessions, workspaces) are re-pulled from
 * the backend at once.  Previously each panel pulled its own slice at its own
 * time, which is why after a mode switch some data (e.g. the model selector)
 * stayed stale or half-filled while others refreshed.
 */
export function refreshAllData(): void {
  send({ type: "get_config" });
  send({ type: "list_sessions" });
  send({ type: "list_all_sessions" });
  send({ type: "list_workspaces" });
  send({ type: "list_models" });
  // Static snapshot domains: the backend also pushes these in the unified
  // snapshot, so these requests are a belt-and-suspenders guarantee that no
  // panel is ever left with stale/empty data after a mode switch.
  send({ type: "list_global_rules" });
  send({ type: "get_memory_list" });
  send({ type: "get_memory_graph" } as any);
  send({ type: "get_profile" });
  send({ type: "library_list" } as any);
  send({ type: "get_usage_stats" });
  send({ type: "automation_list_jobs" });
  send({ type: "list_project_rules" });
  send({ type: "list_project_hooks" } as any);
  send({ type: "list_archived_sessions" });
}

/**
 * How long to wait for the backend's connect push before pulling planes
 * explicitly.  The push is the single source of truth on connect; this timer
 * only exists because each push plane is independently try/except-guarded, so
 * a plane that failed server-side must not leave a panel permanently empty.
 */
const BOOT_PUSH_FALLBACK_MS = 1200;
let _bootFallbackTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Boot-time data pull.
 *
 * The backend already pushes the complete snapshot on connect
 * (`_push_all_data`: config, sessions, sessions_all, workspaces, models plus
 * every static plane), so the old 13-request burst fired on connect made the
 * same payloads arrive a second time — and every duplicate re-ran the whole
 * state→subscriber→DOM fan-out (measured: config_data ×3, sessions_list ×3,
 * models_list ×2 before the first frame settled).  Boot therefore requests the
 * single plane the push does not cover (archived sessions) and arms a fallback
 * that asks for the boot-critical planes only if the push never materialised.
 * The full `refreshAllData()` sweep is still used for mode switches, where the
 * fan-out cost is worth paying to guarantee no panel is left stale.
 */
export function pullBootData(): void {
  if (_bootFallbackTimer !== null) clearTimeout(_bootFallbackTimer);
  send({ type: "list_archived_sessions" });
  _bootFallbackTimer = setTimeout(() => {
    _bootFallbackTimer = null;
    const st = state.getState();
    if (!st.connected) return;
    if (!("startup_session_mode" in st.settings)) send({ type: "get_config" });
    // Only re-request when the push never materialised.  An *empty* session
    // list that arrived normally is a complete answer — re-requesting it
    // makes the backend rebuild and re-broadcast the whole listing for
    // nothing (and zero-session users hit this on every single launch).
    if (!_sessionsArrived) {
      send({ type: "list_sessions" });
      send({ type: "list_all_sessions" });
    }
    if (st.workspaces.length === 0) send({ type: "list_workspaces" });
    if (st.availableModels.length === 0) send({ type: "list_models" });
  }, BOOT_PUSH_FALLBACK_MS);
}

/** Initializes the stream layer with the chat/tools/permissions/settings controllers. */
export function init(c: Chat, t: Tools, p: Permissions, s?: Settings): void {
  chat = c;
  tools = t;
  permissions = p;
  _settings = s ?? null;
  // Replay a channel-mismatched sessions_list once the local mode catches
  // up with the backend's channel.  Subscribed once here (init runs exactly
  // once per page load); the callback is a no-op while nothing is stashed.
  state.subscribe(() => {
    const pending = _pendingChannelSessions;
    if (!pending) return;
    const cur = state.getState().workspaceMode === "iwork" ? "iwork" : "normal";
    if (pending.channel === cur) {
      _pendingChannelSessions = null;
      // Re-dispatch through the normal path: the channel filter now passes,
      // so this applies the list, tray cache and session-state sync exactly
      // as an in-order frame would have.
      handleEvent(pending.event);
    }
  }, ["workspaceMode"]);
}

/**
 * Resolves when the `model_validated` probe payload arrives for the given
 * correlation id.  Requests are keyed by `requestId` (falling back to the
 * model id for legacy callers) so several outstanding validations — one per
 * row in a provider save — can run in parallel and each response is routed
 * to its own waiter.  The key MUST be unique per request: model ids repeat
 * across providers (two providers may both list "deepseek-v4-flash"), so
 * keying by model id alone would orphan earlier waiters until they time out
 * and abort the whole provider save.
 *
 * The per-waiter timeout is intentionally a bit wider than the backend's
 * worst-case probe budget so a legitimately slow endpoint is not reported
 * as a client-side timeout.
 */
export function waitForModelValidation(modelId: string, requestId?: string): Promise<any> {
  const key = requestId || modelId;
  return new Promise((resolve, reject) => {
    const entry = { resolve, reject };
    _validationPending.set(key, entry);
    setTimeout(() => {
      if (_validationPending.get(key) === entry) {
        _validationPending.delete(key);
        reject(t("stream.validationTimedOut"));
      }
    }, 75000);
  });
}

/**
 * Boot gate — planes that must not compete with the boot-critical ones.
 *
 * The backend pushes every plane in one snapshot on connect (`_push_all_data`),
 * so these events are already queued behind config/sessions/models while the
 * splash is still up.  Applying them immediately spreads the state→subscriber→
 * DOM fan-out across exactly the window the splash is covering, which is what
 * made the first painted frame arrive while side panels were still churning.
 * Holding them back and applying them in one batch right after the reveal
 * keeps the boot path short without changing what any panel ends up showing
 * (the automation jobs and history planes are deliberately NOT deferred: the
 * automation view is a first-class startup surface and must be fully populated
 * when it is revealed — entering it must never trigger a per-entry refill).
 */
/**
 * Data-application priorities (lower = earlier). Streaming and boot-critical
 * frames are absent: they apply immediately through the ordered inbound
 * chain. Everything here is panel/cold data that must never compete with a
 * render frame — it is applied in idle slices instead.
 */
const COLD_PLANE_PRIORITY: Readonly<Record<string, number>> = {
  // Visible plugin chrome / tool cards: near-term, still idle-only.
  plugin_ui_sync: 1,
  plugin_changed: 1,
  market_catalog: 1,
  tools_updated: 1,
  skills_updated: 2,
  skills_list: 2,
  automation_jobs_list: 2,
  automation_job_history: 2,
  memory_list: 3,
  memory_graph: 3,
  library_tree: 3,
  usage_stats: 3,
  profile_data: 3,
  global_rules_list: 4,
  project_rules_list: 4,
  project_hooks_list: 4,
  archived_sessions_list: 4,
  memory_detail: 4,
  gitignore_content: 4,
};

function _planePriority(type: string): number {
  return COLD_PLANE_PRIORITY[type] ?? 3;
}

let _coldQueue: ServerEvent[] = [];
let _coldDraining = false;
let _coldScheduled = false;

function _scheduleColdDrain(): void {
  if (_coldDraining || _coldScheduled) return;
  // Never apply cold planes while the splash window is still settling; the
  // reveal path drains its own prioritised queue and then kicks this one.
  if (_bootGateHolding) return;
  _coldScheduled = true;
  const run = () => {
    _coldScheduled = false;
    _drainColdPlanes();
  };
  const ric = (window as any).requestIdleCallback as
    | ((cb: () => void, opts?: { timeout: number }) => number)
    | undefined;
  if (ric) ric(run, { timeout: 400 });
  else window.setTimeout(run, 16);
}

/** Apply queued cold planes in priority order, ~8ms per idle slice. */
function _drainColdPlanes(): void {
  if (_coldDraining) return;
  _coldDraining = true;
  // Stable sort: priority tiers first, arrival order within a tier.
  const batch = _coldQueue;
  _coldQueue = [];
  batch.sort((a, b) => _planePriority(a.type) - _planePriority(b.type));
  const started = performance.now();
  let index = 0;
  for (; index < batch.length; index++) {
    if (index > 0 && performance.now() - started >= 8) break;
    try {
      handleEvent(batch[index]);
    } catch (e) {
      console.warn(`[stream] cold plane failed for ${(batch[index] as any).type}:`, e);
    }
  }
  if (index < batch.length) {
    _coldQueue = batch.slice(index).concat(_coldQueue);
    _coldDraining = false;
    _scheduleColdDrain();
    return;
  }
  _coldDraining = false;
  if (_coldQueue.length > 0) _scheduleColdDrain();
}

const BOOT_DEFERRED_EVENTS: ReadonlySet<string> = new Set([
  "global_rules_list",
  "memory_list",
  "memory_graph",
  "library_tree",
  "usage_stats",
  "project_rules_list",
  "project_hooks_list",
  "archived_sessions_list",
  "profile_data",
  "skills_list",
  "memory_detail",
  "gitignore_content",
]);

let _bootGateHolding = false;
let _bootDeferredQueue: ServerEvent[] = [];
let _bootGateSafetyTimer: ReturnType<typeof setTimeout> | null = null;
/** True while the queued planes are being drained in idle slices, so a second
 *  release call (safety timer + reveal) cannot replay the same queue twice. */
let _bootGateDraining = false;

/**
 * Whether at least one sessions plane has arrived from the backend this
 * session.  The boot reveal waits on this (via `sessionsArrived()`) instead
 * of `sessionsList.length > 0`: a brand-new user has zero sessions, and
 * requiring a non-empty list made every one of their launches sit on the
 * splash for the full priority-data deadline even though the (empty) session
 * snapshot had long since landed.
 */
let _sessionsArrived = false;

/** True once a `sessions_list` or `sessions_all` plane has been applied. */
export function sessionsArrived(): boolean {
  return _sessionsArrived;
}

/**
 * Latest `sessions_list` whose channel did not match the local mode at
 * arrival time, waiting for the matching `workspaceMode` flip.  At most one
 * is kept: every frame carries the full authoritative list, so the newest
 * always supersedes an older stash.
 */
let _pendingChannelSessions: { channel: string; event: ServerEvent } | null = null;

/**
 * How long the boot gate may stay shut if the reveal never happens (e.g. the
 * first connect fails and the splash falls back to its error/retry state). The
 * gate must never be able to swallow a plane permanently.
 */
const BOOT_GATE_MAX_HOLD_MS = 5000;

/** Starts holding back non-critical planes until the window is revealed. */
export function holdNonCriticalPlanes(): void {
  _bootGateHolding = true;
  if (_bootGateSafetyTimer !== null) clearTimeout(_bootGateSafetyTimer);
  _bootGateSafetyTimer = setTimeout(() => {
    _bootGateSafetyTimer = null;
    releaseNonCriticalPlanes();
  }, BOOT_GATE_MAX_HOLD_MS);
}

/** Applies the planes held back during boot, in arrival order.
 *
 *  Drained in ~8 ms slices rather than in one loop: this runs exactly as the
 *  splash hides, and applying a dozen planes synchronously (memory graph,
 *  library tree, skills, usage stats, ...) blocked the first interactive
 *  frames -- the "window is up but every button is stuck" boot symptom.
 *  The gate stays closed while draining so a plane arriving now queues
 *  behind the older ones instead of overtaking them.
 */
export function releaseNonCriticalPlanes(): void {
  if (_bootGateSafetyTimer !== null) {
    clearTimeout(_bootGateSafetyTimer);
    _bootGateSafetyTimer = null;
  }
  if (!_bootGateHolding || _bootGateDraining) return;
  _bootGateDraining = true;
  // Apply visible/near-term planes before cold settings-only planes.
  _bootDeferredQueue.sort((a, b) => _planePriority(a.type) - _planePriority(b.type));
  let index = 0;
  const step = () => {
    // Reclaim the processed prefix instead of keeping one ever-growing array.
    if (index > 512) {
      _bootDeferredQueue = _bootDeferredQueue.slice(index);
      index = 0;
    }
    const started = performance.now();
    while (index < _bootDeferredQueue.length && performance.now() - started < 8) {
      const ev = _bootDeferredQueue[index++];
      try {
        // The gate must be OPEN while replaying: handleEvent re-queues any
        // event seen while the gate is held, so replaying with it closed
        // pushed every event back onto this queue until it hit
        // "RangeError: Invalid array length" -- the boot stall.
        const wasHolding = _bootGateHolding;
        _bootGateHolding = false;
        try {
          handleEvent(ev);
        } finally {
          _bootGateHolding = wasHolding;
        }
      } catch (e) {
        // One malformed deferred event must not discard the rest of the batch.
        console.warn(`[stream] deferred replay failed for ${(ev as any).type}:`, e);
      }
    }
    if (index < _bootDeferredQueue.length) {
      const ric = (window as any).requestIdleCallback as
        | ((cb: () => void, opts?: { timeout: number }) => number)
        | undefined;
      if (ric) ric(step, { timeout: 300 });
      else setTimeout(step, 0);
      return;
    }
    _bootDeferredQueue = [];
    _bootGateHolding = false;
    _bootGateDraining = false;
    // Boot is settled: cold planes queued meanwhile can now drain in idle.
    if (_coldQueue.length > 0) _scheduleColdDrain();
  };
  step();
}

/**
 * Resolve which model the refreshed list should mark active.
 *
 * A `config_patch` is diffed key by key, so `models` can arrive WITHOUT
 * `active_model_index` whenever the index itself did not change.  Reading
 * that absent key as `0` silently moved the active model to the FIRST entry
 * of the list -- and the selector's auto-fallback then persisted the switch,
 * so picking a reasoning level looked like it reset the user's model choice.
 * An omitted index therefore means "keep the current active model": hold the
 * index when the entry at that position is still the same model, otherwise
 * follow the model through the new list, and only fall back to index 0 when
 * the model is genuinely gone.
 *
 * @param cfg - The `config` field of a `config_data` / `config_patch` frame.
 * @param models - The refreshed flat model list.
 * @returns The index to activate within *models*.
 */
function resolveActiveModelIndex(cfg: any, models: any[]): number {
  if (typeof cfg.active_model_index === "number") {
    return models.length ? Math.max(0, Math.min(cfg.active_model_index, models.length - 1)) : 0;
  }
  const cur = state.getState();
  const prev = (cur.modelConfigs || [])[cur.activeModelIndex];
  if (!prev) return Math.min(cur.activeModelIndex || 0, Math.max(0, models.length - 1));
  const same = (m: any): boolean =>
    !!m && m.model_id === prev.model_id && m.backend_type === prev.backend_type && m.base_url === prev.base_url;
  if (same(models[cur.activeModelIndex])) return cur.activeModelIndex;
  const found = models.findIndex(same);
  return found >= 0 ? found : Math.min(cur.activeModelIndex || 0, Math.max(0, models.length - 1));
}

/**
 * Applies one config payload (full snapshot OR incremental patch) into the
 * state store.  Every section is guarded by the presence of its keys, so the
 * same applier serves both frame types: a full `config_data` always carries
 * every field, while a `config_patch` carries only the changed ones and the
 * absent keys are simply no-ops instead of resets.
 *
 * @param cfg - The `config` field of a `config_data` or `config_patch` frame.
 */
function applyConfigSnapshot(cfg: Record<string, unknown>): void {
  const cfgAny = cfg as any;
  if (cfg.models && Array.isArray(cfg.models)) {
    const idx = resolveActiveModelIndex(cfgAny, cfg.models as any[]);
    const cfgSid = typeof cfgAny.session_id === "string" ? cfgAny.session_id : "";
    const activeSid = state.getState().sessionId;
    state.setModelConfigs(cfg.models as any[], idx);
    // Pin the settled index onto the active session's snapshot so each
    // conversation remembers its own model across session switches. A
    // snapshot built from another session must not touch the active one.
    if (!cfgSid || cfgSid === activeSid) {
      state.recordSessionModel(idx);
      state.markModelIndexSynced(cfgSid);
    }
  }
  if (cfg.providers && Array.isArray(cfg.providers)) {
    state.setProviders(cfg.providers as any[]);
  }
  if (cfg.mcp_servers && Array.isArray(cfg.mcp_servers)) {
    // Normalize old-format MCP servers to new standard format
    const normalized = (cfg.mcp_servers as any[]).map(srv => {
      if (srv.type) return srv; // already new format
      return {
        name: srv.name,
        type: srv.transport || "stdio",
        ...(srv.transport === "http"
          ? { url: srv.server_url || "", timeout: srv.http_timeout ?? 60, headers: srv.headers || {} }
          : { command: srv.command || "", args: srv.args || [] }),
        ...(srv.cwd ? { cwd: srv.cwd } : {}),
        ...(srv.env ? { env: srv.env } : {}),
        ...(srv.disabled ? { disabled: true } : {}),
      };
    });
    state.setMcpServers(normalized);
  }
  if (cfg.enabled_skills && Array.isArray(cfg.enabled_skills)) {
    state.setEnabledSkills(cfg.enabled_skills as string[]);
  }
  if (cfg.available_skills && Array.isArray(cfg.available_skills)) {
    state.setSkillsList(cfg.available_skills as any[]);
  }
  if (cfg.model_catalog && typeof cfg.model_catalog === "object") {
    state.setModelCatalog(cfg.model_catalog as any);
  }
  if (cfg.mcp_catalog && typeof cfg.mcp_catalog === "object") {
    state.setMcpCatalog(cfg.mcp_catalog as any);
  }
  if (cfg.custom_slash_commands && Array.isArray(cfg.custom_slash_commands)) {
    state.setCustomCommands(cfg.custom_slash_commands as any[]);
    applyServerCommands(cfg.custom_slash_commands as any[]);
  }
  if (cfg.slash_commands && Array.isArray(cfg.slash_commands)) {
    applyProjectCommands(cfg.slash_commands as any[]);
  }
  // Restore persisted active_command from the snapshot (survives restart).
  // The snapshot is tagged with the session it was built from: only
  // mirror it when it describes the session actually being viewed,
  // otherwise another session's sticky command leaks into this one and
  // resurrects the chip after restarts / config refreshes.
  if ("active_command" in cfg) {
    const ac = (cfg as any).active_command;
    const snapshotSid = (cfg as any).session_id || "";
    const cmdApp = (window as any).__app;
    if (cmdApp && (!snapshotSid || snapshotSid === state.getState().sessionId)) {
      const hasCmd = !!(ac && ac.name);
      cmdApp._activeCommand = hasCmd
        ? { name: ac.name, prompt: ac.prompt, icon: ac.icon, title: ac.title }
        : null;
      // Re-render the toolbar command chip: without this the restored (or
      // cleared) command only shows up on the next unrelated chip update.
      cmdApp.updateChipState?.();
    }
  }
  if (("custom_slash_commands" in cfg) || ("slash_commands" in cfg)) {
    window.dispatchEvent(new CustomEvent("slash-commands-updated"));
  }
  if (cfg.sub_agents && Array.isArray(cfg.sub_agents)) {
    state.setSubAgents(cfg.sub_agents as any[]);
  }
  // Root for the editor tree in general mode: the backend-resolved
  // per-session files directory (sessions/<sid>/files). Stored keyed by
  // the session the snapshot was built from, so a config snapshot for
  // one session can never seed another session's editor view.
  const snapshotSid = (cfg as any).session_id || "";
  if (typeof cfg.session_files_dir === "string" && cfg.session_files_dir && snapshotSid) {
    state.setSessionFilesDir(snapshotSid, cfg.session_files_dir);
  }
  if (("system_prompt" in cfg) || ("default_specialty" in cfg) || ("permission_mode" in cfg) || ("max_turns" in cfg)) {
    state.setAgentConfig({
      system_prompt: (cfg.system_prompt as string) || "",
      specialty: (cfg.default_specialty as string) || "general",
      permission_mode: (cfg.permission_mode as string) || "default",
      max_turns: (cfg.max_turns as number) ?? 0,
    });
  }
  if (cfg.permission_settings) {
    state.setPermissionPolicies(normalizePermissionPolicies(cfg.permission_settings));
  }
  // Sync workspace mode from server config (don't override active workspace).
  // Skip the iWork branch when the renderer just closed a workspace: the
  // config snapshot may have been generated before the backend processed
  // close_workspace, and flipping back to iWork would re-enter a mode the
  // user already left.
  //
  // Only ever *enter* iWork from here, never leave it. A config snapshot
  // can lag behind a just-sent open_workspace (its `workspace_mode` is
  // derived from the backend's current `_workspace_path`, which is still
  // empty until the open is processed). Forcing "normal" on that stale
  // snapshot was dropping the user straight back to normal right after
  // they clicked the workspace tab. Genuine exits set "normal" explicitly
  // via exitWorkspaceMode / workspace_closed.
  if (("workspace_mode" in cfg) && !state.getState().activeWorkspace) {
    if (cfg.workspace_mode === "iwork" && getWorkspaceIntent() !== "close") {
      state.setWorkspaceMode("iwork");
    }
  }
  // Surface general settings from backend config into state.settings
  // so the settings panel shows the correct values on page refresh.
  const _settingsUpdate: Record<string, unknown> = {};
  const _generalKeys = [
    "shortcut_send_mode", "default_link_behavior",
    "auto_expand", "sub_agent_auto_open_view", "automation_auto_open_view",
    "startup_session_mode", "startup_session_behavior",
    "default_search_engine", "default_search_engine_url",
    "language", "language_preference",
    // Model pool + cycling strategy (pool settings dialog reads these).
    "model_pool_enabled", "model_pool_fallback_enabled",
    "model_pool_cycle_order", "model_pool_cooldown_ms",
    "model_pool_recycle_wait_ms", "model_pool_max_fallbacks",
    // Auxiliary-panel display preferences (assist panel).
    "terminal_font_size", "terminal_cursor_blink", "terminal_scrollback",
    "terminal_font_family", "terminal_line_height", "terminal_cursor_style",
    "terminal_cursor_inactive", "terminal_bold_bright",
    "terminal_bell", "terminal_copy_on_select", "terminal_right_click",
    "terminal_cols_mode", "terminal_shell", "terminal_start_dir",
    "terminal_cursor_width", "terminal_font_weight", "terminal_letter_spacing",
    "terminal_min_contrast", "terminal_convert_eol", "terminal_word_separator",
    "editor_font_size", "editor_word_wrap", "editor_minimap",
    "editor_line_numbers", "editor_tab_size", "editor_detect_indentation",
    "editor_render_whitespace", "editor_cursor_blink", "editor_font_ligatures",
    "editor_sticky_scroll", "editor_auto_save",
    "editor_tab_indent", "editor_use_spaces", "editor_word_wrap_column",
    "editor_folding", "editor_glyph_margin", "editor_format_on_paste",
    "editor_rulers", "editor_cursor_style",
    "editor_cursor_blinking",
    "review_split_view", "review_collapse_unified",
    "review_wrap", "review_word_diff", "review_hide_whitespace", "review_full_file",
    "review_default_filter", "review_max_lines",
    "browser_homepage", "browser_record_history", "browser_do_not_track",
    "browser_deny_site_permissions",
    "browser_block_popups", "browser_zoom_percent", "browser_ask_before_download",
    "browser_download_dir", "browser_block_third_party_cookies", "browser_fresh_session",
    "browser_force_light_page", "browser_identity", "browser_history_limit",
    // Device permissions (general panel).  `device_context_providers` is
    // an array, so the `!== ""` guard below keeps even an empty list.
    "device_context_enabled", "device_context_providers",
  ] as const;
  for (const key of _generalKeys) {
    const val = cfg[key];
    if (val !== undefined && val !== null && val !== "") {
      _settingsUpdate[key] = val;
    }
  }
  // Ensure startup_session_mode is always set so the startup mode
  // subscribe fires -- but only for FULL snapshots: a patch that did not
  // touch the startup mode must not inject the "normal" default over the
  // user's persisted choice.
  if (!("startup_session_mode" in cfg)) {
    delete _settingsUpdate["startup_session_mode"];
  } else {
    _settingsUpdate["startup_session_mode"] = _settingsUpdate["startup_session_mode"] || cfg["startup_session_mode"] || "normal";
  }
  if (Object.keys(_settingsUpdate).length > 0) {
    state.setSettings({ ...state.getState().settings, ..._settingsUpdate });
  }
  // Note: locale is a frontend preference stored in localStorage.
  // The backend's language field is only synced for settings display;
  // do NOT call setLocale() here or it would override the user's choice
  // when the backend (default zh) pushes config asynchronously.
  // Sync keybinds from backend config.  Patches that do not carry the
  // keybinds field skip this entirely -- the localStorage fallback below
  // exists for corrupted FULL snapshots, not for absent patch fields.
  if ("keybinds" in cfg) {
    if (cfgAny.keybinds && typeof cfgAny.keybinds === "object" && Array.isArray(cfgAny.keybinds.keybinds)) {
      // Backend data may still carry ids whose feature was removed (the
      // encrypted file is only rewritten when defaults change shape).
      // Prune so removed shortcuts never reach the store or the panel.
      state.setSettings({ ...state.getState().settings, keybinds: pruneStaleKeybinds(cfgAny.keybinds) });
    } else if (Array.isArray(cfgAny.models)) {
      // Full-snapshot path only: backend sent no keybinds or corrupted data
      try {
        const saved = localStorage.getItem("encre_keybinds");
        if (saved) {
          const parsed = JSON.parse(saved);
          if (parsed && typeof parsed === "object" && Array.isArray(parsed.keybinds)) {
            state.setSettings({ ...state.getState().settings, keybinds: pruneStaleKeybinds(parsed) });
          }
        }
      } catch { /* ignore */ }
    }
  }
  // Sync adapter_* keys from backend config into settings (for gateway panel)
  const _adapterKeys = Object.keys(cfgAny).filter(k => k.startsWith("adapter_"));
  if (_adapterKeys.length > 0) {
    const _adapterSettings: Record<string, unknown> = {};
    for (const k of _adapterKeys) {
      _adapterSettings[k] = cfg[k];
    }
    state.setSettings({ ...state.getState().settings, ..._adapterSettings });
  }
}

/** Routes a single server event into the state store and chat view. */
export function handleEvent(event: ServerEvent): void {
  const _perfNow = performance.now();
  const _perfType = (event as any).type;
  if (_perfType === "config_data" || _perfType === "sessions_list" || _perfType === "sessions_all" || _perfType === "models_list" || _perfType === "workspaces_list" || _perfType === "session_ready") {
    console.log("[perf] received %s at %dms", _perfType, Math.round(_perfNow - (window as any).__perfStart || _perfNow));
  }
  // Side-panel planes are held back while the splash is up (see BOOT_DEFERRED_EVENTS).
  if (_bootGateHolding && BOOT_DEFERRED_EVENTS.has(_perfType)) {
    _bootDeferredQueue.push(event);
    return;
  }
  // Cold/panel data: never apply inside an interactive frame. Queued and
  // drained in idle slices, lowest priority first; the boot drain bypasses
  // this because it already slices and prioritises its own queue.
  if (
    !_bootGateDraining &&
    !_coldDraining &&
    COLD_PLANE_PRIORITY[_perfType] !== undefined
  ) {
    _coldQueue.push(event);
    _scheduleColdDrain();
    return;
  }
  // Each config_data frame carries its own session_id + session_files_dir.
  // The generic session-scope filters further below reject config frames
  // whose session doesn't match the active one (or while no session is
  // active), which orphans the per-session files directory root used by
  // the editor tree.  Record it by the frame's own sid so the map stays
  // complete; keying on the frame's session_id cannot cross sessions.
  if (_perfType === "config_data") {
    const cfg = (event as any).config as
      | { session_id?: string | null; session_files_dir?: string | null }
      | undefined;
    const fsSid = (cfg?.session_id as string) || ((event as any).session_id as string) || "";
    if (fsSid && cfg?.session_files_dir) {
      state.setSessionFilesDir(fsSid, cfg.session_files_dir);
    }
  }
  if (event.type === "plan_review") {
    // Breadcrumb before the session filters: a review card that fails to
    // appear can be dropped either here or by the timeline anchor, and neither
    // was visible before -- see the matching "[review] card suppressed" log.
    console.log("[stream] plan_review inbound: status=", (event as any).status,
      "session_id=", (event as any).session_id, "active=", state.getState().sessionId,
      "review=", (event as any).review?.review_id);
  }
  if (_requiresExplicitSessionId(event.type) && !_hasSessionId(event as { session_id?: string | null })) {
    return;
  }
  // Skip streaming events from background sessions -- they carry a session_id
  // that does not match the currently active session.  session_ready is exempt
  // because it IS the message that sets the current session_id.
  if (event.type !== "session_ready") {
    if (_shouldRejectSessionScopedEvent(event)) {
      return;
    }
  } else {
    // session_ready: only process if it matches the last user-requested session
    const eventSid = (event as any).session_id;
    const eventRequestId = (event as any).request_id as string | undefined;
    if (_requestedSessionRequestId && !eventRequestId) {
      console.log("[stream] REJECT session_ready missing requestId requested=%s sid=%s", _requestedSessionRequestId, eventSid);
      return;
    }
    if (_requestedSessionRequestId && eventRequestId && eventRequestId !== _requestedSessionRequestId) {
      console.log("[stream] REJECT session_ready requestId=%s requested=%s", eventRequestId, _requestedSessionRequestId);
      return;
    }
    if (_requestedSessionId && eventSid && eventSid !== _requestedSessionId) {
      console.log("[stream] REJECT session_ready eventSid=%s requested=%s", eventSid, _requestedSessionId);
      return;
    }
    // Guard against a WebSocket reconnect (or a stray startup reply) yanking
    // the user away from the session they are already viewing.  Once we have
    // an active sessionId, only accept a session_ready that explicitly
    // matches it (or is a genuine user-initiated resume/new).  Unsolicited
    // session_ready events for a different session are dropped.
    //
    // WebSocket reconnect recovery: when the backend creates a new session
    // on the fresh connection and sends session_ready with a different sid,
    // clear the stale pending-request state (which would block all future
    // session_ready events) and send a resume for the session we were
    // viewing.  The backend will load it from the session manager and send
    // a fresh session_ready with the correct sid + matching request_id.
    const _activeReadySid = state.getState().sessionId;
    if (
      _activeReadySid &&
      eventSid &&
      eventSid !== _activeReadySid &&
      !_requestedSessionRequestId
    ) {
      console.log("[stream] REJECT session_ready stray sid=%s active=%s — reconnect recovery: resume active session", eventSid, _activeReadySid);
      // Clear stale pending request state from before the disconnect.
      _requestedSessionId = "";
      _requestedSessionRequestId = "";
      // Re-request the session we were viewing so the backend loads it.
      send({ type: "resume", session_id: _activeReadySid, request_id: crypto.randomUUID() });
      return;
    }
  }
  switch (event.type) {
    case "session_ready": {
      // New session — increment generation to discard stale tool events.
      // Also clear _activeStreamSessionId because the previous stream may
      // belong to a different session (e.g. user clicked a different
      // session while one was running).  Without this reset, the old
      // stream sid would race against the new session and could either
      // (a) lock out legitimate events for the new session, or
      // (b) make the rejected-by-_shouldRejectSessionScopedEvent check
      //     miss the swap and let the old session's events render here.
      _sessionGeneration++;
      const previousSid = state.getState().sessionId;
      const nextSid = event.session_id || "";
      // The backend answered the resume: the loading placeholder (if any)
      // can be considered served from here on.
      _pendingResumes.delete(nextSid);
      const switched = previousSid && previousSid !== nextSid;
      // session_ready is an authoritative load and does NOT apply a pending
      // rollback edit, so a marker left over from an operation in the
      // previous session would only ever cut the wrong list later.
      state.clearPendingRollbackEdit();
      // When this switch is the result of the user opening a *session-backed*
      // sub-agent from its parent tool-call card, the breadcrumb pushed by
      // __openSubAgentView already records the parent session (and the exact
      // tool-call id) so the user can navigate back and switch between sibling
      // sub-agents.  Clearing it here would strand the user inside the
      // sub-agent session with no way back to the parent and make two
      // same-type sub-agents indistinguishable during navigation.  Preserve
      // the sub-agent view + breadcrumb for that specific requested switch.
      const pendingSubAgent = (window as any).__pendingSubAgentView;
      const isPendingSubSwitch = !!(pendingSubAgent && pendingSubAgent.sessionId === nextSid);
      // Drop the stale active-stream pointer unless the new session is
      // explicitly continuing the same run.  Also discard any sub-agent view
      // state from the previous session so it cannot bleed into the new one.
      if (switched) {
        _activeStreamSessionId = "";
        _toolCallGeneration = {};
        if (!isPendingSubSwitch) {
          state.setSubAgentView(null);
          state.clearSubAgentBreadcrumb();
        }
      }
      console.log("[stream] session_ready received, messages:", event.messages?.length ?? 0, "session_id:", event.session_id, "gen:", _sessionGeneration);
      // In temp-chat mode every session that becomes active is ephemeral:
      // mark its id immediately so subsequent renames, broadcasts, or
      // stream events can never surface it in the sidebar.
      if (state.getState().tempChat && event.session_id) {
        state.markTempChatSession(event.session_id);
      }
      // External-channel sessions (iclaw, telegram, qq, ...) are openable
      // read-only from global search but must never appear in the
      // normal/iwork sidebar. The backend reports the session's channel
      // on session_ready; poison the id accordingly.
      const readyChannel = (event as { channel?: string }).channel || "";
      if (
        event.session_id &&
        readyChannel &&
        readyChannel !== "normal" &&
        readyChannel !== "iwork"
      ) {
        state.markExternalChannelSession(event.session_id);
      }
      state.setSessionId(event.session_id);
      // The resume reply carries this session's backend-resolved files dir:
      // record it so the session-inner editor tree roots at the right folder
      // immediately, without a config re-pull after the session switch.
      const readyFilesDir = (event as { session_files_dir?: string }).session_files_dir;
      if (readyFilesDir) state.setSessionFilesDir(event.session_id, readyFilesDir);
      // The backend is the single source of truth for a session's model: the
      // resume reply carries the target session's own active index, so the
      // indicator always reflects what this conversation will actually run on.
      const backendModelIdx = (event as { active_model_index?: number }).active_model_index;
      if (typeof backendModelIdx === "number") {
        const models = state.getState().modelConfigs || [];
        state.setActiveModelIndex(models.length
          ? Math.max(0, Math.min(backendModelIdx, models.length - 1))
          : backendModelIdx);
      }
      // Reset persistent mode on session switch - backend will re-send mode_changed if the session has a mode
      const _app = (window as any).__app;
      if (_app) {
        _app._persistentMode = "";
        _app._currentChipMode = "";
        _app._activeCommand = null;
        // Clear input box on session switch so stale text/chip from the
        // previous session doesn't leak into the new one.
        const _input = document.getElementById("prompt-input") as HTMLElement | null;
        if (_input) { _input.innerHTML = ""; _input.style.height = "56px"; }
        _app.updateChipState();
        _app.updatePlaceholder();
        _app.updateSendButton();
        // Restore the unsent draft the user left in this session before
        // switching away.  The input was just cleared above so the draft
        // content (text/chip) comes back cleanly for the resumed session.
        _app.restoreDraft?.(event.session_id);
      }
      const evState = (event as any).state || "idle";
      if (evState === "idle") {
        _activeStreamSessionId = "";
      } else {
        _activeStreamSessionId = (event.session_id || "");
      }
      state.setConnected(true);
      state.setSessionState(evState, event.session_id);
      if (event.messages && event.messages.length > 0) {
        // Tail-first open: the backend ships only the most recent turns in
        // this frame; the absolute user-message ordinal continues where the
        // (still incoming) backfill chunk leaves off, keeping the per-user
        // storage keys (fr:<sid>:<idx>) aligned with full-list numbering.
        state.loadSessionMessages(event.messages, event.session_id, {
          userIndexOffset: (event as any).earlier_user_count || 0,
        });
        console.log("[stream] loadSessionMessages done, state.messages.length:", state.getState().messages.length);
        // The tail paints now; the older history lands via the generic
        // sharded-topic channel and must be prepended exactly once, into
        // THIS conversation. Any topic buffers from the previous session
        // are dead on arrival -- drop them so they can never leak memory
        // or fire into the wrong conversation later.
        _pendingBackfillSid = (event as any).has_earlier_history ? (event.session_id || "") : "";
        for (const topic of Array.from(_topicBuffers.keys())) {
          if (topic.startsWith("session:")) _topicBuffers.delete(topic);
        }
        // Repopulate _toolCallGeneration for the current session's tool calls.
        // This is critical for running sub-agents: after switching back to this
        // session, subsequent tool_progress events must pass the generation check
        // to update subAgentMessages. Without this, the cards show merged/empty.
        const currentMessages = state.getState().messages;
        let tcCount = 0;
        for (const msg of currentMessages) {
          for (const tc of msg.toolCalls) {
            // Register both renderer and backend ids. Resumed sessions may
            // receive progress using either id while the card itself uses the
            // renderer-facing client id.
            _toolCallGeneration[tc.id] = _sessionGeneration;
            if (tc.backendId) _toolCallGeneration[tc.backendId] = _sessionGeneration;
            tcCount++;
            if (tc.name === "agent") {
              console.log("[stream] Registered agent tc.id:", tc.id, "hasSubAgentMsg:", !!tc.subAgentMessages, "subAgentMsgCount:", tc.subAgentMessages?.length ?? 0);
            }
          }
        }
        console.log("[stream] Repopulated _toolCallGeneration with", tcCount, "tool calls, sessionGen:", _sessionGeneration, "tc_ids:", Object.keys(_toolCallGeneration).filter(k => k.startsWith("call_")).join(","));
      } else {
        state.clearMessages(event.session_id);
      }
      // A session-backed sub-agent is opened from its parent tool call.  The
      // session swap above deliberately clears normal sub-agent state, so
      // restore that view only for the matching, user-requested child reply.
      // (``pendingSubAgent`` / ``isPendingSubSwitch`` were resolved above.)
      if (isPendingSubSwitch) {
        const toolCall = {
          ...pendingSubAgent.toolCall,
          subAgentMessages: state.getState().messages,
          status: (event as any).state === "running" ? "running" : "done",
        };
        state.setSubAgentView(toolCall);
        (window as any).__activeSubAgentSessionId = event.session_id;
        (window as any).__pendingSubAgentView = undefined;
      }
      if (event.plan_items) {
        state.setPlanItems(event.plan_items, event.session_id);
      } else {
        state.setPlanItems([], event.session_id);
      }
      if (event.artifacts) {
        state.setArtifacts(event.artifacts, event.session_id);
      }
      if (event.references) {
        state.setReferences(event.references, event.session_id);
      }
      // Rebuild the review card from the session's persisted submit_review
      // record; clear the slot when the session has none so a stale card
      // from an earlier resume of this same session cannot linger.
      {
        const pr = (event as any).plan_review;
        state.updatePlanReview(pr && pr.review_id ? pr : null, event.session_id);
      }
      // Restore branch state
      if (event.branches && event.active_branch_id) {
        state.setBranches(event.branches, event.active_branch_id, event.session_id);
      }
      // Render on the next frame instead of synchronously: the state writes
      // above already queued a render, and a restored transcript used to be
      // parsed (markdown + highlight.js) in this event handler, freezing the
      // title bar, sidebar and composer together. Larger sessions first paint
      // as plain text and are upgraded to markdown during idle time.
      if (chat) {
        const restored = event.messages?.length ?? 0;
        chat.deferMarkdown = restored > 20;
        chat.requestRender();
        if (restored > 20) chat.beginMarkdownUpgrade();
      }
      (window as any).__sessionInner?.requestRender?.();
      _requestedSessionId = "";
      _requestedSessionRequestId = "";
      break;
    }

    case "data_begin": {
      handleDataBegin((event as any).topic, (event as any).meta || {}, (event as any).total_shards || 0);
      break;
    }

    case "data_shard": {
      handleDataShard((event as any).topic, (event as any).seq, (event as any).items);
      break;
    }

    case "data_end": {
      handleDataEnd((event as any).topic);
      break;
    }

    case "text_delta": {
      // Defense-in-depth: even though the outer _shouldRejectSessionScopedEvent
      // already filters mismatched events, also guard inside the handler so a
      // token from session 1 can never be appended to session 2's snapshot
      // (e.g. if the outer filter is bypassed by a future code change).
      const _tdSid = _eventSessionId(event);
      const _activeTdSid = state.getState().sessionId;
      if (_tdSid && _activeTdSid && _tdSid !== _activeTdSid) break;
      state.clearRetryNotice(_tdSid || _activeTdSid);
      _ensureAssistantMessage(_tdSid);
      state.appendTextDelta(event.text, _tdSid);
      // Deltas arrive per-token (dozens per second). A synchronous render on
      // every one saturates the main thread and makes every CSS transition
      // stutter — coalesce into one render per frame instead.
      chat?.requestRender();
      _syncSessionEntry(_activeTdSid, state.getState(), true);
      break;
    }

    case "thinking_delta": {
      const _thSid = _eventSessionId(event);
      const _activeThSid = state.getState().sessionId;
      if (_thSid && _activeThSid && _thSid !== _activeThSid) break;
      state.clearRetryNotice(_thSid || _activeThSid);
      _ensureAssistantMessage(_thSid);
      state.appendThinkingDelta(event.text, _thSid);
      chat?.requestRender();
      _syncSessionEntry(_activeThSid, state.getState(), true);
      break;
    }

    case "tool_call_start": {
      // Track which session generation this tool call belongs to
      _toolCallGeneration[event.id] = _sessionGeneration;
      state.clearRetryNotice(_eventSessionId(event));
      _ensureAssistantMessage(_eventSessionId(event));
      // Thinking phase ends when tool calls begin
      state.finishThinking();
      // Find existing (may have been auto-created by deltas arriving first) or create
      let tc = state.findToolCall(event.id, _eventSessionId(event));
      if (tc) {
        tc.name = event.name || tc.name;
        tc.status = "pending";
      } else {
        state.recordSegment("tool", event.id, _eventSessionId(event));
        state.addToolCall({
          id: event.id,
          name: event.name,
          params: {},
          status: "pending",
        }, _eventSessionId(event));
      }
      // Auto-open the sub-agent view when the main agent spawns a sub-agent,
      // so the user can see the inner process live (matching the spec the
      // main process produces when it streams). The view is still closable
      // and re-openable via the agent card. Gated by the
      // ``sub_agent_auto_open_view`` setting (default ON) — when disabled
      // the view only opens if the user clicks the agent card manually.
      //
      // Multi-call guard: when the main agent spawns MULTIPLE sub-agents
      // in a single turn (e.g. two parallel ``agent`` tool calls), the
      // second tool_call_start would clobber the first's view.  We only
      // auto-open the FIRST one in a turn; the rest must be opened
      // explicitly via the agent card.  A user who has already navigated
      // away (setSubAgentView was set to a different tc) also wins.
      if (event.name === "agent") {
        const tcf = state.findToolCall(event.id, _eventSessionId(event));
        if (tcf) {
          const raw = state.getState().settings?.sub_agent_auto_open_view;
          const autoOpen = raw === undefined ? true : state.isEnabled(raw);
          const cur = state.getState().subAgentView;
          // Only auto-open if no view is currently active.  If the user
          // already has a sub-agent open (or another tool_call_start
          // just opened one), do NOT overwrite their selection.
          //
          // Dismiss guard: once the user has explicitly navigated back to
          // the main agent during this run (breadcrumb / close), respect
          // that choice -- do NOT keep yanking them back into a freshly
          // spawned sub-agent on every subsequent ``agent`` tool_call_start.
          const dismissed = !!(window as any).__subAgentAutoOpenDismissed;
          if (autoOpen && cur == null && !dismissed) {
            state.setSubAgentView(tcf);
            chat?.renderForce?.();
          }
        }
      }
      // Auto-open sidebar + browser tab when the model starts using the
      // browser tool, so the user can see what the model is doing.
      if (event.name === "browser") {
        window.dispatchEvent(new CustomEvent("browser-tool-call"));
        // Announce "Agent is operating" once per turn: the lock engages on
        // the FIRST browser call and stays up across every consecutive one.
        // Re-announcing mid-turn would reset a user takeover in main.
        if (!_browserControlAnnounced) {
          _browserControlAnnounced = true;
          window.electronAPI?.agentControlUpdate?.(true);
        }
      }
      tools?.requestRender();
      // A new tool item only extends the timeline tail, which the regular
      // render path patches in place. Forcing a full transcript rebuild here
      // re-parsed every earlier turn on every single tool call.
      chat?.requestRender();
      break;
    }

    case "tool_call_delta": {
      // Reject stale tool events from previous sessions
      if (_toolCallGeneration[event.id] !== undefined && _toolCallGeneration[event.id] !== _sessionGeneration) break;
      // Accumulate params bytes into the tool call — create if not yet seen
      let tc = state.findToolCall(event.id, _eventSessionId(event));
      if (!tc) {
        state.addToolCall({
          id: event.id,
          name: "",
          params: {},
          status: "running",
        }, _eventSessionId(event));
        state.recordSegment("tool", event.id, _eventSessionId(event));
        tc = state.findToolCall(event.id, _eventSessionId(event));
      }
      if (tc) {
        const key = event.key;
        tc.params[key] = (tc.params[key] ?? "") + event.value;
        tc.status = "running";
        tools?.requestRender();
        // Param fragments stream just like text tokens — throttle the chat
        // re-render the same way so dense tool_call_delta bursts don't
        // freeze transitions.
        if (tc.name) chat?.requestRender();
      }
      break;
    }

    case "question_request": {
      // The question tool blocks BEFORE tool_call_end fires — end only lands
      // after the user answers, so the raw `{` fragment placeholder would sit
      // on screen for the whole wait.  The request carries the fully parsed
      // questions: fill the tool call's params directly and re-render.
      if (_toolCallGeneration[event.tool_call_id] !== undefined && _toolCallGeneration[event.tool_call_id] !== _sessionGeneration) break;
      const tc = state.findToolCall(event.tool_call_id, _eventSessionId(event));
      if (tc && Array.isArray(event.questions) && event.questions.length > 0) {
        tc.params = { questions: event.questions };
        tc.status = "running";
        chat?.renderForce?.();
      }
      break;
    }

    case "tool_call_end": {
      if (_toolCallGeneration[event.id] !== _sessionGeneration) break;
      // Try to parse accumulated JSON params into a structured form
      const tc = state.findToolCall(event.id, _eventSessionId(event));
      if (tc) {
        try {
          const rawArgs = tc.params["arguments"];
          if (typeof rawArgs === "string" && rawArgs) {
            tc.params = JSON.parse(rawArgs) as Record<string, unknown>;
          }
        } catch {
          // Keep raw string params if JSON parse fails
        }
      }
      // Parsing the accumulated JSON changed tool params, which the summary
      // line updates in place. Only the question card derives its markup from
      // params (it cannot be patched incrementally), so only it needs a
      // structural rebuild.
      if (tc?.name === "question") chat?.renderForce?.();
      else chat?.requestRender();
      break;
    }

    case "tool_progress":
      if (_toolCallGeneration[event.id] !== _sessionGeneration) {
        break;
      }
      const progressPatch: Partial<import("./types.js").ToolCallState> = {
        status: event.status === "done" ? "done" : "running",
      };
      if (event.sub_agent_messages && event.sub_agent_messages.length > 0) {
        const current = state.findToolCall(event.id, _eventSessionId(event));
        progressPatch.subAgentMessages = state.mergeSubAgentMessages(
          current?.subAgentMessages,
          state.restoreMessages(event.sub_agent_messages),
        );
      }
      if (typeof event.content === "string" && event.content.length > 0) {
        const current = state.findToolCall(event.id, _eventSessionId(event));
        progressPatch.result = (current?.result ?? "") + event.content;
      }
      if ((event as any).sub_agent_session_id) {
        progressPatch.subAgentSessionId = (event as any).sub_agent_session_id;
      }
      state.updateToolCall(event.id, progressPatch, _eventSessionId(event));
      // Keep the sub-agent view's snapshot in sync with the latest tool
      // call state so the in-progress timeline keeps streaming.
      _syncSubAgentView(event.id);
      tools?.requestRender();
      // Progress ticks can arrive in dense bursts; a full renderForce each
      // time is what stalled animations during tool runs. The plain render
      // path picks up status changes incrementally and the render key
      // forces a rebuild exactly when structure (task dividers) changes.
      chat?.requestRender();
      break;

    case "tool_result":
      if (_toolCallGeneration[event.id] !== undefined && _toolCallGeneration[event.id] !== _sessionGeneration) break;
      const resultPatch: Partial<import("./types.js").ToolCallState> = {
        result: event.content,
        isError: event.is_error,
        errorCode: (event as any).error_code || "",
        errorCategory: (event as any).error_category || "",
        // Stamped once, here: the fault card renders on every chat repaint, so
        // a render-time clock would show a different time each time.
        ...(event.is_error ? { errorAt: (event as any).occurred_at || Date.now() } : {}),
        status: "done",
      };
      if (event.sub_agent_messages && event.sub_agent_messages.length > 0) {
        const current = state.findToolCall(event.id, _eventSessionId(event));
        resultPatch.subAgentMessages = state.mergeSubAgentMessages(
          current?.subAgentMessages,
          state.restoreMessages(event.sub_agent_messages),
        );
      }
      if ((event as any).sub_agent_session_id) {
        resultPatch.subAgentSessionId = (event as any).sub_agent_session_id;
      }
      state.updateToolCall(event.id, resultPatch, _eventSessionId(event));
      _syncSubAgentView(event.id);
      // Note: a browser tool_result does NOT release the agent-control lock
      // — the turn may issue more browser calls; release is turn-scoped.
      tools?.requestRender();
      // Only this card's markup changed (body / error state / info source).
      // Patch that single card rather than repainting every earlier turn.
      if (!chat?.patchToolItem?.(event.id)) chat?.renderForce?.();
      _syncSessionEntry(state.getState().sessionId, state.getState());
      break;

    case "permission_request":
      if (permissionResolve) {
        permissionResolve(false);
        state.setSessionState("idle", _eventSessionId(event));
      }
      // Pause-like state: the agent loop is blocked awaiting user consent,
      // so flip the session's breathing light to yellow (sidebar + tray).
      state.setSessionState("awaiting_approval", _eventSessionId(event));
      permissions?.show(
        event.tool_name,
        event.reason,
        (decision: "allow" | "allow_session" | "deny") => {
          send({
            type: "respond_permission",
            tool_name: event.tool_name,
            decision: decision !== "deny",
            remember: decision === "allow_session",
          });
          permissionResolve = null;
          permissions?.hide();
          // The consent gate just released the agent loop, so the session is
          // running again -- NOT idle.  Setting "idle" here made the sidebar
          // light disappear and the turn look finished while the agent was
          // still working on the very tool the user just approved.
          state.setSessionState("running", _eventSessionId(event));
        },
        document.getElementById("main-content") ?? undefined,
      );
      // Ensure the main chat area updates immediately so users see the
      // tool call card with a "pending" indicator rather than a frozen UI.
      chat?.renderForce?.();
      break;

    case "engine_install_request":
      // Backend raised a request for the user (NOT the LLM) to
      // provision a browser engine.  Pop the dialog chain; the
      // choice is shipped back via the engine_install_response
      // message so the original tool action resumes.
      void handleEngineInstallRequest({
        request_id: (event as any).request_id,
        engine: (event as any).engine,
        title: (event as any).title,
        body: (event as any).body,
        hint: (event as any).hint,
        options: (event as any).options,
      }, document.getElementById("main-content") ?? undefined);
      break;

    case "engine_install_progress":
      handleEngineInstallProgress({
        request_id: (event as any).request_id,
        pct: (event as any).pct,
        message: (event as any).message,
        sub_message: (event as any).sub_message,
        indeterminate: (event as any).indeterminate,
        status: (event as any).status,
        message_code: (event as any).message_code,
        message_args: (event as any).message_args,
        sub_message_code: (event as any).sub_message_code,
        sub_message_args: (event as any).sub_message_args,
      }, document.getElementById("main-content") ?? undefined);
      break;

    case "finish": {
      // Turn boundary — release the browser lock unconditionally, BEFORE any
      // session-scoped early return (a background session finishing must
      // release too, or the overlay would stick). This is the safety net
      // that keeps the lock from outliving a turn that dies mid-tool
      // (interrupt / error) and whose browser tool_result never lands.
      _browserControlAnnounced = false;
      window.electronAPI?.agentControlUpdate?.(false);
      if (!_hasSessionId(event)) break;
      state.clearRetryNotice(_eventSessionId(event));
      if (event.reason === "error" || event.error) {
        console.log("[stream] finish with error:", event.reason, event.error);
      }
      // Background session finished — update its snapshot's running flag
      // so the state is correct when the user switches back, but don't
      // touch the active session's UI state.
      if (event.session_id && event.session_id !== state.getState().sessionId) {
        state.setSessionState("idle", event.session_id);
        break;
      }
      _activeStreamSessionId = "";
      state.setSessionState("idle", _eventSessionId(event));
      // A turn that finishes while boot/session deferral is still running
      // would leave its completed blocks as plain text; upgrade them in the
      // same idle pipeline as the restored history.
      if (chat?.deferMarkdown) chat.beginMarkdownUpgrade();
      const sid = _eventSessionId(event);
      // For error finishes without a streaming assistant (e.g. API failed
      // immediately, no text_delta received), create a placeholder so the
      // error card renders on the correct turn rather than the previous one.
      // Same user-boundary rule as _ensureAssistantMessage: a stale streaming
      // assistant from an earlier turn must not suppress the placeholder.
      if (event.reason === "error" || event.error) {
        if (!state.getLastAssistantMessage(sid)) {
          state.startAssistantMessage(sid);
        }
      }
      // Store server-side message ID before finishing — emit() in
      // finishAssistantMessage will trigger segment cache save with this set.
      const lastMsg = state.getLastAssistantMessage(sid);
      if (lastMsg) {
        if (event.assistant_message_id) {
          lastMsg.serverId = event.assistant_message_id;
        }
        // Capture turn status card data from finish event
        if (event.reason === "error" || event.error) {
          lastMsg.errorMessage = event.error || "";
          lastMsg.errorCode = (event as any).error_code || (event.reason === "error" ? "execution_error" : "unknown");
          (lastMsg as any).errorCategory = (event as any).error_category || "";
        } else if (event.reason === "interrupted" || event.reason === "cancelled") {
          if (event.reason === "cancelled") {
            lastMsg.cancelledText = t("chat.abnormalInterruption");
          } else {
            lastMsg.interruptedReason = event.error || event.reason;
          }
        } else if (event.reason === "complete" || event.reason === "ok" || event.reason === "stop") {
          lastMsg.turnStatusText = t("chat.taskComplete");
        }
      }
      if (event.usage) {
        const u = event.usage as Record<string, unknown>;
        const input = typeof u.input_tokens === "number" ? u.input_tokens : 0;
        const output = typeof u.output_tokens === "number" ? u.output_tokens : 0;
        const total = typeof u.total_tokens === "number" ? u.total_tokens : input + output;
        state.setTokenUsage({
          input_tokens: input,
          output_tokens: output,
          total_tokens: total,
        }, sid);
        state.finishAssistantMessage({
          input_tokens: input,
          output_tokens: output,
          total_tokens: total,
        }, sid);
      } else {
        state.finishAssistantMessage(undefined, sid);
      }
      const btnStop = document.getElementById("btn-stop");
      btnStop?.classList.remove("cancelling");
      if (btnStop) btnStop.style.pointerEvents = "";
      _ensureAssistantMessage();
      // If compaction occurred during this turn, reload the session
      // messages from the backend so the frontend's state matches.
      // Without this, compacted-away messages stay in the frontend
      // and cause "Message not found" errors on rollback.
      if ((event as any).compacted && (event as any).messages) {
        state.loadSessionMessages((event as any).messages, sid);
      }
      // Summary-panel data is persisted independently of compaction on the
      // backend; restore it so entangled entries survive compaction.
      if ((event as any).compacted && (event as any).plan_items) {
        state.setPlanItems((event as any).plan_items, sid);
      }
      if ((event as any).compacted && (event as any).artifacts) {
        state.setArtifacts((event as any).artifacts, sid);
      }
      if ((event as any).compacted && (event as any).references) {
        state.setReferences((event as any).references, sid);
      }
      chat?.requestRender();
      (window as any).__sessionInner?.requestRender?.();
      break;
    }

    case "queued_submits": {
      // Backend-owned queue snapshot: repaint the panel only when the
      // event is for the conversation being viewed.
      const q = event as QueuedSubmitsEvent;
      if (q.session_id && q.session_id !== state.getState().sessionId) break;
      state.setQueuedPrompts(
        q.items.map(it => ({
          text: it.prompt || "",
          mode: (it.metadata && it.metadata.mode) || it.mode || undefined,
        })),
      );
      break;
    }

    case "queued_run_started": {
      // A drained queue entry began running server-side: render the user
      // bubble and switch to the running state exactly like a local send.
      const re = event as QueuedRunStartedEvent;
      if (re.session_id && re.session_id !== state.getState().sessionId) break;
      state.addUserMessage(re.prompt, re.mode || undefined);
      state.startAssistantMessage();
      state.setSessionState("running");
      _activeStreamSessionId = re.session_id || state.getState().sessionId || "";
      // A brand-new turn: reset the auto-open dismiss guard so the first
      // sub-agent of this turn opens live again (per the auto-open setting).
      (window as any).__subAgentAutoOpenDismissed = false;
      break;
    }

    case "run_started": {
      // The backend just staged this run's user message; it reports the
      // message's server id here so the newest optimistic user bubble can be
      // stamped with it. Without this the just-sent message can only be
      // addressed by positional index on rollback/edit, and that index drifts
      // one turn off after retries and branch operations.
      if (!_hasSessionId(event)) break;
      if (event.session_id && event.session_id !== state.getState().sessionId) break;
      const umid = event.user_message_id;
      if (!umid) break;
      const msgs = state.getState().messages;
      for (let i = msgs.length - 1; i >= 0; i--) {
        const m = msgs[i];
        if (m.role === "user" && !state.isNoticeMode(m.mode) && !m.serverId) {
          m.serverId = umid;
          break;
        }
      }
      break;
    }

    case "pong":
      state.setConnected(true);
      break;

    case "error":
      {
        // A failed resume (e.g. empty model pool) must clear the pending
        // marker, otherwise a later switch to the same session would be
        // treated as "still loading" forever.
        if (event.session_id) _pendingResumes.delete(event.session_id);
        const _migCode = (event as any).code || "";
        if (_migCode === "export_data_error" || _migCode === "import_data_error") {
          const msg = (event as any).message || "";
          const friendly = /not a valid Encre backup/i.test(msg)
            ? t("settings.storageImportInvalidZip")
            : msg;
          _migrationProgress?.fail(friendly || t("settings.storageFailed"));
          _migrationProgress = null;
          break;
        }
      }
      // The backend queues prompts for busy conversations instead of
      // dropping them; the authoritative snapshot arrives via the
      // ``queued_submits`` broadcast.  Swallow the busy notice here so it
      // does not flip the viewed session to idle or show an error box.
      if ((event as any).code === "busy") {
        break;
      }
      // Operation-level failures (a button whose handler raised, a frame that
      // could not be parsed or dispatched) say nothing about the running turn.
      // Marking the session idle here hides live UI that is still waiting --
      // e.g. the review card of a turn that is genuinely blocked on the user.
      {
        const _opCode = (event as any).code || "";
        if (_opCode === "handler_error" || _opCode === "parse_error" || _opCode === "unhandled") {
          state.addNotification({
            id: crypto.randomUUID(),
            type: "error",
            title: t("stream.notificationError"),
            message: event.message,
            source: "System",
            timestamp: Date.now(),
            read: false,
          });
          break;
        }
      }
      // code="no_session" answers a rollback with no session id at all, so
      // the guard below would swallow it. The rollback was refused with
      // nothing to roll back against, the optimistic local truncation is
      // still on screen, and the pending marker is void -- drop both and
      // re-request the session that is actually in view.
      if (!_hasSessionId(event) && (event as any).code === "no_session") {
        resyncActiveSession();
        break;
      }
      if (!_hasSessionId(event)) break;
      _activeStreamSessionId = "";
      state.clearRetryNotice(_eventSessionId(event));
      // Background session error — update snapshot without touching active UI.
      if (event.session_id && event.session_id !== state.getState().sessionId) {
        state.setSessionState("idle", event.session_id);
        break;
      }
      state.setSessionState("idle", _eventSessionId(event));
      // Session/operation-level errors (rollback, branch, retry, capacity,
      // job/execution lookup, parse, etc.) are NOT tied to any single turn —
      // stamping them onto the last assistant message would show an error box
      // under the WRONG (previous, correct) turn. Only attach turn-scoped
      // errors, and only to the current turn's last assistant message.
      const _errCode = event.code || "";
      const _turnScopedError = (
        _errCode === "execution_error"
        || _errCode === "run_error"
        || _errCode === "tool_error"
        || _errCode === "context"
        || _errCode === "network"
        || _errCode === "server"
        || _errCode === "rate_limit"
        || _errCode === "auth"
        || _errCode === "capacity"
        || (!_errCode && !!(event as any).category)
      );
      if (_turnScopedError) {
        const msgs = state.getState().messages;
        const lastStreaming = msgs.some(m => m.role === "assistant" && m.isStreaming);
        const lastMsg = state.getLastAssistantMessage(_eventSessionId(event));
        // Only stamp when the session is mid-stream (the error belongs to the
        // current in-flight turn), or when there is no assistant at all yet.
        if (lastMsg && (lastStreaming || msgs.filter(m => m.role === "assistant").length === 1)) {
          lastMsg.hasError = true;
          lastMsg.errorMessage = event.message;
          lastMsg.errorCode = event.code;
          (lastMsg as any).errorCategory = (event as any).category || "";
        }
      }
      state.addNotification({
        id: crypto.randomUUID(),
        type: "error",
        title: t("stream.notificationError"),
        message: event.message,
        source: "System",
        timestamp: Date.now(),
        read: false,
      });
      // Optimistic rollback / branch / delete / retry requests truncate the
      // UI before the server answers. When the server refuses any of them,
      // re-request the authoritative session state so the frontend stops
      // rendering the ghost user message and matches the real backend
      // conversation.
      if (RESYNC_ERROR_CODES.has(_errCode)) resyncActiveSession();
      break;

    case "qa_notification": {
      state.addNotification({
        id: crypto.randomUUID(),
        type: "info",
        title: (event as any).title || "QA Notification",
        message: (event as any).message || "This is a QA test notification.",
        source: (event as any).source || "QA",
        timestamp: Date.now(),
        read: false,
      });
      break;
    }

    case "configured":
      console.log("[stream] configured event, config keys:", Object.keys(event.config ?? {}));
      state.setSettings({ ...state.getState().settings, ...event.config });
      if (event.config && typeof event.config === "object") {
        if ("permission_settings" in event.config) {
          const raw = (event.config as Record<string, unknown>).permission_settings;
          state.setPermissionPolicies(normalizePermissionPolicies(raw));
        }
        // Note: locale is a frontend preference stored in localStorage.
        // The backend's language field is only synced for settings display;
        // do NOT call setLocale() here or it would override the user's choice
        // when the backend (default zh) pushes config asynchronously.
      }
      break;

    case "telemetry":
      if (!_hasSessionId(event)) break;
      // Ignore telemetry updates for non-active sessions so background
      // sessions cannot overwrite the canvas panel currently shown.
      if (event.session_id && event.session_id !== state.getState().sessionId) break;
      state.setTelemetry(event.data, event.session_id);
      break;

    case "usage_stats":
      state.setUsageStats(event.stats);
      break;

    case "gateway_status":
      state.setGatewayStatus(event.status);
      break;

    case "plugin_ui_sync": {
      syncPluginUiProviders(event.providers || []);
      const toolMetaChanged = setToolMetaEntries(event.tool_meta || []);
      syncPluginContributions(event.contributions || []);
      if (toolMetaChanged) {
        // A client connecting during plugin warmup painted its transcript
        // against an empty catalog; every tool card resolves its card shape /
        // icon / label from that catalog at render time. Announce the real
        // catalog's arrival so already-rendered cards restyle immediately
        // instead of waiting for an unrelated view switch to force a rebuild.
        window.dispatchEvent(new CustomEvent("encre-tool-meta-updated"));
      }
      break;
    }

    case "plugin_changed": {
      // The connect-time replay of the persisted snapshot only carries the
      // installed-plugin facts (no provider code): apply it without tearing
      // down mounted plugin UI. A live lifecycle change may carry changed
      // provider code, so that one still resets everything and lets the
      // following `plugin_ui_sync` re-mount the fresh set.
      if (!(event as { cached?: boolean }).cached) {
        resetPluginUi();
        resetPluginContributions();
      }
      // Feed the shared plugin-fact store the fault card reads, then publish
      // the lifecycle snapshot for management surfaces (settings plugins
      // panel) through the same DOM-event dispatch.
      setPluginInfo(event.plugins || []);
      dispatchServerSignal(
        "encre-plugin-states",
        event.plugins || [],
        _pluginStatesSignature(event.plugins || []),
      );
      break;
    }

    case "plugin_ui_result": {
      // Backend result for a plugin UI action (or any plugin-pushed UI
      // event).  Re-dispatched as a DOM event so subscribers registered via
      // `onPluginUiResult` (plugin bridge, contributed surfaces) receive it
      // through the same unified dispatch as every other server event.
      // One result per user action: never dedupe these, a legitimate repeat
      // click must reach its subscriber.
      window.dispatchEvent(new CustomEvent("encre-plugin-ui-result", {
        detail: { kind: event.kind, plugin: event.plugin, action: event.action, ok: event.ok, data: event.data },
      }));
      break;
    }

    case "plugin_installed":
    case "plugin_uninstalled":
    case "market_installed":
    case "plugin_enabled":
    case "plugin_disabled": {
      // Lifecycle replies acknowledge user actions; always deliver them.
      window.dispatchEvent(new CustomEvent("encre-plugin-lifecycle-ok", {
        detail: { type: event.type, name: event.name },
      }));
      break;
    }

    case "plugin_install_error":
    case "plugin_uninstall_error":
    case "market_install_error":
    case "plugin_enable_error":
    case "plugin_disable_error": {
      window.dispatchEvent(new CustomEvent("encre-plugin-lifecycle-error", {
        detail: { type: event.type, name: (event as { name?: string }).name || "", message: event.message },
      }));
      break;
    }

    case "plugin_confirm_request": {
      // Dynamic authorisation: third-party plugins discovered by the
      // backend are waiting for the user's approve/deny decision.  Their
      // code is NOT imported until approved, so each decision is final
      // for this run of the app.
      void handlePluginConfirmQueue(event.plugins || []);
      break;
    }

    case "market_catalog": {
      // Central-repository (ea-cwh) catalogue: pushed on connect and again
      // whenever the daily index refresh changes its fingerprint.  The
      // market view merges it with the local `encre-plugin-states` snapshot
      // (installable / updatable / installed), so hand it over verbatim.
      // It also lands in the index store: the market binds its listener
      // when it opens, which is normally after this single frame went by.
      const catalog = {
        source: event.source,
        index_version: event.index_version,
        generated_at: event.generated_at,
        plugins: event.plugins || [],
      };
      setMarketIndex(catalog);
      dispatchServerSignal(
        "encre-market-catalog",
        catalog,
        `${catalog.source}|${catalog.index_version}|${catalog.generated_at}|${catalog.plugins.length}`,
      );
      break;
    }

    case "wechat_scan_result":
      if (typeof _wechatScanCallback === "function") {
        _wechatScanCallback(event as any);
      }
      break;

    case "plan_update":
      if (!_hasSessionId(event)) break;
      {
        const sid = _eventSessionId(event);
        const prevCount = (state.getState().planItems || []).length;
        state.setPlanItems(event.plan_items, sid);
        const newCount = (event.plan_items || []).length;
        // When the model first creates todos (plan items go 0 -> >0) in the
        // active session, auto-open the summary panel so progress is visible.
        const activeSid = state.getState().sessionId;
        if (sid && activeSid && sid === activeSid && prevCount === 0 && newCount > 0) {
          (window as any).__app?.openSummaryPanel?.();
        }
      }
      chat?.requestRender();
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "plan_proposal":
      if (!_hasSessionId(event)) break;
      state.addPlanProposal(event, _eventSessionId(event));
      break;

    case "plan_mode_changed":
      if (!_hasSessionId(event)) break;
      state.setPlanModeActive(event.active, _eventSessionId(event));
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "mode_changed":
      if (!_hasSessionId(event)) break;
      // Update the persistent mode in state
      state.setInputMode(event.mode);
      // Update the toolbar chip via the App instance
      const app = (window as any).__app;
      if (app) {
        app._persistentMode = event.mode;
        // Remove any stale chip left over from a previous session or
        // turn so updateChipState() can insert the correct one.
        if (app.input && app.hasModeChip()) {
          app.removeModeChip();
          app._currentChipMode = "";
        }
        app.updateChipState();
        app.updatePlaceholder();
      }
      break;

    case "command_changed": {
      // A sticky slash *command* (NOT a mode) was activated/cleared.  Mirror
      // it into the App so its chip persists across turns / sessions.
      const cmdApp = (window as any).__app;
      if (cmdApp) {
        const c = event.command;
        cmdApp._activeCommand = (c && c.name) ? c : null;
        cmdApp.updateChipState();
      }
      break;
    }

    case "plan_resolved":
      if (!_hasSessionId(event)) break;
      state.removePlanProposal(event.proposal_id, _eventSessionId(event));
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "models_list":
      state.setAvailableModels(event.models);
      break;

    case "sessions_list": {
      _sessionsArrived = true;
      const currentChannel = state.getState().workspaceMode === "iwork" ? "iwork" : "normal";
      if (event.channel && event.channel !== currentChannel) {
        // Stash instead of dropping: the frame's channel is authoritative
        // backend state (e.g. the user's workspace is open server-side) and
        // the local mode may lag it by a frame or fail to arrive at all (a
        // failed config build).  Re-dispatched on the workspaceMode change
        // that matches, so the sidebar fills with the right channel's data
        // instead of staying empty until an unrelated push.
        _pendingChannelSessions = { channel: event.channel, event };
        break;
      }
      _pendingChannelSessions = null;

      state.setSessionsList(event.sessions);
      const isIworkMode = state.getState().workspaceMode === "iwork";
      const freshSessions = event.sessions as any[];
      const trayCache = state.getTraySessions();
      state.setTraySessions(
        isIworkMode ? trayCache.normal : freshSessions,
        isIworkMode ? freshSessions : trayCache.iwork,
      );
      if (state.getState().sessionId) {
        const sid = state.getState().sessionId;
        const cur = (event.sessions as any[]).find(
          (s: any) => s.session_id === sid
        );
        // Adopt the server's state only when it actually differs from the
        // local one.  The previous check compared a state *string* against
        // the boolean `running` flag, so it was always true and clobbered the
        // local state (e.g. "awaiting_approval" -> "running"/"idle") on every
        // listing broadcast.
        const localState = state.getState().sessionStore[sid]?.sessionState;
        if (cur && cur.state && cur.state !== localState) {
          state.setSessionState(cur.state, sid);
        }
      }
      break;
    }

    case "sessions_all": {
      _sessionsArrived = true;
      const normal = (event as any).normal || [];
      const iwork = (event as any).iwork || [];
      state.setTraySessions(normal, iwork);
      // Authoritative full listing: seed the global search session cache so
      // session search works in every mode (including iwork, where the
      // sidebar sessionsList is intentionally emptied).
      state.setAllSessions([...normal, ...iwork], "replace");
      // Also refresh the sidebar for the current mode so status lights
      // stay in sync when only sessions_all (not sessions_list) is received.
      const currentMode = state.getState().workspaceMode === "iwork" ? "iwork" : "normal";
      const modeSessions = currentMode === "iwork" ? iwork : normal;
      if (modeSessions.length > 0 || state.getState().sessionsList.length === 0) {
        state.setSessionsList(modeSessions);
      }
      break;
    }

    case "archived_sessions_list": {
      // Archive manager view (workspace management dialog): replace the
      // archived session list with the authoritative backend snapshot.
      state.setArchivedSessions((event as any).sessions || []);
      break;
    }

    case "config_data": {
      // A config_data frame dispatches ~15 set* calls in one tick.  Without a
      // batch each one schedules its own microtask flush; with it the whole
      // plane lands as a single transaction, so subscribers with keys fire at
      // most once and never see a half-applied snapshot.
      state.batch(() => {
        applyConfigSnapshot(event.config as Record<string, unknown>);
      });
      break;
    }

    case "config_patch": {
      // Incremental config update: the backend diffs the post-change config
      // against its last full snapshot and sends ONLY the changed top-level
      // fields.  Applied through the same applier as config_data, so every
      // panel consumer stays identical -- the guards inside
      // applyConfigSnapshot make absent keys no-ops instead of resets.
      state.batch(() => {
        applyConfigSnapshot(event.config as Record<string, unknown>);
      });
      break;
    }

    case "models_updated": {
      const eventSid = (event as { session_id?: string }).session_id || "";
      const activeSid = state.getState().sessionId;
      // A background conversation (or a run that fell back while the user
      // moved on) must never repaint the selector of the conversation that
      // is actually open. Frames are tagged with their owning session.
      if (eventSid && activeSid && eventSid !== activeSid) {
        console.log("[stream] models_updated for background session", eventSid, "ignored");
        break;
      }
      console.log("[stream] models_updated, count:", event.models?.length, "active_index:", event.active_model_index, "sid:", eventSid);
      state.setModelConfigs(event.models, event.active_model_index);
      if (event.providers) state.setProviders(event.providers);
      state.recordSessionModel(event.active_model_index);
      state.markModelIndexSynced(eventSid);
      break;
    }

    case "skills_updated":
      state.setEnabledSkills(event.enabled_skills);
      if (event.available_skills) {
        state.setSkillsList(event.available_skills);
      }
      break;

    case "skills_list":
      state.setSkillsList(event.skills);
      break;

    case "skill_detail": {
      // One skill's own description and SKILL.md body, answering the market's
      // per-skill request.  Re-dispatched as a DOM event: the plugin page is
      // the only consumer and it fills the card that asked.
      dispatchServerSignal(
        "encre-skill-detail",
        event,
        `${(event as { name?: string }).name || ""}|${((event as { body?: string }).body || "").length}`,
      );
      break;
    }

    case "skill_file":
      // One file inside a skill package (its references/, templates/, scripts/),
      // which the market's skill reader follows.
      dispatchServerSignal(
        "encre-skill-file",
        event,
        `${(event as { name?: string }).name || ""}|${(event as { path?: string }).path || ""}|${((event as { content?: string }).content || "").length}`,
      );
      break;

    case "mcp_updated":
      console.log("[stream] mcp_updated, count:", event.mcp_servers?.length);
      state.setMcpServers((event.mcp_servers as any[] || []).map(srv => {
        if (srv.type) return srv;
        return {
          name: srv.name,
          type: srv.transport || "stdio",
          ...(srv.transport === "http"
            ? { url: srv.server_url || "", timeout: srv.http_timeout ?? 60, headers: srv.headers || {} }
            : { command: srv.command || "", args: srv.args || [] }),
          ...(srv.cwd ? { cwd: srv.cwd } : {}),
          ...(srv.env ? { env: srv.env } : {}),
          ...(srv.disabled ? { disabled: true } : {}),
        };
      }));
      break;

    case "agent_updated": {
      const ac = event.config as Record<string, unknown>;
      state.setAgentConfig({
        system_prompt: (ac.system_prompt as string) || "",
        specialty: (ac.specialty as string) || "general",
        permission_mode: (ac.permission_mode as string) || "default",
        max_turns: (ac.max_turns as number) ?? 0,
      });
      break;
    }

    case "search_results":
      // `seq` echoes the client's request sequence; stale (out-of-order)
      // responses are dropped inside applySearchResults.
      state.applySearchResults(event.seq ?? 0, event.results);
      break;

    case "memory_list":
      state.setMemoryList(event.entries);
      break;

    case "memory_graph":
      state.setMemoryGraph(event.graph);
      break;

    case "global_rules_list":
      state.setGlobalRulesList(event.rules);
      break;

    case "project_rules_list":
      state.setProjectRulesList(event.rules);
      break;

    case "project_hooks_list":
      state.setProjectHooksList(event.hooks);
      break;

    case "global_rule_saved":
    case "global_rule_deleted":
      // The backend pushes the refreshed global_rules_list in the same
      // operation — nothing to fetch here.
      break;

    case "global_rule_content":
      state.setViewingGlobalRule({ name: event.name, content: event.content, error: event.error });
      break;

    case "profile_data":
      state.setProfile(event.profile);
      break;

    case "gitignore_content":
      state.setGitignoreContent(event.content);
      break;

    case "library_tree":
      state.setLibraryTree(event.nodes);
      break;

    case "library_content":
      state.setLibraryContent({ id: event.id, content: event.content });
      break;

    case "library_error":
      // The backend carries the real failure text; surface it as a toast so
      // a failed tree op never dies silently.
      state.showToast(t("session.documentError"), event.message || t("settings.documentError"), "error", "Library");
      break;

    case "skill_installed":
      if (event.available_skills) {
        state.setSkillsList(event.available_skills);
      }
      break;

    case "skill_install_error":
      break;

    case "skill_uninstalled":
      if (event.available_skills) {
        state.setSkillsList(event.available_skills);
      }
      break;

    case "sub_agents_updated":
      console.log("[stream] sub_agents_updated, count:", event.sub_agents?.length);
      state.setSubAgents(event.sub_agents);
      break;

    case "memory_detail":
      state.setMemoryDetail({ path: event.path, content: event.content, error: event.error });
      break;

    case "retry":
      // Transient: a failed model call is being retried.  Shown at the bottom
      // of the conversation until the retried attempt produces output (the
      // streaming handlers below clear it) or the turn ends.
      if (!_hasSessionId(event as any)) break;
      state.setRetryNotice({
        scope: (event as any).scope || "request",
        attempt: Number((event as any).attempt) || 1,
        maxAttempts: Number((event as any).max_attempts) || 0,
        delayMs: Number((event as any).delay_ms) || 0,
        message: (event as any).message || "",
        statusCode: Number((event as any).status_code) || 0,
        errorClass: (event as any).error_class || "",
        timestamp: Date.now(),
      }, _eventSessionId(event as any));
      break;

    case "compact_starting":
      if (!_hasSessionId(event as any)) break;
      state.addCompactStartingEvent(
        String((event as any).notice_id || ""),
        String((event as any).anchor_id || ""),
        _eventSessionId(event as any),
      );
      break;

    case "model_switch":
      // The active model changed -- manual selection or automatic model-pool
      // cycling.  The backend has already persisted the notice on the
      // session (recorded like a tool call), so appending it here makes the
      // line appear immediately and a later full-history reload replaces it
      // in place at the same anchor.
      if (!_hasSessionId(event as any)) break;
      state.appendNoticeMessage(
        "model_switch",
        {
          id: String((event as any).notice_id || "") || `ms-${Date.now()}`,
          timestamp: Date.now(),
          from_model: (event as any).from_model || "",
          to_model: (event as any).to_model || "",
          reason: (event as any).reason || "",
          source: (event as any).source || "manual",
          // Server id of the message the switch happened after — the line is
          // inserted THERE, never below a still-streaming reply.
          anchor_id: (event as any).anchor_id || "",
        },
        _eventSessionId(event as any),
      );
      break;

    case "compact_failed":
      // Compaction did not produce a result (failure or no-op): dismiss
      // the "compressing" indicator; the conversation continues with the
      // uncompressed context.
      if (!_hasSessionId(event as any)) break;
      state.clearCompactStartingEvents(String((event as any).notice_id || ""), _eventSessionId(event as any));
      break;

    case "compact":
      if (!_hasSessionId(event as any)) break;
      // Compaction finished: drop the "compressing" indicator; the inline
      // divider comes from the compact notice in the refreshed messages.
      state.clearCompactStartingEvents(String((event as any).notice_id || ""), _eventSessionId(event as any));
      state.addCompactEvent({
        old_count: (event as any).old_count,
        new_count: (event as any).new_count,
        old_tokens: (event as any).old_tokens,
        new_tokens: (event as any).new_tokens,
      }, _eventSessionId(event as any));
      // Guard: an empty payload means "no session attached", not "the
      // conversation is empty" -- reloading with [] would wipe the
      // transcript on screen.
      if (Array.isArray((event as any).messages) && (event as any).messages.length > 0) {
        state.loadSessionMessages((event as any).messages, _eventSessionId(event as any));
      }
      // The backend persists summary-panel data (plan/artifacts/references)
      // independently of the compacted message list, so restore it here to
      // prevent the panel from losing entries after compaction.
      if ((event as any).plan_items) {
        state.setPlanItems((event as any).plan_items, _eventSessionId(event as any));
      }
      if ((event as any).artifacts) {
        state.setArtifacts((event as any).artifacts, _eventSessionId(event as any));
      }
      if ((event as any).references) {
        state.setReferences((event as any).references, _eventSessionId(event as any));
      }
      break;

    case "system_message":
      {
        const sev = event as any;
        state.addSystemMessage({
          content: sev.content || "",
          kind: sev.kind || "info",
          sessionId: _eventSessionId(sev),
        });
      }
      break;

    case "spec_update":
      {
        const sev = event as any;
        state.updateSpec(sev.spec || null, _eventSessionId(sev));
        (window as any).__sessionInner?.requestRender?.();
      }
      break;

    case "plan_review":
      {
        const sev = event as any;
        console.log("[stream] plan_review accepted:", sev.status, "review=", sev.review?.review_id, "call=", sev.review?.call_id);
        if (!sev.review) {
          // Mode closed: the backend dismissed the pending review, so drop
          // the card instead of merging into the stale one.
          state.updatePlanReview(null, _eventSessionId(sev));
          break;
        }
        const review = sev.review || {};
        const existing = state.getState().planReview || {} as any;
        if (
          existing.review_id && review.review_id
          && existing.review_id !== review.review_id && sev.status !== "review"
        ) {
          // Late echo for a review the card no longer shows (e.g. a decision
          // that only surfaced after the user already resubmitted). Applying
          // it would swap the live card back to the dead one.
          break;
        }
        const merged = {
          review_id: review.review_id || existing.review_id || "",
          content: review.content || existing.content || "",
          file_path: review.file_path || existing.file_path || "",
          dir_path: review.dir_path || existing.dir_path || "",
          mode: review.mode || existing.mode || "",
          status: sev.status || existing.status || "review",
          files: review.files || existing.files || [],
          feedback: sev.feedback || review.feedback || existing.feedback || "",
          call_id: review.call_id || existing.call_id || "",
        };
        // The card is one-shot: it exists only while the decision is owed.
        // Approving, rejecting, cancelling or timing out retires it, and the
        // turn's own output is the record -- a resolved card lingering at
        // the bottom of the transcript reads as a second pending question.
        state.updatePlanReview(
          merged.status === "review" || merged.status === "draft" ? merged : null,
          _eventSessionId(sev),
        );
      }
      break;

    case "context_usage":
      if (!_hasSessionId(event as any)) break;
      state.updateContextUsage(
        (event as any).context_tokens,
        (event as any).context_window,
        _eventSessionId(event as any),
        (event as any).breakdown ?? null,
      );
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "agent_state":
      if (!_hasSessionId(event as any)) break;
      const _mode = (event as any).mode;
      state.setAgentState(
        _mode
          ? { ...((event as any).state || {}), mode: _mode }
          : (event as any).state,
        _eventSessionId(event as any),
      );
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "artifacts_update":
      if (!_hasSessionId(event)) break;
      // Append new artifacts; the server sends one at a time during streaming.
      state.appendArtifacts(event.artifacts, _eventSessionId(event));
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "references_update":
      if (!_hasSessionId(event)) break;
      // Append new references; the server sends one at a time during streaming.
      state.appendReferences(event.references, _eventSessionId(event));
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "messages_updated":
      if (!_hasSessionId(event)) break;
      if (_shouldRejectSessionScopedEvent(event)) break;
      // If the user already started a new run (submit() raced ahead of this
      // response), do NOT override the running state or replace messages.
      if (state.getState().running) {
        if (event.session_id) {
          state.setSessionId(event.session_id);
        }
        // Drop the frame, but not a pending rollback edit. The server can
        // answer a rollback only after the user already fired the next turn,
        // and leaving the marker alive would let the next list load cut the
        // user's own re-sent message, which sits at the same ordinal with
        // the same text. The optimistic local truncation already stands, so
        // no list rewrite is needed here.
        state.clearPendingRollbackEdit();
        break;
      }
      // Sync session ID and reset running state for reliable subsequent sends.
      // Do NOT call resetChat() on empty messages — that clears sessionId and
      // locks the user out of sending further messages on the same session.
      if (event.session_id) {
        state.setSessionId(event.session_id);
      }
      state.setSessionState("idle", _eventSessionId(event));
      // If this confirms a pending rollback edit, the conversation was
      // truncated: pin the next render to the bottom instead of keeping
      // the pre-rollback scroll offset.
      if (state.hasPendingRollbackEdit(_eventSessionId(event))) chat?.requestPinToBottom?.();
      state.loadSessionMessages(state.applyPendingRollbackEdit(event.messages || [], _eventSessionId(event)), _eventSessionId(event));
      state.setPlanItems(event.plan_items || [], _eventSessionId(event));
      if (event.artifacts) {
        state.setArtifacts(event.artifacts, _eventSessionId(event));
      }
      if (event.references) {
        state.setReferences(event.references, _eventSessionId(event));
      }
      chat?.requestRender();
      (window as any).__sessionInner?.requestRender?.();
      // Sync sidebar session entry (message count, preview)
      _syncSessionEntry(event.session_id, state.getState());
      break;

    case "rollback_checkout":
      if (!_hasSessionId(event)) break;
      if (_shouldRejectSessionScopedEvent(event)) break;
      // Server has truncated the session to the chosen commit and persisted it.
      // Mirror that on the client so the UI actually reflects the rollback.
      if (event.session_id) {
        state.setSessionId(event.session_id);
      }
      state.setSessionState("idle", _eventSessionId(event));
      state.loadSessionMessages(state.applyPendingRollbackEdit(event.messages || [], _eventSessionId(event)), _eventSessionId(event));
      state.setPlanItems(event.plan_items || [], _eventSessionId(event));
      if (event.artifacts) {
        state.setArtifacts(event.artifacts, _eventSessionId(event));
      }
      if (event.references) {
        state.setReferences(event.references, _eventSessionId(event));
      }
      _syncSessionEntry(event.session_id, state.getState());
      // Restore the last user message into the input box for re-editing
      let userInput = (event as any).user_input as string | undefined;
      // A message submitted with a command chip and no text is stored as
      // ``<command>name</command>`` — strip it from the restored text and
      // re-render it as a chip instead of raw markup.
      const rbCmdMatch = userInput ? userInput.match(/^<command>(\w[\w-]*)<\/command>$/s) : null;
      if (userInput && (userInput.includes("<terminal>") || userInput.includes("<attach ") || userInput.includes("<mode>") || rbCmdMatch)) userInput = "";
      const input = document.getElementById("prompt-input") as HTMLElement | null;
      if (input && userInput !== undefined) { input.textContent = userInput; input.focus(); }
      if (rbCmdMatch) {
        state.restoreInputCommandChip(rbCmdMatch[1]);
      }
      // Restore mode chip AFTER user_input (which clears the input via textContent)
      {
        const msgs = state.getState().messages;
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === "user" && (msgs[i] as any).mode) {
            state.restoreInputModeChip((msgs[i] as any).mode);
            break;
          }
        }
      }
      // Restore attachment chips from the SAME user message whose text was
      // restored above (the new tail of the truncated conversation).  The
      // old code scanned backwards past the tail for ANY user message with
      // refs, so after a rollback + text-only resend it resurrected chips
      // from an older image message the moment the turn ended (the ghost
      // image chip bug).  Replace semantics: stale chips never survive.
      {
        const msgs = state.getState().messages;
        let tailRefs: import("./types.js").AttachmentMeta[] | undefined;
        for (let i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].role === "user") {
            tailRefs = (msgs[i] as any).fileRefs;
            break;
          }
        }
        const atts: import("./types.js").AttachmentMeta[] = [];
        for (const r of tailRefs || []) {
          if (r.mime_type === "text/x-terminal") continue;
          if (r.path) {
            atts.push({ name: r.name, path: r.path, content: "", mime_type: r.mime_type || "", size: r.size, is_binary: false });
          }
        }
        state.clearAttachments();
        if (atts.length > 0) state.addAttachments(atts);
      }
      _syncSessionEntry(event.session_id, state.getState());
      // The session was truncated by the rollback: pin the next render to
      // the bottom instead of preserving the pre-rollback scroll offset.
      chat?.requestPinToBottom?.();
      chat?.renderForce?.();
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "session_deleted":
      // Local removal — the server has already deleted it, so don't
      // re-fetch the session list (it may resurrect the session from
      // other data sources like the EventRouter).  Rely on the local
      // state filter to keep the UI consistent.
      state.removeSessionById(event.session_id);
      // The deleted session may also be listed in the archive view.
      state.removeArchivedSessionById(event.session_id);
      // If the deleted session is currently open, clear chat immediately.
      if (state.getState().sessionId === event.session_id) {
        state.setSessionId("");
        state.clearMessages();
        state.setSessionState("idle", "");
      }
      chat?.requestRender();
      (window as any).__sessionInner?.requestRender?.();
      // Refresh automation history — the deleted session may be a sub-agent
      // session shown in the automation history timeline, which uses its own
      // independent this.history array (not global sessionsList).
      send({ type: "automation_get_history" });
      break;

    case "session_exported":
      downloadMarkdownFile(event.markdown, event.filename || event.session_id);
      break;

    case "sessions_exported_zip":
      {
        // Use native save dialog so the user picks where to save
        const api = window.electronAPI;
        if (api?.exportBinary) {
          api.exportBinary({
            base64: event.zip_base64,
            defaultName: event.filename || "export.zip",
            filters: [{ name: t("stream.zipArchive"), extensions: ["zip"] }],
          });
        }
      }
      break;

    case "data_exported_zip":
      {
        // Copy the on-disk backup zip to a user-chosen location via the native
        // save dialog (avoids round-tripping a large archive over base64).
        const api = window.electronAPI;
        if (api?.copyFileTo) {
          api.copyFileTo({
            sourcePath: event.zip_path,
            defaultName: event.filename || "encre-backup.zip",
            filters: [{ name: t("stream.zipArchive"), extensions: ["zip"] }],
          }).then(() => {
            _migrationProgress?.succeed(t("settings.storageExportDone"));
            _migrationProgress = null;
          });
        } else {
          _migrationProgress?.fail(t("settings.storageExportFailed"));
          _migrationProgress = null;
        }
      }
      break;

    case "data_export_progress":
    case "data_import_progress":
      {
        if (!_migrationProgress) {
          const title = event.type === "data_export_progress"
            ? t("settings.storageExportTitle")
            : t("settings.storageImportTitle");
          _migrationProgress = Dialog.progress(title, "", {
            cancellable: false,
            scope: "app"
          });
        }
        _migrationProgress.update(event.percent, `${event.done} / ${event.total}`);
        const cur = (event as any).file as string | undefined;
        if (cur) {
          _migrationProgress.setSubMessage(`${t("settings.storageCurrentFile")} ${cur}`);
        }
      }
      break;

    case "data_import_done":
      {
        const e = event as any;
        const summary = t("settings.storageImportResult")
          .replace("{restored}", String(e.restored ?? 0))
          .replace("{overwritten}", String(e.overwritten ?? 0))
          .replace("{kept}", String(e.kept ?? 0))
          .replace("{skipped}", String(e.skipped ?? 0));
        _migrationProgress?.succeed(summary);
        _migrationProgress = null;
        state.showToast(summary, "", "success", "Storage");
      }
      break;

    case "session_renamed":
      // The backend broadcasts the refreshed sessions_list with the rename —
      // nothing to fetch here.
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "model_validated": {
      // Route by the caller's correlation id first; legacy backends (or
      // callers) that never sent one still route by model id.
      const key = ((event as any).request_id as string | undefined) || ((event as any).model_id as string | undefined);
      const entry = key ? _validationPending.get(key) : undefined;
      if (entry) {
        _validationPending.delete(key as string);
        entry.resolve(event);
      }
      break;
    }

    case "model_validation_error": {
      const key = ((event as any).request_id as string | undefined) || ((event as any).model_id as string | undefined);
      const entry = key ? _validationPending.get(key) : undefined;
      if (entry) {
        _validationPending.delete(key as string);
        entry.reject(event.message);
      }
      break;
    }

    case "workspace_opened":
      state.setWorkspaces(event.workspaces);
      // Stale-open guard: `open_workspace` is answered asynchronously and
      // `workspace_opened` carries no request id. If the user already left
      // (a close_workspace was sent after the open), applying this would drag
      // them back into iWork a few seconds later. Keep the refreshed
      // workspace list but ignore the mode switch.
      if (getWorkspaceIntent() === "close") {
        (window as any).__sessionInner?.requestRender?.();
        break;
      }
      state.setActiveWorkspace(event.path);
      state.setWorkspaceMode("iwork");
      // NOTE: no setSessionsList([]) here.  The sidebar was already seeded
      // with cached iWork rows by enterWorkspaceMode(); clearing between
      // that seed and the authoritative sessions_list (pushed by the
      // backend's unified snapshot below) produced the
      // "sessions → empty → sessions" flash on every mode switch.
      // The backend's _push_all_data (sent right after workspace_opened)
      // replaces the seeded list in one step.
      // If a tray-driven resume is pending after opening this workspace, fire it.
      const pending = (window as any).__pendingTrayResume;
      if (pending && pending.sessionId) {
        (window as any).__pendingTrayResume = null;
        state.setSessionId(pending.sessionId);
        send({ type: "resume", session_id: pending.sessionId, request_id: pending.requestId });
      } else {
        // `workspace_opened` is the backend confirmation that the selected
        // tree row now owns the agent context. Only now leave the previous
        // conversation and show the workspace-root welcome state.
        state.setSessionId("");
        state.clearMessages("");
        (window as any).__appCleanupContentArea?.({ keepAutomationFlag: false });
        chat?.renderForce?.();
        (window as any).__sessionInner?.restoreSidebarVisibility?.();
      }
      // No refreshAllData() here either: the backend pushes the full unified
      // snapshot (config/sessions/workspaces/workspace_config/models) as part
      // of processing open_workspace, so an explicit re-pull would only
      // duplicate every frame and re-render the same data twice.
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "workspace_closed":
      // Symmetric stale-close guard: if the user already asked for a new
      // workspace (open_workspace sent after this close), this late close
      // must not drop them back to normal — the pending open will land next.
      if (getWorkspaceIntent() === "open") {
        (window as any).__sessionInner?.requestRender?.();
        break;
      }
      state.setActiveWorkspace("");
      state.setWorkspaceMode("normal");
      // NOTE: no setSessionsList([]) — symmetric to workspace_opened.
      // exitWorkspaceMode() already seeded the sidebar with cached normal
      // rows; clearing here flashed "no sessions" until the backend's
      // unified push (sent after workspace_closed) refilled the list.
      // Back to the global/normal context: the backend's _push_all_data
      // triggered by close_workspace refreshes sidebar / model selector /
      // settings from the global state — no explicit re-pull needed.
      state.setSessionState("idle");
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "workspaces_list":
      state.setWorkspaces(event.workspaces);
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "workspace_config":
      state.setWorkspaceConfig(event.path, event.config);
      if (event.files) state.setWorkspaceFiles(event.path, event.files);
      (window as any).__workspaceMgr?.refresh?.();
      break;

    case "workspace_removed":
      state.setWorkspaces(event.workspaces);
      // If the deleted workspace was active, clean up state.  The backend
      // pushes the unified snapshot after removal — nothing to fetch here,
      // and no list clear: the pushed sessions_list replaces the rows in
      // one step (clearing would flash an empty sidebar first).
      if (state.getState().activeWorkspace === (event as any).path) {
        state.setActiveWorkspace("");
        state.setWorkspaceMode("normal");
        state.setSessionState("idle");
      }
      (window as any).__sessionInner?.requestRender?.();
      break;

    case "assistant_boundary":
      // Intra-turn segment marker — no longer creates a new message bubble.
      // The session now keeps a single assistant message per user turn, so
      // splitting on this boundary would leave an orphan "empty" bubble and
      // duplicate the assistant output in the timeline.
      _syncSessionEntry(state.getState().sessionId, state.getState());
      break;

    case "branch_updated": {
      const ev = event as BranchUpdated;
      if (_shouldRejectSessionScopedEvent(ev)) break;
      state.setBranches(ev.branches, ev.active_branch_id, ev.session_id);
      // Only replace messages when the session is NOT running.
      // During retry (running=true), the frontend manages messages locally
      // (removes old assistant + streams new), and replacing them would
      // orphan the streaming placeholder.
      if (ev.messages && !state.getState().running) {
        state.loadSessionMessages(ev.messages, ev.session_id);
      }
      chat?.requestRender();
      (window as any).__sessionInner?.requestRender?.();
      break;
    }

    case "branch_switched": {
      const ev = event as BranchSwitched;
      if (_shouldRejectSessionScopedEvent(ev)) break;
      // A branch switch is only the rollback's own reply when it belongs to
      // the conversation the marker was started in. A switch from anywhere
      // else must not consume the pending edit -- and a marker for another
      // session is stale by the time the user is navigating this branch.
      if (!state.hasPendingRollbackEdit(ev.session_id)) state.clearPendingRollbackEdit();
      state.setBranches(ev.branches, ev.branch_id, ev.session_id);
      state.loadSessionMessages(state.applyPendingRollbackEdit(ev.messages, ev.session_id), ev.session_id);
      if (ev.artifacts) {
        state.setArtifacts(ev.artifacts, ev.session_id);
      }
      if (ev.references) {
        state.setReferences(ev.references, ev.session_id);
      }
      // Ensure the session is marked as not running and the session ID is
      // synced so the next user message is sent immediately rather than
      // being queued waiting for a "running=false" transition that never comes.
      state.setSessionState("idle", ev.session_id);
      state.setSessionId(ev.session_id);
      // Branch switch replaces the whole timeline: pin the next render to
      // the bottom so the user lands on the last turn of the new branch.
      chat?.requestPinToBottom?.();
      chat?.requestRender();
      (window as any).__sessionInner?.requestRender?.();
      break;
    }

    case "branch_rolled_back": {
      const ev = event as BranchRolledBack;
      state.removeBranchMessages(new Set(ev.removed_message_ids), ev.session_id);
      chat?.requestRender();
      (window as any).__sessionInner?.requestRender?.();
      break;
    }

    case "automation_jobs_list":
      // Single source of truth: the automation panel reads jobs from state
      // (same as automationHistory) — no separate callback cache.
      state.setAutomationJobs(event.jobs);
      break;

    case "automation_job_history":
      // Store in global state so automation panel reads from the same
      // data source as general/workspace mode (sessionsList).
      state.setAutomationHistory(event.history || []);
      break;

    case "automation_job_created":
      _automationJobCreatedCallback?.(event);
      // Refresh list after creating
      send({ type: "automation_list_jobs" });
      break;

    case "automation_job_cancelled":
      _automationJobCancelledCallback?.(event.job_id);
      send({ type: "automation_list_jobs" });
      break;

    case "automation_job_toggled":
      send({ type: "automation_list_jobs" });
      break;

    case "automation_job_updated":
      _automationJobUpdatedCallback?.();
      send({ type: "automation_list_jobs" });
      break;

    case "automation_job_deleted":
      send({ type: "automation_list_jobs" });
      send({ type: "automation_get_history" });
      break;

    case "automation_execution_deleted":
      send({ type: "automation_get_history" });
      break;

    case "automation_execution_renamed":
      send({ type: "automation_get_history" });
      break;

    case "automation_stream_event":
      _automationStreamCallback?.(event as import("./types.js").AutomationStreamEvent);
      break;

    case "automation_job_update":
      // A job's state changed (e.g. running → completed) — refresh lists
      // Use inline history data so the view updates immediately
      if ((event as any).history) {
        state.setAutomationHistory((event as any).history);
      }
      // If the backend included result data, show it in the chat area
      if ((event as any).result && _automationShowResultCallback) {
        _automationShowResultCallback((event as any).result);
      }
      send({ type: "automation_list_jobs" });
      break;

    case "workflow_started":
      if (_shouldRejectSessionScopedEvent(event)) break;
      state.setWorkflowState({
        workflowId: (event as any).workflow_id,
        goal: (event as any).goal,
        totalTasks: (event as any).total_tasks,
        taskIds: (event as any).task_ids,
        active: true,
        completedCount: 0,
        failedCount: 0,
        skippedCount: 0,
      });
      chat?.requestRender();
      break;

    case "workflow_task":
      if (_shouldRejectSessionScopedEvent(event)) break;
      state.updateWorkflowTask({
        workflowId: (event as any).workflow_id,
        taskId: (event as any).task_id,
        taskName: (event as any).task_name,
        status: (event as any).status,
      });
      chat?.requestRender();
      break;

    case "workflow_completed":
      if (_shouldRejectSessionScopedEvent(event)) break;
      state.setWorkflowState({
        workflowId: (event as any).workflow_id,
        goal: (event as any).goal,
        totalTasks: state.getState().workflowState?.totalTasks ?? 0,
        taskIds: state.getState().workflowState?.taskIds ?? [],
        active: false,
        success: (event as any).success,
        completedCount: (event as any).completed_count,
        failedCount: (event as any).failed_count,
        skippedCount: (event as any).skipped_count,
        totalDuration: (event as any).total_duration,
      });
      chat?.requestRender();
      break;

    default:
      break;
  }
  // Attribution for the remaining synchronous work (state application and
  // subscribers that run inside this task). Scheduled paints are logged
  // separately by the render scheduler.
  const _evtMs = performance.now() - _perfNow;
  if (_evtMs >= 30) {
    console.warn(`[perf] handleEvent ${_perfType} ${Math.round(_evtMs)}ms`);
  }
}

let _lastSync = "";

/** If the sub-agent view is currently pointing at a tool call that just
 *  received a fresh sub_agent_messages payload, re-anchor the view to the
 *  updated tool call state so the timeline keeps streaming. */
function _syncSubAgentView(toolCallId: string): void {
  const st = state.getState();
  const view = st.subAgentView;
  if (!view || !state.toolCallMatchesId(view, toolCallId)) return;
  const fresh = state.findToolCall(toolCallId);
  if (!fresh) return;
  // The combined view (no task picked) now tiles each parallel task into its
  // own pane (fullRenderParallel), so it is a legitimate live view and no
  // longer dropped back to the parent transcript on the first tick.
  state.setSubAgentView({ ...fresh, taskIndex: view.taskIndex });
}

/** Update the sidebar session entry to reflect current message count + preview.
 *  Called from streaming event handlers so the sidebar stays live during a run.
 *  IMPORTANT: always read from the target session's own snapshot, never from
 *  the currently active session, otherwise background sessions overwrite the
 *  wrong sidebar entry when the user switches sessions while one is running.
 *  Also: derive `last_active` and `created_at` from message timestamps so
 *  the displayed time reflects the real last conversation (matching the
 *  backend's `sess.last_message_at` semantics), NOT wall-clock `Date.now()`. */
function _syncSessionEntry(sessionId: string, st: ReturnType<typeof state.getState>, light = false): void {
  if (!sessionId) return;
  if (st.tempChat) return;
  // A session that was ever a temp chat must never be (re-)added to the
  // sidebar, even after tempChat was switched off while its delete was
  // still in flight or its stream events were still arriving.
  if (state.isTempChatSession(sessionId)) return;
  // External-channel sessions (iclaw, telegram, qq, ...) must never be
  // synced into the normal/iwork sidebar -- not even the "update found
  // entry" path, since a leaked entry should be dropped, not refreshed.
  if (state.isExternalChannelSession(sessionId)) return;
  const snapshot = st.sessionStore[sessionId] || { messages: [], running: false, sessionState: "idle" as const };
  const snapMsgs = snapshot.messages;
  const lastContent = snapMsgs.length > 0 ? String(snapMsgs[snapMsgs.length - 1].content || "").slice(0, 20) : "";
  const key = `${sessionId}:${snapMsgs.length}:${lastContent}`;
  if (key === _lastSync) return;
  // Light mode (per-token delta path): skip the sidebar rebuild unless the
  // message count changed. Content-only changes carry no information the
  // sidebar displays (preview comes from the first user message), yet each
  // one re-renders the entire session list — that was the other half of the
  // mid-stream animation freeze.
  if (light && _lastSync.startsWith(`${sessionId}:${snapMsgs.length}:`)) return;
  _lastSync = key;
  const firstUser = snapMsgs.find(m => m.role === "user");
  const preview = firstUser?.content?.slice(0, 100) || "";
  const isRunning = snapshot.running ?? false;
  // Keep the precise state so a session parked on a permission prompt stays
  // "awaiting_approval" (yellow) instead of being flattened to "running"
  // (green) by the coarse boolean every time messages stream in.
  const entryState: SessionState = snapshot.sessionState ?? (isRunning ? "running" : "idle");
  // Derive sidebar ordering timestamps from the real message stream so the
  // user sees when the conversation actually happened, not when this sync
  // function happened to be called. Message.timestamp is in milliseconds
  // (see Message interface); SessionEntryData.* uses Unix seconds.
  const nowSec = Date.now() / 1000;
  const lastMsgMs = snapMsgs.length > 0 ? snapMsgs[snapMsgs.length - 1].timestamp : 0;
  const firstMsgMs = snapMsgs.length > 0 ? snapMsgs[0].timestamp : 0;
  const derivedLastActive = lastMsgMs > 0 ? Math.floor(lastMsgMs / 1000) : nowSec;
  const derivedCreatedAt = firstMsgMs > 0 ? Math.floor(firstMsgMs / 1000) : nowSec;
  const found = st.sessionsList.some(e => e.session_id === sessionId);
  if (found) {
    state.setSessionsList(
      st.sessionsList.map(e =>
        e.session_id === sessionId
          ? {
              ...e,
              // Refresh last_active so the sidebar re-orders when this session
              // receives a new message. We clamp to never go backwards.
              last_active: Math.max(e.last_active ?? 0, derivedLastActive),
              message_count: snapMsgs.length,
preview,
               state: entryState,
             }
          : e
      )
    );
    // Keep the tray popup's status lights in sync with the sidebar.
    state.syncTraySessionState(sessionId, entryState);
    return;
  }
  const tray = state.getTraySessions();
  const inIworkTray = tray.iwork.some(e => e.session_id === sessionId);
  const inNormalTray = tray.normal.some(e => e.session_id === sessionId);
  const trueChannel = inIworkTray ? "iwork" : inNormalTray ? "normal" : "";
  const currentChannel = st.workspaceMode === "iwork" ? "iwork" : "normal";
  if (trueChannel && trueChannel !== currentChannel) return;
  const channel = trueChannel || currentChannel;
  const known = [...tray.iwork, ...tray.normal].find(e => e.session_id === sessionId);
  const knownMeta = (known?.metadata || {}) as Record<string, unknown>;
  const knownWs = (knownMeta.workspace || knownMeta.workspace_path) as string | undefined;
  state.setSessionsList([...st.sessionsList, {
    session_id: sessionId,
    message_count: snapMsgs.length,
    preview,
    created_at: derivedCreatedAt,
    last_active: derivedLastActive,
    state: entryState,
    channel,
    metadata: {
      workspace: knownWs || (channel === "iwork" ? st.activeWorkspace : undefined),
      workspace_path: knownWs || (channel === "iwork" ? st.activeWorkspace : undefined),
    },
  }]);
  // Keep the tray popup's status lights in sync with the sidebar.
  state.syncTraySessionState(sessionId, entryState);
}

/** Triggers a markdown file download in the browser. */
export function downloadMarkdownFile(markdown: string, filename: string): void {
  const sanitized = filename.replace(/[<>:"/\\|?*]/g, "_");
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = sanitized;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function normalizePermissionPolicies(raw: unknown): import("./types.js").PermissionPolicies {
  const empty: import("./types.js").PermissionPolicies = { tools: {}, capabilities: {} };
  if (!raw || typeof raw !== "object") return empty;
  const result: import("./types.js").PermissionPolicies = { tools: {}, capabilities: {} };
  const capabilityKeys = new Set([
    "network", "file", "bash_io", "docker", "browser",
    "workflow", "git", "deploy", "desktop", "database", "misc", "mcp",
  ]);
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const policy = normalizePolicyValue(value);
    if (policy) {
      if (capabilityKeys.has(key)) result.capabilities[key] = policy;
      else result.tools[key] = policy;
    }
  }
  return result;
}

function normalizePolicyValue(raw: unknown): import("./types.js").PermissionPolicy | null {
  if (typeof raw === "string") {
    const value = raw as "allow" | "deny" | "ask" | "default";
    if (["allow", "deny", "ask", "default"].includes(value)) {
      return { value };
    }
  }
  if (raw && typeof raw === "object") {
    const obj = raw as Record<string, unknown>;
    const value = obj.value;
    if (typeof value === "string" && ["allow", "deny", "ask", "default"].includes(value)) {
      return {
        value: value as "allow" | "deny" | "ask" | "default",
        source: typeof obj.source === "string" ? (obj.source as any) : undefined,
      };
    }
  }
  return null;
}
