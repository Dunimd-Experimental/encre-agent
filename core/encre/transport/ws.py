#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
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

"""WebSocket connection layer for the Encre client protocol.

This module is the ``transport/`` end of Encre's three-layer design.  The
:class:`EncreWSHandler` class is instantiated once by
:class:`encre.server.app.EncreServer` and reused for every accepted
socket; it therefore has to hold no per-connection state in its
constructor and must re-initialise that state at the start of
:meth:`EncreWSHandler.handle`.

Responsibilities are split deliberately:

* *this* module owns the wire layer only -- raw frame reading, optional
  transport-encryption negotiation, protocol-version handshake, frame
  dispatch, error reporting, connection bookkeeping, and the initial
  state snapshot pushed to a fresh client.
* every client-message *domain* (sessions, run, chat, permissions,
  skills, history, workspaces, profile, automation, plugins, terminal,
  settings, gateway events) is implemented by one mixin under
  :mod:`encre.protocol.handlers` and composed into the handler through
  inheritance.
* typed message parsing lives in :mod:`encre.server.protocol`
  (:func:`encre.server.protocol.parse_client_message`); the mapping from
  message class to handler function is the :data:`EncreWSHandler._DISPATCH`
  table below.
* :mod:`encre.gateway.ws_bridge` is a separate WebSocket product for
  running platform adapters in another process; it reuses this handler's
  event broadcasting rather than its own server.

Inbound data flow: frame -> encryption latch -> protocol-version latch ->
:func:`encre.server.protocol.parse_client_message` -> :data:`_DISPATCH`
or plugin fallback -> domain handler on :mod:`encre.protocol.handlers`.
Outbound data flow: domain handlers and event publishers call
:meth:`HandlerBase._send`, which applies the negotiated encryption mode.

Public API: :class:`EncreWSHandler`.  The module-level helpers are
implementation detail kept local so the transport layer never imports the
harness package for slash-command loading.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import websockets
from encre.protocol.handlers.automation import AutomationHandlers
from encre.protocol.handlers.base import HandlerBase
from encre.protocol.handlers.chat import ChatHandlers
from encre.protocol.handlers.events import EventDispatchMixin
from encre.protocol.handlers.history import HistoryHandlers
from encre.protocol.handlers.permissions import PermissionHandlers
from encre.protocol.handlers.plugins import PluginHandlers
from encre.protocol.handlers.profile import ProfileHandlers
from encre.protocol.handlers.run import RunHandlers
from encre.protocol.handlers.sessions import SessionsHandlers
from encre.protocol.handlers.settings import SettingsHandlers
from encre.protocol.handlers.skills import SkillsHandlers
from encre.protocol.handlers.terminal import TerminalHandlers
from encre.protocol.handlers.workspaces import WorkspacesHandlers
from encre.server.protocol import (
    PROTOCOL_VERSION,
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
    ClientPluginInstall,
    ClientPluginUninstall,
    ClientPluginEnable,
    ClientPluginDisable,
    ClientPluginInstallMCP,
    ClientPluginInstallAgent,
    ClientMarketInstall,
    ClientDeleteGlobalRule,
    ClientDeleteMessage,
    ClientDeleteModel,
    ClientClearAllSessions,
    ClientDeleteSession,
    ClientEditMessage,
    ClientEngineInstallResponse,
    ClientExportData,
    ClientExportSession,
    ClientExportSessionsBatch,
    ClientFetchModels,
    ClientGetConfig,
    ClientGetGitignore,
    ClientToolStop,
    ClientGetGlobalRuleContent,
    ClientGetMemoryDetail,
    ClientGetMemoryGraph,
    ClientGetMemoryList,
    ClientGetProfile,
    ClientGetUsageStats,
    ClientGetWorkspaceConfig,
    ClientIclawResume,
    ClientImportData,
    ClientGetSkillDetail,
    ClientGetSkillFile,
    ClientInstallSkill,
    ClientLibraryAddClip,
    ClientLibraryAddFile,
    ClientLibraryCreateNode,
    ClientLibraryDeleteNode,
    ClientLibraryGetContent,
    ClientLibraryList,
    ClientLibraryMoveNode,
    ClientLibraryRenameNode,
    ClientLibrarySaveContent,
    ClientListAllSessions,
    ClientListArchivedSessions,
    ClientListGlobalRules,
    ClientListModels,
    ClientListProjectHooks,
    ClientListProjectRules,
    ClientListSessions,
    ClientListWorkspaces,
    ClientNewSession,
    ClientOpenWorkspace,
    ClientPing,
    ClientPluginConfirm,
    ClientPlanApprove,
    ClientPlanReject,
    ClientQueueList,
    ClientQueueMove,
    ClientQueuePop,
    ClientQueueRemove,
    ClientQueueSteer,
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
    ClientUpdateProviders,
    ClientUpdateSkill,
    ClientUpdateSkills,
    ClientUpdateSubAgents,
    ClientUploadWorkspaceIcon,
    ClientValidateModel,
    ClientWechatScan,
    parse_client_message,
)
from encre.settings_manager import (  # noqa: E402
    load_custom_slash_commands,
)
from encre.slash_commands import set_custom_commands_provider  # noqa: E402
from encre.utils.types import BackendError, BackendFinish

# Wire the harness-side slash-command loader into the shared slash-command
# registry at import time.  Doing it here keeps :mod:`encre.slash_commands`
# free of any import from core packages, which would invert the dependency
# direction (slash command matching is also used by the harness loop).
set_custom_commands_provider(load_custom_slash_commands)

logger = logging.getLogger("encre.transport.ws")

# One plugin UI handler is a request/response action, not a long-running job.
# Bounding it keeps a wedged plugin from holding the socket's receive loop.
_PLUGIN_HANDLER_TIMEOUT_S = 30.0

# ``handle()`` waits at most this long for the client's first frame to verify
# that it speaks encrypted transport.  The renderer finishes ``initCrypto()``
# *before* it opens the socket and sends its first frame the instant the socket
# opens, so the wait is normally free; the timeout only bounds a client that
# never speaks.  A long wait here would be visible as UI freeze on desktop
# startup, but too short a one would reject a merely slow client, because the
# handshake now fails closed.
_ENCRYPTION_PEEK_TIMEOUT_S = 0.5

#: WebSocket close code sent to a client whose first frame was not encrypted.
#: 4400 is in the private-use range, so it cannot collide with a standard
#: protocol or library close code.
_HANDSHAKE_PLAINTEXT_CLOSE_CODE = 4400


def _detect_client_encryption(raw: str | bytes) -> bool:
    """Decide whether one inbound frame is encrypted transport ciphertext.

    The transport encryption scheme is stream-style base64 encoding of
    ``nonce || ciphertext || tag``; it carries no framing field, so the mode
    has to be inferred from the bytes themselves.  The discriminator used
    here is structural: a plaintext frame is JSON and therefore always
    begins with ``{`` once stripped of leading whitespace, while a base64
    alphabet never produces that.  Frames that are not valid UTF-8 at all
    are treated as encrypted, since plaintext protocol frames are always
    valid UTF-8 JSON.

    Args:
        raw: One raw WebSocket frame, either ``str`` or ``bytes`` as
            delivered by :mod:`websockets` depending on binary/text use.

    Returns:
        ``True`` if the frame should be decoded with the encrypted client
        path (see :func:`encre.server.protocol._parse_client_encrypted`),
        ``False`` if it is plain JSON.

    Raises:
        Nothing; every failure mode collapses to ``True`` so a misread frame
        can never be silently mis-parsed as plaintext JSON.
    """
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except UnicodeDecodeError:
        return True
    return not text.strip().startswith("{")


# Mirror this module's standard-library logging into the loguru sink so the
# transport layer is visible in the Encre Agent Desktop's unified log output.  The
# guard is total: if loguru is not installed (headless/harness runs) the
# whole block is dropped and only the stdlib handler stays active.
try:

    from loguru import logger as _loguru_logger
    class _LoguruHandler(logging.Handler):
        """Forward ``encre.transport.ws`` records into the loguru pipeline."""

        def emit(self, record):
            """Copy a stdlib record to loguru, preserving tracebacks.

            ``depth=6`` is what the loguru call sits below in the stack; a
            wrong depth does not lose the message, it only mis-attributes
            the source file/line in the log record.  ``exception=record.exc_info``
            is mandatory: without it, ``logger.exception`` calls from the
            handler mixins would log a traceback-free line.
            """
            try:
                _loguru_logger.opt(depth=6, exception=record.exc_info).log(record.levelname, record.getMessage())
            except Exception:
                pass
    _loguru_handler = _LoguruHandler()
    _loguru_handler.setLevel(logging.DEBUG)
    logging.getLogger("encre.transport.ws").addHandler(_loguru_handler)
    logging.getLogger("encre.transport.ws").setLevel(logging.DEBUG)
    # websockets is chatty at DEBUG/TRACE level (every PING/PONG is logged).
    # Those frames are protocol plumbing, not Encre state, so they are clamped
    # to WARNING to keep connection churn out of the desktop log.
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
    PluginHandlers,
    TerminalHandlers,
    SettingsHandlers,
    EventDispatchMixin,
):
    """Per-connection handler for the Encre WebSocket protocol.

    One instance of this class serves every socket of the server's lifetime
    (see :class:`encre.server.app.EncreServer`), so all per-connection
    knowledge -- current session, negotiated protocol version, encryption
    mode -- is written into instance attributes
    inside :meth:`handle` rather than in ``__init__``.  Any code that reads
    those attributes must therefore only run while ``handle`` owns the
    connection; the background task bookkeeping in :attr:`_tasks` exists
    precisely so tasks outlive their owning frame loop without leaking.

    Inheritance order matters: :class:`HandlerBase` supplies shared
    plumbing (``_send``, ``_connections``, ``_tasks``, session/config
    managers), the domain mixins supply one :meth:`_h_*` method each, and
    :class:`EventDispatchMixin` fans agent events out to all connected
    clients.  A handler registered twice in the MRO would silently shadow
    the other, so each domain mixin appears exactly once.

    Invariants maintained by :meth:`handle`:

    * ``_client_encrypted`` is ``None`` until first detection, then only
      ever moves ``None -> False`` or ``None -> True``, and ``False -> True``
      is legal but ``True -> False`` is not.  Downgrading would decrypt a
      ciphertext frame as JSON and drop the client silently.
    * ``_current_session_id`` is non-``None`` for the entire frame loop,
      holding either the placeholder session id or the resumed session id.
    * The socket is appended to ``_connections`` only after the initial
      snapshot is pushed and removed in ``finally``, so broadcast loops
      never observe a half-initialised peer.
    """

    def __init__(self, session_manager, config: Any = None,
                 adapter_manager=None, scheduler=None, plugin_host: Any = None) -> None:
        """Construct a handler shared by every connection.

        Args:
            session_manager: The :class:`encre.server.session_manager`
                instance that owns session persistence and resume logic.
            config: Optional default configuration snapshot used when no
                workspace-scoped config is available yet.
            adapter_manager: Optional gateway adapter manager; when present,
                ``gateway_status`` is pushed to every new client.
            scheduler: Optional automation scheduler consumed by
                :class:`encre.protocol.handlers.AutomationHandlers`.
            plugin_host: Optional registry-owned plugin host whose
                ``ws_handlers`` map backs the dispatch fallback for custom
                message types and whose ``ui_provider_entries`` back
                :meth:`_send_plugin_ui_sync`.
        """
        super().__init__(session_manager, config=config,
                         adapter_manager=adapter_manager, scheduler=scheduler)
        # Held only for lookups; the registry, not this handler, owns the
        # plugin lifecycle.
        self._plugin_host = plugin_host
        # Set once the first connection's boot snapshot has been pushed (see
        # the connect handler).  The server's plugin warmup waits on this
        # before importing hundreds of EA packages, so heavy discovery never
        # starves the first client's snapshot under the GIL.  Created lazily
        # is not an option: the event loop that waits on it and the loop that
        # sets it are the same, so a plain asyncio.Event bound at construction
        # is safe.
        self._boot_push_done = asyncio.Event()

    # Client message class -> unbound handler function.  Functions are looked
    # up on the *mixin class*, not on ``self``, so a mixin can override a base
    # helper without affecting dispatch; dispatch itself then resolves ``self``
    # at call time (``handler(self, ws, msg)``), which keeps per-connection
    # state live.  Adding a new client message type means: define the typed
    # class in :mod:`encre.server.protocol`, add its parser there, add one row
    # here, and implement ``_h_*`` on the owning domain mixin.
    _DISPATCH: dict[type, Any] = {
        ClientPing: SettingsHandlers._h_ping,
        ClientListModels: SettingsHandlers._h_list_models,
        ClientListSessions: SessionsHandlers._h_list_sessions,
        ClientListAllSessions: SessionsHandlers._h_list_all_sessions,
        ClientNewSession: SessionsHandlers._h_new_session,
        ClientConfigure: SettingsHandlers._h_configure,
        ClientWechatScan: SettingsHandlers._h_wechat_scan,
        ClientRun: RunHandlers._h_run,
        ClientQueueList: RunHandlers._h_queue_list,
        ClientQueuePop: RunHandlers._h_queue_pop,
        ClientQueueRemove: RunHandlers._h_queue_remove,
        ClientQueueMove: RunHandlers._h_queue_move,
        ClientQueueSteer: RunHandlers._h_queue_steer,
        ClientRespondPermission: PermissionHandlers._h_respond_permission,
        ClientPluginConfirm: PermissionHandlers._h_plugin_confirm,
        ClientRespondPlan: PermissionHandlers._h_respond_plan,
        ClientSetPlanMode: PermissionHandlers._h_set_plan_mode,
        ClientSetMode: PermissionHandlers._h_set_mode,
        ClientSetCdpUrl: PermissionHandlers._h_set_cdp_url,
        ClientSetCommand: PermissionHandlers._h_set_command,
        ClientRespondQuestion: ChatHandlers._h_respond_question,
        ClientEngineInstallResponse: PermissionHandlers._h_engine_install_response,
        ClientCancel: ChatHandlers._h_cancel,
        ClientToolStop: ChatHandlers._h_tool_stop,
        ClientSteer: ChatHandlers._h_steer,
        ClientSpecApprove: PermissionHandlers._h_spec_approve,
        ClientSpecReject: PermissionHandlers._h_spec_reject,
        ClientPlanApprove: PermissionHandlers._h_plan_approve,
        ClientPlanReject: PermissionHandlers._h_plan_reject,
        ClientGetConfig: SettingsHandlers._h_get_config,
        ClientUpdateProviders: SettingsHandlers._h_update_providers,
        ClientSetActiveModel: SettingsHandlers._h_set_active_model,
        ClientDeleteModel: SettingsHandlers._h_delete_model,
        ClientFetchModels: SettingsHandlers._h_fetch_models,
        ClientValidateModel: SettingsHandlers._h_validate_model,
        ClientUpdateSkills: SkillsHandlers._h_update_skills,
        ClientGetSkillDetail: SkillsHandlers._h_get_skill_detail,
    ClientGetSkillFile: SkillsHandlers._h_get_skill_file,
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
        ClientClearAllSessions: SessionsHandlers._h_clear_all_sessions,
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
        ClientGetMemoryGraph: ProfileHandlers._h_get_memory_graph,
        ClientListGlobalRules: ProfileHandlers._h_list_global_rules,
        ClientListProjectRules: ProfileHandlers._h_list_project_rules,
        ClientListProjectHooks: ProfileHandlers._h_list_project_hooks,
        ClientSaveGlobalRule: ProfileHandlers._h_save_global_rule,
        ClientDeleteGlobalRule: ProfileHandlers._h_delete_global_rule,
        ClientGetGlobalRuleContent: ProfileHandlers._h_get_global_rule_content,
        ClientGetProfile: ProfileHandlers._h_get_profile,
        ClientGetGitignore: WorkspacesHandlers._h_get_gitignore,
        ClientSetGitignore: WorkspacesHandlers._h_set_gitignore,
        ClientLibraryAddClip: ProfileHandlers._h_library_add_clip,
        ClientLibraryAddFile: ProfileHandlers._h_library_add_file,
        ClientLibraryCreateNode: ProfileHandlers._h_library_create_node,
        ClientLibraryDeleteNode: ProfileHandlers._h_library_delete_node,
        ClientLibraryGetContent: ProfileHandlers._h_library_get_content,
        ClientLibraryList: ProfileHandlers._h_library_list,
        ClientLibraryMoveNode: ProfileHandlers._h_library_move_node,
        ClientLibraryRenameNode: ProfileHandlers._h_library_rename_node,
        ClientLibrarySaveContent: ProfileHandlers._h_library_save_content,
        ClientResume: RunHandlers._h_resume,
        ClientIclawResume: SessionsHandlers._h_iclaw_resume,
        ClientTerminalListShells: TerminalHandlers._h_terminal_list_shells,
        ClientTerminalSpawn: TerminalHandlers._h_terminal_spawn,
        ClientTerminalWrite: TerminalHandlers._h_terminal_write,
        ClientTerminalResize: TerminalHandlers._h_terminal_resize,
        ClientTerminalKill: TerminalHandlers._h_terminal_kill,
        ClientPluginInstall: PluginHandlers._h_plugin_install,
        ClientPluginUninstall: PluginHandlers._h_plugin_uninstall,
        ClientPluginEnable: PluginHandlers._h_plugin_enable,
        ClientPluginDisable: PluginHandlers._h_plugin_disable,
        ClientPluginInstallMCP: PluginHandlers._h_plugin_install_mcp,
        ClientPluginInstallAgent: PluginHandlers._h_plugin_install_agent,
        ClientMarketInstall: PluginHandlers._h_market_install,
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
        """Own one WebSocket connection from accept until close.

        The method runs in three phases:

        1. *Setup.*  Re-initialise per-connection state, sniff the
           client's transport-encryption mode from its first frame, and
           send a placeholder ``session_ready`` so the renderer can start
           rendering chrome immediately.
        2. *Bootstrap.*  Kick off background resume, publish initial
           snapshots (gateway status, unified config/models/sessions/
           workspaces push, plugin UI manifest, pending plugin
           confirmations).
        3. *Frame loop.*  Read frames (replaying the peeked one first),
           parse, dispatch, and report errors without dropping the
           connection.

        Args:
            ws: The connected :class:`websockets.WebSocketServerProtocol`
                for this client.  All sends go through
                :meth:`HandlerBase._send` so the negotiated encryption
                mode is applied uniformly.

        Returns:
            ``None``; control returns only when the socket closes or the
            connection resets.

        Raises:
            ``ConnectionResetError``, ``OSError``, and
            ``websockets.exceptions.ConnectionClosed`` are swallowed at
            DEBUG level: they are the normal end of life for a desktop
            client that gets killed.  Any other exception propagates so
            the server's supervisor can see a genuine bug.

        Note:
            Everything between the placeholder ``session_ready`` and the
            ``finally`` block is optional-from-the-client's-point-of-view;
            the renderer tolerates any subset of the initial pushes
            arriving, so failures here degrade to a slower first paint
            rather than a broken connection.
        """
        # One ConnectionState per socket, bound as the ambient connection for
        # the whole of this task.  Every ``self._xyz`` per-connection read and
        # write below now resolves to *this* socket's state, so two concurrent
        # clients can no longer overwrite each other's session binding,
        # workspace, terminal table or write lock.
        state = self._connections_state.register(ws)
        # Start the outbound writer before anything can send: every frame for
        # this socket is queued and drained by this one task, so the run loop
        # never waits on socket I/O.  The writer inherits no ambient binding;
        # it only touches the state it was given.
        state.writer_task = asyncio.create_task(self._writer_loop(state))
        _conn_token = self._connections_state.bind(state)
        # Suppress session-change broadcasts for this connection until the
        # unified push below completes: the placeholder session creation,
        # the background resume and the push's own ``_get_or_create_session``
        # all fire the manager's sessions-changed callback, and each fired
        # callback sends a sessions_list that duplicates the plane the push
        # already carries.  Cleared after the push AND a bounded wait for the
        # resume task, so a late resume-created session cannot re-duplicate.
        _push_state = state
        _push_state.suppress_session_broadcast = True
        t_conn = time.time()
        logger.info("[perf] backend ws.accept")

        # Learn the client's transport-encryption capability from its first
        # frame *before* anything is sent back.  Encryption is mandatory, so
        # this is a fail-closed handshake rather than a negotiation: a client
        # that opens with plaintext is rejected instead of being served clear
        # frames.  ``websockets`` retains the frame in its read queue, so the
        # peeked frame is replayed into the loop below (see ``_replay_first``).
        _peeked_frame: str | bytes | None = None
        try:
            _peeked_frame = await asyncio.wait_for(ws.recv(), timeout=_ENCRYPTION_PEEK_TIMEOUT_S)
        except Exception as _peek_err:  # timeout / already closing
            logger.debug("[ws] encryption peek unavailable: %s", _peek_err)
            _peeked_frame = None
        if not _detect_client_encryption(_peeked_frame):
            logger.warning(
                "[ws] rejecting plaintext client handshake on %s -- "
                "transport encryption is mandatory", state.ws_id,
            )
            with contextlib.suppress(Exception):
                await ws.close(code=_HANDSHAKE_PLAINTEXT_CLOSE_CODE,
                               reason="transport encryption required")
            self._connections_state.unregister(ws)
            self._connections_state.unbind(_conn_token)
            return
        state.client_encrypted = True
        logger.info("[ws] client transport encryption=on (verified from first frame)")

        # Send a placeholder ``session_ready`` immediately so the frontend can
        # start filling sidebar/tray chrome without waiting for the session
        # resume.  The real resume runs as a background task and sends a second
        # ``session_ready`` carrying the full message history.  The
        # ``protocol_version`` rides on this first frame: old clients ignore
        # the extra JSON key, new clients read it as the handshake answer.
        placeholder = self._get_or_create_session()
        self._info = placeholder
        self._current_session_id = placeholder.session_id
        await self._send(ws, "session_ready", session_id=placeholder.session_id, plan_items=[],
                         protocol_version=PROTOCOL_VERSION,
                         active_model_index=getattr(placeholder.agent.config, "active_model_index", 0))
        logger.info("[perf] placeholder session_ready sent in %.0fms", (time.time() - t_conn) * 1000)

        # Resolve startup behaviour and resume the most recent session in the
        # background.  Doing this off the frame loop keeps a slow session load
        # (large JSONL history, cold index build, agent bootstrap) from
        # starving the dispatch loop below or delaying the client's own
        # config and session-list requests.
        startup_behavior = self._resolve_startup_behavior()

        async def _background_resume() -> None:
            """Load the resume target without blocking the connection.

            Runs as a sibling task of the frame loop and communicates back
            only through ``_send`` frames plus the ``_info``/
            ``_current_session_id`` attributes the dispatch loop reads.

            Returns:
                ``None``.

            Raises:
                Anything is caught here and logged; a failed resume must
                never tear down a healthy connection.
            """
            try:
                if startup_behavior == "last":
                    t_resume = time.time()
                    # Agent boot plus full JSONL history parse is CPU/IO heavy,
                    # so it runs in a worker thread.  Keeping it off the event
                    # loop guarantees the client's config and session-list
                    # requests are still serviced while the snapshot builds.
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
                        # mode so config and derived ``plan_mode_active`` agree
                        # with the stored metadata before the first run starts.
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
                                         branches=branches_list, active_branch_id=sess.active_branch_id,
                                         active_model_index=getattr(
                                             resumed.agent.config, "active_model_index", 0))
                        # Surface the persisted mode and command right away so
                        # composer chips reflect reality BEFORE the first run.
                        # Without this, the mode only reached the frontend at
                        # run end (from the run handler's ``finally`` block),
                        # which made the state look like it carried over from
                        # nowhere after a restart while the first run already
                        # executed in that mode with no UI indication.
                        await self._send_session_mode(ws, resumed)
                        await self._send_session_command(ws, resumed)
            except Exception as exc:
                logger.warning("[resume] background resume failed: %s", exc)

        _resume_task = asyncio.create_task(_background_resume())
        # Prune finished tasks from the shared set: this handler serves every
        # connection for the whole server lifetime, so an unpruned set would
        # accumulate done task objects (and their frames) across a long
        # running deployment.
        _resume_task.add_done_callback(self._tasks.discard)
        self._tasks.add(_resume_task)

        # Register the socket for gateway status broadcasts.  This list is the
        # single source of truth every ``broadcast_*`` method iterates over.
        self._connections.append(ws)
        if self._adapter_manager:
            try:
                status = self._adapter_manager.get_status()
                await self._send(ws, "gateway_status", status=status)
            except Exception as e:
                logger.warning("[gateway_status] send error: %s", e)

        # Unified initial push: config+models, sessions, workspaces in one
        # consistent snapshot so the renderer (GUI or TUI alike) never has to
        # piece the UI together from per-panel requests.  Each of the pushes
        # below is individually failure-isolated so one broken source cannot
        # blank the whole initial state.
        try:
            await self._push_all_data(ws)
        except Exception as exc:
            logger.warning("[push_all_data] initial push failed: %s", exc)
        finally:
            # Bounded wait for the resume task: its session load also fires
            # the sessions-changed callback, so the broadcast suppression
            # must outlive the push.  The wait is capped so a hung resume
            # cannot delay the dispatch loop forever.
            with contextlib.suppress(Exception):
                await asyncio.wait({_resume_task}, timeout=20)
            _push_state.suppress_session_broadcast = False
            # Signal the server's plugin warmup that this connection's boot
            # snapshot has landed, releasing the heavy plugin-module import
            # phase (it waits on this event, bounded by its own timeout, so
            # a headless server with no client still warms up).
            with contextlib.suppress(Exception):
                self._boot_push_done.set()

        # Push plugin UI provider manifests so the renderer can dynamically
        # mount plugin-contributed frontend components.  The lifecycle
        # snapshot and the warmup-only frames below stay gated on warmup
        # completion, but the UI sync itself goes out unconditionally: the
        # builtin tool-UI catalog is seeded on the plugin host at server
        # construction, so even a client connecting during plugin warmup gets
        # every core tool's icon/label/card on first paint instead of neutral
        # strips until warmup ends.  The end-of-warmup broadcast then adds the
        # plugin-sourced entries; the renderer diffs the catalog signature and
        # only rebuilds tool cards when it actually changed.
        if getattr(self, "_plugin_warmup_complete", False):
            # Push the lifecycle snapshot to this connection only: management
            # surfaces (the plugin market, the settings plugins panel) render
            # from ``plugin_changed`` and a client that connects after the
            # end-of-warmup broadcast would otherwise show them stale or
            # empty until the next lifecycle change.  It goes out BEFORE the
            # UI sync because the renderer tears its mounted plugin UI down
            # on every ``plugin_changed`` and expects the sync that follows
            # to re-mount it.
            try:
                from encre.agent import _get_shared_plugin_registry_raw
                await self._send(ws, "plugin_changed",
                                 plugins=_get_shared_plugin_registry_raw().snapshot_states())
            except Exception as exc:
                logger.warning("[plugin_changed] initial push failed: %s", exc)

            # Push third-party plugins awaiting user confirmation (dynamic
            # authorisation) so the renderer pops the approval dialog.
            try:
                await self._send_plugin_pending(ws)
            except Exception as exc:
                logger.warning("[plugin_confirm_request] initial push failed: %s", exc)

            # Push the central-repository index (ea-cwh catalog, or the last
            # known-good cache) so the market can render remote entries —
            # installable / updatable — alongside the local snapshot.
            try:
                from encre.market_catalog import load_catalog
                await self._send(ws, "market_catalog", **await asyncio.to_thread(load_catalog))
            except Exception as exc:
                logger.warning("[market_catalog] initial push failed: %s", exc)
                # Never leave the market spinning on a frame that will not
                # come: an empty catalog is an honest, complete answer.
                with contextlib.suppress(Exception):
                    await self._send(
                        ws, "market_catalog",
                        source="none", index_version="", generated_at="", plugins=[],
                    )

        # Replay the last persisted plugin snapshot before warmup finishes:
        # the market's installed tab becomes usable immediately instead of
        # waiting for hundreds of plugin imports. The authoritative snapshot
        # follows after warmup.
        try:
            from encre.plugin_state_cache import read_plugin_states
            cached_plugins = await asyncio.to_thread(read_plugin_states)
            if cached_plugins:
                await self._send(ws, "plugin_changed", plugins=cached_plugins, cached=True)
        except Exception as exc:
            logger.debug("[plugin_states] cached snapshot push failed: %s", exc)

        try:
            await self._send_plugin_ui_sync(ws)
        except Exception as exc:
            logger.warning("[plugin_ui_sync] initial push failed: %s", exc)

        async def _replay_first():
            """Yield the peeked first frame, then the live connection.

            ``websockets`` treats ``ws.recv()`` as consuming the frame from
            its queue, but iterating the connection object asynchronously
            resumes exactly where the queue left off.  Stitching the two
            together here means the peeked frame is processed exactly once
            and the loop below stays a single ``async for``.

            Yields:
                Raw frames in original arrival order.
            """
            if _peeked_frame is not None:
                yield _peeked_frame
            async for _next_raw in ws:
                yield _next_raw

        try:
            async for raw in _replay_first():
                # Transport encryption is already proven and mandatory, so
                # there is no latching or upgrade logic here: every frame in
                # both directions is AES-256-GCM, and the handshake above has
                # already rejected a client that did not comply.

                # Protocol-version handshake: old clients never declare a
                # version and stay compatible with the server default; new
                # clients may declare one on any frame (typically the first).
                if self._protocol_version is None:
                    # Sniff through the same decrypt path as normal parsing:
                    # the frame is ciphertext, so a raw json.loads() always
                    # failed here and silently pinned every client to the
                    # server's default version.
                    _obj = self._decode_raw_data(raw)
                    if isinstance(_obj, dict) and _obj.get("protocol_version"):
                        self._protocol_version = int(_obj["protocol_version"])
                        logger.info("[protocol] client protocol_version=%s (server=%s)",
                                    self._protocol_version, PROTOCOL_VERSION)
                        if self._protocol_version > PROTOCOL_VERSION:
                            logger.warning(
                                "[protocol] client version %s newer than server %s",
                                self._protocol_version, PROTOCOL_VERSION,
                            )
                    else:
                        # No declaration (or unreadable frame): assume the
                        # server's own version rather than treating it as legacy.
                        self._protocol_version = PROTOCOL_VERSION

                try:
                    # The handshake above proved this client encrypts, so
                    # every later frame must stay ciphertext: a plaintext
                    # frame is dropped instead of parsed.
                    msg = parse_client_message(raw, require_encryption=True)
                except Exception:
                    # Name the type: an unparseable frame used to be invisible
                    # server-side (the client just got a generic error), which
                    # is how four review-decision messages missing a `from_dict`
                    # went undetected for weeks -- every click died here.
                    _hint = "?"
                    with contextlib.suppress(Exception):
                        _data = self._decode_raw_data(raw)
                        if isinstance(_data, dict):
                            _hint = str(_data.get("type", "?"))
                    logger.warning("[ws] failed to parse client message: type=%s", _hint)
                    await self._send(ws, "error", message="Failed to parse message", code="parse_error")
                    continue

                if msg is None:
                    # Plugin-registered WS handlers: custom client message
                    # types not in the builtin protocol are dispatched to
                    # plugins registered via ``PluginContext.ws_handlers``.
                    plugin_data = self._decode_raw_data(raw)
                    plugin_handler = None
                    if plugin_data is not None and isinstance(plugin_data, dict):
                        _ptype = str(plugin_data.get("type", "") or "")
                        _plugin_id = str(plugin_data.get("plugin", "") or "")
                        if _ptype == "plugin_ui_action" and not _plugin_id:
                            await self._send(
                                ws, "error",
                                message="Plugin UI action is missing its plugin id",
                                code="plugin_action_unowned",
                            )
                            continue
                        plugin_handler = self._lookup_plugin_ws_handler(_ptype, _plugin_id)
                    if plugin_handler is not None:
                        try:
                            # A plugin handler owns only its own namespace; a
                            # hang must not stall the connection's receive
                            # loop or starve every other plugin.
                            await asyncio.wait_for(
                                plugin_handler(self, ws, plugin_data),
                                timeout=_PLUGIN_HANDLER_TIMEOUT_S,
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                "[ws] plugin handler %s timed out after %.0fs",
                                plugin_data.get("type", "?"), _PLUGIN_HANDLER_TIMEOUT_S,
                            )
                            with contextlib.suppress(Exception):
                                await self._send(
                                    ws, "error",
                                    message="Plugin handler timed out",
                                    code="plugin_timeout",
                                )
                        except (ConnectionResetError, websockets.exceptions.ConnectionClosed):
                            raise
                        except Exception as exc:
                            logger.error(
                                "[ws] plugin handler %s failed: %s",
                                plugin_data.get("type", "?"), exc, exc_info=True,
                            )
                            with contextlib.suppress(Exception):
                                await self._send(ws, "error", message=f"Internal error: {exc}", code="handler_error")
                        continue
                    await self._send(ws, "error", message="Unknown message type", code="parse_error")
                    continue

                handler = self._DISPATCH.get(type(msg))
                if handler is not None:
                    # Long, independent work (currently only model validation)
                    # is dispatched as a fire-and-forget task so several
                    # requests queued back-to-back on the same socket overlap
                    # instead of serialising.  Without this a save with N
                    # models costs N round trips of sequential probing.
                    if type(msg) is ClientValidateModel:
                        task = asyncio.create_task(
                            self._run_validation_handler(ws, handler, msg)
                        )
                        task.add_done_callback(self._tasks.discard)
                        self._tasks.add(task)
                        continue
                    # A handler failure must never kill the connection
                    # silently: report it to the client as an error event and
                    # keep serving.  Only genuine connection-level failures
                    # propagate to the except clauses below.
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
                else:
                    # A parsed message with no dispatch entry used to fall
                    # through the loop in silence: the client saw nothing and
                    # the server left no trace.
                    logger.warning(
                        "[ws] no handler registered for %s", type(msg).__name__
                    )
                    with contextlib.suppress(Exception):
                        await self._send(
                            ws, "error",
                            message=f"No handler for {getattr(msg, 'type', type(msg).__name__)}",
                            code="unhandled",
                        )

        except (ConnectionResetError, OSError) as _conn_err:
            logger.debug("[ws] connection reset: %s", _conn_err)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("[ws] connection closed")
        finally:
            # Release everything this connection owned.  Previously only the
            # iClaw task and the socket list entry were cleaned up, so PTYs,
            # queued run tasks and spec engines
            # outlived the client that created them.
            await self._teardown_connection(state)
            self._connections_state.unregister(ws)
            self._connections_state.unbind(_conn_token)

    async def _teardown_connection(self, state) -> None:
        """Release every resource owned by a closing connection.

        Args:
            state: The :class:`ConnectionState` of the closing socket.

        Returns:
            None.
        """
        ws_id = state.ws_id
        # Stop the outbound writer first: marking the connection closed keeps
        # late producers from enqueueing more frames, and cancelling the task
        # releases any in-flight socket write.
        state.closed = True
        _writer = state.writer_task
        state.writer_task = None
        if _writer is not None and not _writer.done():
            _writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _writer
        # An in-flight iClaw (embedded CLI agent) task must not outlive its
        # owning socket, or it would keep the Encre Agent Core busy after the client
        # is gone.
        if state.iclaw_task and not state.iclaw_task.done():
            state.iclaw_task.cancel()
        state.iclaw_task = None
        for task in list(state.wechat_poll_tasks.values()):
            if not task.done():
                task.cancel()
        state.wechat_poll_tasks.clear()
        for task in list(state.tasks):
            if not task.done():
                task.cancel()
        state.tasks.clear()
        # Terminated PTYs would otherwise keep their child processes and
        # reader tasks running for the rest of the server's life.
        for tinfo in list(state.term_sessions.values()):
            proc = tinfo.get("proc") if isinstance(tinfo, dict) else None
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.terminate()
        state.term_sessions.clear()
        state.spec_engines.clear()
        self._connections = [c for c in self._connections if c is not state.ws]
        logger.info("[ws] connection torn down (%s)", ws_id)

    # ---- Plugin dispatch fallback ----

    async def _run_validation_handler(self, ws: Any, handler: Any, msg: Any) -> None:
        """Run a :class:`ClientValidateModel` handler without blocking dispatch.

        Several consecutive validate_model frames on the same socket are
        spawned as independent tasks so their HTTP probes overlap; without
        this a provider save with N models costs N sequential round trips.
        A handler failure is reported to the client as an error frame and
        must never tear the connection down.
        """
        try:
            await handler(self, ws, msg)
        except (ConnectionResetError, websockets.exceptions.ConnectionClosed):
            return
        except Exception as exc:
            logger.error(
                "[ws] handler %s failed: %s",
                getattr(msg, "type", type(msg).__name__), exc, exc_info=True,
            )
            with contextlib.suppress(Exception):
                await self._send(ws, "error", message=f"Internal error: {exc}", code="handler_error")

    def _decode_raw_data(self, raw: Any) -> dict | None:
        """Decode one raw frame into a dict, regardless of encryption mode.

        This is only used on the plugin fallback path, after
        :func:`encre.server.protocol.parse_client_message` already failed to
        recognise the frame.  It re-runs the encrypted decode explicitly so a
        plugin message sent over an encrypted transport is still visible to
        the plugin registry.

        Args:
            raw: One raw frame (``str`` or ``bytes``).

        Returns:
            The decoded dict, or ``None`` if the frame is not a JSON object.

        Raises:
            Nothing; malformed frames collapse to ``None`` so the caller can
            emit its ``Unknown message type`` error.
        """
        try:
            from encre.server.protocol import _parse_client_encrypted
            decrypted = _parse_client_encrypted(raw, require_encryption=True)
            if isinstance(decrypted, bytes):
                decrypted = decrypted.decode("utf-8")
            data = json.loads(decrypted)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _plugin_ws_handlers(plugin_host: Any) -> dict:
        """Read the plugin host's WS handler map defensively.

        The registry may expose ``ws_handlers`` as a plain dict, as a
        property returning a snapshot, or not at all (plugin system
        disabled).  All three shapes are normalised here so the lookup path
        never has to care which one it got.

        Args:
            plugin_host: Optional registry-owned plugin host, or ``None``.

        Returns:
            A mapping from custom message type string to async handler
            callable, empty when nothing is registered.

        Raises:
            Nothing; an unreadable host degrades to ``{}``.
        """
        if plugin_host is None:
            return {}
        try:
            handlers = getattr(plugin_host, "ws_handlers", None)
            if handlers is None:
                return {}
            if isinstance(handlers, dict):
                return handlers
            # Some registry versions expose a property that returns a copy.
            return dict(handlers)  # type: ignore[arg-type]
        except Exception:
            return {}

    def _lookup_plugin_ws_handler(self, message_type: str, plugin: str = "") -> Any:
        """Resolve a plugin-registered handler for one custom message type.

        Args:
            message_type: The ``type`` field of the decoded frame, exactly as
                the plugin declared it.
            plugin: Owning plugin id from the frame.  When present the lookup
                is strictly namespaced, so plugin A can never be handed
                plugin B's handler.

        Returns:
            The async handler callable, or ``None`` if no plugin owns that
            type (or the frame's plugin is not the owner).
        """
        host = self._plugin_host
        if host is None:
            return None
        fast = getattr(host, "get_ws_handler", None)
        if callable(fast):
            try:
                return fast(message_type, plugin)
            except TypeError:
                # Older host without namespaced lookup: fall back to the flat
                # map only when the frame did not claim a different owner.
                return fast(message_type)
            except Exception:
                return None
        return self._plugin_ws_handlers(host).get(message_type)

    async def _send_plugin_ui_sync(self, ws) -> None:
        """Publish the plugin UI manifest to one connected client.

        The renderer consumes this frame to dynamically mount
        plugin-contributed frontend components.  ``assets_url`` points at
        the plugin-bundled ``ui/`` file served by the server's static route
        (``/plugin-assets/<name>/<path>``), so a plugin ships both its
        manifest and its frontend bundle.

        The frame carries three independent collections: UI providers
        (mountable components), tool metadata (renderer-side rendering
        hints for plugin tools), and generic contributions (any typed spec
        the renderer knows how to render).  Each is collected in its own
        ``try`` so a broken provider cannot prevent the others from
        arriving.

        Args:
            ws: Target socket.

        Returns:
            ``None``.

        Raises:
            Nothing; failures are logged at WARNING level so the client
            still receives the (possibly partial) manifest.
        """
        providers: list[dict[str, Any]] = []
        tool_meta: list[dict[str, Any]] = []
        # Engine-side frontend surface arbitration: every entry is checked
        # against the plugin's ``[tool.ea.ui_permissions]`` grants inside the
        # Rust engine before it is put on the wire.  Ungranted surfaces are
        # filtered out here, so the renderer physically never receives data
        # for a surface the plugin is not allowed to reach (fail-closed:
        # unknown plugins deny).
        try:
            from encre import native as _native

            _ui_gate = _native.permission_check_plugin_ui
        except Exception as exc:  # pragma: no cover - native is mandatory
            logger.error("[plugin_ui_sync] permission engine unavailable: %s", exc)
            _ui_gate = None
        if self._plugin_host is not None:
            # Builtin catalog/tool declarations are trusted core data, not
            # plugin-contributed, so they bypass the plugin UI gate (which is
            # fail-closed and only knows real plugin ids).  They must ship
            # even when the permission engine itself is unavailable -- without
            # them the renderer cannot know which tools are hidden and falls
            # back to rendering every tool call as a neutral strip.
            def _builtin_source(s: str) -> bool:
                return s in ("builtin-catalog", "builtin-tool")

            try:
                all_tool_meta = [
                    entry
                    for entry in (getattr(self._plugin_host, "tool_meta_entries", []) or [])
                    if isinstance(entry, dict)
                ]
            except Exception as exc:
                logger.warning("[plugin_ui_sync] tool_meta collect failed: %s", exc)
                all_tool_meta = []
            if _ui_gate is None:
                # Engine unavailable: plugin surfaces stay fail-closed
                # (nothing plugin-sourced goes out), trusted builtins ship.
                providers = []
                tool_meta = [
                    entry for entry in all_tool_meta
                    if _builtin_source(str(entry.get("source", "") or ""))
                ]
            else:
                try:
                    entries = getattr(self._plugin_host, "ui_provider_entries", []) or []
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        provider = entry.get("provider")
                        source = entry.get("source", "") or ""
                        if provider is None:
                            continue
                        if not _ui_gate(str(source), "providers", ""):
                            continue
                        name = self._provider_attr(provider, "name", source)
                        entry_point = self._provider_attr(provider, "entry_point", "")
                        mount_point = self._provider_attr(provider, "mount_point", "panel")
                        title = self._provider_attr(provider, "title", name)
                        description = self._provider_attr(provider, "description", "")
                        assets_url = ""
                        if entry_point and source:
                            assets_url = f"/plugin-assets/{source}/{entry_point}"
                        providers.append({
                            "name": name,
                            # Identity is (plugin, provider_id, mount_point),
                            # never the display name: two plugins may legally
                            # ship the same provider name without sharing a
                            # renderer container or state.
                            "plugin": str(source),
                            "provider_id": name,
                            "title": title,
                            "description": description,
                            "mount_point": mount_point,
                            "entry_point": entry_point,
                            "assets_url": assets_url,
                        })
                except Exception as exc:
                    logger.warning("[plugin_ui_sync] collect failed: %s", exc)
                try:
                    tool_meta = [
                        entry
                        for entry in all_tool_meta
                        if (
                            _builtin_source(str(entry.get("source", "") or ""))
                            or _ui_gate(str(entry.get("source", "") or ""), "tool_meta", "")
                        )
                    ]
                except Exception as exc:
                    logger.warning("[plugin_ui_sync] tool_meta collect failed: %s", exc)
        contributions: list[dict[str, Any]] = []
        if _ui_gate is not None:
            try:
                contributions = [
                    {"kind": str(entry.get("kind", "")), "plugin": str(entry.get("source", "")), "spec": entry.get("spec")}
                    for entry in (getattr(self._plugin_host, "contribution_entries", []) or [])
                    if isinstance(entry, dict)
                    and entry.get("kind")
                    and isinstance(entry.get("spec"), dict)
                    and _ui_gate(str(entry.get("source", "") or ""), "contribution", str(entry.get("kind", "")))
                ]
            except Exception as exc:
                logger.warning("[plugin_ui_sync] contributions collect failed: %s", exc)
        try:
            await self._send(
                ws,
                "plugin_ui_sync",
                providers=providers,
                tool_meta=tool_meta,
                contributions=contributions,
            )
        except Exception as exc:
            logger.warning("[plugin_ui_sync] send failed: %s", exc)

    async def broadcast_plugin_changed(self, plugins: list[dict[str, Any]]) -> None:
        """Broadcast ``plugin_changed`` to every connected client.

        Emitted after a plugin hot reload or any other plugin lifecycle
        change.  ``plugins`` is the registry's lifecycle snapshot
        (name/version/tier/source/state).  The renderer resets its mounted
        plugin UI on this frame; the follow-up
        :meth:`broadcast_plugin_ui_sync` re-mounts the fresh provider set
        and tool metadata, which is why the two are always emitted in that
        order.

        Args:
            plugins: Registry snapshot of every plugin's lifecycle state.

        Returns:
            ``None``.
        """
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            try:
                await self._send(state.ws, "plugin_changed", plugins=plugins)
            except Exception:
                pass
        # Persist the authoritative snapshot so the next launch can replay it
        # on connect instead of waiting for discovery to import every plugin.
        try:
            from encre.plugin_state_cache import write_plugin_states
            await asyncio.to_thread(write_plugin_states, plugins)
        except Exception as exc:
            logger.debug("[plugin_states] cache write failed: %s", exc)

    def _pending_plugin_payload(self) -> list[dict[str, Any]]:
        """Collect third-party plugins awaiting user confirmation.

        Reads through the shared plugin registry rather than the plugin
        host so that this works even when no plugin host has been injected
        into the handler.

        Returns:
            A list of plugin descriptors pending approval, or ``[]`` when
            the registry is unavailable or nothing is pending.

        Raises:
            Nothing; a failing registry call logs at DEBUG and returns
            ``[]``, which the renderer interprets as "clear stale dialogs".
        """
        try:
            from encre.agent import _get_shared_plugin_registry_raw
            return _get_shared_plugin_registry_raw().pending_plugins()
        except Exception as exc:
            logger.debug("[plugin_confirm_request] collect failed: %s", exc)
            return []

    async def _send_plugin_pending(self, ws) -> None:
        """Send the pending-plugin confirmation list to one client.

        Emitted on connect (and after warmup) so the renderer can pop the
        dynamic-authorisation dialog for every third-party plugin that is
        discovered but not yet approved.  An empty list is still sent so
        the frontend can clear dialogs left over from a previous session.

        Args:
            ws: Target socket.

        Returns:
            ``None``.

        Raises:
            Nothing; failures log at WARNING level.
        """
        try:
            await self._send(ws, "plugin_confirm_request", plugins=self._pending_plugin_payload())
        except Exception as exc:
            logger.warning("[plugin_confirm_request] send failed: %s", exc)

    async def broadcast_plugin_confirm_request(self) -> None:
        """Broadcast the pending-plugin confirmation list to every client.

        Kept separate from :meth:`broadcast_plugin_ui_sync` because the two
        frames drive different renderer surfaces (approval dialog vs
        mounted components) and plugins can become pending without any UI
        manifest change.

        Returns:
            ``None``.
        """
        plugins = self._pending_plugin_payload()
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            try:
                await self._send(state.ws, "plugin_confirm_request", plugins=plugins)
            except Exception:
                pass

    async def broadcast_plugin_ui_sync(self) -> None:
        """Re-send the plugin UI manifest and tool metadata to every client.

        Called after a plugin install/uninstall/reload so already-connected
        renderers do not need to reconnect to pick up new components.

        Returns:
            ``None``.
        """
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            try:
                await self._send_plugin_ui_sync(state.ws)
            except Exception:
                pass

    async def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Broadcast a custom event frame to every connected client.

        Used by the gateway and plugin layers to push arbitrary
        server-initiated frames (for example agent lifecycle events relayed
        by :class:`encre.protocol.handlers.EventDispatchMixin`) without
        having to import this module's connection bookkeeping.

        Args:
            event_type: Outbound frame type, e.g. ``"gateway_status"``.
            payload: Keyword arguments expanded into the frame body.

        Returns:
            ``None``; individual socket failures are dropped silently so one
            dead client cannot stall a broadcast.
        """
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            try:
                await self._send(state.ws, event_type, **payload)
            except Exception:
                pass

    @staticmethod
    def _provider_attr(provider: Any, key: str, default: Any = "") -> Any:
        """Read one field from a provider entry, tolerant of both shapes.

        Plugins may register UI providers either as plain dicts (the common
        case, since manifests are JSON-driven) or as dataclass instances.
        Accepting both keeps the manifest builder independent of the
        plugin packaging format.

        Args:
            provider: A dict or object describing one UI provider.
            key: Field name to read.
            default: Value returned when the field is absent or unreadable.

        Returns:
            The field value, or ``default``.

        Raises:
            Nothing; attribute access failures collapse to ``default``.
        """
        if isinstance(provider, dict):
            return provider.get(key, default)
        try:
            return getattr(provider, key, default)
        except Exception:
            return default

    # ---- Session auto-naming ----

    @staticmethod
    def _truncate_name(text: str) -> str:
        """Clamp a generated or manual title proportionally to its script.

        CJK scripts (Chinese/Japanese/Korean) are compact and
        information-dense, so their titles are cut to the first 8
        characters.  Spacing scripts (Latin, Cyrillic, Arabic, etc.) are
        cut to the first 8 whitespace-delimited words.  The result is
        stripped of wrapping quotes and Japanese quotation marks that
        small models like to add around short answers.

        Args:
            text: Raw title text, possibly quoted.

        Returns:
            A clamped title string with wrapping punctuation removed.
        """
        import re
        text = text.strip().strip('"').strip("'").strip("「").strip("」").strip("『").strip("』")
        if re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text):
            return text[:8]
        return ' '.join(text.split()[:8])

    async def _auto_name_session(self, session: Any, first_user_msg: str) -> str:
        """Generate a concise session title from the user's first message.

        A DEDICATED backend instance is built with thinking disabled rather
        than reusing the shared loop backend, for two reasons:

        * The shared loop backend follows the user's thinking config, and a
          reasoning model would spend the entire ``max_tokens`` budget on
          reasoning tokens, yielding an empty title (the historical cause of
          titles always falling back to truncated text).
        * Building a separate instance avoids racing the concurrently running
          agent turn for the same connection object.

        Args:
            session: The live session whose agent provides the model config.
            first_user_msg: The user's first message, used as the prompt.

        Returns:
            The generated title, or ``""`` when generation fails, times
            out, or produces a too-short result.  Callers treat ``""`` as
            "no name" and fall back to a truncated prompt.

        Raises:
            Nothing; every failure mode collapses to ``""``.
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
                        # Relays that force-require sampling fields reject the
                        # title call without the model's configured overrides.
                        **cfg.active_model_sampling_overrides(),
                        # A stale sampling copy persisted inside
                        # backend_kwargs must not shadow the explicit values.
                        **{
                            k: v
                            for k, v in cfg.backend_kwargs.items()
                            if k not in getattr(cfg, "_SAMPLING_KEYS", ())
                        },
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
            # Only close the dedicated instance -- never the shared loop
            # backend, which other connections still use.
            if backend is not None and backend is not getattr(
                getattr(session.agent, "loop", None), "backend", None
            ):
                try:
                    await backend.aclose()
                except Exception:
                    pass

    async def _auto_name_and_rename(self, session: Any, prompt: str) -> None:
        """Generate a session title in the background (fire-and-forget).

        Runs after the first user message of a session.  Two races are
        defended against explicitly:

        * temp chat sessions must never be renamed (nor have a rename
          broadcast for them) because they are intentionally invisible to
          the sidebar;
        * a user rename arriving while title generation is still in flight
          must win, so the live session is re-read before writing and a
          ``name_manually_renamed`` flag short-circuits the write.

        Args:
            session: The session that received the first message.
            prompt: The first user message, also used as the fallback name.

        Returns:
            ``None``.

        Raises:
            Nothing; a 15 second timeout or any exception leaves the
            session unnamed rather than blocking the caller.
        """
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
                # A user may rename the session while title generation is
                # still running.  Never let this late background result
                # replace the manual name.
                live_session = self._manager.get_session(session.session_id)
                if live_session and live_session.metadata.get("name_manually_renamed"):
                    return
                self._manager.rename_session(session.session_id, name, manual=False)
                logger.info("[session] auto-named %s -> %s", session.session_id[:8], name)
                # Notify the frontend so it can update the session bar
                # immediately instead of waiting for the next snapshot.
                await self._broadcast_session_renamed(session.session_id, name)
            else:
                # Fallback: use the truncated first user message as the name.
                # The same manual-rename defense as the generated branch
                # applies: a user rename that lands while title generation is
                # in flight must win over the fallback too, otherwise the
                # manual title is silently replaced by the first prompt.
                live_fallback = self._manager.get_session(session.session_id)
                if live_fallback and live_fallback.metadata.get("name_manually_renamed"):
                    return
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
        """Announce a session rename to every client and refresh the snapshot.

        Args:
            session_id: The renamed session's identifier.
            new_name: The new title.

        Returns:
            ``None``.
        """
        for state in self._connections_state.states():
            if state.ws is None:
                continue
            try:
                await self._send(state.ws, "session_renamed",
                                 session_id=session_id, new_name=new_name)
            except Exception:
                pass
        self._broadcast_sessions()
