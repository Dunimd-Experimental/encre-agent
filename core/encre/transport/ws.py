#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""Encre WebSocket message handler.

:class:`EncreWSHandler` is the per-connection handler bound to
:class:`~encre.server.app.EncreServer`.  It owns the frame-encoding /
connection-lifecycle layer (the ``handle`` loop) and composes every
client-message domain from :mod:`encre.protocol.handlers` into a single
object via mixin inheritance.  Each incoming frame is parsed by
:mod:`encre.server.protocol` and routed through the :data:`_DISPATCH`
table to the owning domain handler.

iClaw (``channel == "iclaw"``) runs are dispatched through the adapter
:class:`~encre.gateway.ws_bridge.server.WsBridgeServer`'s EventRouter in
a background task.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import websockets

from encre.server.protocol import (
    PROTOCOL_VERSION,
    ClientAddDocument,
    ClientAgentCreate,
    ClientAgentDelete,
    ClientAgentList,
    ClientAgentSetActive,
    ClientAgentUpdate,
    ClientArchiveSession,
    ClientAutomationCancelJob,
    ClientAutomationCreateJob,
    ClientAutomationDeleteExecution,
    ClientAutomationDeleteJob,
    ClientAutomationGetHistory,
    ClientAutomationListJobs,
    ClientAutomationRenameExecution,
    ClientAutomationToggleJob,
    ClientAutomationUpdateJob,
    ClientCancel,
    ClientCloseWorkspace,
    ClientConfigure,
    ClientDeleteGlobalRule,
    ClientDeleteIndex,
    ClientDeleteMessage,
    ClientDeleteModel,
    ClientDeleteSession,
    ClientEditMessage,
    ClientEngineInstallResponse,
    ClientExportData,
    ClientExportSession,
    ClientExportSessionsBatch,
    ClientFetchModels,
    ClientGetConfig,
    ClientGetWorkspaceConfig,
    ClientGetGitignore,
    ClientGetGlobalRuleContent,
    ClientGetIndexStatus,
    ClientGetMemoryDetail,
    ClientGetMemoryList,
    ClientGetProfile,
    ClientGetUsageStats,
    ClientIclawResume,
    ClientImportData,
    ClientInstallSkill,
    ClientListAllSessions,
    ClientListArchivedSessions,
    ClientListDocuments,
    ClientListGlobalRules,
    ClientListModels,
    ClientListProjectHooks,
    ClientListProjectRules,
    ClientListSessions,
    ClientListWorkspaces,
    ClientNewSession,
    ClientOpenWorkspace,
    ClientPing,
    ClientReindexWorkspace,
    ClientRemoveDocument,
    ClientRemoveWorkspace,
    ClientRenameSession,
    ClientRenameWorkspace,
    ClientReplayGetSession,
    ClientRespondPermission,
    ClientRespondPlan,
    ClientRespondQuestion,
    ClientResume,
    ClientRetry,
    ClientRollbackBranch,
    ClientRollbackCheckout,
    ClientRollbackLog,
    ClientRun,
    ClientSaveGlobalRule,
    ClientSaveWorkspaceConfig,
    ClientSearch,
    ClientSetActiveModel,
    ClientSetCdpUrl,
    ClientSetCommand,
    ClientSetGitignore,
    ClientSetMode,
    ClientSetPlanMode,
    ClientSpecApprove,
    ClientSpecReject,
    ClientPlanApprove,
    ClientPlanReject,
    ClientSteer,
    ClientSwitchBranch,
    ClientTerminalKill,
    ClientTerminalListShells,
    ClientTerminalResize,
    ClientTerminalSpawn,
    ClientTerminalWrite,
    ClientUnarchiveSession,
    ClientUninstallSkill,
    ClientUpdateAgent,
    ClientUpdateMCP,
    ClientUpdateModels,
    ClientUpdateSkill,
    ClientUpdateSkills,
    ClientUpdateSubAgents,
    ClientUploadWorkspaceIcon,
    ClientValidateModel,
    ClientWechatScan,
    parse_client_message,
)
from encre.protocol.handlers.automation import AutomationHandlers
from encre.protocol.handlers.base import HandlerBase
from encre.protocol.handlers.chat import ChatHandlers
from encre.protocol.handlers.events import EventDispatchMixin
from encre.protocol.handlers.history import HistoryHandlers
from encre.protocol.handlers.permissions import PermissionHandlers
from encre.protocol.handlers.profile import ProfileHandlers
from encre.protocol.handlers.run import RunHandlers
from encre.protocol.handlers.sessions import SessionsHandlers
from encre.protocol.handlers.settings import SettingsHandlers
from encre.protocol.handlers.skills import SkillsHandlers
from encre.protocol.handlers.terminal import TerminalHandlers
from encre.protocol.handlers.workspaces import WorkspacesHandlers
from encre.utils.types import BackendError, BackendFinish

