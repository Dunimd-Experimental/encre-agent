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

import { ServerEvent, WorkspaceEntry, BranchUpdated, BranchSwitched, BranchRolledBack, UsageStatsEvent, SessionState } from "./types.js";
import * as state from "./state.js";
import { send, getWorkspaceIntent } from "./ws.js";
import { Chat } from "../chat/chat.js";
import { Tools } from "../features/tools.js";
import { Permissions } from "../features/permissions.js";
import { Settings } from "../settings/settings.js";
import { t } from "../features/i18n.js";
import { Dialog } from "../ui/dialog.js";
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
/** Registers a callback for WeChat QR code scan results. */
export function onWechatScanResult(cb: (event: WechatScanResultEvent) => void): void {
  _wechatScanCallback = cb;
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
import { applyServerCommands, applyProjectCommands } from "../features/slash_commands.js";

let chat: Chat | null = null;
let tools: Tools | null = null;
let permissions: Permissions | null = null;
let _settings: Settings | null = null;
let permissionResolve: ((allowed: boolean) => void) | null = null;
let _sessionGeneration = 0;
let _requestedSessionId = "";
let _requestedSessionRequestId = "";
let _activeStreamSessionId = "";
let _toolCallGeneration: Record<string, number> = {};
let _validationResolve: (() => void) | null = null;
let _validationReject: ((reason: string) => void) | null = null;

function _hasSessionId(event: { session_id?: string | null }): event is { session_id: string } {
  return typeof event.session_id === "string" && event.session_id.length > 0;
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
    type === "compact_starting";
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
 *  assistant exists yet. */
function _ensureAssistantMessage(sessionId?: string): void {
  const st = state.getState();
  // Bail early if the event belongs to a different session than the one
  // being viewed — never mutate another session's messages from here.
  const resolvedSid = sessionId || st.sessionId;
  if (resolvedSid && resolvedSid !== st.sessionId) return;

  let msgs = st.messages;
  const last = msgs[msgs.length - 1];
  let shouldSendRun = false;
  if (last?.role !== "user" && st.queuedPrompts.length > 0 && !st.running) {
    const qp = st.queuedPrompts[0];
    state.addUserMessage(qp.text, qp.mode);
    msgs = state.getState().messages;
    shouldSendRun = true;
  }
  const lastAfter = msgs[msgs.length - 1];
  if (lastAfter?.role !== "user") return;
  const streamingAsst = msgs.find(m => m.role === "assistant" && m.isStreaming);
  if (streamingAsst) return;
  state.startAssistantMessage();
  if (shouldSendRun) {
    state.setSessionState("running");
    state.shiftQueuedPrompt();
    state.setPendingQueueCount(Math.max(0, st.pendingQueueCount - 1));
    const text = typeof lastAfter.content === "string" ? lastAfter.content : "";
    if (text) {
      // Use the explicit session id passed in (already validated above
      // to match st.sessionId) so we always record the right owner.
      _activeStreamSessionId = st.sessionId || "";
      // A brand-new turn: reset the auto-open dismiss guard so the first
      // sub-agent of this turn opens live again (per the auto-open setting).
      (window as any).__subAgentAutoOpenDismissed = false;
      send({ type: "run", prompt: text, session_id: st.sessionId || undefined } as any);
    }
  }
}

function _eventSessionId(event: { session_id?: string }): string {
  return event.session_id || state.getState().sessionId || "";
}

/** Records the requested session id and its matching request id. */
export function setRequestedSessionId(sid: string, requestId = ""): void {
  _requestedSessionId = sid;
  _requestedSessionRequestId = requestId;
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
  send({ type: "list_documents" } as any);
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
    if (st.sessionsList.length === 0 && st.allSessions.length === 0) {
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
}

/** Resolves once model validation has completed. */
export function waitForModelValidation(): Promise<void> {
  return new Promise((resolve, reject) => {
    _validationResolve = resolve;
    _validationReject = reject;
    setTimeout(() => {
      if (_validationReject) {
        _validationReject(t("stream.validationTimedOut"));
        _validationResolve = null;
        _validationReject = null;
      }
    }, 30000);
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
 * (the automation jobs plane is deliberately NOT deferred: the automation view
 * is a first-class startup surface and must be populated when it is revealed).
 */
const BOOT_DEFERRED_EVENTS: ReadonlySet<string> = new Set([
  "global_rules_list",
  "memory_list",
  "documents_list",
  "usage_stats",
  "project_rules_list",
  "project_hooks_list",
  "archived_sessions_list",
  "index_status",
  "profile_data",
  "skills_list",
  "memory_detail",
  "gitignore_content",
]);

let _bootGateHolding = false;
let _bootDeferredQueue: ServerEvent[] = [];
let _bootGateSafetyTimer: ReturnType<typeof setTimeout> | null = null;

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

/** Applies every plane held back during boot, in arrival order. */
export function releaseNonCriticalPlanes(): void {
  if (_bootGateSafetyTimer !== null) {
    clearTimeout(_bootGateSafetyTimer);
    _bootGateSafetyTimer = null;
  }
  if (!_bootGateHolding) return;
  _bootGateHolding = false;
  const queued = _bootDeferredQueue;
  _bootDeferredQueue = [];
  for (const ev of queued) {
    handleEvent(ev);
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
      const switched = previousSid && previousSid !== nextSid;
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
        state.loadSessionMessages(event.messages, event.session_id);
        console.log("[stream] loadSessionMessages done, state.messages.length:", state.getState().messages.length);
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
      // Restore branch state
      if (event.branches && event.active_branch_id) {
        state.setBranches(event.branches, event.active_branch_id, event.session_id);
      }
      // Re-render when session changes (skip sub-agent sessions)
      chat?.renderForce?.();
      (window as any).__sessionInner?.render?.();
      _requestedSessionId = "";
      _requestedSessionRequestId = "";
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
      _ensureAssistantMessage(_thSid);
      state.appendThinkingDelta(event.text, _thSid);
      chat?.requestRender();
      _syncSessionEntry(_activeThSid, state.getState(), true);
      break;
    }

    case "tool_call_start": {
      // Track which session generation this tool call belongs to
      _toolCallGeneration[event.id] = _sessionGeneration;
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
      }
      tools?.requestRender();
      chat?.renderForce?.();
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
      // Force re-render so question card shows parsed params
      chat?.renderForce?.();
      break;
    }

    case "tool_progress":
      if (_toolCallGeneration[event.id] !== _sessionGeneration) {
        console.log("[stream] tool_progress REJECTED by generation check, id:", event.id, "tc_gen:", _toolCallGeneration[event.id], "session_gen:", _sessionGeneration);
        break;
      }
      console.log("[stream] tool_progress accepted, id:", event.id, "sub_agent_messages:", event.sub_agent_messages?.length ?? 0);
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
      tools?.requestRender();
      chat?.render();
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
      if (!_hasSessionId(event)) break;
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
      // For error finishes without a streaming assistant (e.g. API failed
      // immediately, no text_delta received), create a placeholder so the
      // error card renders on the correct turn rather than the previous one.
      if (event.reason === "error" || event.error) {
        const msgs = state.getState().messages;
        const hasStreaming = msgs.some(m => m.role === "assistant" && m.isStreaming);
        if (!hasStreaming) {
          state.startAssistantMessage();
        }
      }
      // Store server-side message ID before finishing — emit() in
      // finishAssistantMessage will trigger segment cache save with this set.
      const sid = _eventSessionId(event);
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
      chat?.render();
      (window as any).__sessionInner?.render?.();
      break;
    }

    case "run_queued":
      state.setPendingQueueCount(event.position);
      chat?.render();
      break;

    case "pong":
      state.setConnected(true);
      break;

    case "error":
      {
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
      if (!_hasSessionId(event)) break;
      _activeStreamSessionId = "";
      // Background session error — update snapshot without touching active UI.
      if (event.session_id && event.session_id !== state.getState().sessionId) {
        state.setSessionState("idle", event.session_id);
        break;
      }
      state.setSessionState("idle", _eventSessionId(event));
      state.clearPendingQueueCount();
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
      // Rollback/branch operations are optimistically truncated in the UI before
      // the server confirms. When the server rejects them ("message not found", 
      // "branch not found", retry of a missing user message), re-request the
      // authoritative session state so the frontend stops rendering the ghost
      // user message and matches the real backend conversation.
      if (_errCode === "rollback_error" || _errCode === "branch_not_found" || _errCode === "retry_error") {
        const _rs = state.getState().sessionId;
        const _rid = crypto.randomUUID();
        setRequestedSessionId(_rs, _rid);
        send({ type: "resume", session_id: _rs, request_id: _rid } as any);
      }
      break;

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
      chat?.render();
      (window as any).__sessionInner?.render?.();
      break;

    case "plan_proposal":
      if (!_hasSessionId(event)) break;
      state.addPlanProposal(event, _eventSessionId(event));
      break;

    case "plan_mode_changed":
      if (!_hasSessionId(event)) break;
      state.setPlanModeActive(event.active, _eventSessionId(event));
      (window as any).__sessionInner?.render?.();
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
      (window as any).__sessionInner?.render?.();
      break;

    case "models_list":
      state.setAvailableModels(event.models);
      break;

    case "sessions_list": {
      const currentChannel = state.getState().workspaceMode === "iwork" ? "iwork" : "normal";
      if (event.channel && event.channel !== currentChannel) break;

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
      const cfg = event.config as Record<string, unknown>;
      if (cfg.models && Array.isArray(cfg.models)) {
        state.setModelConfigs(cfg.models as any[], (cfg.active_model_index as number) || 0);
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
      // Restore persisted active_command from config_data (survives restart).
      // The snapshot is tagged with the session it was built from: only
      // mirror it when it describes the session actually being viewed,
      // otherwise another session's sticky command leaks into this one and
      // resurrects the chip after restarts / config refreshes.
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
      window.dispatchEvent(new CustomEvent("slash-commands-updated"));
      if (cfg.sub_agents && Array.isArray(cfg.sub_agents)) {
        state.setSubAgents(cfg.sub_agents as any[]);
      }
      state.setAgentConfig({
        system_prompt: (cfg.system_prompt as string) || "",
        specialty: (cfg.default_specialty as string) || "general",
        permission_mode: (cfg.permission_mode as string) || "default",
        max_turns: (cfg.max_turns as number) ?? 0,
      });
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
      if (!state.getState().activeWorkspace) {
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
      ] as const;
      for (const key of _generalKeys) {
        const val = cfg[key];
        if (val !== undefined && val !== null && val !== "") {
          _settingsUpdate[key] = val;
        }
      }
      // Always ensure startup_session_mode is set so the startup mode subscribe fires
      _settingsUpdate["startup_session_mode"] = _settingsUpdate["startup_session_mode"] || cfg["startup_session_mode"] || "normal";
      if (Object.keys(_settingsUpdate).length > 0) {
        state.setSettings({ ...state.getState().settings, ..._settingsUpdate });
      }
      // Note: locale is a frontend preference stored in localStorage.
      // The backend's language field is only synced for settings display;
      // do NOT call setLocale() here or it would override the user's choice
      // when the backend (default zh) pushes config asynchronously.
      // Sync keybinds from backend config
      const cfgAny = cfg as any;
      if (cfgAny.keybinds && typeof cfgAny.keybinds === "object" && Array.isArray(cfgAny.keybinds.keybinds)) {
        state.setSettings({ ...state.getState().settings, keybinds: cfgAny.keybinds });
      } else {
        // Backend sent no keybinds or corrupted data — try localStorage fallback
        try {
          const saved = localStorage.getItem("encre_keybinds");
          if (saved) {
            const parsed = JSON.parse(saved);
            if (parsed && typeof parsed === "object" && Array.isArray(parsed.keybinds)) {
              state.setSettings({ ...state.getState().settings, keybinds: parsed });
            }
          }
        } catch { /* ignore */ }
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
      break;
    }

    case "models_updated": {
      console.log("[stream] models_updated, count:", event.models?.length, "active_index:", event.active_model_index);
      state.setModelConfigs(event.models, event.active_model_index);
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

    case "index_status":
      state.setIndexStatus(event.status as any);
      state.setIndexFiles(event.files);
      if (event.progress !== undefined) {
        state.setIndexProgress(event.progress);
      }
      // Also update per-workspace index state for sidebar tree
      if ((event as any).workspace_id) {
        state.updateWorkspaceIndex(
          (event as any).workspace_id,
          event.status as string,
          event.files,
          event.progress,
        );
      }
      break;

    case "gitignore_content":
      state.setGitignoreContent(event.content);
      break;

    case "documents_list":
      state.setDocsList(event.documents);
      break;

    case "document_added":
    case "document_updated":
    case "document_removed":
      // The backend pushes the refreshed documents_list with each document
      // operation — nothing to fetch here.
      break;

    case "document_error":
      state.showToast(t("common.documentError"), "", "error", "Index");
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

    case "compact_starting":
      if (!_hasSessionId(event as any)) break;
      state.addCompactStartingEvent(_eventSessionId(event as any));
      break;

    case "compact_failed":
      // Compaction did not produce a result (failure or no-op): dismiss
      // the "compressing" indicator; the conversation continues with the
      // uncompressed context.
      if (!_hasSessionId(event as any)) break;
      state.clearCompactStartingEvents();
      break;

    case "compact":
      if (!_hasSessionId(event as any)) break;
      // Compaction finished: drop the "compressing" indicator; the inline
      // divider comes from the compact_marker in the refreshed messages.
      state.clearCompactStartingEvents();
      state.addCompactEvent({
        old_count: (event as any).old_count,
        new_count: (event as any).new_count,
        old_tokens: (event as any).old_tokens,
        new_tokens: (event as any).new_tokens,
      }, _eventSessionId(event as any));
      if ((event as any).messages) {
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
        (window as any).__sessionInner?.render?.();
      }
      break;

    case "plan_review":
      {
        const sev = event as any;
        const review = sev.review || {};
        const existing = state.getState().planReview || {} as any;
        state.updatePlanReview({
          review_id: review.review_id || existing.review_id || "",
          content: review.content || existing.content || "",
          file_path: review.file_path || existing.file_path || "",
          dir_path: review.dir_path || existing.dir_path || "",
          mode: review.mode || existing.mode || "",
          status: sev.status || existing.status || "review",
        }, _eventSessionId(sev));
      }
      break;

    case "context_usage":
      if (!_hasSessionId(event as any)) break;
      state.updateContextUsage(
        (event as any).context_tokens,
        (event as any).context_window,
        _eventSessionId(event as any),
      );
      (window as any).__sessionInner?.render?.();
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
      (window as any).__sessionInner?.render?.();
      break;

    case "artifacts_update":
      if (!_hasSessionId(event)) break;
      // Append new artifacts; the server sends one at a time during streaming.
      state.appendArtifacts(event.artifacts, _eventSessionId(event));
      (window as any).__sessionInner?.render?.();
      break;

    case "references_update":
      if (!_hasSessionId(event)) break;
      // Append new references; the server sends one at a time during streaming.
      state.appendReferences(event.references, _eventSessionId(event));
      (window as any).__sessionInner?.render?.();
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
        break;
      }
      // Sync session ID and reset running state for reliable subsequent sends.
      // Do NOT call resetChat() on empty messages — that clears sessionId and
      // locks the user out of sending further messages on the same session.
      if (event.session_id) {
        state.setSessionId(event.session_id);
      }
      state.setSessionState("idle", _eventSessionId(event));
      state.loadSessionMessages(state.applyPendingRollbackEdit(event.messages || []), _eventSessionId(event));
      state.setPlanItems(event.plan_items || [], _eventSessionId(event));
      if (event.artifacts) {
        state.setArtifacts(event.artifacts, _eventSessionId(event));
      }
      if (event.references) {
        state.setReferences(event.references, _eventSessionId(event));
      }
      chat?.render();
      (window as any).__sessionInner?.render?.();
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
      state.loadSessionMessages(state.applyPendingRollbackEdit(event.messages || []), _eventSessionId(event));
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
      // Restore attachment chips from the rolled-back user message
      {
        const msgs = state.getState().messages;
        for (let i = msgs.length - 1; i >= 0; i--) {
          const refs = msgs[i].fileRefs;
          if (msgs[i].role === "user" && refs && refs.length > 0) {
            const atts: import("./types.js").AttachmentMeta[] = [];
            for (const r of refs) {
              if (r.mime_type === "text/x-terminal") continue;
              if (r.path) {
                atts.push({ name: r.name, path: r.path, content: "", mime_type: r.mime_type || "", size: r.size, is_binary: false });
              }
            }
            if (atts.length > 0) state.addAttachments(atts);
            break;
          }
        }
      }
      _syncSessionEntry(event.session_id, state.getState());
      chat?.renderForce?.();
      (window as any).__sessionInner?.render?.();
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
      chat?.render();
      (window as any).__sessionInner?.render?.();
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
            container: document.getElementById("settings-view") ?? undefined,
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
      (window as any).__sessionInner?.render?.();
      break;

    case "model_validated":
      if (_validationResolve) {
        _validationResolve();
        _validationResolve = null;
        _validationReject = null;
      }
      break;

    case "model_validation_error":
      if (_validationReject) {
        _validationReject(event.message);
        _validationResolve = null;
        _validationReject = null;
      }
      break;

    case "workspace_opened":
      state.setWorkspaces(event.workspaces);
      // Stale-open guard: `open_workspace` is answered asynchronously and
      // `workspace_opened` carries no request id. If the user already left
      // (a close_workspace was sent after the open), applying this would drag
      // them back into iWork a few seconds later. Keep the refreshed
      // workspace list but ignore the mode switch.
      if (getWorkspaceIntent() === "close") {
        (window as any).__sessionInner?.render?.();
        break;
      }
      state.setActiveWorkspace(event.path);
      state.setWorkspaceMode("iwork");
      state.setSessionsList([]);
      // Capture initial index state for sidebar tree display
      if ((event as any).index_status) {
        state.updateWorkspaceIndex(
          (event as any).id || "",
          (event as any).index_status,
          (event as any).index_files || 0,
          (event as any).progress,
        );
      }
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
      // Refresh every data plane (sessions, config+models, workspaces) so the
      // sidebar, model selector and settings all reflect the new workspace
      // context instead of keeping the previous session's stale data.
      refreshAllData();
      (window as any).__sessionInner?.render?.();
      break;

    case "workspace_closed":
      // Symmetric stale-close guard: if the user already asked for a new
      // workspace (open_workspace sent after this close), this late close
      // must not drop them back to normal — the pending open will land next.
      if (getWorkspaceIntent() === "open") {
        (window as any).__sessionInner?.render?.();
        break;
      }
      state.setActiveWorkspace("");
      state.setWorkspaceMode("normal");
      state.setSessionsList([]);
      // Back to the global/normal context: pull the full data set again so
      // the sidebar / model selector / settings show the global state.
      refreshAllData();
      state.setSessionState("idle");
      (window as any).__sessionInner?.render?.();
      break;

    case "workspaces_list":
      state.setWorkspaces(event.workspaces);
      (window as any).__sessionInner?.render?.();
      break;

    case "workspace_config":
      state.setWorkspaceConfig(event.path, event.config);
      if (event.files) state.setWorkspaceFiles(event.path, event.files);
      (window as any).__workspaceMgr?.refresh?.();
      break;

    case "workspace_removed":
      state.setWorkspaces(event.workspaces);
      // If the deleted workspace was active, clean up state.  The backend
      // pushes the unified snapshot after removal — nothing to fetch here.
      if (state.getState().activeWorkspace === (event as any).path) {
        state.setActiveWorkspace("");
        state.setWorkspaceMode("normal");
        state.setSessionsList([]);
        state.setSessionState("idle");
      }
      (window as any).__sessionInner?.render?.();
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
      chat?.render();
      (window as any).__sessionInner?.render?.();
      break;
    }

    case "branch_switched": {
      const ev = event as BranchSwitched;
      if (_shouldRejectSessionScopedEvent(ev)) break;
      state.setBranches(ev.branches, ev.branch_id, ev.session_id);
      state.loadSessionMessages(state.applyPendingRollbackEdit(ev.messages), ev.session_id);
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
      chat?.render();
      (window as any).__sessionInner?.render?.();
      break;
    }

    case "branch_rolled_back": {
      const ev = event as BranchRolledBack;
      state.removeBranchMessages(new Set(ev.removed_message_ids), ev.session_id);
      chat?.render();
      (window as any).__sessionInner?.render?.();
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
      chat?.render();
      break;

    case "workflow_task":
      if (_shouldRejectSessionScopedEvent(event)) break;
      state.updateWorkflowTask({
        workflowId: (event as any).workflow_id,
        taskId: (event as any).task_id,
        taskName: (event as any).task_name,
        status: (event as any).status,
      });
      chat?.render();
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
      chat?.render();
      break;

    default:
      break;
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
  const dividerCount = fresh.subAgentMessages?.filter((m) => m.mode === "task_divider").length ?? 0;
  // Parallel runs now surface each task as its own card in the parent
  // transcript. Drop the combined sub-agent view unless the user already
  // picked a specific task to inspect.
  if (dividerCount >= 2 && view.taskIndex === undefined) {
    state.setSubAgentView(null);
    return;
  }
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
