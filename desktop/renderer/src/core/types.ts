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
 * Shared type definitions for the renderer.
 *
 * The single source of truth for every TypeScript type used across the
 * renderer: the server→client {@link ServerEvent} union, the client→server
 * {@link ClientMessage} union, the global {@link AppState}, message/tool-call
 * shapes, settings/config payloads, and all entity records (sessions,
 * workspaces, models, MCP, skills, memories, notifications, …).
 */

export type ServerEvent =
  | TextDelta
  | ThinkingDelta
  | ToolCallStart
  | ToolCallDelta
  | ToolCallEnd
  | ToolProgress
  | ToolResult
  | FinishEvent
  | PongEvent
  | ErrorEvent
  | SessionReady
  | ConfiguredEvent
  | TelemetryEvent
  | PlanUpdate
  | PlanProposal
  | PlanModeChanged
  | PlanResolved
  | ArtifactsUpdate
  | AssistantBoundaryEvent
  | CompactStartingEvent
  | CompactFailedEvent
  | CompactEvent
  | SystemMessageEvent
  | ModelsList
  | SessionsList
  | SessionsAll
  | ArchivedSessionsList
  | ConfigData
  | ModelsUpdated
  | ModelsFetched
  | ModelValidated
  | ModelValidationError
  | SkillsUpdated
  | SkillsList
  | SkillInstalled
  | SkillInstallError
  | SkillUninstalled
  | McpUpdated
  | AgentUpdated
  | AgentStateEvent
  | SearchResults
  | RollbackLogEvent
  | RollbackCheckoutEvent
  | MessagesUpdated
  | SessionDeleted
  | SessionExported
  | SessionsExportedZip
  | DataExportedZip
  | DataExportProgress
  | DataImportProgress
  | DataImportDone
  | SessionRenamed
  | SubAgentsUpdated
  | WorkspaceOpened
  | WorkspacesList
  | WorkspaceRemoved
  | WorkspaceConfigResponse
  | WorkspaceClosed
  | MemoryList
  | MemoryDetailEvent
  | GlobalRulesList
  | ProjectRulesList
  | ProjectHooksList
  | GlobalRuleSaved
  | GlobalRuleDeleted
  | GlobalRuleContentEvent
  | ProfileMessage
  | IndexStatusEvent
  | GitignoreContentEvent
  | DocumentsListEvent
  | DocumentAddedEvent
  | DocumentUpdatedEvent
  | DocumentRemovedEvent
  | DocumentErrorEvent
  | PermissionRequest
  | EngineInstallRequestEvent
  | EngineInstallProgressEvent
  | EngineInstallResponseAck
  | BranchUpdated
  | BranchSwitched
  | BranchRolledBack
  | UsageStatsEvent
  | GatewayStatusEvent
  | RunQueued
  | WechatScanResultEvent
  | AutomationJobsList
  | AutomationJobHistory
  | AutomationJobCreated
  | AutomationJobCancelled
  | AutomationJobUpdate
  | AutomationJobToggled
  | AutomationJobUpdated
  | AutomationJobDeleted
  | AutomationExecutionDeleted
  | AutomationExecutionRenamed
  | AutomationStreamEvent
  | WorkflowStarted
  | WorkflowTask
  | WorkflowCompleted
  | ContextUsageEvent
  | ReferencesUpdateEvent
  | SpecUpdateEvent
  | PlanReviewEvent
  | ModeChanged
  | CommandChanged;

export interface RunQueued {
  type: "run_queued";
  position: number;
}

export interface WorkflowStarted {
  type: "workflow_started";
  workflow_id: string;
  goal: string;
  total_tasks: number;
  task_ids: string[];
  session_id?: string;
}

export interface WorkflowTask {
  type: "workflow_task";
  workflow_id: string;
  task_id: string;
  task_name: string;
  status: string;
  session_id?: string;
}

export interface WorkflowCompleted {
  type: "workflow_completed";
  workflow_id: string;
  goal: string;
  success: boolean;
  completed_count: number;
  failed_count: number;
  skipped_count: number;
  total_duration: number;
  session_id?: string;
}

export interface SearchResultEntry {
  kind: string;
  session_id?: string;
  role?: string;
  snippet: string;
  preview?: string;
  path?: string;
  line?: number;
  name?: string;
  icon?: string;
  workspace_id?: string;
}

/** Keys for the user-configurable search filter (Settings → Search Filter). */
export type SearchFilterKey =
  | "session"
  | "memory"
  | "global_rule"
  | "project_rule"
  | "skill"
  | "workspace"
  | "workspace_file"
  | "model"
  | "mcp_server"
  | "sub_agent"
  | "document"
  | "notification"
  | "settings"
  | "action"
  | "slash_command"
  | "custom_command"
  | "tool"
  | "automation"
  | "workflow";

export type SearchFilter = Record<SearchFilterKey, boolean>;

/** All search-filter keys enabled by default. */
export function defaultSearchFilter(): SearchFilter {
  return {
    session: true,
    memory: true,
    global_rule: true,
    project_rule: true,
    skill: true,
    workspace: true,
    workspace_file: true,
    model: true,
    mcp_server: true,
    sub_agent: true,
    document: true,
    notification: true,
    settings: true,
    action: true,
    slash_command: true,
    custom_command: true,
    tool: true,
    automation: true,
    workflow: true,
  };
}

export interface SearchResults {
  type: "search_results";
  results: SearchResultEntry[];
  /** Echo of the client's `ClientSearch.seq`; undefined for legacy servers. */
  seq?: number;
}

/** A streaming delta of assistant text content. */
export interface TextDelta {
  type: "text_delta";
  text: string;
  session_id?: string;
}

export interface ThinkingDelta {
  type: "thinking_delta";
  text: string;
  session_id?: string;
}

export interface ToolCallStart {
  type: "tool_call_start";
  name: string;
  id: string;
  session_id?: string;
}

/** A streaming delta for an in-flight tool call (partial JSON arguments). */
export interface ToolCallDelta {
  type: "tool_call_delta";
  id: string;
  key: string;
  value: string;
  session_id?: string;
}

export interface ToolCallEnd {
  type: "tool_call_end";
  id: string;
  session_id?: string;
}

export interface ToolProgress {
  type: "tool_progress";
  id: string;
  tool_name: string;
  status: string;
  session_id?: string;
  sub_agent_messages?: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string; segments?: Array<{kind: string; text?: string; tool_id?: string}>; created_at?: number; mode?: string }>;
  sub_agent_session_id?: string;
}

export interface ToolResult {
  type: "tool_result";
  id: string;
  content: string;
  is_error: boolean;
  error_code?: string;
  error_category?: string;
  session_id?: string;
  sub_agent_messages?: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string; segments?: Array<{kind: string; text?: string; tool_id?: string}>; created_at?: number; mode?: string }>;
  sub_agent_session_id?: string;
}

