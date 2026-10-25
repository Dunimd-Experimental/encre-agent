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

"""Automation domain handlers: scheduled-job CRUD, execution history, broadcasts.

Owned client-message domain: every ``ClientAutomation*`` message type that the
desktop automation panel emits.  :class:`AutomationHandlers` is composed into
:class:`encre.transport.ws.EncreWSHandler` by mixin inheritance and its
methods are the values of the automation entries in that handler's
``_DISPATCH`` table, so a ``ClientAutomationCreateJob`` frame always lands
here and nowhere else.

The backend is ``self._scheduler``, the iClaw job scheduler handed to the
handler by :class:`encre.server.app.EncreServer` at construction time.  Jobs
are *definitions* (prompt, cron, model, push gateways); each firing of a job
spawns a sub-agent run that is persisted under
``<data_dir>/sub_agents/<session_id>/`` and recorded as a ``JobExecution``
entry in the scheduler's own execution history.  Job definitions and
executions are two independent stores: deleting a job keeps its executions,
which is why the history serializer has to tolerate a missing job and fall
back to the execution's own fields.

This module is also the callback target of the scheduler itself.  When a job
fires the scheduler calls :meth:`AutomationHandlers.broadcast_automation_update`
and, during the run, :meth:`AutomationHandlers.broadcast_automation_progress`;
neither of those is reachable from ``_DISPATCH`` -- they are inbound from the
scheduler rather than outbound from a client frame.  Both push over every
connection in ``self._connections``, not just the one that created the job,
because any desktop window left open should see the automation panel move.

Routing into the rest of Encre: the CRUD handlers go straight to the scheduler
(they never open a session, they only call ``self._get_or_create_session()``
plus :meth:`encre.server.session_manager.SessionManager.touch` so the
connection's session entry stays warm against idle cleanup).  The agent-side
model is not resolved from the active session's config but from
``self._default_config.models`` and snapshotted per job, so a job keeps
running the model it was created with even after the user switches models.

Public API:

- :class:`AutomationHandlers`
"""

import json
import logging
import shutil
from typing import Any

from encre.config import ModelConfig
from encre.server.protocol import (
    ClientAutomationCancelJob,
    ClientAutomationCreateJob,
    ClientAutomationDeleteExecution,
    ClientAutomationDeleteJob,
    ClientAutomationGetHistory,
    ClientAutomationListJobs,
    ClientAutomationRenameExecution,
    ClientAutomationToggleJob,
    ClientAutomationUpdateJob,
)

logger = logging.getLogger("encre.transport.ws")