from encre.settings_manager import (  # noqa: E402
    load_custom_slash_commands,
)
from encre.slash_commands import set_custom_commands_provider  # noqa: E402

# Inject the harness-side loader for user-defined slash commands so
# encre.slash_commands never needs to import core modules directly.
set_custom_commands_provider(load_custom_slash_commands)

logger = logging.getLogger("encre.transport.ws")

# How long ``handle()`` waits for the client's first frame in order to learn
# its transport-encryption mode before the first outbound frame leaves.  A
# desktop client sends its (encrypted) first frame within a few milliseconds
# of the socket opening, so this is normally free; a purely passive client
# only pays it once, at connect time.
_ENCRYPTION_PEEK_TIMEOUT_S = 0.15


def _detect_client_encryption(raw: str | bytes) -> bool:
    """Return True when a client frame looks like base64 ciphertext.

    Plaintext clients send JSON, which always starts with ``{``; an encrypted
    frame is base64 (``nonce || ciphertext || tag``) and never does.  Frames
    that are not valid UTF-8 at all count as encrypted.
    """
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except UnicodeDecodeError:
        return True
    return not text.strip().startswith("{")


# Bridge standard logging to loguru so ws.py logs appear in desktop output
try:
    from loguru import logger as _loguru_logger
    import sys
    class _LoguruHandler(logging.Handler):
        def emit(self, record):
            try:
                _loguru_logger.opt(depth=6, exception=record.exc_info).log(record.levelname, record.getMessage())
            except Exception:
                pass
    _loguru_handler = _LoguruHandler()
    _loguru_handler.setLevel(logging.DEBUG)
    logging.getLogger("encre.transport.ws").addHandler(_loguru_handler)
    logging.getLogger("encre.transport.ws").setLevel(logging.DEBUG)
    # Suppress noisy websocket keepalive PING/PONG/DEBUG logs
    for _name in ("websockets.server", "websockets.client", "websockets"):
        logging.getLogger(_name).setLevel(logging.WARNING)
except Exception:
    pass