export interface FinishEvent {
  type: "finish";
  reason: string;
  usage?: Record<string, unknown>;
  assistant_message_id?: string;
  error?: string;
  error_code?: string;
  error_category?: string;
  session_id?: string;
}

export interface PongEvent {
  type: "pong";
}

export interface ErrorEvent {
  type: "error";
  message: string;
  code: string;
  category?: string;
  retryable?: boolean;
  retry_after?: number;
  details?: Record<string, unknown>;
  session_id?: string;
}

export interface SessionReady {
  type: "session_ready";
  session_id: string;
  request_id?: string;
  messages?: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string }>;
  plan_items?: PlanItem[];
  artifacts?: ArtifactItem[];
  references?: ReferenceItem[];
  branches?: BranchMeta[];
  active_branch_id?: string;
  state?: SessionState;
}

export interface ConfiguredEvent {
  type: "configured";
  config: Record<string, unknown>;
}

export interface TelemetryEvent {
  type: "telemetry";
  data: TelemetryData;
  session_id?: string | null;
}

export interface TelemetryData {
  session_duration_s: number;
  total_turns: number;
  total_tool_calls: number;
  successful_tool_calls: number;
  failed_tool_calls: number;
  avg_tool_latency_ms: number;
  avg_turn_latency_ms: number;
  total_events: number;
  compactions: number;
  tool_usage: Record<string, number>;
  total_retries: number;
  retry_by_error: Record<string, number>;
}

export interface UsageStatsSessionEntry {
  session_id: string;
  model: string;
  /** "active" = currently configured, "deleted" = no longer in config,
   *  "unknown" = session had no recorded model. */
  model_status?: "active" | "deleted" | "unknown";
  channel?: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  turns: number;
  tool_calls: number;
  first_active: number;
  /** Timestamp of the session's most recent activity. Falls back to
   *  `first_active` when the backend predates this field. */
  last_active?: number;
}

export interface UsageStatsData {
  total_sessions: number;
  total_tokens: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tool_calls: number;
  tool_call_breakdown: Record<string, number>;
  model_breakdown: Record<string, {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    turns: number;
  }>;
  sessions: UsageStatsSessionEntry[];
}

export interface UsageStatsEvent {
  type: "usage_stats";
  stats: UsageStatsData;
}

export interface GatewayStatusEvent {
  type: "gateway_status";
  status: GatewayStatusData;
}

/** Response from a WeChat QR code scan request.
 *  Contains the QR code URL for the frontend to display. */
export interface WechatScanResultEvent {
  type: "wechat_scan_result";
  qrcode_url: string;
  success: boolean;
  message: string;
  scan_confirmed?: boolean;
  credentials?: {
    ilink_bot_id: string;
    bot_token: string;
    baseurl: string;
    ilink_user_id: string;
  };
}

export interface AutomationJobsList {
  type: "automation_jobs_list";
  jobs: any[];
}

export interface AutomationJobHistory {
  type: "automation_job_history";
  history: any[];
}

export interface AutomationJobCreated {
  type: "automation_job_created";
}

export interface AutomationJobCancelled {
  type: "automation_job_cancelled";
  job_id: string;
}

export interface AutomationJobUpdate {
  type: "automation_job_update";
  history?: any[];
}

export interface AutomationJobToggled {
  type: "automation_job_toggled";
}

export interface AutomationJobUpdated {
  type: "automation_job_updated";
}

export interface AutomationJobDeleted {
  type: "automation_job_deleted";
}

export interface AutomationExecutionDeleted {
  type: "automation_execution_deleted";
  entry_id: string;
}

export interface AutomationExecutionRenamed {
  type: "automation_execution_renamed";
  entry_id: string;
  new_name: string;
}

export interface AutomationStreamEvent {
  type: "automation_stream_event";
  job_id: string;
  event_type: string;
  event_data: Record<string, unknown>;
}

// ── Client → Server message types ─────────────────────────────────────────

/** The client→server message union sent over the WebSocket. */
export type ClientMessage =
  | ClientRun
  | ClientCancel
  | ClientResume
  | ClientConfigure
  | ClientPing
  | ClientListModels
  | ClientListSessions
  | ClientListAllSessions
  | ClientNewSession
  | ClientGetConfig
  | ClientUpdateModels
  | ClientSetActiveModel
  | ClientDeleteModel
  | ClientFetchModels
  | ClientValidateModel
  | ClientUpdateSkills
  | ClientInstallSkill
  | ClientUninstallSkill
  | ClientUpdateSkill
  | ClientUpdateMCP
  | ClientUpdateAgent
  | ClientSearch
  | ClientRollbackLog
  | ClientRollbackCheckout
  | ClientEditMessage
  | ClientDeleteMessage
  | ClientDeleteSession
  | ClientExportSession
  | ClientExportSessionsBatch
  | ClientExportData
  | ClientImportData
  | ClientRenameSession
  | ClientArchiveSession
  | ClientUnarchiveSession
  | ClientListArchivedSessions
  | ClientListWorkspaces
  | ClientOpenWorkspace
  | ClientCloseWorkspace
  | ClientRemoveWorkspace
  | ClientUploadWorkspaceIcon
  | ClientRenameWorkspace
  | ClientGetWorkspaceConfig
  | ClientSaveWorkspaceConfig
  | ClientUpdateSubAgents
  | ClientGetMemoryList
  | ClientGetMemoryDetail
  | ClientListGlobalRules
  | ClientListProjectRules
  | ClientSaveGlobalRule
  | ClientDeleteGlobalRule
  | ClientGetGlobalRuleContent
  | ClientGetProfile
  | ClientRespondPermission
  | ClientRespondTakeover
  | ClientRespondPlan
  | ClientPlanApprove
  | ClientPlanReject
  | ClientRespondQuestion
  | ClientSetPlanMode
  | ClientSetMode
  | ClientSetCdpUrl
  | ClientRetry
  | ClientSwitchBranch
  | ClientRollback
  | ClientReindexWorkspace
  | ClientDeleteIndex
  | ClientGetUsageStats
  | ClientWechatScan
  | ClientAutomationListJobs
  | ClientAutomationGetHistory
  | ClientAutomationUpdateJob
  | ClientAutomationCreateJob
  | ClientAutomationDeleteJob
  | ClientAutomationDeleteExecution
  | ClientAutomationRenameExecution
  | ClientAutomationToggleJob
  | ClientSteer;

export interface ClientSteer {
  type: "steer";
  session_id?: string;
  prompt: string;
}

export interface ClientGetUsageStats {
  type: "get_usage_stats";
}

