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

"""Training-job management for the ARK trainer CLI.

Two concerns live here because they are coupled by the ``HEARTBEAT.md``
workflow:

1. **Job queries** (:func:`get_job_status`, :func:`get_output_model_id`)
   talk to the ARK SDK to fetch live status and, on completion, the
   custom-model ID produced by the run.
2. **Heartbeat registration** (:func:`register_heartbeat_command`) writes
   a Markdown table row that a human or an AI agent can inspect to keep
   track of all in-flight training jobs across experiments.

The heartbeat block is a load-bearing piece of text: AI agents that
take over the job later scan it to decide whether they may resume,
retry, or must ask the user for clarification. The reminder constants
below therefore mirror the wording in the skill's ``SKILL.md`` template
verbatim ? if you edit one, edit the other.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from ark_sdk.resources.model_customization_job import ModelCustomizationJob
from loguru import logger


# =========================================================================
# HEARTBEAT.md scaffolding
# =========================================================================

# Six items are load-bearing for downstream AI agents; they are also the
# "must-read" list documented in the byted-ark-trainer SKILL.md.
HEARTBEAT_SYSTEM_REMINDER = """# byted-ark-trainer Heartbeat Monitoring Task List

> ?? [SYSTEM REMINDER ? MUST READ WHEN AN AI AGENT TAKES OVER A TRAINING JOB]
> 1. **Load the byted-ark-trainer skill first**: keep the byted-ark-trainer skill always in context; if it is not present, actively load it (read that skill's SKILL.md).
> 2. **Read the experiment directory before taking over**: before acting on any job in the table below, open the ``EXPERIMENT.md`` under that job's absolute experiment directory, understand the plan, the confirmed configuration, and the follow-up flow. **Do not execute any action until ``EXPERIMENT.md`` has been fully read**.
> 3. **Check each task's status individually**: for each ARK training job in the table, run ``byted-ark-trainer-helper job status --job-id <task-id>`` to fetch the latest state, then write the result back into the "Latest Status" column.
> 4. **No second confirmation needed when there is a documented follow-up**: if a task's status becomes Completed, immediately execute the next step recorded in that task's ``EXPERIMENT.md`` follow-up section (e.g. after RFT completes, submit GRPO; after training completes, evaluate on the test set). Only notify the user with the result afterwards, and update ``EXPERIMENT.md`` incrementally.
> 5. **Failed tasks must be reported, never silently removed**: if status is Failed/Terminated, immediately show the user the full error message and failure reason, and ask whether to retry or adjust configuration. **Only remove the row from the table after the user explicitly confirms**; until then keep the row so the incident remains traceable.
> 6. **Never fabricate context**: if the experiment directory or ``EXPERIMENT.md`` is missing and the task's intent is unclear, do not guess ? ask the user first.
"""

HEARTBEAT_TABLE_HEADER = (
    "| Job ID | Job Type | Submit Time | Latest Status | Job URL | Absolute Experiment Directory |\n"
    "|--------|----------|-------------|---------------|---------|-------------------------------|\n"
)

# Order-sensitive tokens used to verify the system reminder block is intact.
# Detection is by ordered substring match, so if the wording of any of the
# six items above changes, this list must be updated in lock-step or the
# auto-repair logic in :func:`register_heartbeat_command` will treat an
# already-correct file as corrupted and re-emit the reminder block.
_REMINDER_TOKENS = [
    "SYSTEM REMINDER ? MUST READ WHEN AN AI AGENT TAKES OVER A TRAINING JOB",
    "Load the byted-ark-trainer skill first",
    "Read the experiment directory before taking over",
    "Check each task's status individually",
    "No second confirmation needed when there is a documented follow-up",
    "Failed tasks must be reported, never silently removed",
    "Never fabricate context",
]


def _reminder_block_intact(text: str) -> bool:
    """Return True iff all six reminder tokens appear in the expected order.

    We check for strict ordering (each token's position must be greater
    than the previous one's) rather than only presence ? this catches
    hand-edited files where the items have been reordered but all tokens
    are technically still present.
    """
    last_idx = -1
    for tok in _REMINDER_TOKENS:
        idx = text.find(tok)
        if idx == -1 or idx < last_idx:
            return False
        last_idx = idx
    return True


def _append_task_row(heartbeat_text: str, row: str, job_id: str) -> tuple[str, bool]:
    """Append a task row to the heartbeat table, idempotently.

    Args:
        heartbeat_text: Current full content of the heartbeat file.
        row: Fully-formatted Markdown row to append.
        job_id: Identifier used as the deduplication key.

    Returns:
        ``(new_text, added)`` where ``added`` is False when the job ID
        was already present and the row was not written.
    """
    if job_id in heartbeat_text:
        return heartbeat_text, False
    # Make sure the appended row lands on its own line.
    if not heartbeat_text.endswith("\n"):
        heartbeat_text += "\n"
    # If the table header is missing (e.g. the file was hand-created
    # without it), re-inject the header before appending.
    if "| Job ID |" not in heartbeat_text:
        heartbeat_text += "\n" + HEARTBEAT_TABLE_HEADER
    heartbeat_text += row + "\n"
    return heartbeat_text, True


def register_heartbeat_command(args) -> None:
    """Register a training job as a row in ``HEARTBEAT.md``.

    Three input states are handled:

    * File does not exist -> create it with the full template
      (reminder block + table header + new row).
    * File exists but reminder block is missing or reordered ->
      re-emit the reminder block at the top, then append the row.
    * File exists and the ``--job-id`` is already registered -> no-op
      (idempotent).

    The ``--heartbeat-file`` argument is constrained to live under
    ``~/.openclaw`` because that is the OpenClaw workspace root that the
    OpenClaw runtime actually watches for heartbeat triggers; writing
    the file anywhere else would silently disable the whole flow.

    Args:
        args: ``argparse.Namespace`` populated by the ``register-heartbeat``
            sub-parser (``job_id``, ``job_type``, ``job_url``,
            ``exp_dir``, ``submit_time``, ``status``, ``heartbeat_file``).

    Raises:
        SystemExit: If the heartbeat file path is outside the OpenClaw
            workspace root.
    """
    heartbeat_path = Path(args.heartbeat_file).expanduser().resolve()

    # Enforce that the file lives under ~/.openclaw so the OpenClaw
    # heartbeat scanner can actually pick it up.
    openclaw_root = Path("~/.openclaw").expanduser().resolve()
    try:
        heartbeat_path.relative_to(openclaw_root)
    except ValueError:
        raise SystemExit(
            f"REFUSED: --heartbeat-file must live under {openclaw_root}."
            f"Given path: {heartbeat_path}."
            f"Put HEARTBEAT.md inside an OpenClaw workspace, e.g. {openclaw_root}/workspace/<project>/HEARTBEAT.md, then re-login."
        )

    submit_time = args.submit_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    status = args.status or "Running"
    exp_dir = str(Path(args.exp_dir).expanduser().resolve())

    row = (
        f"| {args.job_id} | {args.job_type} | {submit_time} | {status} "
        f"| {args.job_url} | {exp_dir} |"
    )

    print(f"\n=== Register heartbeat task ({args.job_id}) ===")
    print(f"Target HEARTBEAT.md: {heartbeat_path}")

    if not heartbeat_path.exists():
        # New file: emit the complete template in one atomic write.
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        content = HEARTBEAT_SYSTEM_REMINDER + "\n" + HEARTBEAT_TABLE_HEADER + row + "\n"
        heartbeat_path.write_text(content, encoding="utf-8")
        print("? HEARTBEAT.md did not exist; created with the full template (reminder block + new task row)")
        print(f"? Appended task {args.job_id} to the heartbeat summary table")
        return

    # Existing file: verify and, if needed, restore the reminder block.
    text = heartbeat_path.read_text(encoding="utf-8")
    reminder_ok = _reminder_block_intact(text)

    if not reminder_ok:
        # Prepend the full reminder block; the pre-existing content is
        # preserved below so any hand-edited notes are not lost.
        text = HEARTBEAT_SYSTEM_REMINDER + "\n" + text
        print("? Reminder block at the top was missing or incomplete; full reminder block prepended")
    else:
        print("? Reminder block at the top is intact; left unchanged")

    new_text, added = _append_task_row(text, row, args.job_id)
    heartbeat_path.write_text(new_text, encoding="utf-8")

    if added:
        print(f"? Appended task {args.job_id} to the heartbeat summary table")
    else:
        print(f"? Task {args.job_id} was already registered; skipped append (idempotent)")


def get_job_status(job_id: str) -> ModelCustomizationJob:
    """Fetch the current ``ModelCustomizationJob`` for ``job_id``.

    Args:
        job_id: ARK job identifier (e.g. ``mcj-20260225163459-thh2g``).

    Returns:
        The ``ModelCustomizationJob`` object returned by the SDK. The
        caller is expected to read status fields off ``.phase``,
        ``.status.*`` and ``.name`` / ``.description`` directly.
    """
    return ModelCustomizationJob.get(job_id)


def get_output_model_id(job_id: str) -> str:
    """Return the custom-model ID produced by a completed training job.

    The SDK exposes the list of produced artefacts under a name-mangled
    private attribute (``_ModelCustomizationJob__outputs``) and each
    artefact may carry the identifier under any of several case-variations.
    We iterate ``reversed(outputs)`` because later checkpoints are the
    ones that matter ? an ARK job can emit intermediate saves.

    Args:
        job_id: ARK job identifier.

    Returns:
        The custom-model ID string.

    Raises:
        ValueError: If the job has not completed, or if no output model
            was found for a completed job.
    """
    job = ModelCustomizationJob.get(job_id)
    finetune_status = job.phase
    logger.info(f"Task {job_id} finetune status is {finetune_status}")
    if finetune_status == "Completed":
        # Refresh so the outputs list is populated; the initial
        # ``.get()`` does not always fetch the artefact references.
        job.refresh()
        # Read the SDK's name-mangled private attribute.
        if hasattr(job, "_ModelCustomizationJob__outputs"):
            outputs = getattr(job, "_ModelCustomizationJob__outputs")
            logger.info(f"Outputs found: {outputs}")
            if outputs and len(outputs) > 0:
                # Iterate in reverse so the newest checkpoint wins.
                for output in reversed(outputs):
                    if hasattr(output, "CustomModelId") and output.CustomModelId:
                        return output.CustomModelId
                    # Tolerate alternate attribute names ? different
                    # SDK versions have shipped slightly different casing.
                    for attr in [
                        "custom_model_id",
                        "CustomModelID",
                        "model_id",
                        "ModelId",
                    ]:
                        if hasattr(output, attr) and getattr(output, attr):
                            return getattr(output, attr)

    raise ValueError(
        f"Task {job_id} finetune status is {finetune_status}, not Completed or no output model found"
    )


def job_status_command(args):
    """Handle the ``job status`` sub-command.

    Pretty-prints every status field that the ARK SDK exposes so the
    output is usable both by a human reading it and by an AI agent that
    parses it back out of the terminal.

    Args:
        args: ``argparse.Namespace`` with ``job_id``.

    Returns:
        The ``ModelCustomizationJob`` on success, or ``None`` on error.
    """
    print(f"\n=== Query training job status ({args.job_id}) ===")
    try:
        job = get_job_status(args.job_id)
        print(f"Job ID: {job.id}")
        print(f"Job name: {job.name}")
        print(f"Job status: {job.phase}")
        print(f"Status time: {job.status.PhaseTime}")
        print(f"Resumable: {job.status.Resumable}")
        print(f"Retry limit: {job.status.RetryLimit}")

        if job.status.Message:
            print(f"Status message: {job.status.Message}")
        if job.status.QueuePosition is not None:
            print(f"Queue position: {job.status.QueuePosition}")
        if job.status.BillableTokens is not None:
            print(f"Billing tokens: {job.status.BillableTokens}")
        if job.status.TrainingTokensPerEpoch is not None:
            print(f"Training tokens per epoch: {job.status.TrainingTokensPerEpoch}")
        if job.status.RLPluginInfos:
            print(f"RL plugin info: {job.status.RLPluginInfos}")

        print(f"\nJob description: {job.description}")
        print(f"Project: {job.project}")
        print(f"Customization type: {job.customization_type}")
        print(f"Model reference: {job.model_reference}")
        return job
    except Exception as e:
        print(f"Query failed: {str(e)}")
        return None


def job_get_model_command(args):
    """Handle the ``job get-model`` sub-command.

    Args:
        args: ``argparse.Namespace`` with ``job_id``.

    Returns:
        The custom-model ID produced by the training run, or ``None``
        on error / if the job has not completed.
    """
    print(f"\n=== Get training output model ID ({args.job_id}) ===")
    try:
        model_id = get_output_model_id(args.job_id)
        print(f"Training complete! Model ID: {model_id}")
        return model_id
    except Exception as e:
        print(f"Get failed: {str(e)}")
        return None
