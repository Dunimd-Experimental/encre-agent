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

"""Architecture contract artifacts for production-grade delivery.

The workspace-mode flagship (architect / planner on top of general mode)
needs more than ephemeral advisor guidance: the design decisions a planner
or architect sub-agent produces must be persisted as a *living contract*
in the workspace and re-injected into the agent's context on every turn so
the coder stays anchored to the agreed architecture instead of drifting.

Files written by :func:`save_architecture_contract` live under
``<workspace>/.encre/contracts/`` and are loaded by
:func:`load_architecture_contract` into the system prompt each turn.
"""

import os
import re
import time

_CONTRACT_DIR_REL = os.path.join(".encre", "contracts")
# Order matters: later roles override earlier ones for the same sub-area.
_ROLE_FILES: tuple[tuple[str, str], ...] = (
    ("planner", "PLAN.md"),
    ("architect", "ARCHITECTURE.md"),
)

_MAX_CONTRACT_CHARS = 12000


def _contract_dir(workspace: str) -> str:
    return os.path.join(workspace, _CONTRACT_DIR_REL)


def _load_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def save_architecture_contract(
    workspace: str,
    role: str,
    content: str,
    parent_task: str = "",
) -> str | None:
    """Persist a planner/architect sub-agent's output as a contract artifact.

    Writes ``<workspace>/.encre/contracts/PLAN.md`` for planner and
    ``ARCHITECTURE.md`` for architect.  Returns the written file path, or
    ``None`` when no workspace is configured or the content is empty.

    The artifact is capped to avoid bloating the per-turn context; a
    truncated marker is appended so downstream readers know the contract
    is a digest, not the full transcript.
    """
    if not workspace or not os.path.isdir(workspace):
        return None
    content = (content or "").strip()
    if not content:
        return None

    rel = _ROLE_FILES[0][1]  # default PLAN.md
    for role_name, rel_file in _ROLE_FILES:
        if role_name == role:
            rel = rel_file
            break

    if len(content) > _MAX_CONTRACT_CHARS:
        content = content[:_MAX_CONTRACT_CHARS] + "\n\n[TRUNCATED: contract is a digest of a longer sub-agent output]"

    cdir = _contract_dir(workspace)
    try:
        os.makedirs(cdir, exist_ok=True)
    except OSError:
        return None
    path = os.path.join(cdir, rel)
    header_parts = [f"# {rel.rsplit('.', 1)[0]} Contract"]
    if parent_task:
        header_parts.append(f"\n> Parent task: {parent_task[:500]}")
    header_parts.append(f"> Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    body = "\n".join(header_parts) + "\n" + content
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError:
        return None
    return path


def load_architecture_contract(workspace: str) -> str:
    """Return the persisted architecture contract blocks for the workspace.

    Loads ``PLAN.md`` and ``ARCHITECTURE.md`` (when present) and returns a
    single markdown block ready to be appended to the system prompt.  Returns
    ``""`` when no workspace or no contract file exists.
    """
    if not workspace or not os.path.isdir(workspace):
        return ""
    cdir = _contract_dir(workspace)
    if not os.path.isdir(cdir):
        return ""
    blocks: list[str] = []
    for _role, rel_file in _ROLE_FILES:
        content = _load_file(os.path.join(cdir, rel_file))
        if content:
            blocks.append(content)
    if not blocks:
        return ""
    return (
        "## Architecture Contract (binding)\n"
        "The following design decisions were agreed with the architect / planner "
        "role and are BINDING for implementation. Follow them. If a requirement "
        "conflicts with this contract, update the contract first and note the "
        "change rather than silently deviating.\n\n"
        + "\n\n---\n\n".join(blocks)
    )


def is_contract_role(name: str) -> bool:
    """True for sub-agent roles whose output should be persisted as a contract."""
    return bool(name) and any(name.lower() == r for r, _ in _ROLE_FILES)


def latest_contract_mtime(workspace: str) -> float:
    """Return the newest modification time across contract files (0 if none)."""
    if not workspace or not os.path.isdir(workspace):
        return 0.0
    cdir = _contract_dir(workspace)
    if not os.path.isdir(cdir):
        return 0.0
    latest = 0.0
    for _role, rel_file in _ROLE_FILES:
        try:
            mtime = os.path.getmtime(os.path.join(cdir, rel_file))
            if mtime > latest:
                latest = mtime
        except OSError:
            continue
    return latest


def scrub_contract_mentions(text: str) -> str:
    """Remove stale contract-style sections from a compact summary.

    Compaction may snapshot an earlier contract block into the summary; when a
    newer contract supersedes it, the stale copy must not survive compaction or
    the model anchors on outdated architecture.  Best-effort regex removal.
    """
    if not text:
        return text
    # Strip "## Architecture Contract (binding)" through the following blank
    # line pair or the end of the block, whichever comes first.
    pattern = re.compile(
        r"## Architecture Contract \(binding\)\s*\n.*?(?=\n\n\s*(?:##|\Z)|\Z)",
        re.DOTALL,
    )
    return pattern.sub("", text).strip()