export interface ClientWechatScan {
  type: "wechat_scan";
  adapter_id: string;
}

export interface ClientAutomationListJobs {
  type: "automation_list_jobs";
}

export interface ClientAutomationGetHistory {
  type: "automation_get_history";
}

export interface ClientAutomationUpdateJob {
  type: "automation_update_job";
  job_id: string;
  name: string;
  prompt: string;
  cron: string;
  tag: string;
  model_index: number;
}

export interface ClientAutomationCreateJob {
  type: "automation_create_job";
  name: string;
  prompt: string;
  cron: string;
  tag: string;
  model_index: number;
}

export interface ClientAutomationDeleteJob {
  type: "automation_delete_job";
  job_id: string;
}

export interface ClientAutomationDeleteExecution {
  type: "automation_delete_execution";
  entry_id: string;
}

export interface ClientAutomationRenameExecution {
  type: "automation_rename_execution";
  entry_id: string;
  new_name: string;
}

export interface ClientAutomationToggleJob {
  type: "automation_toggle_job";
  job_id: string;
}

export interface ClientRun {
  type: "run";
  prompt: string;
  system_prompt?: string;
  session_id?: string;
  specialty?: string;
  attachments?: AttachmentMeta[];
  mode?: string;
  mode_prompt?: string;
}

export interface ClientCancel {
  type: "cancel";
  session_id: string;
}

export interface ClientResume {
  type: "resume";
  session_id: string;
  request_id?: string;
}

export interface ClientConfigure {
  type: "configure";
  config: Record<string, unknown>;
}

export interface ClientPing {
  type: "ping";
}

export interface ClientListModels {
  type: "list_models";
}

export interface ClientListSessions {
  type: "list_sessions";
}

export interface ClientListAllSessions {
  type: "list_all_sessions";
}

export interface ClientNewSession {
  type: "new_session";
  request_id?: string;
}

export interface ClientGetConfig {
  type: "get_config";
}

export interface ClientUpdateModels {
  type: "update_models";
  models: ModelConfigMeta[];
  active_model_index: number;
}

export interface ClientSetActiveModel {
  type: "set_active_model";
  model_index: number;
}

export interface ClientDeleteModel {
  type: "delete_model";
  model_index: number;
}

export interface ClientFetchModels {
  type: "fetch_models";
  backend_type: string;
  api_key: string;
  base_url: string;
}

export interface ClientValidateModel {
  type: "validate_model";
  backend_type: string;
  api_key: string;
  base_url: string;
  model_id: string;
  max_tokens: number;
  name?: string;
  /** Index of the model being edited; omit or -1 to add a new model. */
  model_index?: number;
  multimodal?: boolean;
  thinking_config?: ThinkingConfigData;
}

export interface ClientUpdateSkills {
  type: "update_skills";
  enabled_skills: string[];
}

export interface ClientInstallSkill {
  type: "install_skill";
  name: string;
  content: string;
  file_path: string;
}

export interface ClientUninstallSkill {
  type: "uninstall_skill";
  name: string;
}

export interface ClientUpdateMCP {
  type: "update_mcp";
  mcp_servers: MCPServerConfig[];
}

export interface ClientUpdateAgent {
  type: "update_agent";
  system_prompt?: string;
  specialty?: string;
  permission_mode?: string;
  max_turns?: number;
}

export interface ClientSearch {
  type: "search";
  query: string;
  /** Client-incremented sequence; echoed back so stale responses are droppable. */
  seq: number;
}

export interface ClientRollbackLog {
  type: "rollback_log";
  session_id?: string;
}

export interface ClientRollbackCheckout {
  type: "rollback_checkout";
  session_id?: string;
  commit_hash: string;
}

export interface ClientEditMessage {
  type: "edit_message";
  message_index: number;
  new_content: string;
  session_id?: string;
}

export interface ClientDeleteMessage {
  type: "delete_message";
  message_index: number;
  session_id?: string;
}

export interface ClientDeleteSession {
  type: "delete_session";
  session_id: string;
}

export interface ClientExportSession {
  type: "export_session";
  session_id: string;
}

export interface ClientExportSessionsBatch {
  type: "export_sessions_batch";
  session_ids: string[];
}

export interface ClientExportData {
  type: "export_data";
  request_id?: string;
}

export interface ClientImportData {
  type: "import_data";
  zip_path: string;
  mode?: "overwrite" | "replace" | "skip";
  request_id?: string;
}

export interface ClientRenameSession {
  type: "rename_session";
  session_id: string;
  new_name: string;
}

export interface ClientArchiveSession {
  type: "archive_session";
  session_id: string;
}

export interface ClientUnarchiveSession {
  type: "unarchive_session";
  session_id: string;
}

export interface ClientListArchivedSessions {
  type: "list_archived_sessions";
}

export interface ClientListWorkspaces {
  type: "list_workspaces";
}

export interface ClientOpenWorkspace {
  type: "open_workspace";
  path: string;
  request_id?: string;
}

export interface ClientCloseWorkspace {
  type: "close_workspace";
  path: string;
  request_id?: string;
}

export interface ClientRemoveWorkspace {
  type: "remove_workspace";
  path: string;
}

export interface ClientUploadWorkspaceIcon {
  type: "upload_workspace_icon";
  path: string;
  /** Full data URL of the picked image (data:image/...;base64,...). */
  icon_data: string;
}

export interface ClientRenameWorkspace {
  type: "rename_workspace";
  path: string;
  name: string;
}

/** Per-workspace config (mirrors the backend .encre/config.json).
 *  null/absent key = reuse the global config for that key (3-layer:
 *  global -> workspace -> runtime). */
export interface WorkspaceConfig {
  /** Model ids this workspace may use; null/absent = reuse the global set. */
  models?: string[] | null;
  /** Persisted enable toggle for the model selection, independent of `models`
   *  so "enabled but nothing picked yet" survives a reload. */
  models_enabled?: boolean | null;
  /** Per-file enable toggles for workspace context files. Absent key = enabled;
   *  `{ "CLAUDE.md": false }` disables a file so its contents never reach the model. */
  context_files?: Record<string, boolean> | null;
  permissions?: Record<string, unknown> | null;
  mcp?: Record<string, unknown> | null;
}

export interface ClientGetWorkspaceConfig {
  type: "get_workspace_config";
  path: string;
}

export interface ClientSaveWorkspaceConfig {
  type: "save_workspace_config";
  path: string;
  config: WorkspaceConfig;
}

export interface WorkspaceConfigResponse {
  type: "workspace_config";
  path: string;
  config: WorkspaceConfig;
  /** Which context files (AGENTS.md / CLAUDE.md) exist in this workspace. */
  files?: Record<string, boolean>;
}