class EncreWSHandler(
    HandlerBase,
    SessionsHandlers,
    RunHandlers,
    ChatHandlers,
    PermissionHandlers,
    SkillsHandlers,
    HistoryHandlers,
    WorkspacesHandlers,
    ProfileHandlers,
    AutomationHandlers,
    TerminalHandlers,
    SettingsHandlers,
    EventDispatchMixin,
):
    """Per-connection handler for the Encre WebSocket protocol.

    Frame encoding and connection management live here; every client
    message domain (sessions, run, chat, permissions, skills, history,
    workspaces, profile, automation, terminal, settings) is implemented
    by a mixin under :mod:`encre.protocol.handlers` and dispatched via
    :data:`_DISPATCH`.
    """

    # Client message type -> unbound handler function (resolved through
    # the mixin MRO).  Raw-dict legacy messages are handled inline in
    # ``handle`` before the table lookup.
    _DISPATCH: dict[type, Any] = {
        ClientPing: SettingsHandlers._h_ping,
        ClientListModels: SettingsHandlers._h_list_models,
        ClientListSessions: SessionsHandlers._h_list_sessions,
        ClientListAllSessions: SessionsHandlers._h_list_all_sessions,
        ClientNewSession: SessionsHandlers._h_new_session,
        ClientConfigure: SettingsHandlers._h_configure,
        ClientWechatScan: SettingsHandlers._h_wechat_scan,
        ClientRun: RunHandlers._h_run,
        ClientRespondPermission: PermissionHandlers._h_respond_permission,
        ClientRespondPlan: PermissionHandlers._h_respond_plan,
        ClientSetPlanMode: PermissionHandlers._h_set_plan_mode,
        ClientSetMode: PermissionHandlers._h_set_mode,
        ClientSetCdpUrl: PermissionHandlers._h_set_cdp_url,
        ClientSetCommand: PermissionHandlers._h_set_command,
        ClientRespondQuestion: ChatHandlers._h_respond_question,
        ClientEngineInstallResponse: PermissionHandlers._h_engine_install_response,
        ClientCancel: ChatHandlers._h_cancel,
        ClientSteer: ChatHandlers._h_steer,
        ClientSpecApprove: PermissionHandlers._h_spec_approve,
        ClientSpecReject: PermissionHandlers._h_spec_reject,
        ClientPlanApprove: PermissionHandlers._h_plan_approve,
        ClientPlanReject: PermissionHandlers._h_plan_reject,
        ClientGetConfig: SettingsHandlers._h_get_config,
        ClientUpdateModels: SettingsHandlers._h_update_models,
        ClientSetActiveModel: SettingsHandlers._h_set_active_model,
        ClientDeleteModel: SettingsHandlers._h_delete_model,
        ClientFetchModels: SettingsHandlers._h_fetch_models,
        ClientValidateModel: SettingsHandlers._h_validate_model,
        ClientUpdateSkills: SkillsHandlers._h_update_skills,
        ClientInstallSkill: SkillsHandlers._h_install_skill,
        ClientUninstallSkill: SkillsHandlers._h_uninstall_skill,
        ClientUpdateSkill: SkillsHandlers._h_update_skill,
        ClientUpdateMCP: SettingsHandlers._h_update_mcp,
        ClientUpdateAgent: SettingsHandlers._h_update_agent,
        ClientSearch: HistoryHandlers._h_search,
        ClientRollbackLog: HistoryHandlers._h_rollback_log,
        ClientRollbackCheckout: HistoryHandlers._h_rollback_checkout,
        ClientEditMessage: ChatHandlers._h_edit_message,
        ClientDeleteMessage: ChatHandlers._h_delete_message,
        ClientDeleteSession: SessionsHandlers._h_delete_session,
        ClientArchiveSession: SessionsHandlers._h_archive_session,
        ClientUnarchiveSession: SessionsHandlers._h_unarchive_session,
        ClientListArchivedSessions: SessionsHandlers._h_list_archived_sessions,
        ClientExportSession: SessionsHandlers._h_export_session,
        ClientExportSessionsBatch: SessionsHandlers._h_export_sessions_batch,
        ClientExportData: SettingsHandlers._h_export_data,
        ClientImportData: SettingsHandlers._h_import_data,
        ClientRenameSession: SessionsHandlers._h_rename_session,
        ClientAgentList: SettingsHandlers._h_agent_list,
        ClientAgentCreate: SettingsHandlers._h_agent_create,
        ClientAgentDelete: SettingsHandlers._h_agent_delete,
        ClientAgentUpdate: SettingsHandlers._h_agent_update,
        ClientAgentSetActive: SettingsHandlers._h_agent_set_active,
        ClientUpdateSubAgents: SettingsHandlers._h_update_sub_agents,
        ClientOpenWorkspace: WorkspacesHandlers._h_open_workspace,
        ClientListWorkspaces: WorkspacesHandlers._h_list_workspaces,
        ClientRemoveWorkspace: WorkspacesHandlers._h_remove_workspace,
        ClientCloseWorkspace: WorkspacesHandlers._h_close_workspace,
        ClientUploadWorkspaceIcon: WorkspacesHandlers._h_upload_workspace_icon,
        ClientRenameWorkspace: WorkspacesHandlers._h_rename_workspace,
        ClientGetWorkspaceConfig: WorkspacesHandlers._h_get_workspace_config,
        ClientSaveWorkspaceConfig: WorkspacesHandlers._h_save_workspace_config,
        ClientGetMemoryList: ProfileHandlers._h_get_memory_list,
        ClientGetMemoryDetail: ProfileHandlers._h_get_memory_detail,
        ClientListGlobalRules: ProfileHandlers._h_list_global_rules,
        ClientListProjectRules: ProfileHandlers._h_list_project_rules,
        ClientListProjectHooks: ProfileHandlers._h_list_project_hooks,
        ClientSaveGlobalRule: ProfileHandlers._h_save_global_rule,
        ClientDeleteGlobalRule: ProfileHandlers._h_delete_global_rule,
        ClientGetGlobalRuleContent: ProfileHandlers._h_get_global_rule_content,
        ClientGetProfile: ProfileHandlers._h_get_profile,
        ClientReindexWorkspace: WorkspacesHandlers._h_reindex_workspace,
        ClientGetGitignore: WorkspacesHandlers._h_get_gitignore,
        ClientSetGitignore: WorkspacesHandlers._h_set_gitignore,
        ClientDeleteIndex: WorkspacesHandlers._h_delete_index,
        ClientGetIndexStatus: WorkspacesHandlers._h_get_index_status,
        ClientAddDocument: ProfileHandlers._h_add_document,
        ClientRemoveDocument: ProfileHandlers._h_remove_document,
        ClientListDocuments: ProfileHandlers._h_list_documents,
        ClientResume: RunHandlers._h_resume,
        ClientIclawResume: SessionsHandlers._h_iclaw_resume,
        ClientTerminalListShells: TerminalHandlers._h_terminal_list_shells,
        ClientTerminalSpawn: TerminalHandlers._h_terminal_spawn,
        ClientTerminalWrite: TerminalHandlers._h_terminal_write,
        ClientTerminalResize: TerminalHandlers._h_terminal_resize,
        ClientTerminalKill: TerminalHandlers._h_terminal_kill,
        ClientRetry: RunHandlers._h_retry,
        ClientSwitchBranch: HistoryHandlers._h_switch_branch,
        ClientRollbackBranch: HistoryHandlers._h_rollback_branch,
        ClientAutomationListJobs: AutomationHandlers._h_automation_list_jobs,
        ClientAutomationCreateJob: AutomationHandlers._h_automation_create_job,
        ClientAutomationCancelJob: AutomationHandlers._h_automation_cancel_job,
        ClientAutomationToggleJob: AutomationHandlers._h_automation_toggle_job,
        ClientAutomationUpdateJob: AutomationHandlers._h_automation_update_job,
        ClientAutomationDeleteJob: AutomationHandlers._h_automation_delete_job,
        ClientAutomationDeleteExecution: AutomationHandlers._h_automation_delete_execution,
        ClientAutomationRenameExecution: AutomationHandlers._h_automation_rename_execution,
        ClientAutomationGetHistory: AutomationHandlers._h_automation_get_history,
        ClientGetUsageStats: SettingsHandlers._h_get_usage_stats,
        ClientReplayGetSession: HistoryHandlers._h_replay_get_session,
    }

    async def handle(self, ws) -> None:
        """Main per-connection message loop.

        Reads raw WebSocket frames, parses them into typed client messages,
        routes each through :data:`_DISPATCH`, and manages connection
        lifecycle (startup-mode session restore, encryption detection,
        protocol-version handshake).
        """
        self._info = None
        self._current_session_id = None
        self._current_ws_id = ""
        self._index_progress_callback = None
        # One handler instance serves every socket (see EncreServer), so the
        # encryption mode has to be re-detected per connection instead of
        # inheriting whatever the previous client negotiated.
        self._client_encrypted = None
        t_conn = time.time()
        logger.info("[perf] backend ws.accept")

        # Learn the client's transport-encryption mode from its first frame
        # *before* anything is sent back.  Detection used to happen only once
        # the read loop started, which meant the placeholder `session_ready`
        # and the entire unified initial snapshot below always left in
        # plaintext — even for a client that had already proven it encrypts.
        # `websockets` keeps the underlying frame queue, so the peeked frame is
        # replayed into the read loop (see `_replay_first`).
        _peeked_frame: str | bytes | None = None
        if self._client_encrypted is None:
            try:
                _peeked_frame = await asyncio.wait_for(ws.recv(), timeout=_ENCRYPTION_PEEK_TIMEOUT_S)
            except Exception as _peek_err:  # timeout / already closing
                logger.debug("[ws] encryption peek unavailable: %s", _peek_err)
                _peeked_frame = None
            if _peeked_frame is not None:
                self._client_encrypted = _detect_client_encryption(_peeked_frame)
                logger.info("[ws] client transport encryption=%s (from first frame)",
                            self._client_encrypted)

        # Send a placeholder session_ready immediately so the frontend can
        # start filling sidebar/tray data without waiting for the session
        # resume.  The real resume runs in a background task (below) and
        # sends a second session_ready when the session is fully loaded.
        # The protocol_version piggybacks on this first frame: old clients
        # ignore the extra JSON field, new clients read it as the handshake
        # answer.
        placeholder = self._get_or_create_session()
        self._info = placeholder
        self._current_session_id = placeholder.session_id
        await self._send(ws, "session_ready", session_id=placeholder.session_id, plan_items=[],
                         protocol_version=PROTOCOL_VERSION)
        logger.info("[perf] placeholder session_ready sent in %.0fms", (time.time() - t_conn) * 1000)

        # Background: resolve the startup behavior and resume the most recent
        # session.  This avoids blocking the event loop (and the dispatch
        # loop below) on potentially slow session loading (large JSONL,
        # cold index build, etc.).
        startup_behavior = self._resolve_startup_behavior()

        async def _background_resume() -> None:
            try:
                if startup_behavior == "last":
                    t_resume = time.time()
                    # Heavy session load (agent boot + full JSONL parse) runs
                    # in a worker thread so it cannot starve the event loop and
                    # delay the client's own config / session-list requests.
                    resumed = await asyncio.to_thread(
                        self._manager.try_resume_most_recent, self._default_config
                    )
                    logger.info("[perf] resume load took %.0fms", (time.time() - t_resume) * 1000)
                    if resumed is not None:
                        self._info = resumed
                        self._current_session_id = resumed.session_id
                        sess = resumed.agent.session
                        sess.ensure_artifacts_from_messages()
                        # Re-apply the resumed session's persisted slash-command
                        # mode so config + derived plan_mode_active agree with
                        # metadata before the first run.
                        self._restore_persisted_mode(self._info)
                        self._restore_persisted_command(self._info)
                        t_build = time.time()
                        msgs = self._renderer_session_messages(sess)
                        branches_list = [b.__dict__ for b in sess.branches.values()]
                        logger.info("[perf] renderer message build took %.0fms (%d msgs)",
                                    (time.time() - t_build) * 1000, len(msgs))
                        await self._send(ws, "session_ready", session_id=resumed.session_id,
                                         messages=msgs, plan_items=sess.plan_items,
                                         artifacts=sess.artifacts, references=sess.references,
                                         branches=branches_list, active_branch_id=sess.active_branch_id)
                        # Surface the persisted mode/command right away so the
                        # composer chips reflect reality BEFORE the first run.
                        # Previously the mode only reached the frontend at run
                        # end (run.py's finally block), which made the state
                        # look like it "carried over" out of nowhere after a
                        # restart -- and the first run silently executed in
                        # that mode with no UI indication.
                        await self._send_session_mode(ws, resumed)
                        await self._send_session_command(ws, resumed)
            except Exception as exc:
                logger.warning("[resume] background resume failed: %s", exc)

        self._tasks.add(asyncio.create_task(_background_resume()))

        # Track connection for gateway status broadcasting
        self._connections.append(ws)
        if self._adapter_manager:
            try:
                status = self._adapter_manager.get_status()
                await self._send(ws, "gateway_status", status=status)
            except Exception as e:
                logger.warning("[gateway_status] send error: %s", e)

        # Unified initial push: config+models, sessions, workspaces in one
        # consistent snapshot so the frontend (GUI/TUI alike) never has to
        # piece data together from per-panel requests.
        try:
            await self._push_all_data(ws)
        except Exception as exc:
            logger.warning("[push_all_data] initial push failed: %s", exc)

        async def _replay_first():
            """Yields the peeked first frame, then the live connection."""
            if _peeked_frame is not None:
                yield _peeked_frame
            async for _next_raw in ws:
                yield _next_raw

        try:
            async for raw in _replay_first():
                # Encryption is latched *on*, never off: a client whose keyfile
                # was not ready on its first frame may upgrade to encrypted
                # frames later, and one later frame must never be read as a
                # downgrade back to plaintext.
                if self._client_encrypted is not True and _detect_client_encryption(raw):
                    self._client_encrypted = True
                    logger.info("[ws] client transport encryption upgraded to on")

                # Protocol-version handshake: old clients never send a
                # version and stay compatible; new clients may declare one
                # on any frame (typically their first).
                if self._protocol_version is None:
                    try:
                        _text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        _obj = json.loads(_text)
                        if isinstance(_obj, dict) and _obj.get("protocol_version"):
                            self._protocol_version = int(_obj["protocol_version"])
                            logger.info("[protocol] client protocol_version=%s (server=%s)",
                                        self._protocol_version, PROTOCOL_VERSION)
                            if self._protocol_version > PROTOCOL_VERSION:
                                logger.warning(
                                    "[protocol] client version %s newer than server %s",
                                    self._protocol_version, PROTOCOL_VERSION,
                                )
                    except Exception:
                        self._protocol_version = PROTOCOL_VERSION

                try:
                    msg = parse_client_message(raw)
                except Exception:
                    await self._send(ws, "error", message="Failed to parse message", code="parse_error")
                    continue

                if msg is None:
                    await self._send(ws, "error", message="Unknown message type", code="parse_error")
                    continue

                # Legacy raw-dict messages (pre-typed protocol clients).
                if isinstance(msg, dict) and msg.get("type") == "delete_index":
                    from encre.server.protocol import ClientDeleteIndex
                    await self._h_delete_index(ws, ClientDeleteIndex(type="delete_index", path=msg.get("path", "")))
                    continue

                handler = self._DISPATCH.get(type(msg))
                if handler is not None:
                    # A handler failure must never kill the connection
                    # silently: report it to the client as an error event and
                    # keep serving.  Only genuine connection-level failures
                    # propagate to the outer except clauses below.
                    try:
                        await handler(self, ws, msg)
                    except (ConnectionResetError, websockets.exceptions.ConnectionClosed):
                        raise
                    except Exception as exc:
                        logger.error(
                            "[ws] handler %s failed: %s",
                            getattr(msg, "type", type(msg).__name__), exc, exc_info=True,
                        )
                        with contextlib.suppress(Exception):
                            await self._send(ws, "error", message=f"Internal error: {exc}", code="handler_error")

        except (ConnectionResetError, OSError) as _conn_err:
            logger.debug("[ws] connection reset: %s", _conn_err)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("[ws] connection closed")
        finally:
            if self._iclaw_task and not self._iclaw_task.done():
                self._iclaw_task.cancel()
            self._connections = [c for c in self._connections if c is not ws]

    # ── Session auto-naming ──────────────────────────────────────────────

    @staticmethod
    def _truncate_name(text: str) -> str:
        """Clamp a generated/manual title by script, proportionally.

        CJK (Chinese/Japanese/Korean): first 8 characters -- compact,
        information-dense scripts.  Other (Latin/Cyrillic/etc.) scripts:
        first 8 words.  The result is stripped of wrapping quotes/brackets
        that models like to add around short answers.
        """
        import re
        text = text.strip().strip('"').strip("'").strip("「").strip("」").strip("『").strip("』")
        if re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text):
            return text[:8]
        return ' '.join(text.split()[:8])

    async def _auto_name_session(self, session: Any, first_user_msg: str) -> str:
        """Generate a concise session name from the user's first message.

        Builds a DEDICATED backend instance with thinking disabled: the
        shared loop backend follows the user's thinking config, and a
        reasoning model would burn the whole ``max_tokens`` budget on
        reasoning tokens, yielding an empty title (the historical cause
        of titles always falling back to truncated text).  The dedicated
        instance also avoids racing the concurrently-running agent turn.
        If the call fails or times out, returns empty string (no name set).
        """
        from encre.backend import create_backend
        from encre.utils.types import DisabledThinking

        backend = None
        try:
            cfg = getattr(session.agent, "config", None)
            loop_backend = getattr(getattr(session.agent, "loop", None), "backend", None)
            if cfg is not None and getattr(cfg, "backend_type", ""):
                try:
                    backend = create_backend(
                        cfg.backend_type,
                        api_key=cfg.api_key,
                        base_url=cfg.base_url,
                        model=cfg.model,
                        models=cfg.models,
                        thinking_config=DisabledThinking(),
                        **cfg.backend_kwargs,
                    )
                except Exception:
                    backend = None
            if backend is None:
                backend = loop_backend
            if backend is None:
                logger.debug("[session] auto-name: backend is None")
                return ""
            prompt_text = first_user_msg.strip()[:500]
            if not prompt_text:
                return ""
            sys_prompt = (
                "You are a title naming assistant. Based on the user's message, "
                "generate a concise title in the SAME language as the message. "
                "For Chinese: at most 8 characters. "
                "For English: at most 8 words. "
                "For other languages: at most 8 words (or 8 CJK characters if applicable). "
                "Return ONLY the title text, no quotes, no explanation, no punctuation."
            )
            gen = backend.chat(
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt_text},
                ],
                max_tokens=30,
                stream=True,
            )
            full_text = ""
            from encre.utils.types import BackendText
            async for event in gen:
                if isinstance(event, BackendText):
                    full_text += event.text
                elif isinstance(event, BackendFinish):
                    break
                elif isinstance(event, BackendError):
                    logger.debug("[session] auto-name: backend error: %s", event.error)
                    return ""
            name = self._truncate_name(full_text)
            if len(name) < 2:
                logger.debug("[session] auto-name: generated name too short: '%s'", full_text[:50])
                return ""
            return name
        except Exception as e:
            logger.warning("[session] auto-name failed: %s", e, exc_info=True)
            return ""
        finally:
            # Only close the dedicated instance -- never the shared loop backend.
            if backend is not None and backend is not getattr(
                getattr(session.agent, "loop", None), "backend", None
            ):
                try:
                    await backend.aclose()
                except Exception:
                    pass

    async def _auto_name_and_rename(self, session: Any, prompt: str) -> None:
        """Generate a session name in the background (fire-and-forget)."""
        try:
            # Defense-in-depth: never rename (nor broadcast a rename for)
            # temp chat sessions -- they must stay invisible to the sidebar.
            live_check = self._manager.get_session(session.session_id)
            _meta = getattr(live_check, "metadata", {}) or {}
            _agent_sess = getattr(getattr(live_check, "agent", None), "session", None)
            if _meta.get("temp_chat") or (
                _agent_sess is not None and _agent_sess.metadata.get("temp_chat")
            ):
                return
            name = await asyncio.wait_for(
                self._auto_name_session(session, prompt), timeout=15.0)
            if name:
                # A user may rename the session while title generation is still
                # running. Never let this late background result replace it.
                live_session = self._manager.get_session(session.session_id)
                if live_session and live_session.metadata.get("name_manually_renamed"):
                    return
                self._manager.rename_session(session.session_id, name, manual=False)
                logger.info("[session] auto-named %s -> %s", session.session_id[:8], name)
                # Notify the frontend so it can update the session bar immediately.
                await self._broadcast_session_renamed(session.session_id, name)
            else:
                # Fallback: use truncated first user message as name
                fallback = self._truncate_name(prompt)
                if fallback:
                    self._manager.rename_session(session.session_id, fallback, manual=False)
                    logger.info("[session] auto-name fallback %s -> %s", session.session_id[:8], fallback)
                    await self._broadcast_session_renamed(session.session_id, fallback)
        except TimeoutError:
            logger.debug("[session] auto-name timed out (15s)")
        except Exception as e:
            logger.debug("[session] auto-name failed: %s", e, exc_info=True)

    async def _broadcast_session_renamed(self, session_id: str, new_name: str) -> None:
        """Send session_renamed to all connected clients, then refresh the
        unified session snapshot (sidebar + tray read the same list)."""
        for ws in list(self._connections):
            try:
                await self._send(ws, "session_renamed", session_id=session_id, new_name=new_name)
            except Exception:
                pass
        self._broadcast_sessions()