class AutomationHandlers:
    """Scheduled-job CRUD, execution-history serialisation and broadcast mixin.

    Mixed into :class:`encre.transport.ws.EncreWSHandler`; the class declares no
    state of its own and relies entirely on the collaborators installed by
    :class:`~encre.protocol.handlers.base.HandlerBase` and
    :class:`encre.server.app.EncreServer`:

    - ``_scheduler`` -- the iClaw scheduler holding job definitions, the
      execution history, and the sub-agent factory. ``None`` whenever the
      iClaw extra is not installed; every method tolerates that and either
      returns an empty snapshot or a ``no_scheduler`` error frame.
    - ``_default_config.models`` -- the model list jobs are bound to.
    - ``_adapter_manager`` / ``_connections`` / ``_tasks`` -- used by the two
      broadcast methods to fan a push out over every open socket.

    Workspace binding is explicit per job: the automation dialog lets the user
    pick a registered workspace and the choice rides the create/update frame
    into the job's ``agent_config["workspace"]``. Jobs created without a
    selection carry no workspace key and keep the default execution sandbox.

    Lifecycle: stateless. It is constructed once per server (a single
    :class:`EncreWSHandler` instance serves every socket), so nothing here is
    per-connection, and the broadcast helpers iterate ``self._connections``
    instead of the caller's socket.

    Invariants:

    - Every public snapshot builder is a pure read that must never raise; a
      settings panel is more useful with a partial list than with no
      response at all.
    - Job history entries are ordered newest-first and are keyed by
      ``"<job_id>_<execution_time>"`` because executions from different jobs
      are stored in one flat list.
    - Sub-agent conversations are never mirrored into the regular session
      store; they are only re-read from ``<data_dir>/sub_agents/<sid>/``.

    Args:
        None. Inherits ``__init__`` from
        :class:`~encre.protocol.handlers.base.HandlerBase` through the
        composed handler.
    """

    # ---- Snapshot builders -------------------------------------------------
    # Both the per-request ``*_list`` responses and the full-data snapshot
    # push reuse these, so a settings refresh and a scheduled-job broadcast
    # can never disagree about the same job.

    def _build_automation_jobs(self) -> list[dict[str, Any]]:
        """Serialise every scheduled job definition into a response payload.

        Reads ``self._scheduler.list_jobs()`` and flattens each job into the
        shape the desktop automation panel consumes. Shared by
        ``automation_jobs_list`` responses and by the full-data snapshot, so
        there is a single serialization site to keep in sync.

        Returns:
            One dict per job, in scheduler order. Empty when there is no
            scheduler.

        Note:
            Enum-valued fields are stringified through ``.name`` so the wire
            payload is protocol-stable regardless of the enum class used by
            the scheduler, and ``cron`` is rendered with
            ``to_expression()`` so an interval schedule does not arrive as a
            raw object. ``tag`` is read out of ``job.metadata`` rather than a
            dedicated field because the tag is only editor metadata.
        """
        if self._scheduler is None:
            return []
        jobs = self._scheduler.list_jobs()
        job_list: list[dict[str, Any]] = []
        for j in jobs:
            job_list.append({
                "id": j.id,
                "name": j.name,
                "prompt": j.prompt,
                "cron": j.cron.to_expression() if j.cron else "",
                "schedule_type": j.schedule_type.name,
                "state": j.state.name,
                "suspended": j.suspended,
                "created_at": j.created_at,
                "last_fired": j.last_fired,
                "last_result": j.last_result,
                "fail_count": j.fail_count,
                "max_failures": j.max_failures,
                "tag": j.metadata.get("tag", ""),
                "model_index": j.model_index,
                "workspace": (getattr(j, "_agent_config", None) or {}).get("workspace", ""),
                "permission_mode": (getattr(j, "_agent_config", None) or {}).get("permission_mode", ""),
                "push_gateways": list(j.push_gateways),
            })
        return job_list

    def _build_automation_agent_config(self, mc: ModelConfig, workspace: str = "", permission_mode: str = "") -> dict[str, Any]:
        """Snapshot one :class:`encre.config.ModelConfig` into a job agent config.

        Automation jobs run in a separate sub-agent process path that rebuilds
        its model client from a plain dict, so the live config object cannot be
        shared -- the credentials and endpoint have to be materialised at
        schedule time. Without that snapshot a job would silently follow the
        user's later model switch instead of the model it was created with.

        Args:
            mc: The model entry being bound to the job.
            workspace: Workspace path the user explicitly picked for this job
                in the automation dialog, or ``""`` when none was chosen.
            permission_mode: Permission preset the user picked for this job
                (``default``/``auto``/``bypass``/``custom`` -- the same values
                the safety engine dispatches on elsewhere), or ``""`` when the
                ambient agent permission should be kept.

        Returns:
            A dict with the transport fields the agent factory consumes
            (``backend_type``, ``api_key``, ``base_url``, ``model_id``,
            ``max_tokens``), plus ``workspace`` and ``permission_mode`` when
            one was selected.

        Note:
            ``workspace`` and ``permission_mode`` are set only when the caller
            passes a non-empty value. Leaving a key absent in the unselected
            case is load-bearing: a missing ``workspace`` key makes the
            scheduler agent keep its default sandbox and the original execution
            logic, whereas an empty string would be interpreted as a real path
            and pin the job to ``""``.
        """
        agent_config = {
            "backend_type": mc.backend_type,
            "api_key": mc.api_key,
            "base_url": mc.base_url,
            "model_id": mc.model_id,
            "max_tokens": mc.max_tokens,
        }
        # Pin the job to the user-selected workspace so each firing executes
        # inside that directory. The key stays absent when nothing was picked.
        if workspace:
            agent_config["workspace"] = workspace
        # Same permission system as every other mode: the safety engine reads
        # ``config.permission_mode`` to dispatch manual / auto / bypass /
        # custom approval for each tool call the automation makes.
        if permission_mode:
            agent_config["permission_mode"] = permission_mode
        return agent_config

    def _resolve_automation_model(self, model_index: int, workspace: str = "", permission_mode: str = "") -> tuple[int, dict[str, Any] | None]:
        """Resolve a requested model index to an *enabled* model, defensively.

        A job that snapshots a disabled (or deleted) model cannot run at all:
        the scheduler would try to build a backend from credentials the user has
        already turned off. Rather than failing the create/update request, this
        substitutes the first still-enabled model and logs which one was chosen,
        so the job keeps working and the user can see the substitution.

        Args:
            model_index: The index the client asked for, into
                ``self._default_config.models``.
            workspace: Workspace path the user explicitly picked for this job,
                forwarded into the agent config snapshot; ``""`` keeps the
                default execution sandbox.
            permission_mode: Permission preset the user picked for this job,
                forwarded into the agent config snapshot; ``""`` keeps the
                ambient agent permission.

        Returns:
            ``(effective_index, agent_config)`` where ``effective_index`` is the
            index actually stored on the job and ``agent_config`` is the dict
            from :meth:`_build_automation_agent_config`. When no enabled model
            exists at all, returns ``(model_index, None)`` so the scheduler falls
            back to the currently active model at fire time.

        Note:
            Enabled defaults to ``True`` (``getattr(..., "enabled", True)``)
            because older saved configs predate the field entirely, and
            treating an absent flag as disabled would make every legacy model
            unreachable.
        """
        models = getattr(self._default_config, "models", None)
        if models:
            if 0 <= model_index < len(models) and getattr(models[model_index], "enabled", True):
                return model_index, self._build_automation_agent_config(models[model_index], workspace, permission_mode)
            for i, mc in enumerate(models):
                if getattr(mc, "enabled", True):
                    logger.info(
                        "[automation] requested model_index=%d disabled/unavailable, "
                        "falling back to enabled model index=%d", model_index, i,
                    )
                    return i, self._build_automation_agent_config(mc, workspace, permission_mode)
        return model_index, None

    def _load_sub_agent_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        """Re-read the conversation produced by one automation sub-agent run.

        Each automation firing executes as a sub-agent whose turns are
        persisted by the loop under ``<data_dir>/sub_agents/<session_id>/`` as
        one file per turn -- the same on-disk layout a regular session uses,
        just in a different root. There is deliberately no live
        :class:`~encre.server.session_manager.SessionInfo` for these, so the
        only way to show the automation's output in the UI is to reconstruct
        the message list from those files with
        :func:`encre.session.EncreSession.load_from_dir`.

        Args:
            session_id: The sub-agent session id recorded on the execution or
                job, or an empty string when the run never got one.

        Returns:
            The flat list of message dicts for that run, ``dict`` copies so
            callers can serialize them without touching the loader's objects,
            or ``None`` when there is nothing to load.

        Note:
            ``None`` is returned rather than raising for every failure mode --
            unknown id, missing directory, unreadable files -- because this is
            called while serialising a list that is also used in a broadcast,
            and one corrupt sub-agent directory must not take down the whole
            history payload. Callers treat ``None`` as "no messages to show".
        """
        if not session_id:
            return None
        try:
            from encre.config import EncreConfig, get_data_dir
            from encre.session import EncreSession
            sess_dir = get_data_dir() / "sub_agents" / session_id
            if not sess_dir.is_dir():
                return None
            cfg = EncreConfig()
            session = EncreSession.load_from_dir(str(sess_dir), config=cfg)
            return [dict(m) for m in session.messages]
        except Exception:
            logger.warning("[automation] failed to load sub-agent session %s", session_id, exc_info=True)
            return None

    def _build_automation_history(self) -> list[dict[str, Any]]:
        """Serialise the global execution history for the UI.

        Builds one entry per past firing across *all* jobs -- not per job --
        because the desktop history view is a single newest-first timeline.
        Jobs are listed with ``include_finished=True`` and indexed by id so
        each execution can be decorated with its job's name, prompt and tag
        when the job still exists.

        Returns:
            Entries sorted newest-first, each carrying a stable ``id`` of the
            form ``"<job_id>_<execution.time>"``, the execution's state and
            fail count, a ``last_result`` truncated to 500 characters so a
            runaway run cannot bloat the payload, and a ``messages`` list
            holding the sub-agent conversation when it could be re-read.

        Note:
            Executions whose job has since been deleted degrade gracefully:
            the name falls back to ``"Deleted automation"`` and the prompt and
            tag become empty strings. The keys stay present so the frontend can
            rely on a fixed payload shape instead of guarding every field.
        """
        if self._scheduler is None:
            return []

        jobs = self._scheduler.list_jobs(include_finished=True)
        job_map = {job.id: job for job in jobs}
        history: list[dict[str, Any]] = []
        for execution in self._scheduler.get_execution_history():
            job = job_map.get(execution.job_id)
            entry: dict[str, Any] = {
                "id": f"{execution.job_id}_{execution.time}",
                "job_id": execution.job_id,
                "name": execution.name or (job.name if job else "Deleted automation"),
                "prompt": job.prompt if job else "",
                "tag": job.metadata.get("tag", "") if job else "",
                "time": execution.time,
                "state": execution.state,
                "last_result": execution.result[:500] if execution.result else "",
                "fail_count": execution.fail_count,
                "messages": [],
            }
            if execution.state == "FAILED":
                try:
                    from encre.errors import classify_error_code
                    entry["error_code"] = classify_error_code(execution.result or "").value
                except Exception:
                    entry["error_code"] = "AUTOMATION_EXECUTION_FAILED"
            if execution.session_id:
                entry["session_id"] = execution.session_id
                messages = self._load_sub_agent_messages(execution.session_id)
                if messages:
                    try:
                        json.dumps(messages, ensure_ascii=False)
                    except (TypeError, ValueError) as exc:
                        logger.warning(
                            "[automation] messages not serializable for session %s: %s",
                            execution.session_id,
                            exc,
                        )
                    else:
                        entry["messages"] = messages
            history.append(entry)
        history.sort(key=lambda entry: entry["time"] or 0, reverse=True)
        return history

    # ---- Broadcasts (scheduler callbacks) ------------------------------
    # Neither method in this group is reachable from _DISPATCH: the scheduler
    # calls them itself when a job fires or streams, so they fan out over
    # self._connections instead of replying to the requesting socket.

    def broadcast_automation_update(self, job: Any = None) -> None:
        """Push the refreshed history (and a finished result) to every client.

        Invoked by the scheduler as a completion callback rather than by any
        client request, so every desktop window with the automation panel open
        converges on the same state without polling. It always ships the full
        history, which is what lets the panel treat this one frame as its
        single refresh event, and ships ``result`` only when the job that just
        fired has something worth displaying.

        Args:
            job: The job that finished, or ``None`` when the update came from a
                structural change (job created, deleted, or an execution
                renamed) with no fresh run. ``None`` skips the ``result`` block
                but still broadcasts the refreshed history.

        Returns:
            None.

        Note:
            Synchronous by contract -- the scheduler callback expects it to
            return immediately -- so the per-connection writes are spawned with
            ``ensure_future`` and tracked in ``self._tasks`` instead of being
            awaited. Sockets that fail to accept the spawn are collected and
            pruned after the loop, so a dead connection is never written to
            twice within one broadcast. Result text is truncated at 2000
            characters so a single verbose run cannot stall the socket.
        """
        history = self._build_automation_history()

        # Result data for frontend display -- pull messages from the
        # sub-agent session, not from JobExecution.
        result_data: dict[str, Any] | None = None
        if job and job.last_result:
            messages: list[dict[str, Any]] | None = None
            if getattr(job, "session_id", None):
                messages = self._load_sub_agent_messages(job.session_id)
                if messages:
                    try:
                        json.dumps(messages, ensure_ascii=False)
                    except (TypeError, ValueError) as e:
                        logger.warning("[automation] result messages not serializable for session %s: %s", job.session_id, e)
                        messages = None
            execution_failed = job.last_result.startswith("Error:")
            result_data = {
                "action": "failed" if execution_failed else "completed",
                "id": job.id,
                "job_id": job.id,
                "name": job.name,
                "prompt": job.prompt,
                "result": job.last_result[:2000],
                "messages": messages or [],
                "state": "FAILED" if execution_failed else job.state.name,
            }
            if execution_failed:
                # The raw exception text is intentionally not shown in the UI,
                # but a stable classified code (rate_limit / network_timeout /
                # ...) is attached so the panel can group failures by cause
                # instead of only showing a generic placeholder.
                try:
                    from encre.errors import classify_error_code
                    result_data["error_code"] = classify_error_code(job.last_result or "").value
                except Exception:
                    result_data["error_code"] = "AUTOMATION_EXECUTION_FAILED"
            if getattr(job, "session_id", None):
                result_data["session_id"] = job.session_id

        # ---- Push the result through the configured gateways -----------
        if job and job.last_result and hasattr(job, "push_gateways") and job.push_gateways and self._adapter_manager:
            # Delivery goes through the DeliveryRouter so truncation, fan-out
            # and audit logging are applied consistently with the rest of the
            # gateway layer. Each push_gateways entry is either
            # "platform:chat_id" (an explicit chat target) or a bare adapter
            # id, which the router resolves to that adapter's default push
            # destination. Spawned as a task: delivery can block on network
            # I/O and must not hold up the socket broadcast below.
            push_text = f"馃 {job.name}\n\n{job.last_result}"
            try:
                router = self._adapter_manager.delivery
                self._spawn_background(
                    router.deliver(push_text, list(job.push_gateways))
                )
                logger.debug("[automation] routed push to %s via DeliveryRouter", job.push_gateways)
            except Exception as exc:
                logger.warning("[automation] DeliveryRouter push failed: %s", exc)

        logger.debug("[broadcast_automation_update] job=%s state=%s connections=%s history_len=%s",
                    getattr(job, 'id', None) if job else None,
                    getattr(job, 'state', None) if job else None,
                    len(self._connections), len(history))
        self._fanout("automation_job_update",
                     on_error=self._drop_connection,
                     history=history, result=result_data)

    async def broadcast_automation_progress(self, job: Any = None, event_type: str = "", event_data: dict[str, Any] | None = None) -> None:
        """Relay one live streaming event from a running automation job.

        Called -- and awaited -- by the scheduler's progress callback from
        inside ``agent.run()``, so each event reaches the clients in the order
        the loop produced it. This mirrors the main chat's sub-agent streaming
        path: instead of only reporting a final result, the automation panel
        can render tool calls, thoughts and deltas as they happen.

        Args:
            job: The job currently firing, or ``None``. ``job.id`` identifies
                the stream on the client, so a missing job degrades to an empty
                id rather than raising.
            event_type: The agent-event kind being forwarded, e.g. a tool call
                or a text delta.
            event_data: The event payload, normalized to ``{}`` when empty so
                the client always receives an object it can iterate.

        Returns:
            None.

        Note:
            Unlike :meth:`broadcast_automation_update` this one awaits each
            ``_send`` inline. Order matters here: the client renders a partial
            transcript, and an out-of-order event would leave the panel in an
            inconsistent state. Sockets that fail are collected and pruned
            after the loop rather than mid-iteration, since removing from the
            registry while iterating it would skip entries.
        """
        if not event_data:
            event_data = {}
        states = [s for s in self._connections_state.states() if s.ws is not None]
        failed: list[Any] = []
        for state in states:
            try:
                # Awaits inline to preserve stream order, and addresses this
                # target's own connection state rather than the ambient one.
                await self._send(state.ws, "automation_stream_event",
                    job_id=job.id if job else "",
                    event_type=event_type,
                    event_data=event_data,
                )
            except Exception:
                failed.append(state.ws)
        for ws in failed:
            self._drop_connection(ws)

    # ---- Client message handlers -----------------------------------------
    # Each entry in this group is the value of an automation key in the
    # composed handler's _DISPATCH table. They follow one shape: resolve or
    # create the connection's session, touch it so idle cleanup keeps the
    # entry alive, bail out with a "no_scheduler" error frame when iClaw is
    # not installed, then round-trip through self._scheduler.

    async def _h_automation_list_jobs(self, ws: Any, msg: ClientAutomationListJobs) -> None:
        """Reply with the current job definitions.

        Args:
            ws: The connection to answer.
            msg: The parsed ``ClientAutomationListJobs`` frame; it carries no
                payload beyond the connection binding.

        Returns:
            None. Sends ``automation_jobs_list`` with the flattened job list.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        await self._send(ws, "automation_jobs_list", jobs=self._build_automation_jobs())

    async def _h_automation_create_job(self, ws: Any, msg: ClientAutomationCreateJob) -> None:
        """Schedule a new automation job.

        Resolves the requested model to an enabled one before scheduling, so
        the job never binds to credentials the user has disabled, then hands
        the definition to the scheduler.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationCreateJob`` with ``name``, ``prompt``,
                ``cron``, ``tag``, ``model_index``, ``workspace``,
                ``permission_mode`` and ``push_gateways``. ``workspace`` pins
                the job to a registered workspace directory when non-empty; an
                empty value keeps the default execution sandbox.
                ``permission_mode`` selects the same permission preset the
                safety engine dispatches on elsewhere; an empty value keeps the
                ambient agent permission.

        Returns:
            None. Sends ``automation_job_created`` on success.

        Raises:
            Never. A missing scheduler yields an ``error`` frame with code
            ``no_scheduler``; any scheduler exception yields code
            ``create_job_error`` with the original message.

        Note:
            ``cron`` is stored as an empty string rather than ``None`` when the
            client sends nothing, and ``push_gateways`` is materialised with
            ``list()`` so the scheduler holds its own copy of a mutable client
            field.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "error", message="Scheduler not available", code="no_scheduler")
            return
        try:
            workspace = (msg.workspace or "").strip()
            permission_mode = (msg.permission_mode or "").strip()
            effective_index, agent_config = self._resolve_automation_model(msg.model_index, workspace, permission_mode)
            job_id = self._scheduler.schedule(
                name=msg.name,
                prompt=msg.prompt,
                cron=msg.cron if msg.cron else "",
                metadata={"tag": msg.tag} if msg.tag else {},
                agent_config=agent_config,
                model_index=effective_index,
                push_gateways=list(msg.push_gateways),
            )
            await self._send(ws, "automation_job_created",
                job_id=job_id, name=msg.name)
        except Exception as e:
            await self._send(ws, "error",
                message=f"Failed to create job: {e}", code="create_job_error")

    async def _h_automation_cancel_job(self, ws: Any, msg: ClientAutomationCancelJob) -> None:
        """Cancel a job so it no longer fires on its schedule.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationCancelJob`` naming the job id.

        Returns:
            None. Sends ``automation_job_cancelled`` or an ``error`` frame
            with code ``job_not_found`` / ``no_scheduler``.

        Note:
            Cancellation suppresses future firings but does not delete the job
            definition, so the scheduler still owns it and the execution
            history keeps referencing its id.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "error", message="Scheduler not available", code="no_scheduler")
            return
        ok = self._scheduler.cancel(msg.job_id)
        if ok:
            await self._send(ws, "automation_job_cancelled", job_id=msg.job_id)
        else:
            await self._send(ws, "error",
                message="Job not found", code="job_not_found")

    async def _h_automation_toggle_job(self, ws: Any, msg: ClientAutomationToggleJob) -> None:
        """Enable or disable a job without deleting it.

        A suspended job keeps its schedule and history but never fires, which
        is how the UI's pause toggle is implemented without losing the
        definition or forcing a delete-then-recreate.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationToggleJob`` naming the job id.

        Returns:
            None. Sends ``automation_job_toggled`` carrying the new running
            state, or an ``error`` frame with code ``job_not_found`` /
            ``no_scheduler``.

        Note:
            The scheduler reports ``None`` for an unknown id, so ``None``
            doubles as the not-found sentinel rather than raising.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "error", message="Scheduler not available", code="no_scheduler")
            return
        running = self._scheduler.toggle_job(msg.job_id)
        if running is not None:
            await self._send(ws, "automation_job_toggled", job_id=msg.job_id, running=running)
        else:
            await self._send(ws, "error",
                message="Job not found", code="job_not_found")

    async def _h_automation_update_job(self, ws: Any, msg: ClientAutomationUpdateJob) -> None:
        """Rewrite an existing job definition.

        Re-runs the same model resolution as create, so editing the prompt or
        schedule cannot accidentally re-bind the job to a disabled model.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationUpdateJob`` with the fields being replaced.
                ``workspace`` re-pins the job's execution directory when
                non-empty; an empty value clears the binding back to the
                default execution sandbox. ``permission_mode`` re-selects the
                job's permission preset; an empty value keeps the ambient
                agent permission.

        Returns:
            None. Sends ``automation_job_updated`` or an ``error`` frame with
            code ``job_not_found`` / ``no_scheduler``.

        Note:
            Unlike create, the update path assumes ``_resolve_automation_model``
            returns a usable config; when no model is enabled it stores
            ``agent_config=None`` and the scheduler falls back to the active
            model at fire time.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "error", message="Scheduler not available", code="no_scheduler")
            return
        workspace = (msg.workspace or "").strip()
        permission_mode = (msg.permission_mode or "").strip()
        effective_index, agent_config = self._resolve_automation_model(msg.model_index, workspace, permission_mode)
        ok = self._scheduler.update_job(
            msg.job_id,
            name=msg.name,
            prompt=msg.prompt,
            cron=msg.cron,
            tag=msg.tag,
            model_index=effective_index,
            agent_config=agent_config,
            push_gateways=list(msg.push_gateways),
        )
        if ok:
            await self._send(ws, "automation_job_updated", job_id=msg.job_id)
        else:
            await self._send(ws, "error",
                message="Job not found", code="job_not_found")

    async def _h_automation_delete_job(self, ws: Any, msg: ClientAutomationDeleteJob) -> None:
        """Delete a job definition while retaining its execution history.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationDeleteJob`` naming the job id.

        Returns:
            None. Sends ``automation_job_deleted``, or an ``error`` frame with
            code ``job_not_found`` / ``no_scheduler``.

        Note:
            The scheduler stores definitions and executions independently, so
            a delete leaves every past firing visible in the history view. The
            retained history is broadcast immediately so all windows drop the
            job from the list at the same moment.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "error", message="Scheduler not available", code="no_scheduler")
            return
        ok = self._scheduler.delete_job(msg.job_id)
        if ok:
            # Execution history is a separate store from job definitions, so
            # the deleted job's past firings remain visible. Broadcast the
            # refreshed list plus the retained history in one frame.
            self.broadcast_automation_update()
            await self._send(ws, "automation_job_deleted", job_id=msg.job_id)
        else:
            await self._send(ws, "error",
                message="Job not found", code="job_not_found")

    async def _h_automation_delete_execution(self, ws: Any, msg: ClientAutomationDeleteExecution) -> None:
        """Remove one past execution, including its stored sub-agent transcript.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationDeleteExecution`` naming the history entry.

        Returns:
            None. Sends ``automation_execution_deleted``, or an ``error`` frame
            with code ``execution_not_found`` / ``no_scheduler``.

        Note:
            Deleting the record is not enough: the sub-agent conversation files
            would stay on disk and be re-read on the next snapshot. The
            scheduler returns the sub-agent session id so this handler can
            remove ``<data_dir>/sub_agents/<sid>/`` as well. Both stores are
            then broadcast so the panel refreshes in one frame.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "error", message="Scheduler not available", code="no_scheduler")
            return
        sid = self._scheduler.delete_job_execution(msg.entry_id)
        if sid:
            # The execution record alone is not enough: the sub-agent's turn
            # files would survive on disk and be re-read into the next history
            # snapshot, resurrecting a deleted entry.
            from encre.config import get_data_dir as _get_data_dir
            sub_agent_dir = _get_data_dir() / "sub_agents" / sid
            if sub_agent_dir.is_dir():
                shutil.rmtree(str(sub_agent_dir))
            self.broadcast_automation_update()
            await self._send(ws, "automation_execution_deleted", entry_id=msg.entry_id)
        else:
            await self._send(ws, "error",
                message="Execution not found", code="execution_not_found")

    async def _h_automation_rename_execution(self, ws: Any, msg: ClientAutomationRenameExecution) -> None:
        """Rename a past execution entry in the history view.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationRenameExecution`` with the entry id and the
                replacement display name.

        Returns:
            None. Sends ``automation_execution_renamed``, or an ``error`` frame
            with code ``invalid_request`` when the trimmed name is empty,
            ``execution_not_found`` for an unknown entry, or ``no_scheduler``.

        Note:
            Only the execution's own ``name`` is rewritten. Names are taken from
            the job definition for executions whose job still exists, so a
            manual rename is the only per-entry label override and it is stored
            on the execution rather than on the job.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "error", message="Scheduler not available", code="no_scheduler")
            return
        new_name = (msg.new_name or "").strip()
        if not new_name:
            await self._send(ws, "error", message="Name cannot be empty", code="invalid_request")
            return
        ok = self._scheduler.rename_job_execution(msg.entry_id, new_name)
        if ok:
            self.broadcast_automation_update()
            await self._send(ws, "automation_execution_renamed", entry_id=msg.entry_id, new_name=new_name)
        else:
            await self._send(ws, "error",
                message="Execution not found", code="execution_not_found")

    async def _h_automation_get_history(self, ws: Any, msg: ClientAutomationGetHistory) -> None:
        """Reply with the newest-first execution history timeline.

        Args:
            ws: The connection to answer.
            msg: ``ClientAutomationGetHistory``; it selects no particular job,
                so the response is the whole timeline across all jobs.

        Returns:
            None. Sends ``automation_job_history``.

        Note:
            With no scheduler installed this still sends the frame with an empty
            ``history`` list rather than an ``error``. A missing optional extra
            is a legitimate empty state, not a failure, and the panel is written
            to render either shape.
        """
        info = self._get_or_create_session()
        self._manager.touch(info.session_id)
        if self._scheduler is None:
            await self._send(ws, "automation_job_history", history=[])
            return
        await self._send(
            ws,
            "automation_job_history",
            history=self._build_automation_history(),
        )