export interface ClientReindexWorkspace {
  type: "reindex_workspace";
  path?: string;
}

export interface ClientDeleteIndex {
  type: "delete_index";
  path?: string;
}

export interface ClientUpdateSubAgents {
  type: "update_sub_agents";
  agents: SubAgentConfig[];
}

export interface ThinkingConfigData {
  type: "enabled" | "disabled" | "adaptive";
  enabled?: boolean;
  level?: string;
  selectable?: boolean;
  budget_tokens?: number;
  min_tokens?: number;
  max_tokens?: number;
  budget_ratio?: number;
}

export interface ModelConfigMeta {
  name: string;
  model_id: string;
  backend_type: string;
  api_key: string;
  base_url: string;
  max_tokens: number;
  context_window: number;
  enabled: boolean;
  multimodal: boolean;
  /** Recorded probe result: "supported" | "unsupported" | "unknown". */
  multimodal_support?: "supported" | "unsupported" | "unknown";
  /** Verified per-endpoint capability map: node name -> "supported" | "unsupported". */
  capabilities?: Record<string, string>;
  thinking_config?: ThinkingConfigData;
}

export interface ContextUsageEvent {
  type: "context_usage";
  context_tokens: number;
  context_window: number;
  session_id?: string;
}

export interface AgentStateSnapshot {
  task_stage: string;
  task_stage_history: Array<Record<string, unknown>>;
  working_set: Record<string, unknown>;
  turn_summaries: Array<Record<string, unknown>>;
  delegate_history: Array<Record<string, unknown>>;
  stuck_events: Array<Record<string, unknown>>;
  tool_semantics: Record<string, unknown>;
  mode?: "general" | "workspace" | "automation";
}

export interface AgentStateEvent {
  type: "agent_state";
  state: AgentStateSnapshot;
  session_id?: string;
}

export interface MCPServerConfig {
  /** Server name (used as key in mcpServers map). */
  name: string;
  /** Transport type: "stdio" or "http". */
  type: "stdio" | "http";
  /** Executable command (stdio only). */
  command?: string;
  /** Command arguments (stdio only). */
  args?: string[];
  /** Remote URL (http only). */
  url?: string;
  /** HTTP headers (http only). */
  headers?: Record<string, string>;
  /** Environment variables (stdio only). */
  env?: Record<string, string>;
  /** Working directory (stdio only). */
  cwd?: string;
  /** Request timeout in seconds (http only, default 60). */
  timeout?: number;
  /** Disabled flag (default false). */
  disabled?: boolean;
  /** Origin: "user" (default) or "model" (created by manage tool). */
  source?: string;
}

export interface SubAgentConfig {
  name: string;
  description: string;
  system_prompt: string;
  hidden?: boolean;
  /** Origin: "user" (default) or "model" (created by manage tool). */
  source?: string;
}

export interface SkillInfo {
  name: string;
  description: string;
  aliases: string[];
  source: string;
  body: string;
  argument_hint: string;
  allowed_tools: string[] | null;
  when_to_use: string;
  context: string;
  model: string | null;
  disable_model_invocation: boolean;
  user_invocable: boolean;
  license: string;
  compatibility: string;
  metadata: Record<string, string>;
}

export interface ClientUpdateSkill {
  type: "update_skill";
  name: string;
  content: string;
}

export interface ModelsList {
  type: "models_list";
  models: string[];
}

export interface WorkspaceEntry {
  id: string;
  path: string;
  name: string;
  opened_at: number;
  created_at?: number;
  /** PNG data URL of the workspace icon (uploaded or generated). */
  icon_data?: string;
  session_count?: number;
  index_status?: string;
  index_files?: number;
  index_progress?: number;
}

export interface WorkspaceOpened {
  type: "workspace_opened";
  path: string;
  name: string;
  id?: string;
  workspaces: WorkspaceEntry[];
  index_status?: string;
  index_files?: number;
  progress?: number;
}

export interface WorkspacesList {
  type: "workspaces_list";
  workspaces: WorkspaceEntry[];
}

export interface WorkspaceRemoved {
  type: "workspace_removed";
  path: string;
  workspaces: WorkspaceEntry[];
}

/** Structured routing origin of a message (aligns with Hermes SessionSource).
 *
 *  Carried on sessions that originated from a platform adapter (Telegram,
 *  Discord, ...); undefined for desktop/normal sessions.  The frontend renders
 *  a platform badge from it when present.  Mirrors the wire shape of
 *  ``encre.gateway.session.SessionSource.to_dict()``.
 */
export interface SessionSource {
  platform: string;
  chat_id: string;
  chat_type: "dm" | "group" | "channel" | "thread" | "forum";
  chat_name?: string | null;
  user_id?: string | null;
  user_name?: string | null;
  thread_id?: string | null;
  chat_topic?: string | null;
  user_id_alt?: string;
  chat_id_alt?: string;
  scope_id?: string;
}

export type SessionState = "idle" | "running" | "awaiting_approval";

export interface SessionEntryData {
  session_id: string;
  created_at: number;
  last_active: number;
  state: SessionState;
  preview?: string;
  name?: string;
  channel?: string;
  /** Structured platform origin (Phase 5).  Undefined for desktop/normal
   *  sessions; populated for adapter-sourced sessions.  Falls back to
   *  ``channel`` for backward compatibility when absent. */
  source?: SessionSource;
  message_count?: number;
  metadata?: Record<string, unknown>;
}

export interface SessionsList {
  type: "sessions_list";
  sessions: SessionEntryData[];
  /** Snapshot mode, used to reject a response that arrived after a mode switch. */
  channel?: "normal" | "iwork";
}

export interface SessionsAll {
  type: "sessions_all";
  normal: SessionEntryData[];
  iwork: SessionEntryData[];
}

export interface ArchivedSessionsList {
  type: "archived_sessions_list";
  sessions: SessionEntryData[];
}

export interface ConfigData {
  type: "config_data";
  config: Record<string, unknown>;
}

export interface ModelsUpdated {
  type: "models_updated";
  models: ModelConfigMeta[];
  active_model_index: number;
}

export interface ModelsFetched {
  type: "models_fetched";
  models: string[];
}

export interface ModelValidated {
  type: "model_validated";
  backend_type?: string;
  model_id?: string;
  model_index?: number;
  /** Recorded probe result: "supported" | "unsupported" | "unknown". */
  multimodal_support?: string;
}

export interface ModelValidationError {
  type: "model_validation_error";
  message: string;
}

export interface SkillsUpdated {
  type: "skills_updated";
  enabled_skills: string[];
  available_skills: SkillInfo[];
}

export interface SkillsList {
  type: "skills_list";
  skills: SkillInfo[];
}

export interface SkillInstalled {
  type: "skill_installed";
  name: string;
  available_skills: SkillInfo[];
}

