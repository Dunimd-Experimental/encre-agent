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

"""Unified cron tool for the Encre Agent scheduler system.

This module consolidates the cron lifecycle — create, list, delete — into a
single ``cron`` tool that uses an ``action`` parameter to select the operation.

Previously the cron lifecycle was split across three separate EA packages
(``ea-tool-cron-create``, ``ea-tool-cron-list``, ``ea-tool-cron-delete``).
The merged tool ensures the scheduler is atomic: install or uninstall the
whole thing, not individual operations.
"""

import json
import time
from typing import Any

from encre.tools.base import build_tool
from encre.tools.runtime import get_scheduler, set_scheduler


# ── Helper: ensure scheduler ────────────────────────────────────────────

def _ensure_scheduler() -> Any:
    """Return the process-wide scheduler, bootstrapping the durable one on demand."""
    sched = get_scheduler()
    if sched is None:
        from encre.scheduler import EncreScheduler

        sched = EncreScheduler()
        set_scheduler(sched)
    return sched


# ── Action: create ───────────────────────────────────────────────────────

async def _cron_create(**kwargs: Any) -> str:
    """Schedule a prompt to run automatically on a recurring cron schedule."""
    cron_expr = kwargs.get("cron", "")
    prompt_text = kwargs.get("prompt", "")
    name = kwargs.get("name", "Unnamed job")

    if not cron_expr or not prompt_text:
        return "Error: both 'cron' and 'prompt' are required."

    # Validate cron expression
    from encre.scheduler import CronSchedule
    try:
        CronSchedule.parse(cron_expr)
    except ValueError as e:
        return f"Error: invalid cron expression '{cron_expr}' -- {e}"

    job_id = _ensure_scheduler().schedule(
        name=name,
        prompt=prompt_text,
        cron=cron_expr,
    )
    return json.dumps({
        "status": "scheduled",
        "job_id": job_id,
        "name": name,
        "cron": cron_expr,
        "prompt_preview": prompt_text[:200],
    }, ensure_ascii=False, indent=2)


# ── Action: list ─────────────────────────────────────────────────────────

async def _cron_list(**_kwargs: Any) -> str:
    """List all currently scheduled cron jobs."""
    jobs = _ensure_scheduler().list_jobs()
    if not jobs:
        return json.dumps({"jobs": [], "message": "No scheduled jobs."}, ensure_ascii=False)

    now = time.time()
    result = []
    for job in jobs:
        entry = {
            "id": job.id,
            "name": job.name,
            "state": job.state.name,
            "schedule_type": job.schedule_type.name,
            "prompt_preview": job.prompt[:100] + "..." if len(job.prompt) > 100 else job.prompt,
            "created_at": job.created_at,
        }
        if job.cron:
            entry["cron"] = job.cron.to_expression()
            next_fire = job.cron.next_fire(now) if job.state.name == "PENDING" else None
            if next_fire:
                entry["next_fire"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_fire))
        elif job.fire_at:
            entry["fire_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.fire_at))
        if job.last_fired:
            entry["last_fired"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.last_fired))
        if job.fail_count > 0:
            entry["fail_count"] = job.fail_count
        result.append(entry)

    return json.dumps({"jobs": result, "total": len(result)}, ensure_ascii=False, indent=2)


# ── Action: delete ───────────────────────────────────────────────────────

async def _cron_delete(**kwargs: Any) -> str:
    """Cancel or delete a previously scheduled cron job by its ID."""
    job_id = kwargs.get("job_id", "")
    if not job_id:
        return "Error: job_id is required."

    cancelled = _ensure_scheduler().cancel(job_id)
    if cancelled:
        return json.dumps({"status": "cancelled", "job_id": job_id}, ensure_ascii=False)
    return json.dumps({"status": "not_found", "job_id": job_id,
                       "message": "No such job or already cancelled"}, ensure_ascii=False)


# ── Unified dispatcher ───────────────────────────────────────────────────

async def _cron_execute(**kwargs: Any) -> str:
    """Dispatch the cron action and return the result."""
    action = kwargs.get("action", "")
    if not action:
        return "Error: 'action' is required. Valid actions: create, list, delete."

    dispatch = {
        "create": _cron_create,
        "list": _cron_list,
        "delete": _cron_delete,
    }

    handler = dispatch.get(action)
    if handler is None:
        return f"Error: unknown action '{action}'. Valid actions: {', '.join(dispatch.keys())}."

    return await handler(**kwargs)


# ── Tool definition ──────────────────────────────────────────────────────

cron_tool = build_tool(
    name="cron",
    description=(
        "Manage scheduled cron jobs through the unified cron tool.\n\n"
        "Use the 'action' parameter to select the operation:\n"
        "  - create: schedule a prompt to run on a recurring cron schedule\n"
        "  - list: enumerate all scheduled jobs with their IDs and next fire times\n"
        "  - delete: cancel a scheduled job by its ID\n\n"
        "WHEN to use: reminders, periodic reports, polling, or any task that must "
        "fire unattended at fixed times.\n"
        "WHEN NOT to use: for one-shot delayed tasks if a deferred-task tool is "
        "available, for sub-minute scheduling, or for jobs requiring prior "
        "conversation context.\n"
        "TIPS: use a standard 5-field cron expression in local time, e.g. "
        "'0 9 * * *' (daily 9am) or '*/5 * * * *' (every 5 min).\n"
        "PITFALLS: the agent has no prior context at fire time — include all "
        "needed details in the prompt."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "delete"],
                "description": "The cron operation to perform.",
            },
            # Create params
            "cron": {
                "type": "string",
                "description": "[create] Standard 5-field cron expression in local time (minute hour day-of-month month day-of-week). Examples: '0 9 * * *' (daily 9am), '*/5 * * * *' (every 5 min).",
            },
            "prompt": {
                "type": "string",
                "description": "[create] Self-contained prompt text to execute at each fire time; no prior conversation context is available when the job runs.",
            },
            "name": {
                "type": "string",
                "description": "[create] Human-readable label for the scheduled job; defaults to 'Unnamed job'.",
            },
            # Delete params
            "job_id": {
                "type": "string",
                "description": "[delete] Unique identifier of the scheduled job to cancel; obtain it via list action.",
            },
        },
        "required": ["action"],
    },
    execute=_cron_execute,
    intents=["system"],
    category="task",
    semantic_type="write",
    is_destructive=True,
    is_readonly=lambda args: args.get("action") == "list",
    is_concurrency_safe=lambda args: args.get("action") == "list",
)

# Backward-compat: keep ``.set_scheduler()`` callable on the tool object.
cron_tool.set_scheduler = set_scheduler