export interface SkillInstallError {
  type: "skill_install_error";
  name: string;
  message: string;
}

export interface SkillUninstalled {
  type: "skill_uninstalled";
  name: string;
  available_skills: SkillInfo[];
}

export interface McpUpdated {
  type: "mcp_updated";
  mcp_servers: MCPServerConfig[];
}

export interface RollbackLogEvent {
  type: "rollback_log";
  session_id: string;
  commits: RollbackCommitEntry[];
}

export interface RollbackCommitEntry {
  hash: string;
  parent: string | null;
  timestamp: number;
  turn_count: number;
  message: string;
}

export interface RollbackCheckoutEvent {
  type: "rollback_checkout";
  session_id: string;
  commit_hash: string;
  messages: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string }>;
  turn_count: number;
  plan_items?: PlanItem[];
  artifacts?: any[];
  references?: ReferenceItem[];
  user_input?: string;
}

export interface MessagesUpdated {
  type: "messages_updated";
  session_id: string;
  commit_hash: string;
  messages: Array<{ role: string; content: string | Array<{ type: string; text: string }>; tool_calls?: any[]; reasoning_content?: string }>;
  plan_items?: PlanItem[];
  artifacts?: any[];
  references?: ReferenceItem[];
}

export interface SessionDeleted {
  type: "session_deleted";
  session_id: string;
}

export interface SessionExported {
  type: "session_exported";
  session_id: string;
  markdown: string;
  filename: string;
}

export interface SessionsExportedZip {
  type: "sessions_exported_zip";
  zip_base64: string;
  filename: string;
}

export interface DataExportedZip {
  type: "data_exported_zip";
  zip_path: string;
  filename: string;
  request_id?: string;
}

export interface DataExportProgress {
  type: "data_export_progress";
  done: number;
  total: number;
  percent: number;
  file?: string;
}

export interface DataImportProgress {
  type: "data_import_progress";
  done: number;
  total: number;
  percent: number;
  file?: string;
}

export interface DataImportDone {
  type: "data_import_done";
  files: number;
  restored: number;
  skipped: number;
  overwritten: number;
  kept: number;
}

export interface SessionRenamed {
  type: "session_renamed";
  session_id: string;
  new_name: string;
}

export interface AgentUpdated {
  type: "agent_updated";
  config: Record<string, unknown>;
}

export interface SubAgentsUpdated {
  type: "sub_agents_updated";
  sub_agents: SubAgentConfig[];
}

export interface WorkspaceClosed {
  type: "workspace_closed";
}

export interface MemoryEntry {
  name: string;
  path: string;
  size: number;
  modified: number;
  preview: string;
  title?: string;
  tags?: string[];
  type?: string;
}

export interface MemoryList {
  type: "memory_list";
  entries: MemoryEntry[];
}

export interface ClientGetMemoryList {
  type: "get_memory_list";
}

export interface ClientGetMemoryDetail {
  type: "get_memory_detail";
  path: string;
}

export interface ClientListGlobalRules {
  type: "list_global_rules";
}

export interface ClientListProjectRules {
  type: "list_project_rules";
}

export interface ClientSaveGlobalRule {
  type: "save_global_rule";
  name: string;
  content: string;
}

export interface ClientDeleteGlobalRule {
  type: "delete_global_rule";
  name: string;
}

export interface ClientGetGlobalRuleContent {
  type: "get_global_rule_content";
  name: string;
}

export interface ClientGetProfile {
  type: "get_profile";
}

export interface MemoryDetailEvent {
  type: "memory_detail";
  path: string;
  content: string;
  error?: string;
}

export interface GlobalRulesList {
  type: "global_rules_list";
  rules: GlobalRuleEntry[];
}

export interface GlobalRuleEntry {
  name: string;
  path: string;
  size: number;
  modified: number;
}

export interface ProjectRulesList {
  type: "project_rules_list";
  rules: ProjectRuleEntry[];
}

export interface ProjectRuleEntry {
  name: string;
  path: string;
  priority: number;
  modified: number;
}

export interface ProjectHooksList {
  type: "project_hooks_list";
  hooks: ProjectHookEntry[];
}

export interface ProjectHookEntry {
  handler_id: string;
  event_type: string;
  source_path: string;
  matcher: string;
  command: string;
  hook_type: string;
  timeout_ms: number;
}

export interface GlobalRuleSaved {
  type: "global_rule_saved";
  name: string;
}

export interface GlobalRuleDeleted {
  type: "global_rule_deleted";
  name: string;
}

export interface GlobalRuleContentEvent {
  type: "global_rule_content";
  name: string;
  content: string;
  error?: string;
}

export interface ProfileData {
  name: string;
  language_preference: string;
  timezone: string;
  expertise_level: string;
  domain: string;
  formality: string;
  detail_preference: string;
  tone: string;
  response_style: string;
  preferred_languages: string[];
  preferred_frameworks: string[];
  skill_levels: Record<string, string>;
  os: string;
  editor: string;
  testing_preference: string;
  learning_style: string;
  typical_session_length: string;
  common_goals: string[];
  error_tolerance: string;
  confidence: Record<string, number>;
  schema_version: number;
  last_updated: number;
  update_count: number;
  summary: string;
}

export interface ProfileMessage {
  type: "profile_data";
  profile: ProfileData;
}

export interface IndexStatusEvent {
  type: "index_status";
  files: number;
  status: string;
  progress?: number;
  workspace_id?: string;
}

export interface GitignoreContentEvent {
  type: "gitignore_content";
  path: string;
  content: string;
}

export interface DocumentsListEvent {
  type: "documents_list";
  documents: DocumentEntry[];
}

export interface DocumentAddedEvent {
  type: "document_added";
  document: DocumentEntry;
}

export interface DocumentUpdatedEvent {
  type: "document_updated";
  document: DocumentEntry;
}

export interface DocumentRemovedEvent {
  type: "document_removed";
  id: string;
}

export interface DocumentErrorEvent {
  type: "document_error";
  message: string;
}

export interface PermissionRequest {
  type: "permission_request";
  tool_name: string;
  reason: string;
  session_id?: string;
}

export interface PermissionPolicy {
  value: "allow" | "deny" | "ask" | "default";
  source?: "user" | "project" | "session" | "cli" | "hook" | "default";
}

export interface PermissionPolicies {
  tools: Record<string, PermissionPolicy>;
  capabilities: Record<string, PermissionPolicy>;
}

export interface EngineInstallRequestEvent {
  type: "engine_install_request";
  request_id: string;
  engine: string;
  title: string;
  body: string;
  hint?: string;
  options?: Array<{
    id: string;
    label: string;
    description?: string;
    kind?: "primary" | "secondary" | "danger";
  }>;
}

export interface EngineInstallProgressEvent {
  type: "engine_install_progress";
  request_id: string;
  pct: number;
  message: string;
  sub_message?: string;
  indeterminate?: boolean;
  status?: "running" | "success" | "fail" | "cancelled";
  message_code?: string;
  message_args?: Record<string, string>;
  sub_message_code?: string;
  sub_message_args?: Record<string, string>;
}

export interface EngineInstallResponseAck {
  type: "engine_install_response_ack";
  request_id: string;
  choice: string;
  resolved: boolean;
}

export interface ReferencesUpdateEvent {
  type: "references_update";
  references: import("./types.js").ReferenceItem[];
  session_id?: string;
}

export interface BranchMeta {
  id: string;
  parent_branch_id: string | null;
  fork_point_message_id: string | null;
  created_at: number;
  messages_count: number;
  tokens: { input: number; output: number; total: number };
}

export interface BranchUpdated {
  type: "branch_updated";
  session_id: string;
  active_branch_id: string;
  branches: BranchMeta[];
  messages?: Message[];
}

export interface BranchRolledBack {
  type: "branch_rolled_back";
  session_id: string;
  branch_id: string;
  removed_message_ids: string[];
}

export interface ClientGetProfile {
  type: "get_profile";
}

export interface ClientRespondPermission {
  type: "respond_permission";
  tool_name: string;
  decision: boolean;
  /** When true, persist the approval for the rest of the session ("always allow"). */
  remember?: boolean;
}

export interface ClientRespondTakeover {
  type: "respond_takeover";
  decision: boolean;
}

export interface ClientRespondPlan {
  type: "respond_plan";
  proposal_id: string;
  approved: boolean;
}

export interface ClientPlanApprove {
  type: "plan_approve";
  review_id: string;
  session_id?: string;
}

export interface ClientPlanReject {
  type: "plan_reject";
  review_id: string;
  session_id?: string;
}

export interface ClientRespondQuestion {
  type: "respond_question";
  tool_call_id: string;
  answers: string;
}

export interface ClientSetPlanMode {
  type: "set_plan_mode";
  active: boolean;
  reason?: string;
}

export interface ClientSetMode {
  type: "set_mode";
  session_id?: string;
  mode: string; // "plan" | "spec" | ""
}

export interface ClientSetCdpUrl {
  type: "browser_cdp_url";
  url: string;
  session_id?: string;
}

export interface ClientRetry {
  type: "retry";
  branch_id: string;
  user_message_index: number;
  mode?: "normal" | "detailed" | "concise";
  session_id?: string;
}

export interface ClientSwitchBranch {
  type: "switch_branch";
  branch_id: string;
  session_id: string;
}

export interface ClientRollback {
  type: "rollback";
  branch_id: string;
  message_id: string;
}

// ── Attachment ────────────────────────────────────────────────────────────

/** An attachment (file/folder) attached to a composer message. */
export interface AttachmentMeta {
  name: string;
  path: string;
  content: string;
  mime_type: string;
  size: number;
  is_binary: boolean;
}

// ── App State ─────────────────────────────────────────────────────────────

export interface TimelineSegment {
  kind: "thinking" | "text" | "tool";
  toolId?: string;
  text?: string;  // accumulated text content for text segments
}

/** A single chat message (user or assistant) with its timeline segments. */
export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  thinking?: string;
  thinkingElapsed?: number;  // duration in seconds
  isStreaming: boolean;
  toolCalls: ToolCallState[];
  segments: TimelineSegment[];
  timestamp: number;
  hasError?: boolean;
  /** Turn status card data — populated from backend events. */
  errorMessage?: string;
  errorCode?: string;
  errorCategory?: string;
  interruptedReason?: string;
  turnStatusText?: string;  // e.g. "任务完成" from backend
  cancelledText?: string;   // set when user presses stop button (reason="cancelled")
  mode?: string;  // "plan" | "spec" | "terminal" | "task_divider" for mode-based messages
  taskIndex?: number;   // parallel sub-agent: 0-based task ordinal (task_divider only)
  taskName?: string;    // parallel sub-agent: task label / agent name (task_divider only)
  taskStatus?: string;  // parallel sub-agent: "running" | "done" | "error" (task_divider only)
  fileRefs?: { name: string; size: number; icon: string; path?: string; mime_type?: string }[];
  tokenUsage?: { input_tokens: number; output_tokens: number; total_tokens: number };
  _index?: number;
  serverId?: string;  // server-side message ID (for retry matching)
  parentId?: string;
}

/** The resolved state of a single tool call (status, params, result). */
export interface ToolCallState {
  id: string;
  /** Backend protocol id retained when renderer events use a synthetic client id. */
  backendId?: string;
  name: string;
  params: Record<string, unknown>;
  result?: string;
  subAgentMessages?: Message[];
  subAgentSessionId?: string;
  isError?: boolean;
  errorCode?: string;
  errorCategory?: string;
  status: "pending" | "running" | "done";
  /** When a single agent tool call executed multiple parallel tasks, this
   *  indexes the task currently being viewed (0-based). Undefined means the
   *  whole combined run. */
  taskIndex?: number;
}

export interface DocumentEntry {
  id: string;
  name: string;
  source: string;
  status: string;
  original_url: string;
  original_path: string;
  content_path: string;
  file_type: string;
  size: number;
  added_at: number;
}

export interface CustomCommand {
  name: string;
  title: string;
  description: string;
  icon: string;
  prompt?: string;
}

/** Per-session snapshot of all session-scoped state. */
export interface SessionSnapshot {
  messages: Message[];
  tokenUsage: TokenUsage | null;
  telemetry: TelemetryData | null;
  planItems: PlanItem[];
  planModeActive: boolean;
  planProposals: PlanProposal[];
  artifacts: ArtifactItem[];
  references: ReferenceItem[];
  compactEvents: CompactInfo[];
  compactStartingEvents: CompactStartingInfo[];
  systemMessages: SystemMessageInfo[];
  branches: BranchMeta[];
  activeBranchId: string;
  spec: SpecData | null;
  planReview: PlanReviewData | null;
  running: boolean;
  /** Precise session state mirroring the backend ("idle" | "running" |
   *  "awaiting_approval").  `running` is only the coarse boolean derived from
   *  it, so it cannot distinguish a permission wait from an active run. */
  sessionState: SessionState;
  agentState?: AgentStateSnapshot | null;
}

/** The complete global application state held by the state store. */
export interface AppState {
  messages: Message[];
  attachments: AttachmentMeta[];
  sessionId: string;
  sessionStore: Record<string, SessionSnapshot>;
  connected: boolean;
  running: boolean;  // active session running state (derived from runningSessions)
  subAgentView: ToolCallState | null;
  subAgentBreadcrumb: Array<{sessionId: string; name: string; toolCallId: string; parentToolCallId: string | null}>;
  settings: Record<string, unknown>;
  activeToolId: string | null;
  theme: "dark" | "light";
  themePreference: "system" | "dark" | "light";
  telemetry: TelemetryData | null;
  usageStats: UsageStatsData | null;
  tokenUsage: TokenUsage | null;
  planItems: PlanItem[];
  planModeActive: boolean;
  planProposals: PlanProposal[];
  notifications: NotificationItem[];
  availableModels: string[];
  sessionsList: SessionEntryData[];
  /** Archived sessions shown in the workspace manager's archive view. */
  archivedSessions: SessionEntryData[];
  /** Global session cache for search — NOT cleared on mode switches (unlike
   *  `sessionsList`, which is emptied when entering a workspace), so session
   *  search stays global across normal/iwork/automation modes. */
  allSessions: SessionEntryData[];
  modelConfigs: ModelConfigMeta[];
  activeModelIndex: number;
  skillsList: SkillInfo[];
  enabledSkills: string[];
  mcpServers: MCPServerConfig[];
  subAgents: SubAgentConfig[];
  agentConfig: {
    system_prompt: string;
    specialty: string;
    permission_mode: string;
    max_turns: number;
  };
  searchResults: SearchResultEntry[];
  automationHistory: any[];
  /** Configured automation jobs — the single source for the automation panel
   *  (fed by the unified push, same as automationHistory). */
  automationJobs: any[];
  workspaces: WorkspaceEntry[];
  /** Per-workspace config cache, keyed by workspace path. */
  workspaceConfigs: Record<string, WorkspaceConfig>;
  /** Per-workspace context-file availability (which of AGENTS.md / CLAUDE.md
   *  exist in the workspace), keyed by workspace path. */
  workspaceFiles: Record<string, Record<string, boolean>>;
  activeWorkspace: string;
  workspaceMode: "iwork" | "normal";
  indexStatus: "idle" | "ready" | "indexing" | "error" | "no_workspace";
  indexFiles: number;
  indexProgress: number;
  indexCurrentFile?: string;
  gitignoreContent: string;
  docsList: DocumentEntry[];
  gatewayStatus: GatewayStatusData | null;
  toolsInfo: ToolsInfo;
  modelCatalog: ModelCatalog;
  mcpCatalog: McpCatalog;
  artifacts: ArtifactItem[];
  references: ReferenceItem[];
  compactEvents: CompactInfo[];
  compactStartingEvents: CompactStartingInfo[];
  systemMessages: SystemMessageInfo[];
  memoryList: MemoryEntry[];
  memoryDetail: { path: string; content: string; error?: string } | null;
  globalRules: GlobalRuleEntry[];
  projectRules: ProjectRuleEntry[];
  projectHooks: ProjectHookEntry[];
  viewingGlobalRule: { name: string; content: string; error?: string } | null;
  profile: ProfileData | null;
  inputMode: string;
  pendingPermission: string | null;
  permissionPolicies: PermissionPolicies;
  tempChat: boolean;
  customCommands: CustomCommand[];
  branches: BranchMeta[];
  activeBranchId: string;
  pendingQueueCount: number;
  queuedPrompts: QueuedPrompt[];
  runQueuePosition: number | null;
  workflowState: WorkflowState | null;
  searchFilter: SearchFilter;
  contextTokens: number;
  contextWindow: number;
  agentState: AgentStateSnapshot | null;
  spec: SpecData | null;
  planReview: PlanReviewData | null;
}

export interface WorkflowTaskInfo {
  taskId: string;
  taskName: string;
  status: string;
}

export interface WorkflowState {
  workflowId: string;
  goal: string;
  totalTasks: number;
  taskIds: string[];
  active: boolean;
  tasks: WorkflowTaskInfo[];
  completedCount: number;
  failedCount: number;
  skippedCount: number;
  success?: boolean;
  totalDuration?: number;
}

export interface QueuedPrompt {
  text: string;
  mode?: string;
}

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface ArtifactItem {
  path: string;
  name: string;
  ext: string;
  size: number;
  tool: string;
  created_at: number;
  diff_text?: string;
}

export interface ReferenceItem {
  tool: string;
  summary: string;
  icon: string;
  timestamp: number;
  branch_id?: string;
}

export interface GatewayAdapterInfo {
  name: string;
  platform: string;
  connected: boolean;
  status: "connected" | "error" | "disconnected";
  error?: string;
}

export interface GatewayStatusData {
  running: boolean;
  adapters: GatewayAdapterInfo[];
  adapter_count: number;
}

export interface ToolsInfo {
  base: string[];
  unlocked: string[];
  active: string[];
  by_category: Record<string, string[]>;
  total_available: number;
}

// ── Model Catalog ─────────────────────────────────────────────────────────

export interface ModelEntry {
  id: string;
  label: string;
  context: number;
  modalities: string[];
}

export interface ProviderEntry {
  id: string;
  label: string;
  base_url: string;
  docs: string;
  allow_custom: boolean;
  auth: string;
  models: ModelEntry[];
}

export interface ModelCatalog {
  providers: ProviderEntry[];
  default_output_tokens: Record<string, number>;
}

// ── MCP Catalog ──────────────────────────────────────────────────────────────

export interface McpProviderEntry {
  id: string;
  label: string;
  description: string;
  config: {
    type: "stdio" | "http";
    command?: string;
    args?: string[];
    url?: string;
    env?: Record<string, string>;
  };
  env_fields: Record<string, { label: string; secret?: boolean }>;
  docs: string;
}

export interface McpCatalog {
  providers: McpProviderEntry[];
}

// ── Plan ──────────────────────────────────────────────────────────────────

export interface PlanItem {
  id: string;
  text: string;
  status: "done" | "active" | "pending";
}

export interface PlanUpdate {
  type: "plan_update";
  plan_items: PlanItem[];
  session_id?: string;
}

export interface PlanProposal {
  type: "plan_proposal";
  proposal_id: string;
  tool_call_id: string;
  tool_name: string;
  tool_args?: Record<string, unknown>;
  preview: string;
  diff_text?: string;
  file_path?: string;
  original?: string;
  proposed?: string;
  added?: number;
  removed?: number;
  risk?: "low" | "medium" | "high";
  session_id?: string;
}

export interface PlanModeChanged {
  type: "plan_mode_changed";
  active: boolean;
  reason?: string;
  session_id?: string;
}

export interface ModeChanged {
  type: "mode_changed";
  mode: string; // "plan" | "spec" | ""
  session_id?: string;
}

export interface CommandChanged {
  type: "command_changed";
  command: { name: string; prompt?: string; icon?: string; title?: string } | null;
  session_id?: string;
}

export interface PlanResolved {
  type: "plan_resolved";
  proposal_id: string;
  tool_call_id: string;
  approved: boolean;
  session_id?: string;
}

export interface ArtifactsUpdate {
  type: "artifacts_update";
  artifacts: ArtifactItem[];
  session_id?: string;
}

export interface ReferencesUpdate {
  type: "references_update";
  references: ReferenceItem[];
  session_id?: string;
}

export interface BranchSwitched {
  type: "branch_switched";
  session_id: string;
  branch_id: string;
  messages: Message[];
  branches: BranchMeta[];
  artifacts?: ArtifactItem[];
  references?: ReferenceItem[];
  tokens: TokenUsage | null;
}

export interface AssistantBoundaryEvent {
  type: "assistant_boundary";
  session_id?: string;
}

export interface CompactStartingEvent {
  type: "compact_starting";
  session_id?: string;
}

export interface CompactFailedEvent {
  type: "compact_failed";
  reason?: string;
  session_id?: string;
}

export interface CompactEvent {
  type: "compact";
  old_count: number;
  new_count: number;
  old_tokens: number;
  new_tokens: number;
}

export interface SystemMessageEvent {
  type: "system_message";
  content: string;
  kind: string;
  session_id: string;
}

export interface CompactStartingInfo {
  timestamp: number;
}

export interface CompactInfo {
  old_count: number;
  new_count: number;
  old_tokens: number;
  new_tokens: number;
}

export interface SystemMessageInfo {
  content: string;
  kind: string;
  timestamp: number;
}

export interface SpecSection {
  title: string;
  content: string;
}

export interface SpecData {
  title: string;
  sections: SpecSection[];
  status: string;
  feedback: string;
  raw_text: string;
  metadata: Record<string, unknown>;
}

export interface SpecUpdateEvent {
  type: "spec_update";
  spec: SpecData | null;
  status: string;
  feedback?: string;
  session_id: string;
}

export interface PlanReviewData {
  review_id: string;
  content: string;
  file_path: string;  // path to plan.md
  dir_path: string;   // directory containing plan.md, steps.md, checklist.md
  mode: string;
  status: string;
}

export interface PlanReviewEvent {
  type: "plan_review";
  review_id: string;
  content: string;
  file_path: string;
  mode: string;
  status: string;
  session_id?: string;
}

// ── Edit proposal (Codex-style inline diff / accept-reject) ─────────────

/**
 * Pending file edit surfaced by the agent loop.  Emitted by
 * `file_edit` with `dry_run=true`; the desktop renderer can display
 * an inline diff and write the file on accept.
 */
export interface EditProposalData {
  kind: "edit_proposal";
  tool_call_id: string;
  file_path: string;
  diff_text: string;
  original: string;
  proposed: string;
  added: number;
  removed: number;
  summary: string;
}

export interface EditProposal {
  type: "edit_proposal";
  tool_call_id: string;
  file_path: string;
  diff_text: string;
  original: string;
  proposed: string;
  added: number;
  removed: number;
  summary: string;
}

export function createEmptySessionSnapshot(): SessionSnapshot {
  return {
    messages: [],
    tokenUsage: null,
    telemetry: null,
    planItems: [],
    planModeActive: false,
    planProposals: [],
    artifacts: [],
    references: [],
    compactEvents: [],
    compactStartingEvents: [],
    systemMessages: [],
    branches: [],
    activeBranchId: "",
    spec: null,
    planReview: null,
    running: false,
    sessionState: "idle",
    agentState: null,
  };
}

// ── Notification ──────────────────────────────────────────────────────────

/** A notification item shown in the bell panel / toasts. */
export interface NotificationItem {
  id: string;
  type: "error" | "success" | "info" | "warning";
  title: string;
  message: string;
  source?: string;
  timestamp: number;
  read: boolean;
  media?: { type: "image" | "video"; src: string };
}

// ── App State ─────────────────────────────────────────────────────────────

export function createEmptyState(): AppState {
  const sessionSnapshot = createEmptySessionSnapshot();
  return {
    messages: sessionSnapshot.messages,
    attachments: [],
    sessionId: "",
    sessionStore: {},
    connected: false,
    running: sessionSnapshot.running,
    subAgentView: null,
    subAgentBreadcrumb: [],
    settings: {},
    activeToolId: null,
    theme: "dark",
    themePreference: "system",
    telemetry: null,
    usageStats: null,
    tokenUsage: sessionSnapshot.tokenUsage,
    planItems: sessionSnapshot.planItems,
    planModeActive: sessionSnapshot.planModeActive,
    planProposals: sessionSnapshot.planProposals,
    notifications: [],
    availableModels: [],
    sessionsList: [],
    archivedSessions: [],
    allSessions: [],
    modelConfigs: [],
    activeModelIndex: 0,
    skillsList: [],
    enabledSkills: [],
    mcpServers: [],
    subAgents: [],
    artifacts: sessionSnapshot.artifacts,
    references: sessionSnapshot.references,
    compactEvents: sessionSnapshot.compactEvents,
    compactStartingEvents: sessionSnapshot.compactStartingEvents,
    systemMessages: sessionSnapshot.systemMessages || [],
    spec: null,
    planReview: null,
    memoryList: [],
    memoryDetail: null,
    globalRules: [],
    projectRules: [],
    projectHooks: [],
    viewingGlobalRule: null,
    profile: null,
    inputMode: "",
    pendingPermission: null,
    permissionPolicies: { tools: {}, capabilities: {} },
    tempChat: false,
    customCommands: [],
    agentConfig: {
      system_prompt: "",
      specialty: "general",
      permission_mode: "default",
      max_turns: 0,
    },
    searchResults: [],
    automationHistory: [],
    automationJobs: [],
    workspaces: [],
    workspaceConfigs: {},
    workspaceFiles: {},
    activeWorkspace: "",
    workspaceMode: "normal",
    indexStatus: "idle",
    indexFiles: 0,
    indexProgress: 0,
    indexCurrentFile: undefined,
    gitignoreContent: "",
    docsList: [],
    gatewayStatus: null,
    toolsInfo: {
      base: [],
      unlocked: [],
      active: [],
      by_category: {},
      total_available: 0,
    },
    modelCatalog: { providers: [], default_output_tokens: {} },
    mcpCatalog: { providers: [] },
    branches: sessionSnapshot.branches,
    activeBranchId: sessionSnapshot.activeBranchId,
    pendingQueueCount: 0,
    queuedPrompts: [],
    runQueuePosition: null,
    workflowState: null,
    searchFilter: defaultSearchFilter(),
    contextTokens: 0,
    contextWindow: 0,
    agentState: null,
  };
}
