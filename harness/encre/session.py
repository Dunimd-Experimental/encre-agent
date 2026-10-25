#!/usr/bin/env python3

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

from encre.logging_config import get_logger
import base64
import contextlib
import copy
import json
import logging
import os
import pathlib
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from encre.config import EncreConfig
from encre.utils.idgen import BranchIDGenerator
from encre.utils.tokens import count_message_tokens, estimate_tokens

logger = logging.getLogger("encre.session")


def _atomic_write_text(path: pathlib.Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Write *payload* to *path* atomically.

    Writes to a unique ``.tmp`` file in the same directory and then renames
    it over the target, so a crash mid-write never leaves a truncated turn
    file. ``os.replace`` is atomic on both POSIX and Windows.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding) as fh:
            fh.write(payload)
        os.replace(tmp_name, str(path))
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(tmp_name)
        raise


def _inject_orphan_tool_tombstones(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert synthetic ``tool_result`` blocks for tool calls whose result was never persisted.

    If the process crashed after an assistant emitted ``tool_use`` but before
    the tool result reached disk, the reloaded conversation ends with an
    orphan ``tool_use`` and the next API call rejects it. We insert an error
    tombstone for each orphan so the loop can resume cleanly. Each tombstone
    is placed immediately after the last real ``tool_result`` that follows
    the emitting assistant message (or right after the assistant message
    when no real results follow).
    """
    satisfied = {
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    tombstones_by_id: dict[str, dict[str, Any]] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            cid = call.get("id")
            if not cid or cid in satisfied or cid in tombstones_by_id:
                continue
            name = ""
            fn = call.get("function")
            if isinstance(fn, dict):
                name = fn.get("name", "") or ""
            tombstones_by_id[cid] = {
                "role": "tool",
                "tool_call_id": cid,
                "name": name,
                "content": (
                    "[Tool execution was interrupted by a crash; "
                    "result was not persisted. The tool did not complete.]"
                ),
                "is_error": True,
                # Synthetic recovery shim: needed for model context but must
                # never be persisted -- persisting them snowballs (each reload
                # reinjects duplicates once the emitting assistant message is
                # compacted/rolled-back away) and pollutes the branch filters.
                "synthetic_tombstone": True,
            }

    if not tombstones_by_id:
        return messages

    logger.warning(
        "[session] recovered %d orphan tool_use block(s) with tombstones",
        len(tombstones_by_id),
    )

    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role != "tool" and pending:
            out.extend(pending)
            pending = []
        out.append(m)
        if role == "assistant":
            calls = m.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict):
                        cid = call.get("id")
                        if cid and cid in tombstones_by_id:
                            pending.append(tombstones_by_id[cid])
    out.extend(pending)
    return out


def compact_notice_anchor(context_msgs: list[dict[str, Any]]) -> tuple[str, str]:
    """Return ``(notice_id, anchor_id)`` for a compaction over *context_msgs*.

    The anchor is the id of the LAST message the compaction consumes: the
    persisted notice is spliced into the renderer stream right after it, so
    the live "compressing" indicator and the completed "compacted" notice
    occupy the same line instead of jumping to a different position.
    """
    seq = -1
    anchor_id = ""
    for m in context_msgs or []:
        s = m.get("seq_in_branch")
        if isinstance(s, int) and s > seq:
            seq = s
        mid = m.get("id")
        if mid:
            anchor_id = mid
    return f"cp-{int(time.time() * 1000)}-{seq}", anchor_id


def _is_synthetic_user_message(msg: dict[str, Any]) -> bool:
    """Return ``True`` for compaction-generated user messages.

    Mirrors ``compact.engine._is_synthetic_user_message`` (kept local to avoid
    a circular import): summary blocks, active archives, archive hints and
    workspace-context blocks are ``role: user`` but are NOT user input, so
    user-index arithmetic (retry/rollback) must skip them to stay aligned
    with the renderer projection the frontend counts against.
    """
    return bool(
        msg.get("is_synthetic")
        or msg.get("is_compact_summary")
        or msg.get("is_compact_active_archive")
        or msg.get("is_compact_archive_hint")
        or msg.get("is_compact_context")
        or msg.get("is_compact_boundary")
    )


def _extract_file_paths_from_messages(
    messages: list[dict[str, Any]],
    session: Any = None,
) -> set[str]:
    """On-disk paths touched by write tool_calls in *messages*.

    Resolved through the file tools' own path guard (``session`` supplies the
    workspace root / session id) so the keys match the ones
    :meth:`EncreSession.capture_file_snapshot` stored under.  Raw model
    arguments (``notes.txt``, ``/workspace/x``, ``~/x``) used to be the keys,
    which is why a rollback after a reload restored nothing.
    """
    from encre.tools.path_guard import resolve_write_target
    from encre.utils.loop_helpers import extract_write_paths

    paths: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if not isinstance(func, dict):
                continue
            name = func.get("name", "")
            raw_args = func.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                continue
            if not isinstance(args, dict):
                continue
            for raw in extract_write_paths(name, args):
                resolved = resolve_write_target(raw, session)
                if resolved:
                    paths.add(resolved)
    return paths


_MEMORY_FILE_REF_PREFIXES = (
    "memory: ",
    "read memory: ",
    "deleted memory: ",
    "searched memory: ",
)


def _memory_ref_key(tool: str, summary: str) -> str:
    """Normalization key for memory-file reference dedup.

    Returns "" for non memory-file references (search/profile/web/MCP).
    Memory create/read/update/delete on the same file all map to one key so
    repeated operations on the same memory collapse into a single entry.
    """
    t = (tool or "").lower()
    if not t.startswith("memory_") or t in ("memory_search", "memory_profile"):
        return ""
    fn = (summary or "").strip()
    lowered = fn.lower()
    for prefix in _MEMORY_FILE_REF_PREFIXES:
        if lowered.startswith(prefix):
            fn = fn[len(prefix):]
            break
    fn = fn.strip()
    if not fn:
        return ""
    if not fn.lower().endswith(".md"):
        fn += ".md"
    return "memory:" + fn.lower()


def _tool_to_icon(tool_name: str) -> str:
    """Map a tool name to a Lucide icon for the references panel."""
    if tool_name.startswith("mcp__"):
        return "cable"
    name = tool_name.lower()
    if "memory" in name or "profile" in name:
        return "brain"
    if "web_search" in name:
        return "globe"
    if "web_fetch" in name:
        return "external-link"
    if "agent" in name or "sub-agent" in name or "sub_agent" in name:
        return "bot"
    if "question" in name or "ask" in name:
        return "help-circle"
    if "bash" in name or "shell" in name or "execute" in name:
        return "terminal"
    if "git" in name:
        return "git-branch"
    if "database" in name or "sql" in name or "db" in name:
        return "database"
    if "search" in name or "grep" in name or "glob" in name or "find" in name:
        return "search"
    if "cron" in name or "schedule" in name:
        return "clock"
    if "notebook" in name or "jupyter" in name:
        return "notebook"
    if "browser" in name:
        return "monitor"
    if "workflow" in name or "dag" in name:
        return "git-merge"
    if "email" in name:
        return "mail"
    if "docker" in name:
        return "container"
    if "http" in name or "curl" in name or "api" in name:
        return "cloud"
    if "deploy" in name or "publish" in name:
        return "rocket"
    return "zap"


@dataclass
class SessionCheckpoint:
    checkpoint_id: str
    label: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_call_count: int = 0
    turn_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    plan_items: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    file_snapshots: dict[str, str] = field(default_factory=dict)  # path -> base64-encoded before-content
    created_at: float = 0.0


@dataclass
class BranchMeta:
    id: str
    parent_branch_id: str | None = None
    fork_point_message_id: str | None = None
    created_at: float = 0.0
    messages_count: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[dict[str, Any]] = field(default_factory=list)


class EncreSession:
    SYSTEM_BUDGET_RATIO = 0.15
    HISTORY_BUDGET_RATIO = 0.50
    RESPONSE_BUDGET_RATIO = 0.35

    def __init__(self, config: EncreConfig) -> None:
        self.id: str = str(uuid.uuid4())
        self.config: EncreConfig = config
        self.messages: list[dict[str, Any]] = []
        self.created_at: float = time.time()
        self.updated_at: float = time.time()
        self.last_message_at: float = time.time()  # ONLY updated on add_message, NOT on touch()
        self.tool_call_count: int = 0
        self.turn_count: int = 0
        self.metadata: dict[str, Any] = {}
        self.parent_session_id: str = ""
        self._checkpoints: OrderedDict[str, SessionCheckpoint] = OrderedDict()
        self._max_checkpoints: int = config.checkpoint_max_count
        self.artifacts: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.plan_items: list[dict[str, Any]] = []
        self.compact_archive: list[dict[str, Any]] | None = None
        # Compacted-context overlay (the VIEW layer).  Context compression
        # NEVER rewrites or deletes the persisted branch messages: the full
        # history stays append-only in ``messages`` / turn files.  Instead a
        # compaction stores its reduced output here together with the branch
        # watermark (``upto`` = the highest ``seq_in_branch`` it covers).
        # ``get_context_messages`` composes ``view + tail`` so the model sees
        # the compressed prefix plus every message appended after it.  The
        # overlay is invalidated by any history rewrite (rollback, checkpoint
        # restore, replace) and persisted to ``view.json`` across restarts.
        self.compacted_view: dict[str, Any] | None = None
        self.file_snapshots: dict[str, str] = {}  # path -> base64 content before last write
        # Last ``file_snapshots`` payload written to ``snapshots.json``.  Only
        # used to skip a redundant re-encrypt+rewrite of a multi-megabyte
        # blob on sessions that save after every message.
        self._snapshots_written: dict[str, str] | None = None
        # Paths touched by write tools since the last rollback commit.  The
        # commit stores their POST-turn content (commit-time file states) so
        # a checkout can restore files that were written by both kept and
        # removed turns, where the pre-write snapshot is too old.
        self.files_touched_since_commit: set[str] = set()
        # LIFO undo stack for write-tool side effects (lazily created by
        # ``ensure_undo_stack``; records persist under the session dir).
        self._undo_stack: Any = None
        self.active_branch_id: str = f"br_{1:04d}"
        self._branch_counter: int = 1
        self.branches: dict[str, BranchMeta] = {}
        self.branches[self.active_branch_id] = BranchMeta(id=self.active_branch_id, created_at=time.time())
        self._branch_last_seq: dict[str, int] = {self.active_branch_id: -1}
        self._branch_last_message_id: dict[str, str | None] = {self.active_branch_id: None}
        self._context_cache: dict[str, list[dict[str, Any]]] = {}
        self._messages_version: int = 0
        self._turn_partition_cache: tuple[int, list[list[dict[str, Any]]]] | None = None
        self._summary_cache: tuple[int, int, str] | None = None

    def mark_messages_dirty(self) -> None:
        self._messages_version += 1
        self._context_cache.clear()
        self._turn_partition_cache = None
        self._summary_cache = None

    @property
    def active_turn(self) -> int:
        """1-based index of the turn currently running.

        ``turn_count`` only advances at the end of an iteration, so records
        journalled *during* a turn have to be tagged ``turn_count + 1`` to sit
        in the same numbering as the end-of-turn records (memory nodes from the
        significance judge, turn summaries).  Rollback compares tags against
        the restored ``turn_count``: entries written in the raw 0-based space
        landed one turn early and survived the purge, which is why a
        rolled-back turn kept its memory and its plugin installs.
        """
        return self.turn_count + 1

    def add_message(self, role: str, content: str | list[dict[str, Any]], **kwargs: Any) -> None:
        message: dict[str, Any] = {"role": role, "content": content, "created_at": int(time.time() * 1000)}
        message["branch_id"] = self.active_branch_id
        seq = self._get_next_seq(self.active_branch_id)
        message["seq_in_branch"] = seq
        message["id"] = BranchIDGenerator.message_id(self.id, self.active_branch_id, seq)
        message["parent_id"] = self._branch_last_message_id.get(self.active_branch_id)
        message.update(kwargs)
        self.messages.append(message)
        self._branch_last_seq[self.active_branch_id] = seq
        self._branch_last_message_id[self.active_branch_id] = message["id"]
        self.mark_messages_dirty()
        self.updated_at = time.time()
        self.last_message_at = time.time()
        # Append-only trajectory recording: the sink is attached lazily by
        # the loop (TrajectoryRecorder) and must never break message flow.
        _sink = getattr(self, "trajectory_sink", None)
        if callable(_sink):
            with contextlib.suppress(Exception):
                _sink("message.appended", message)

    def add_user_message_with_image(self, text: str, image_path: str) -> None:
        """Add a user message containing both text and an image."""
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if image_path.startswith(("http://", "https://")):
            content.append({
                "type": "image_url",
                "image_url": {"url": image_path},
            })
        else:
            mime = _guess_image_mime(image_path)
            try:
                with open(image_path, "rb") as f:
                    data = base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                return
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            })
        self.add_message("user", content)

    def add_user_message_with_file(self, text: str, file_path: str) -> None:
        """Add a user message with an attached file's content."""
        try:
            with open(file_path, encoding="utf-8") as f:
                file_content = f.read()
        except Exception:
            return
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        filename = os.path.basename(file_path)
        content.append({
            "type": "text",
            "text": f"\n<attached_file filename=\"{filename}\">\n{file_content}\n</attached_file>",
        })
        self.add_message("user", content)

    def add_message_content(self, role: str, blocks: list[dict[str, Any]]) -> None:
        """Add a message with raw content blocks (for advanced multimodal use)."""
        self.add_message(role, blocks)

    def add_tool_result(
        self,
        tool_call_id: str,
        content: str,
        is_error: bool = False,
        sub_agent_messages: list[dict[str, Any]] | None = None,
        sub_agent_session_id: str | None = None,
        client_id: str | None = None,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        # Secret redaction: strip credential-looking material before the
        # tool result enters the session history (which is persisted with
        # the session context).  Best effort -- never block history writes.
        try:
            from encre.telemetry import redact_text
            content, _redactions = redact_text(content)
        except Exception:
            _redactions = 0
        # Funnel the replacement count into the telemetry
        # ``total_redactions`` channel (the loop wiring exposes its
        # telemetry on the session; absent when no loop is attached).  A
        # zero count skips the lookup entirely.
        if _redactions:
            _t = getattr(self, "telemetry", None)
            if _t is not None:
                _t.add_redaction_count(_redactions)
        seq = self._get_next_seq(self.active_branch_id)
        msg: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
            "branch_id": self.active_branch_id,
            "seq_in_branch": seq,
            "id": BranchIDGenerator.message_id(self.id, self.active_branch_id, seq),
        }
        # Renderer-side correlation: store the synthetic client_id under
        # ``_client_id`` so the renderer's restore path can find the
        # matching tc when its id was overridden to client_id during
        # restore.  The protocol-mandated ``tool_call_id`` is the
        # backend id and must stay unchanged for LLM roundtrips.
        if client_id is not None and client_id != tool_call_id:
            msg["_client_id"] = client_id
        # Screenshot payloads produced by the tool (e.g. the unified
        # ``computer`` tool).  Stored as a side key -- ``content`` stays a
        # plain string so the renderer, restore and compaction paths are
        # untouched; the model-context projection expands these into image
        # content blocks at request-build time (see backends/adaptive.py).
        if images:
            msg["images"] = [
                {"media_type": str(i.get("media_type", "image/png")), "data": str(i.get("data", ""))}
                for i in images
                if isinstance(i, dict) and i.get("data")
            ]
            if not msg["images"]:
                del msg["images"]
        # Persist the error flag so rebuild_artifacts_from_messages can tell
        # failed tool calls apart from successful ones on session reload.
        if is_error:
            msg["is_error"] = True
        if sub_agent_messages is not None:
            msg["sub_agent_messages"] = sub_agent_messages
        if sub_agent_session_id is not None:
            msg["sub_agent_session_id"] = sub_agent_session_id
        msg["parent_id"] = self._branch_last_message_id.get(self.active_branch_id)
        self.messages.append(msg)
        self._branch_last_seq[self.active_branch_id] = seq
        self._branch_last_message_id[self.active_branch_id] = msg["id"]
        self.mark_messages_dirty()
        self.tool_call_count += 1
        self.updated_at = time.time()

    def add_artifact(self, file_path: str, tool_name: str = "file_write", diff_text: str = "") -> dict[str, Any]:
        import os
        # A failed write must never become a change artifact: if the tool
        # reported success but the file is not on disk, drop the entry.
        if not os.path.isfile(file_path):
            return {}
        name = os.path.basename(file_path)
        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        try:
            size = os.path.getsize(file_path)
        except OSError:
            size = 0
        entry: dict[str, Any] = {
            "path": file_path,
            "name": name,
            "ext": ext,
            "size": size,
            "tool": tool_name,
            "created_at": time.time(),
            "diff_text": diff_text,
            "branch_id": self.active_branch_id,
        }
        existing = {a["path"] for a in self.artifacts}
        if file_path not in existing:
            self.artifacts.append(entry)
            if self.active_branch_id in self.branches:
                self.branches[self.active_branch_id].artifacts.append(entry)
        return entry

    def capture_file_snapshot(self, file_path: str) -> None:
        """Capture a file's current content BEFORE a write tool modifies it.
        The snapshot is stored base64-encoded in ``file_snapshots`` and used
        by ``restore_file_snapshots()`` during rollback.

        The key is the resolved on-disk path (see
        :func:`encre.tools.path_guard.resolve_write_target`): the tools write
        there, and rollback looks the file up by the same rule.  A path the
        guard would refuse is a path no tool can write, so nothing is
        recorded for it.
        """
        import base64

        from encre.tools.path_guard import resolve_write_target
        target = resolve_write_target(file_path, self)
        if not target:
            return
        # Track the path for the next rollback commit's file states even when
        # a pre-write snapshot already exists (file written in an earlier
        # turn): the commit must still record this turn's post-write content.
        self.files_touched_since_commit.add(target)
        # Only capture the first (oldest) snapshot per file path.
        if target in self.file_snapshots:
            return
        try:
            with open(target, "rb") as fh:
                raw = fh.read(8 * 1024 * 1024)  # cap at 8 MiB
            self.file_snapshots[target] = base64.b64encode(raw).decode("ascii")
        except (FileNotFoundError, IsADirectoryError):
            # File doesn't exist yet — snapshot as empty (deletion on rollback)
            self.file_snapshots[target] = ""
        except (OSError, PermissionError) as exc:
            logger.warning("[file_snapshot] failed to capture %s: %s", target, exc)

    @staticmethod
    def apply_file_snapshot(file_path: str, b64_content: str) -> bool:
        """Write a base64-encoded snapshot back to *file_path*.

        An empty ``b64_content`` means the file did not exist when the
        snapshot was taken, so an existing file is deleted.  Returns ``True``
        when the file was written (or removed) successfully.
        """
        import base64
        import os as _os
        try:
            if b64_content:
                raw = base64.b64decode(b64_content)
                _os.makedirs(_os.path.dirname(file_path) or ".", exist_ok=True)
                with open(file_path, "wb") as fh:
                    fh.write(raw)
            elif _os.path.exists(file_path):
                _os.remove(file_path)
            return True
        except (OSError, ValueError) as exc:
            logger.warning("[file_snapshot] failed to apply %s: %s", file_path, exc)
            return False

    def restore_file_snapshots_for_paths(self, paths: set[str]) -> int:
        """Write captured snapshots for *paths* back to disk and drop them.

        Only files that were touched by the removed/rolled-back turns are
        restored. Paths that are still referenced by kept messages are left
        untouched so earlier user work is not accidentally reverted.
        """
        count = 0
        for file_path in list(paths):
            b64_content = self.file_snapshots.get(file_path)
            if b64_content is None:
                continue
            if self.apply_file_snapshot(file_path, b64_content):
                count += 1
            self.file_snapshots.pop(file_path, None)
        return count

    def restore_file_snapshots(self) -> int:
        """Write all captured file snapshots back to disk.
        Returns the number of files restored.
        """
        return self.restore_file_snapshots_for_paths(set(self.file_snapshots.keys()))

    def revert_manage_ops(self, keep: int) -> int:
        """Undo manage-tool lifecycle ops recorded after the first *keep* entries.

        The journal (``metadata["manage_ops"]``) is append-only; every
        rollback path keeps a prefix of it and inverts the dropped suffix in
        reverse order (last install/uninstall is undone first).  Returns the
        number of ops successfully reverted.
        """
        ops = self.metadata.get("manage_ops") if isinstance(self.metadata, dict) else None
        if not isinstance(ops, list) or keep < 0 or len(ops) <= keep:
            return 0
        reverted = 0
        for op in reversed(ops[keep:]):
            if not isinstance(op, dict):
                continue
            try:
                from encre.tools.builtin.manage import revert_manage_op
                if revert_manage_op(op):
                    reverted += 1
            except Exception as exc:
                logger.warning(
                    "[revert_manage_ops] failed to revert %s: %s",
                    op.get("action"), exc,
                )
        self.metadata["manage_ops"] = ops[:keep]
        return reverted

    def revert_manage_ops_after_turn(self, turn: int) -> int:
        """Revert every manage-tool op recorded in turns after *turn*."""
        ops = self.metadata.get("manage_ops") if isinstance(self.metadata, dict) else None
        if not isinstance(ops, list):
            return 0
        keep = -1
        for i, op in enumerate(ops):
            if isinstance(op, dict) and isinstance(op.get("turn"), int) and op["turn"] > turn:
                keep = i
                break
        if keep < 0:
            return 0
        return self.revert_manage_ops(keep)

    def purge_memory_after_turn(
        self,
        min_turn: int,
        *,
        inclusive: bool = False,
        created_after: float | None = None,
    ) -> int:
        """Retire memory nodes produced by turns after *min_turn*.

        The memdir store is NOT part of the session snapshot, so without
        this the model keeps recalling content from rolled-back turns
        (the "memory did not roll back" symptom).  Nodes are archived in
        the append-only long-term log and dropped from the short-term
        buffer; entries without turn provenance are only touched when
        ``created_after`` is given (legacy-node timestamp fallback).

        Args:
            min_turn: Turn boundary; turns after it are purged.
            inclusive: Also purge entries tagged exactly *min_turn*.
            created_after: Ingestion-time cutoff for untagged legacy nodes
                (the rollback target commit's timestamp).

        Returns:
            Number of memory entries retired.
        """
        store = getattr(self, "memory_layers", None)
        if store is None:
            return 0
        try:
            return store.purge_session_entries(
                self.id, min_turn, inclusive=inclusive, created_after=created_after)
        except Exception as exc:
            logger.warning("[purge_memory_after_turn] failed: %s", exc)
            return 0

    def ensure_undo_stack(self) -> Any:
        """Return this session's LIFO undo stack, creating it lazily.

        The stack persists under ``data/sessions/<id>/undo/`` (the same
        session-dir convention the loop state machine uses for
        ``events.jsonl``) so undo records survive process restarts.  The
        implementation class lives in the loop's execute phase and is
        imported lazily to avoid a module import cycle.
        """
        stack = self._undo_stack
        if stack is None:
            from encre.loop.phases.execute import UndoStack

            stack = UndoStack(pathlib.Path("data", "sessions", self.id))
            self._undo_stack = stack
        return stack

    async def undo(self) -> dict[str, Any]:
        """Undo the most recent write-tool file change (LIFO).

        Pops the top record from the session undo stack and reverts the
        filesystem: ``created`` -> delete the created file; ``modified`` /
        ``deleted`` -> restore the backed-up prior content.  The operation is
        appended to ``<session_dir>/undo/undo.jsonl`` (audit trail for a
        future redo) and, when the loop state machine is attached
        (``loop_state_machine`` attribute set by the loop wiring), recorded
        in the session event stream as ``undo_executed`` / ``undo_empty``.

        Returns:
            ``{"undone": bool, "tool": str, "path": str}`` -- or
            ``{"undone": False}`` when the stack is empty.
        """
        stack = self.ensure_undo_stack()
        loop_sm = getattr(self, "loop_state_machine", None)
        record = stack.pop()
        if record is None:
            stack.log_operation({"ts": datetime.now(UTC).isoformat(), "event": "undo_empty"})
            if loop_sm is not None:
                loop_sm.record("undo_empty")
            return {"undone": False}
        tool = str(record.get("tool", ""))
        path = str(record.get("path", ""))
        restored = stack.restore(record)
        if not restored and record.get("content_available"):
            # Transient I/O failure with a usable backup: push the record
            # back so a retry can succeed.
            stack.push_back(record)
        stack.log_operation({
            "ts": datetime.now(UTC).isoformat(),
            "event": "undo_executed",
            "tool": tool,
            "path": path,
            "action": record.get("action", ""),
            "undone": restored,
            "undo_seq": record.get("undo_seq", 0),
        })
        if loop_sm is not None:
            loop_sm.record("undo_executed", {
                "tool": tool,
                "path": path,
                "action": record.get("action", ""),
                "undone": restored,
            })
        return {"undone": restored, "tool": tool, "path": path}

    def add_reference(self, tool_name: str, summary: str, icon: str = "") -> dict[str, Any]:
        """Record a non-file tool reference (memory, MCP, web, agent, etc.).

        Memory-file references (create/read/update/delete on the same file)
        are merged into one entry so the summary panel shows a single row per
        memory file.  Returns the reference entry dict.
        """
        if not icon:
            icon = _tool_to_icon(tool_name)
        key = _memory_ref_key(tool_name, summary)
        if key:
            for existing in self.references:
                if _memory_ref_key(
                    str(existing.get("tool", "")), str(existing.get("summary", ""))
                ) == key:
                    # Merge: refresh the existing entry instead of appending.
                    existing["tool"] = tool_name
                    existing["summary"] = summary
                    existing["icon"] = icon
                    existing["timestamp"] = time.time()
                    existing["branch_id"] = self.active_branch_id
                    return existing
        entry: dict[str, Any] = {
            "tool": tool_name,
            "summary": summary,
            "icon": icon,
            "timestamp": time.time(),
            "branch_id": self.active_branch_id,
        }
        self.references.append(entry)
        if self.active_branch_id in self.branches:
            self.branches[self.active_branch_id].references.append(entry)
        return entry

    def rebuild_artifacts_from_messages(self) -> None:
        """Scan self.messages and rebuild artifacts from tool call data.

        Parses file paths from tool call arguments (reliable JSON), extracts
        diffs from corresponding tool results, and sets self.artifacts and
        per-branch artifacts.  Called after loading session from disk so that
        artifacts are always authoritative regardless of streaming bugs.
        """
        _file_tool_names = {"file_write", "file_edit", "write_file", "writeFile", "apply_patch"}
        import re as _re

        # Result content prefixes that mark a failed execution.  Older
        # sessions do not persist the ``is_error`` flag on tool result
        # messages, so fall back to these markers for legacy data.
        _error_prefixes = (
            "error:",
            "permission denied",
            "tool execution crashed",
            "tool execution was cancelled",
        )

        def _result_is_error(content: str) -> bool:
            head = content.strip().lower()
            return any(head.startswith(p) for p in _error_prefixes)

        # Build tool_call_id -> result lookup
        tool_results: dict[str, str] = {}
        tool_error_ids: set[str] = set()
        for msg in self.messages:
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                if tcid:
                    tool_results[tcid] = str(msg.get("content", ""))
                    if msg.get("is_error"):
                        tool_error_ids.add(tcid)

        new_artifacts: list[dict[str, Any]] = []
        branch_artifacts: dict[str, list[dict[str, Any]]] = {}
        global_seen: set[str] = set()

        for msg in self.messages:
            if msg.get("role") != "assistant":
                continue
            branch_id = msg.get("branch_id", self.active_branch_id)
            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                continue

            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                if name not in _file_tool_names:
                    continue

                # Parse arguments to get file_path
                raw_args = func.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        continue
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    continue

                file_path = ""
                if name == "apply_patch":
                    patches = args.get("patches", [])
                    if patches and isinstance(patches, list):
                        file_path = patches[0].get("new_path") or patches[0].get("old_path", "")
                else:
                    file_path = args.get("file_path", "") or args.get("path", "")

                if not file_path:
                    continue

                # Deduplicate globally
                if file_path in global_seen:
                    continue
                global_seen.add(file_path)

                # Get diff text from corresponding tool result
                tc_id = tc.get("id", "")
                result = tool_results.get(tc_id, "")
                # Failed or never-completed calls must not become artifacts.
                if tc_id in tool_error_ids or not result or _result_is_error(result):
                    continue
                diff_text = ""
                if result:
                    m = _re.search(r"```diff\n(.+?)\n```", result, _re.DOTALL)
                    if m:
                        diff_text = m.group(1).strip()

                # The write must actually exist on disk; a tool that claimed
                # success without producing the file leaves no artifact.
                if not os.path.isfile(file_path):
                    continue

                name_from_path = os.path.basename(file_path)
                ext = os.path.splitext(file_path)[1].lower().lstrip(".")
                try:
                    size = os.path.getsize(file_path)
                except OSError:
                    size = 0

                entry: dict[str, Any] = {
                    "path": file_path,
                    "name": name_from_path,
                    "ext": ext,
                    "size": size,
                    "tool": name,
                    "created_at": time.time(),
                    "diff_text": diff_text,
                    "branch_id": branch_id,
                }

                new_artifacts.append(entry)
                branch_artifacts.setdefault(branch_id, []).append(entry)

        self.artifacts = new_artifacts
        for bid, arts in branch_artifacts.items():
            if bid in self.branches:
                self.branches[bid].artifacts = arts

    def ensure_artifacts_from_messages(self) -> None:
        """Rebuild artifacts only when none were persisted.

        Artifacts loaded from the summary DB / meta file are authoritative;
        rebuilding from (potentially compacted) messages would drop the file
        operations that were compacted away.  This fallback exists purely for
        legacy sessions that never persisted artifacts.
        """
        if not self.artifacts:
            self.rebuild_artifacts_from_messages()

    def prune_phantom_references(self) -> None:
        """Drop references whose memory file no longer exists on disk.

        Old sessions can contain references created against a memory file
        that was later deleted, or that never existed because the model passed
        a name that did not match the on-disk (sanitized) filename.  Those
        phantom entries pollute the summary panel, so filter them out after
        loading.  Only memory file references are checked; web/MCP/summary
        references have no on-disk counterpart.
        """
        if not self.references:
            return
        from encre.config import get_data_dir

        memory_dir = get_data_dir() / "memory"
        keep: list[dict[str, Any]] = []
        for ref in self.references:
            tool = str(ref.get("tool", "")).lower()
            if tool.startswith("memory_") and tool not in (
                "memory_search",
                "memory_profile",
            ):
                fn = str(ref.get("summary", ""))
                for prefix in ("Memory: ", "Read memory: ", "Deleted memory: "):
                    if fn.startswith(prefix):
                        fn = fn[len(prefix):]
                        break
                if not fn.endswith(".md"):
                    fn += ".md"
                if not (memory_dir / fn).is_file():
                    continue
            keep.append(ref)
        if len(keep) != len(self.references):
            self.references = keep

    def _get_next_seq(self, branch_id: str) -> int:
        return self._branch_last_seq.get(branch_id, -1) + 1

    def get_branch_messages(
        self,
        branch_id: str,
        *,
        sanitize_for_model: bool = True,
    ) -> list[dict[str, Any]]:
        lineage: list[str] = []
        current = self.branches.get(branch_id)
        while current:
            lineage.insert(0, current.id)
            current = self.branches.get(current.parent_branch_id) if current.parent_branch_id else None

        if not lineage and branch_id == f"br_{1:04d}":
            lineage = [branch_id]

        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_system = False
        for i, bid in enumerate(lineage):
            branch_meta = self.branches.get(bid) if i > 0 else None
            msgs = sorted(
                [m for m in self.messages if m.get("branch_id") == bid],
                key=lambda m: m.get("seq_in_branch", 0),
            )
            if i < len(lineage) - 1:
                next_meta = self.branches.get(lineage[i + 1])
                if next_meta and next_meta.fork_point_message_id:
                    truncated: list[dict[str, Any]] = []
                    for m in msgs:
                        truncated.append(m)
                        if m.get("id", "") == next_meta.fork_point_message_id:
                            break
                    msgs = truncated
            if branch_meta and branch_meta.fork_point_message_id:
                first_id = msgs[0].get("id", "") if msgs else ""
                if first_id == branch_meta.fork_point_message_id:
                    msgs = msgs[1:]
            for m in msgs:
                mid = m.get("id", "")
                if m.get("role") == "system":
                    # Each branch carries its own system message (different
                    # ``id``) so id-dedup misses them; keep only the FIRST
                    # (current branch's) system message when merging lineage,
                    # otherwise the context accumulates stale system prompts
                    # from every fork point.
                    if seen_system:
                        continue
                    seen_system = True
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                result.append(m)

        if branch_id == f"br_{1:04d}":
            # The comprehension below already yields messages in
            # ``self.messages`` order; the old sorted(key=list.index) was an
            # O(n^2) no-op and is removed.
            legacy = [m for m in self.messages if m.get("branch_id") is None]
            for m in legacy:
                mid = m.get("id", "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                result.append(m)

        if not sanitize_for_model:
            return result

        # Sanitize: drop renderer-only annotations before the messages
        # reach the LLM.  These are the ``_client_id`` keys attached by
        # ``add_message`` (assistant tool_calls) and ``add_tool_result``
        # (tool role messages) for renderer correlation.  OpenAI-compatible
        # backends reject unknown fields, so they have to be stripped on
        # the way out.  We deep-copy the touched fields to avoid mutating
        # the persisted session state.
        sanitized: list[dict[str, Any]] = []
        for m in result:
            touched = False
            tcs = m.get("tool_calls")
            new_tcs: list[dict[str, Any]] | None = None
            if tcs:
                new_tcs = [
                    {k: v for k, v in tc.items() if k != "_client_id"}
                    for tc in tcs if isinstance(tc, dict)
                ]
                touched = True
            has_client_id_msg = m.get("_client_id") is not None
            if not touched and not has_client_id_msg:
                sanitized.append(m)
                continue
            mm = dict(m)
            if new_tcs is not None:
                mm["tool_calls"] = new_tcs
            if has_client_id_msg:
                mm.pop("_client_id", None)
            sanitized.append(mm)
        return sanitized

    def get_renderer_messages(self) -> list[dict[str, Any]]:
        """Return active-branch messages with renderer correlation metadata."""
        messages = self.get_branch_messages(
            self.active_branch_id,
            sanitize_for_model=False,
        )
        return [dict(message) for message in messages]

    def create_branch(self, from_branch_id: str, fork_point_message_id: str) -> BranchMeta:
        self._branch_counter += 1
        new_id = f"br_{self._branch_counter:04d}"
        meta = BranchMeta(
            id=new_id,
            parent_branch_id=from_branch_id,
            fork_point_message_id=fork_point_message_id,
            created_at=time.time(),
        )
        self.branches[new_id] = meta
        return meta

    def retry_at_user_index(self, user_index: int) -> tuple[str, BranchMeta]:
        """Fork at the N-th user message (0-based, within the active branch context).

        Finds the user message by its index across messages visible in the
        current branch context (not all messages in the session), creates a
        new branch forking from that message, and switches to the new branch.
        Returns (user_message_text, new_branch).

        Using the active branch context ensures the index matches what the
        frontend computes from displayed messages.  Counting across ALL
        messages would include user messages from unrelated branches and
        shift the index, causing the fork point to point to the wrong message.

        Compaction-generated synthetic user messages (summary block, active
        archive, archive hint, workspace context) are excluded to match the
        renderer projection: the frontend sees them as a ``compact_marker``
        divider (or nothing at all), not as user turns.  Counting them here
        shifted every index after a compaction, making the fork point land on
        the WRONG message -- the user-visible "rollback deleted my message"
        bug.
        """
        branch_msgs = self.get_branch_messages(self.active_branch_id)
        user_msgs = [
            m for m in branch_msgs
            if m.get("role") == "user" and not _is_synthetic_user_message(m)
        ]
        if user_index < 0 or user_index >= len(user_msgs):
            raise ValueError(
                f"User message index {user_index} out of range "
                f"(total user messages in active branch: {len(user_msgs)})"
            )
        target = user_msgs[user_index]
        # Find the actual session message by matching content+role (ID may differ
        # across branches for the same logical message).  Fall back to ID match.
        msg_id = target.get("id", "")
        if msg_id:
            # Verify the target message exists in the session (it should).
            session_match = next((m for m in self.messages if m.get("id") == msg_id), None)
            if not session_match:
                raise ValueError(f"User message {msg_id} not found in session")
        else:
            raise ValueError(f"User message at index {user_index} has no ID")

        original_branch = target.get("branch_id", self.active_branch_id)
        branch = self.create_branch(original_branch, msg_id)
        self.switch_branch(branch.id)

        content = target.get("content", "")
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = " ".join(texts)
        return str(content), branch

    def retry_assistant(self, message_id: str) -> tuple[str, BranchMeta]:
        target = None
        for m in self.messages:
            if m.get("id", "").endswith(f":M:{message_id}") or m.get("id") == message_id:
                target = m
                break
        if not target:
            raise ValueError(f"Message {message_id} not found")

        parent_id = target.get("parent_id")
        if not parent_id:
            raise ValueError("Cannot retry: no parent message")
        parent = None
        for m in self.messages:
            if m.get("id") == parent_id:
                parent = m
                break
        if not parent:
            raise ValueError("Parent message not found")

        original_branch = target.get("branch_id", self.active_branch_id)
        branch = self.create_branch(original_branch, parent_id)
        self.switch_branch(branch.id)
        return parent_id, branch

    def switch_branch(self, branch_id: str) -> None:
        if branch_id in self.branches:
            self.active_branch_id = branch_id

    def rollback_to(
        self,
        branch_id: str,
        message_id: str,
        include_target: bool = False,
    ) -> tuple[list[str], str, bool]:
        """Cross-branch hard rollback.

        Finds the target message across ALL branches (not just ``branch_id``),
        removes all messages after it in that branch -- and, with
        ``include_target``, the target message itself (the rollback-edit
        semantic: the client re-enters the text in its composer) -- wipes
        every descendant branch entirely, restores file snapshots, rebuilds
        artifacts, filters references to the rollback point, and switches to
        the target branch.

        Removing the target in THIS pass (instead of a second filter in the
        protocol handler) keeps one authoritative boundary: the old handler
        re-filter used a different branch condition and diverged from the
        truncation, which left the branch tree and the transcript disagreeing.

        Returns ``(removed_message_ids, actual_target_branch_id, target_found)``.
        ``target_found`` is True only when the target message actually exists in
        the session; a missing target is NOT the same as a target that is the
        last message in its branch (which yields an empty ``removed`` list but
        is still a valid rollback point).
        """
        target_msg = None
        target_branch_id = None
        target_seq = None
        for m in self.messages:
            mid = m.get("id", "")
            if mid.endswith(f":M:{message_id}") or mid == message_id:
                target_msg = m
                target_branch_id = m.get("branch_id", branch_id)
                target_seq = m.get("seq_in_branch", 0)
                break
        if target_msg is None:
            return [], branch_id, False

        # Collect all descendant branch IDs of the target branch
        descendant_ids: set[str] = {target_branch_id}
        changed = True
        while changed:
            changed = False
            for bid, bmeta in list(self.branches.items()):
                if bmeta.parent_branch_id in descendant_ids and bid not in descendant_ids:
                    descendant_ids.add(bid)
                    changed = True

        # Remove:
        #   - messages after target_seq in the target branch
        #   - ALL messages in descendant branches (they are wiped entirely)
        # Collect the removed message dicts BEFORE truncation: the file
        # restore below needs their tool_calls, and extracting them from
        # ``self.messages`` after the reassignment always yielded an empty
        # set, which silently disabled file rollback on this path.
        removed: list[str] = []
        removed_msgs: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for m in self.messages:
            mbranch = m.get("branch_id", "")
            mseq = m.get("seq_in_branch", 0)
            remove = False
            if mbranch == target_branch_id:
                if isinstance(mseq, int) and mseq > target_seq:
                    remove = True
            elif mbranch in descendant_ids:
                remove = True
            # Identity match on purpose: the target's own seq equals
            # target_seq, so the seq comparison above never removes it.
            if include_target and m is target_msg:
                remove = True
            if remove:
                removed.append(m.get("id", ""))
                removed_msgs.append(m)
            else:
                remaining.append(m)
        self.messages = remaining
        # History shrank: any compacted-context overlay is stale.
        self.compacted_view = None

        # Purge descendant branches from the tree
        for bid in descendant_ids:
            if bid != target_branch_id:
                self.branches.pop(bid, None)
                self._branch_last_seq.pop(bid, None)
                self._branch_last_message_id.pop(bid, None)

        # Switch to the target branch and update its metadata
        if target_branch_id in self.branches:
            self.active_branch_id = target_branch_id
            meta = self.branches[target_branch_id]
            meta.messages_count = sum(1 for m in self.messages if m.get("branch_id") == target_branch_id)
            branch_msgs = [m for m in self.messages if m.get("branch_id") == target_branch_id]
            total = self.count_messages_tokens(branch_msgs)
            meta.tokens = {"input": total, "output": 0, "total": total}
            meta.artifacts = []
            meta.references = []
            meta.compactions = []

        # Recalculate counters from remaining messages
        branch_msgs = [m for m in self.messages if m.get("branch_id") == target_branch_id]
        self.turn_count = sum(1 for m in branch_msgs if m.get("role") == "assistant")
        self.tool_call_count = sum(
            1 for m in branch_msgs
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        # The removed turns still live on in metadata-derived prompt blocks
        # (turn summaries, milestones, working set, ...); prune everything at
        # or after the rollback point so the model cannot see them anymore.
        self.prune_derived_state(self.turn_count, inclusive=False)
        # Manage-tool lifecycle ops (installs/uninstalls recorded by the
        # manage builtin) performed after the rollback point are undone, so a
        # rolled-back conversation does not keep their effects.
        self.revert_manage_ops_after_turn(self.turn_count)
        # Memory nodes judged out of the removed turns must stop being
        # recalled, otherwise the model keeps "remembering" rolled-back
        # content (memory is not part of the session snapshot).  The
        # rollback point's wall-clock time is the fallback cutoff for nodes
        # written before turn tagging existed: without it an untagged node
        # from a discarded turn was never matched, let alone retired.
        _rollback_ts = target_msg.get("created_at", 0) / 1000.0 or None
        self.purge_memory_after_turn(
            self.turn_count, created_after=_rollback_ts,
        )

        # Hard-rollback: revert only file operations performed by the removed
        # messages. Files touched by kept messages must stay as-is so that
        # work done before the rollback point is not accidentally reverted.
        #
        # The pre-write snapshot in ``file_snapshots`` is the FIRST write of the
        # session for a path, so for anything written in more than one turn it
        # predates the rollback point.  The authoritative "what did this file
        # look like at the rollback point" answer is therefore the commit-time
        # file state, which must win over the pre-write snapshot for every
        # path it covers.  Paths with no commit state (the file was never
        # committed, or the commit chain is missing) fall back to the
        # pre-write snapshot, which is exactly right for those.
        removed_files = _extract_file_paths_from_messages(removed_msgs, self)
        kept_files = _extract_file_paths_from_messages(self.messages, self)
        files_to_restore = removed_files - kept_files
        restored = 0
        commit_states: dict[str, str] = {}
        if removed_files:
            try:
                from encre.rollback import EncreRollbackGit
                # Wall-clock as-of, not a turn-count filter: turn_count is
                # recounted per branch after a rollback, so it disagrees with
                # the branch the historical commits were taken on.
                commit_states = EncreRollbackGit().file_states_asof(
                    self.id, _rollback_ts)
            except Exception as exc:
                logger.warning(
                    "[rollback_to] commit file-state lookup failed: %s", exc)
                commit_states = {}
        for file_path in files_to_restore:
            if file_path in commit_states:
                if self.apply_file_snapshot(file_path, commit_states[file_path]):
                    restored += 1
                continue
            restored += self.restore_file_snapshots_for_paths({file_path})
        # Files written by BOTH kept and removed turns: only commit-time state
        # can restore them without also reverting the kept turns' work.
        for file_path in removed_files & kept_files:
            if file_path in commit_states and self.apply_file_snapshot(
                    file_path, commit_states[file_path]):
                restored += 1
        if restored:
            logger.info("[rollback_to] restored %d file(s) from snapshots", restored)
        self.rebuild_artifacts_from_messages()
        target_created_at = target_msg.get("created_at", 0) / 1000.0
        self.references = [
            r for r in self.references
            if r.get("branch_id", "") not in descendant_ids
            and not (r.get("branch_id") == target_branch_id and r.get("timestamp", 0) > target_created_at)
        ]
        # Rebuild runtime caches against the SHRUNK list: the stale
        # last-seq / last-message-id values pointed at removed messages,
        # so a new message would chain onto a parent that no longer
        # exists.  rebuild_runtime_caches also clears the context cache.
        self.rebuild_runtime_caches()
        self.updated_at = time.time()
        # Recalculate last_message_at from the newest remaining message
        timestamps = [m.get("created_at", 0) for m in self.messages]
        self.last_message_at = max(timestamps) / 1000.0 if timestamps else time.time()
        return removed, target_branch_id, True

    def checkpoint(self, label: str = "") -> str:
        if self._max_checkpoints <= 0:
            return ""
        cid = str(uuid.uuid4())[:12]
        meta = copy.deepcopy(self.metadata)
        meta["_branch_state"] = {
            "active_branch_id": self.active_branch_id,
            "_branch_counter": self._branch_counter,
            "branches": {k: v.__dict__ for k, v in self.branches.items()},
        }
        cp = SessionCheckpoint(
            checkpoint_id=cid,
            label=label,
            messages=copy.deepcopy(self.messages),
            tool_call_count=self.tool_call_count,
            turn_count=self.turn_count,
            metadata=meta,
            plan_items=copy.deepcopy(self.plan_items),
            artifacts=copy.deepcopy(self.artifacts),
            references=copy.deepcopy(self.references),
            file_snapshots=dict(self.file_snapshots),
            created_at=time.time(),
        )
        self._checkpoints[cid] = cp
        while len(self._checkpoints) > self._max_checkpoints:
            self._checkpoints.popitem(last=False)
        return cid

    def rollback(self, checkpoint_id: str) -> bool:
        cp = self._checkpoints.get(checkpoint_id)
        if cp is None:
            return False
        # Restore file snapshots FIRST so files are reverted before message
        # history is reloaded (the frontend may trigger a render immediately).
        restored = self.restore_file_snapshots()
        if restored:
            logger.info("[rollback] restored %d file(s) from snapshots", restored)
        self.messages = copy.deepcopy(cp.messages)
        # Checkpoint restore rewrites history: the compacted-context overlay
        # no longer matches and must be discarded.
        self.compacted_view = None
        self.tool_call_count = cp.tool_call_count
        self.turn_count = cp.turn_count
        self.metadata = copy.deepcopy(cp.metadata)
        self.plan_items = copy.deepcopy(cp.plan_items)
        self.artifacts = copy.deepcopy(cp.artifacts)
        self.references = copy.deepcopy(cp.references)
        # Also restore the file snapshots that existed at checkpoint time
        # so subsequent rollbacks within the same session can still revert.
        self.file_snapshots = dict(cp.file_snapshots)
        branch_state = cp.metadata.get("_branch_state", {})
        if branch_state:
            self.active_branch_id = branch_state.get("active_branch_id", self.active_branch_id)
            self._branch_counter = branch_state.get("_branch_counter", self._branch_counter)
            branches_data = branch_state.get("branches", {})
            if branches_data:
                self.branches = {}
                for k, v in branches_data.items():
                    self.branches[k] = BranchMeta(**v)
        self.rebuild_runtime_caches()
        self.updated_at = time.time()
        return True

    def list_checkpoints(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for cp in self._checkpoints.values():
            result.append({
                "checkpoint_id": cp.checkpoint_id,
                "label": cp.label,
                "message_count": len(cp.messages),
                "tool_call_count": cp.tool_call_count,
                "turn_count": cp.turn_count,
                "plan_items_count": len(cp.plan_items),
                "artifacts_count": len(cp.artifacts),
                "created_at": cp.created_at,
            })
        return result

    def clear_checkpoints(self) -> None:
        self._checkpoints.clear()

    def is_expired(self) -> bool:
        age = time.time() - self.created_at
        max_age_seconds = self.config.session_max_age_hours * 3600
        return age > max_age_seconds

    def is_max_turns_reached(self) -> bool:
        if self.config.max_turns <= 0:
            return False
        if self.turn_count >= self.config.max_turns:
            logger.warning(
                "[is_max_turns_reached] turn=%d max=%d",
                self.turn_count, self.config.max_turns,
            )
            return True
        return False

    def get_context_messages(self) -> list[dict[str, Any]]:
        cached = self._context_cache.get(self.active_branch_id)
        if cached is not None:
            return cached
        msgs = self.get_branch_messages(self.active_branch_id)
        # Overlay the compacted-context view: compressed prefix + every
        # message appended after the watermark.  The persisted branch itself
        # is never touched by compaction, so a stale overlay is only possible
        # when the branch shrank below the watermark (rollback / restore) --
        # in that case the overlay is discarded and the raw branch is used.
        view = self.compacted_view
        if view and view.get("branch_id") == self.active_branch_id:
            upto = view.get("upto", -1)
            if isinstance(upto, int) and upto >= 0 and self._branch_last_seq.get(self.active_branch_id, -1) >= upto:
                tail = [
                    m for m in msgs
                    if isinstance(m.get("seq_in_branch"), int) and m["seq_in_branch"] > upto
                ]
                msgs = list(view.get("messages") or []) + tail
            else:
                self.compacted_view = None
        self._context_cache[self.active_branch_id] = msgs
        return msgs

    def apply_compacted_view(
        self,
        branch_id: str,
        view_messages: list[dict[str, Any]],
        upto_seq: int,
    ) -> None:
        """Install a compaction result as the model-context overlay.

        This is the ONLY way compaction output reaches the model context:
        the persisted branch messages are left untouched (append-only), so
        no turn file ever loses content.  ``upto_seq`` is the highest
        ``seq_in_branch`` of the branch the compaction consumed; messages
        with a higher seq are appended verbatim after the view on every
        request.  Any history rewrite elsewhere invalidates the overlay.
        """
        self.compacted_view = {
            "branch_id": branch_id,
            "upto": int(upto_seq),
            "messages": list(view_messages),
        }
        self.mark_messages_dirty()
        # Record the overlay so the trajectory stays a faithful replay
        # source.  Unlike the old ``history.replaced`` nothing was destroyed
        # -- the persisted history is intact; only the model's view changed.
        _sink = getattr(self, "trajectory_sink", None)
        if callable(_sink):
            with contextlib.suppress(Exception):
                _sink("context.compacted", {
                    "branch_id": branch_id,
                    "upto": int(upto_seq),
                    "view_count": len(view_messages),
                    "branch_count": self._branch_last_seq.get(branch_id, -1) + 1,
                })

    # ── UI notices (compaction / model switch) ─────────────────────
    #
    # Events the user must be able to SEE in the transcript, recorded the
    # same way tool calls are: append-only, anchored at a branch position,
    # persisted with the session, and replayed into the renderer stream by
    # ``renderer_session_messages``.  They never enter ``self.messages``
    # (the model context must not see UI chrome), which is why they live in
    # metadata and are spliced in at projection time.

    #: metadata key holding the notice list.
    META_UI_NOTICES = "ui_notices"
    #: Hard cap so a very long session cannot grow the list unbounded.
    _UI_NOTICES_MAX = 500

    def _switch_round_watermark(self, branch_id: str) -> tuple[int, int]:
        """Burst identity for model-switch coalescing.

        ``(seq of the last REAL user message in the branch, completed turn
        count)``.  Two switches that share it happened inside the same
        round with no new user input between them: tombstones, streaming
        output and compaction blocks never move it, so a whole fallback
        burst (and any number of idle manual switches) collapses into a
        single notice.
        """
        user_seq = -1
        for m in self.messages:
            if m.get("branch_id") != branch_id or m.get("role") != "user":
                continue
            if _is_synthetic_user_message(m):
                continue
            s = m.get("seq_in_branch")
            if isinstance(s, int) and s > user_seq:
                user_seq = s
        return user_seq, int(self.turn_count)

    def _last_real_user_ts(self, branch_id: str) -> float:
        """Timestamp of the last REAL user message in the branch.

        A seq/turn watermark silently breaks when a session is rebuilt and
        its messages are renumbered (a burst of switches then looks like
        two separate rounds and produces two lines).  Message timestamps
        are stable across rebuilds, so this is the coalescing key that
        survives a restart.
        """
        ts = 0.0
        for m in self.messages:
            if m.get("branch_id") != branch_id or m.get("role") != "user":
                continue
            if _is_synthetic_user_message(m):
                continue
            ts = max(ts, float(m.get("created_at") or m.get("timestamp") or 0))
        return ts

    def record_ui_notice(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        notice_id: str = "",
        anchor_id: str = "",
        branch_id: str = "",
    ) -> dict[str, Any]:
        """Append a UI notice (``"compact"`` / ``"model_switch"``).

        Args:
            kind: Notice kind -- ``"compact"`` or ``"model_switch"``.
            payload: Kind-specific fields (token/message counts for a
                compaction; from/to model ids for a switch).
            notice_id: Stable id shared with the live event so the client
                can dedupe; generated when empty.
            anchor_id: Server id of the message the notice belongs AFTER
                (the compaction watermark message, or the last message
                that existed when a manual switch happened).
            branch_id: Branch the anchor belongs to; defaults to the
                active branch.

        Returns:
            The stored notice dict (including its generated id).  For a
            coalesced model switch this is the PREVIOUS notice with its
            ``to_model``/``reason``/``source`` refreshed and its original
            ``from_model`` (the chain start) kept, so callers emit the net
            change under the original id.  A burst that switches away and
            back (net no change) removes the notice entirely; the returned
            dict then carries ``from_model == to_model`` as the removal
            signal for live clients.
        """
        bid = branch_id or self.active_branch_id
        now = int(time.time() * 1000)
        seq = self._branch_last_seq.get(bid, -1)
        notices = self.metadata.get(self.META_UI_NOTICES)
        if not isinstance(notices, list):
            notices = []
        self.metadata[self.META_UI_NOTICES] = notices

        # Model switches COALESCE within one round: every switch made
        # before the next real user message fires is one burst and must end
        # up as a single line showing the net change -- intermediate models
        # that never produced visible output are noise, not history.
        if kind == "model_switch":
            prev = None
            for n in reversed(notices):
                if not isinstance(n, dict) or n.get("branch_id", bid) != bid:
                    continue
                if n.get("kind") == "model_switch":
                    prev = n
                    break
            if (
                prev is not None
                and (
                    (
                        prev.get("user_ts") is not None
                        and prev.get("user_ts") == self._last_real_user_ts(bid)
                    )
                    or (
                        prev.get("user_ts") is None
                        and (prev.get("user_seq"), prev.get("turn"))
                        == self._switch_round_watermark(bid)
                    )
                )
            ):
                prev["to_model"] = str(payload.get("to_model", "") or prev.get("to_model", ""))
                prev["reason"] = str(payload.get("reason", "") or "")
                prev["source"] = str(payload.get("source", "") or prev.get("source", "manual"))
                prev["ts"] = now
                if prev.get("from_model") and prev.get("from_model") == prev.get("to_model"):
                    notices.remove(prev)
                self.updated_at = time.time()
                return prev

        notice = {
            "id": notice_id or f"ui-{kind}-{now}-{len(self.messages)}",
            "kind": kind,
            "seq": int(seq),
            "branch_id": bid,
            "anchor_id": anchor_id or (self._branch_last_message_id.get(bid) or ""),
            "ts": now,
            **payload,
        }
        if kind == "model_switch":
            notice["user_seq"], notice["turn"] = self._switch_round_watermark(bid)
            notice["user_ts"] = self._last_real_user_ts(bid)
        notices.append(notice)
        if len(notices) > self._UI_NOTICES_MAX:
            del notices[: len(notices) - self._UI_NOTICES_MAX]
        self.updated_at = time.time()
        return notice

    def get_ui_notices(self, branch_id: str = "") -> list[dict[str, Any]]:
        """Return persisted UI notices for *branch_id* (active by default)."""
        bid = branch_id or self.active_branch_id
        notices = self.metadata.get(self.META_UI_NOTICES)
        if not isinstance(notices, list):
            return []
        return [n for n in notices if isinstance(n, dict) and n.get("branch_id", bid) == bid]

    def get_summary(self) -> tuple[int, str]:
        if self._summary_cache is not None and self._summary_cache[0] == self._messages_version:
            return self._summary_cache[1], self._summary_cache[2]

        msg_count = 0
        preview = ""
        for m in self.messages:
            if m.get("role") != "user":
                continue
            msg_count += 1
            if preview:
                continue
            c = m.get("content", "")
            if isinstance(c, str) and c.strip():
                preview = c.strip()[:80]
                continue
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                        preview = b["text"].strip()[:80]
                        break

        self._summary_cache = (self._messages_version, msg_count, preview)
        return msg_count, preview

    def replace_branch_messages(self, branch_id: str, new_messages: list[dict[str, Any]]) -> None:
        other = [m for m in self.messages if m.get("branch_id") != branch_id]
        # Stamp every replacement message with the owning branch and a
        # contiguous seq_in_branch.  Compaction output (``_build_compacted``)
        # creates brand-new dicts for the summary/archive/hint blocks that
        # carry NO branch_id or seq -- without stamping, ``get_branch_messages``
        # filters them out (empty model context on any non-root branch and
        # scrambled legacy-fallback ordering on reload) and ``rollback_to``
        # mis-deletes them (missing seq defaults to 0).  The incoming list is
        # the full replacement for the branch, so renumbering 0..N-1 in list
        # order is the correct, branch-consistent sequence.
        for seq, m in enumerate(new_messages):
            m["branch_id"] = branch_id
            m["seq_in_branch"] = seq
        self.messages = other + new_messages
        # A wholesale branch replacement no longer happens during compaction
        # (compaction uses the view overlay), so reaching this point means a
        # deliberate history rewrite (rollback / checkpoint restore) -- any
        # compacted-context overlay is stale and must go.
        self.compacted_view = None
        last_seq = len(new_messages) - 1 if new_messages else -1
        last_id: str | None = None
        for m in new_messages:
            s = m.get("seq_in_branch", -1)
            if isinstance(s, int) and s >= last_seq:
                last_seq = s
                last_id = m.get("id")
        self._branch_last_seq[branch_id] = last_seq
        self._branch_last_message_id[branch_id] = last_id
        self.mark_messages_dirty()
        # Record the wholesale replacement (compaction / rollback) so the
        # trajectory stays a faithful replay source.
        _sink = getattr(self, "trajectory_sink", None)
        if callable(_sink):
            with contextlib.suppress(Exception):
                _sink("history.replaced", {
                    "branch_id": branch_id,
                    "count": len(new_messages),
                })

    def rebuild_runtime_caches(self) -> None:
        self._branch_last_seq = {}
        self._branch_last_message_id = {}
        self.mark_messages_dirty()
        for branch_id in self.branches:
            self._branch_last_seq.setdefault(branch_id, -1)
            self._branch_last_message_id.setdefault(branch_id, None)
        for m in self.messages:
            branch_id = m.get("branch_id", self.active_branch_id)
            seq = m.get("seq_in_branch", -1)
            if branch_id not in self._branch_last_seq or (isinstance(seq, int) and seq >= self._branch_last_seq[branch_id]):
                self._branch_last_seq[branch_id] = seq if isinstance(seq, int) else self._branch_last_seq.get(branch_id, -1)
                self._branch_last_message_id[branch_id] = m.get("id")
        self._branch_last_seq.setdefault(self.active_branch_id, -1)
        self._branch_last_message_id.setdefault(self.active_branch_id, None)

    def prune_derived_state(self, min_turn: int, *, inclusive: bool = False) -> None:
        """Drop metadata state derived from turns at/after ``min_turn``.

        Editing, deleting and rolling back messages truncates ``messages``,
        but the summary-like state in ``metadata`` (turn summaries, milestone
        summaries, working set, ...) was derived from the OLD conversation.
        None of it is part of the branch message lists, so plain message
        truncation never touches it -- and it keeps leaking removed content
        back into the system prompt ("Prior Turns" / "Current Task" blocks)
        on every later model request.

        Args:
            min_turn: First affected turn number.
            inclusive: Also drop the entry whose ``turn`` equals ``min_turn``.
                Edit / checkout pass True: the redone turn's own summary still
                holds the pre-edit prompt text, so it must go.

        Returns:
            None.
        """
        # Imported lazily: ``encre.compact`` pulls in the compaction engine,
        # which sits above this module in the dependency graph.
        from encre.compact.loop_state.manager import (
            META_DELEGATE_HISTORY,
            META_MILESTONE_SUMMARIES,
            META_STUCK_EVENTS,
            META_TASK_STAGE_HISTORY,
            META_TURN_SUMMARIES,
            META_WORKING_SET,
        )
        for key in (META_TURN_SUMMARIES, META_MILESTONE_SUMMARIES):
            entries = self.metadata.get(key)
            if not isinstance(entries, list) or not entries:
                continue
            kept: list[dict[str, Any]] = []
            for entry in entries:
                turn = entry.get("turn") if isinstance(entry, dict) else None
                # Entries without a usable turn number cannot be attributed
                # to a surviving turn; drop them instead of leaking
                # unattributable content into the prompt.
                if not isinstance(turn, int):
                    continue
                drop = turn >= min_turn if inclusive else turn > min_turn
                if drop:
                    continue
                kept.append(entry)
            self.metadata[key] = kept
        # Working set / task-stage history / delegation history / stuck
        # events carry no turn numbers and describe work done inside the
        # discarded turns; they cannot be filtered precisely, so reset them
        # and let the following turns rebuild them.
        self.metadata[META_WORKING_SET] = {}
        self.metadata[META_TASK_STAGE_HISTORY] = []
        self.metadata[META_DELEGATE_HISTORY] = []
        self.metadata[META_STUCK_EVENTS] = []

    def clear_history(self) -> None:
        self.messages.clear()
        self.compacted_view = None
        self.metadata.pop(self.META_UI_NOTICES, None)
        self.tool_call_count = 0
        self.turn_count = 0
        self.active_branch_id = f"br_{1:04d}"
        self._branch_counter = 1
        self.branches = {self.active_branch_id: BranchMeta(id=self.active_branch_id, created_at=time.time())}
        self._branch_last_seq = {self.active_branch_id: -1}
        self._branch_last_message_id = {self.active_branch_id: None}
        self.mark_messages_dirty()
        self.updated_at = time.time()

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return estimate_tokens(text)

    @staticmethod
    def count_messages_tokens(messages: list[dict[str, Any]]) -> int:
        return count_message_tokens(messages)

    def truncate_messages(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        system_budget = int(max_tokens * self.SYSTEM_BUDGET_RATIO)
        history_budget = int(max_tokens * self.HISTORY_BUDGET_RATIO)

        system_msgs: list[dict[str, Any]] = []
        history_msgs: list[dict[str, Any]] = []

        for msg in messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                history_msgs.append(msg)

        system_truncated = self._truncate_to_budget(system_msgs, system_budget)
        history_truncated = self._smart_truncate_history(history_msgs, history_budget)

        return system_truncated + history_truncated

    def _truncate_to_budget(
        self,
        messages: list[dict[str, Any]],
        budget: int,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        used = 0
        for msg in messages:
            tokens = self.count_messages_tokens([msg])
            if used + tokens > budget:
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 200:
                    truncated = content[:200] + "\n...[truncated]"
                    truncated_msg = dict(msg)
                    truncated_msg["content"] = truncated
                    result.append(truncated_msg)
                break
            result.append(msg)
            used += tokens
        return result

    def _smart_truncate_history(
        self,
        messages: list[dict[str, Any]],
        budget: int,
    ) -> list[dict[str, Any]]:
        total = self.count_messages_tokens(messages)
        if total <= budget:
            return messages

        preserved_pairs: list[int] = []
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "tool":
                found_pair = False
                for j in range(i - 1, -1, -1):
                    msg = messages[j]
                    role = msg.get("role")
                    content = msg.get("content", "")
                    if (role == "assistant" and isinstance(content, str) and content) or (role == "assistant" and msg.get("tool_calls")):
                        preserved_pairs.extend([j, i])
                        found_pair = True
                        break
                if found_pair:
                    continue

        preserved = 0
        for i in preserved_pairs:
            if i < len(messages):
                preserved += self.count_messages_tokens([messages[i]])

        preserved_indices: set[int] = set(preserved_pairs)
        compressed: list[dict[str, Any]] = []
        compressed.append({
            "role": "user",
            "content": "[Previous conversation context has been compressed to save tokens]",
        })

        used = self.count_messages_tokens(compressed)
        for i in range(len(messages) - 1, -1, -1):
            if i in preserved_indices:
                compressed.append(messages[i])
                used += self.count_messages_tokens([messages[i]])
                continue
            tokens = self.count_messages_tokens([messages[i]])
            if used + tokens <= budget:
                compressed.append(messages[i])
                used += tokens
            else:
                break

        compressed.reverse()
        return compressed

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "messages": self.messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_message_at": self.last_message_at,
            "tool_call_count": self.tool_call_count,
            "turn_count": self.turn_count,
            "metadata": self.metadata,
            "plan_items": self.plan_items,
            "compact_archive": self.compact_archive,
            "artifacts": self.artifacts,
            "references": self.references,
            "file_snapshots": self.file_snapshots,
            "active_branch_id": self.active_branch_id,
            "_branch_counter": self._branch_counter,
            "branches": {k: v.__dict__ for k, v in self.branches.items()},
        }

    def to_meta_dict(self) -> dict[str, Any]:
        """Session metadata only -- lightweight, no messages."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_message_at": self.last_message_at,
            "tool_call_count": self.tool_call_count,
            "turn_count": self.turn_count,
            "message_count": self.get_summary()[0],
            "metadata": self.metadata,
            "plan_items": self.plan_items,
            "artifacts": self.artifacts,
            "references": self.references,
            "active_branch_id": self.active_branch_id,
            "_branch_counter": self._branch_counter,
            "branches": {k: v.__dict__ for k, v in self.branches.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: EncreConfig) -> "EncreSession":
        session = cls(config)
        session.id = data.get("id", session.id)
        session.messages = data.get("messages", [])
        session.created_at = data.get("created_at", session.created_at)
        session.updated_at = data.get("updated_at", session.updated_at)
        session.tool_call_count = data.get("tool_call_count", 0)
        session.turn_count = data.get("turn_count", 0)
        session.metadata = data.get("metadata", {})
        session.plan_items = data.get("plan_items", [])
        session.compact_archive = data.get("compact_archive")
        session.artifacts = data.get("artifacts", [])
        session.references = data.get("references", [])
        session.file_snapshots = dict(data.get("file_snapshots", {}))
        session.active_branch_id = data.get("active_branch_id", session.active_branch_id)
        session._branch_counter = data.get("_branch_counter", session._branch_counter)
        branches_raw = data.get("branches", {})
        session.branches = {}
        for k, v in branches_raw.items():
            if isinstance(v, BranchMeta):
                session.branches[k] = v
            elif isinstance(v, dict):
                session.branches[k] = BranchMeta(**v)
        if not session.branches:
            session.branches[session.active_branch_id] = BranchMeta(id=session.active_branch_id, created_at=session.created_at)
        session.rebuild_artifacts_from_messages()
        session.rebuild_runtime_caches()
        return session

    # ── legacy single-file I/O (backwards compat) ──────────────────────

    def save_to_json(self, filepath: str) -> None:
        import json

        from encre.crypto import encrypt
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        # Fail closed: encryption failure must never fall back to plaintext.
        encrypted = encrypt(payload)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(encrypted)

    @classmethod
    def load_from_json(cls, filepath: str, config: EncreConfig) -> "EncreSession":
        import json

        from encre.crypto import decrypt
        with open(filepath, encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            raise ValueError("Empty session file")
        if not raw.startswith("{"):
            with contextlib.suppress(Exception):
                raw = decrypt(raw)
        data = json.loads(raw)
        return cls.from_dict(data, config)

    # ── directory‑based I/O (one turn = one file) ──────────────────────

    def save_to_dir(self, dirpath: str) -> None:
        """Write the full session into '{dirpath}/turn_NNNN.json' files.

        Messages are partitioned by turn: turn 0 = system + first user,
        turnout N = assistant + tool_calls + tool_results + guidance.

        The pre-compaction snapshot (``compact_archive``) is persisted into a
        separate encrypted ``archive.json`` so the old turns survive on disk
        across process restarts and session switches; it is removed when no
        archive is set.

        Safety: if the in-memory session has NO saveable messages while the
        directory already contains turn files, the turn rewrite is skipped
        with a logged warning.  That combination is always a corruption
        signature (a blank/stale session object about to clobber real
        history), never a legitimate user action -- clearing history goes
        through ``delete_session_from_disk`` or leaves an archive behind.
        """
        import json

        from encre.crypto import encrypt
        d = pathlib.Path(dirpath)
        d.mkdir(parents=True, exist_ok=True)

        # Synthetic tombstones are a model-context recovery shim only; they
        # must never reach disk (see ``_inject_orphan_tool_tombstones``).
        saveable = [m for m in self.messages if not m.get("synthetic_tombstone")]

        # Group messages by turn
        existing_turn_files: list[pathlib.Path] = []
        for p in d.iterdir():
            if p.name.startswith("turn_") and p.suffix == ".json":
                existing_turn_files.append(p)

        if not saveable and existing_turn_files:
            logger.warning(
                "[save_to_dir] %s: refusing to overwrite %d turn file(s) "
                "with an empty message list",
                dirpath, len(existing_turn_files),
            )
            turns: list[list[dict[str, Any]]] = []
        else:
            turns = self._partition_messages_into_turns(saveable)
        for idx, msgs in enumerate(turns):
            fpath = d / f"turn_{idx:04d}.json"
            payload = json.dumps(msgs, ensure_ascii=False, separators=(",", ":"))
            # Fail closed: never write plaintext when encryption was requested.
            payload = encrypt(payload)
            _atomic_write_text(fpath, payload)

        for p in existing_turn_files:
            try:
                idx = int(p.stem.split("_", 1)[1])
            except Exception:
                continue
            if idx >= len(turns):
                p.unlink(missing_ok=True)

        # Persist / clear the pre-compaction archive.  Old turns physically
        # leave the turn files after a compaction (the summary replaces them
        # in the branch); the archive is the only copy, so it MUST hit disk.
        archive_path = d / "archive.json"
        try:
            if self.compact_archive:
                payload = json.dumps(
                    [m for m in self.compact_archive if not m.get("synthetic_tombstone")],
                    ensure_ascii=False, separators=(",", ":"),
                )
                # Fail closed: the archive is the only pre-compaction copy.
                payload = encrypt(payload)
                _atomic_write_text(archive_path, payload)
            elif archive_path.exists():
                archive_path.unlink()
        except Exception:
            logger.exception("[session] failed to persist compact archive in %s", dirpath)

        # Persist / clear the compacted-context view.  Compaction no longer
        # rewrites turn files (history is append-only), so the overlay is the
        # only place the reduced context lives -- it must survive restarts.
        view_path = d / "view.json"
        try:
            if self.compacted_view:
                payload = json.dumps(self.compacted_view, ensure_ascii=False, separators=(",", ":"))
                # Fail closed: never write plaintext when encryption is on.
                payload = encrypt(payload)
                _atomic_write_text(view_path, payload)
            elif view_path.exists():
                view_path.unlink()
        except Exception:
            logger.exception("[session] failed to persist compacted view in %s", dirpath)

        # Persist the pre-write file snapshots.  They are the only record of
        # what a file looked like before the model touched it, and the
        # directory store is what the server reloads sessions from -- without
        # this the first session switch or restart left every rollback with an
        # empty snapshot table, so a "Rollback file" action restored nothing.
        snap_path = d / "snapshots.json"
        try:
            if self.file_snapshots and self._snapshots_written != self.file_snapshots:
                payload = json.dumps(self.file_snapshots, ensure_ascii=False, separators=(",", ":"))
                payload = encrypt(payload)
                _atomic_write_text(snap_path, payload)
                self._snapshots_written = dict(self.file_snapshots)
            elif not self.file_snapshots and snap_path.exists():
                snap_path.unlink()
                self._snapshots_written = None
        except Exception:
            logger.exception("[session] failed to persist file snapshots in %s", dirpath)

        # Write meta
        self._save_meta_file(d)

        # Persist summary-panel data (plan/artifacts/references) into the
        # per-session SQLite store.  These tables are never rebuilt from
        # (compacted) messages, so they survive context compaction.
        self._save_summary_db(d)

    @classmethod
    def load_from_dir(cls, dirpath: str, config: EncreConfig) -> "EncreSession":
        """Load a session from a directory of turn files."""
        import json

        from encre.crypto import decrypt
        d = pathlib.Path(dirpath)
        if not d.is_dir():
            raise ValueError(f"Session directory not found: {dirpath}")

        # Read meta first
        meta_path = d / "meta.json"
        session = cls(config)
        if meta_path.exists():
            raw = meta_path.read_text(encoding="utf-8").strip()
            if raw and not raw.startswith("{"):
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            try:
                meta = json.loads(raw)
                session.id = meta.get("id", session.id)
                session.created_at = meta.get("created_at", session.created_at)
                session.updated_at = meta.get("updated_at", session.updated_at)
                # `last_message_at` is the source of truth for sidebar ordering.
                # Fall back to updated_at (NOT the constructor's NOW) when the
                # meta was written by an older build that didn't persist this
                # field -- otherwise reloading would reset the displayed time
                # to "now" on every click.
                stored_last_msg = meta.get("last_message_at")
                if isinstance(stored_last_msg, int | float):
                    session.last_message_at = float(stored_last_msg)
                else:
                    session.last_message_at = meta.get("updated_at", session.created_at)
                session.tool_call_count = meta.get("tool_call_count", 0)
                session.turn_count = meta.get("turn_count", 0)
                session.metadata = meta.get("metadata", {})
                session.plan_items = meta.get("plan_items", [])
                session.artifacts = meta.get("artifacts", [])
                session.references = meta.get("references", [])
                session.active_branch_id = meta.get("active_branch_id", session.active_branch_id)
                session._branch_counter = meta.get("_branch_counter", session._branch_counter)
                branches_raw = meta.get("branches", {})
                session.branches = {}
                for k, v in branches_raw.items():
                    if isinstance(v, BranchMeta):
                        session.branches[k] = v
                    elif isinstance(v, dict):
                        session.branches[k] = BranchMeta(**v)
                if not session.branches:
                    session.branches[session.active_branch_id] = BranchMeta(id=session.active_branch_id, created_at=session.created_at)
            except json.JSONDecodeError:
                logger.debug("exception suppressed", exc_info=True)

        # Read turn files in order
        turn_files = sorted(
            [p for p in d.iterdir() if p.name.startswith("turn_") and p.suffix == ".json"],
            key=lambda p: p.name,
        )
        messages: list[dict[str, Any]] = []
        for fpath in turn_files:
            raw = fpath.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            if not raw.startswith("["):
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            try:
                turn_msgs = json.loads(raw)
                if isinstance(turn_msgs, list):
                    messages.extend(turn_msgs)
            except json.JSONDecodeError:
                continue

        session.messages = _inject_orphan_tool_tombstones(messages)
        # Restore the compacted-context view overlay.  Compaction never
        # rewrites turn files, so the view (reduced model context) lives only
        # here; the full history above is intact and always reloaded.
        view_path = d / "view.json"
        if view_path.exists():
            try:
                raw = view_path.read_text(encoding="utf-8").strip()
                if raw and not raw.startswith("{"):
                    with contextlib.suppress(Exception):
                        raw = decrypt(raw)
                view = json.loads(raw)
                if (isinstance(view, dict) and isinstance(view.get("messages"), list)
                        and isinstance(view.get("branch_id"), str)
                        and isinstance(view.get("upto"), int)):
                    session.compacted_view = view
            except json.JSONDecodeError:
                logger.warning("[session] corrupt view.json in %s -- ignored", dirpath)
        # Restore the pre-write file snapshots so a rollback after a reload
        # still has the content to put back.
        snap_path = d / "snapshots.json"
        if snap_path.exists():
            try:
                raw = snap_path.read_text(encoding="utf-8").strip()
                if raw and not raw.startswith("{"):
                    with contextlib.suppress(Exception):
                        raw = decrypt(raw)
                snaps = json.loads(raw)
                if isinstance(snaps, dict):
                    session.file_snapshots = {
                        str(k): str(v) for k, v in snaps.items() if isinstance(v, str)
                    }
                    session._snapshots_written = dict(session.file_snapshots)
            except json.JSONDecodeError:
                logger.warning("[session] corrupt snapshots.json in %s -- ignored", dirpath)
        # Restore the legacy pre-compaction archive (older sessions whose
        # compactions still physically replaced branch messages).
        archive_path = d / "archive.json"
        if archive_path.exists():
            try:
                raw = archive_path.read_text(encoding="utf-8").strip()
                if raw and not raw.startswith("["):
                    with contextlib.suppress(Exception):
                        raw = decrypt(raw)
                archived = json.loads(raw)
                if isinstance(archived, list):
                    session.compact_archive = archived
            except json.JSONDecodeError:
                logger.warning("[session] corrupt archive.json in %s -- ignored", dirpath)
        # Prefer the per-session SQLite store for summary-panel data: it is
        # written on every save and never derived from (compacted) messages.
        # Older sessions fall back to the values persisted in the meta file,
        # and only legacy sessions with no persisted artifacts fall through to
        # message reconstruction.
        try:
            db_plan, db_arts, db_refs = session._load_summary_db(d)
            if db_plan or db_arts or db_refs:
                session.plan_items = db_plan or session.plan_items
                session.artifacts = db_arts or session.artifacts
                session.references = db_refs or session.references
        except Exception:
            logger.debug("[session] no summary db for %s, using meta values", dirpath)
        session.prune_phantom_references()
        # Rebuild artifacts from authoritative message data rather than
        # relying on potentially-buggy streaming artifact creation -- only
        # when nothing was persisted (legacy sessions).
        session.ensure_artifacts_from_messages()
        session.rebuild_runtime_caches()
        return session

    @staticmethod
    def load_preview(dirpath: str) -> str | None:
        """Read only the first user message for preview (fast)."""
        import json

        from encre.crypto import decrypt
        d = pathlib.Path(dirpath)
        for fpath in sorted(d.iterdir()):
            if not fpath.name.startswith("turn_"):
                continue
            raw = fpath.read_text(encoding="utf-8").strip()
            if raw and not raw.startswith("["):
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            try:
                msgs = json.loads(raw)
                if isinstance(msgs, list):
                    for m in msgs:
                        if m.get("role") == "user":
                            c = m.get("content", "")
                            if isinstance(c, str) and c.strip():
                                return c.strip()[:80]
                            elif isinstance(c, list):
                                for b in c:
                                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                                        return b["text"].strip()[:80]
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def read_meta_last_active(dirpath: str, fallback: float = 0.0) -> float:
        """Read the canonical last-message timestamp from a session's meta.json.

        Used by sidebar listings to keep the displayed time stable across
        reloads and across different list-collection paths.  Prefers
        ``last_message_at`` (the source of truth), falls back to
        ``updated_at`` for older builds that did not persist
        ``last_message_at``, and finally to ``fallback`` for malformed or
        missing meta files.  Never returns the current clock.
        """
        import json as _json
        try:
            meta_path = pathlib.Path(dirpath) / "meta.json"
            if not meta_path.is_file():
                return fallback
            raw = meta_path.read_text(encoding="utf-8").strip()
            if not raw:
                return fallback
            if not raw.startswith("{"):
                with contextlib.suppress(Exception):
                    from encre.crypto import decrypt as _decrypt
                    raw = _decrypt(raw)
            meta = _json.loads(raw)
            if not isinstance(meta, dict):
                return fallback
            value = meta.get("last_message_at")
            if isinstance(value, int | float):
                return float(value)
            value = meta.get("updated_at")
            if isinstance(value, int | float):
                return float(value)
            return fallback
        except Exception:
            return fallback

    @staticmethod
    def count_user_messages(dirpath: str) -> int:
        """Count user-role messages across all turn files (for index repair)."""
        import json

        from encre.crypto import decrypt
        d = pathlib.Path(dirpath)
        count = 0
        for fpath in sorted(d.iterdir()):
            if not fpath.name.startswith("turn_"):
                continue
            raw = fpath.read_text(encoding="utf-8").strip()
            if raw and not raw.startswith("["):
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            try:
                msgs = json.loads(raw)
                if isinstance(msgs, list):
                    count += sum(1 for m in msgs if m.get("role") == "user")
            except json.JSONDecodeError:
                continue
        return count

    @staticmethod
    def search_turns(dirpath: str, query_lower: str) -> list[dict[str, Any]]:
        """Search all turn files in a session directory for a query string."""
        import json

        from encre.crypto import decrypt
        d = pathlib.Path(dirpath)
        results: list[dict[str, Any]] = []
        for fpath in sorted(d.iterdir()):
            if not fpath.name.startswith("turn_"):
                continue
            raw = fpath.read_text(encoding="utf-8").strip()
            if raw and not raw.startswith("["):
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            try:
                msgs = json.loads(raw)
                if isinstance(msgs, list):
                    for m in msgs:
                        role = m.get("role", "")
                        if role not in ("user", "assistant"):
                            continue
                        content = m.get("content", "")
                        text = ""
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        if query_lower in text.lower():
                            idx = text.lower().index(query_lower)
                            start = max(0, idx - 40)
                            end = min(len(text), idx + len(query_lower) + 80)
                            results.append({
                                "role": role,
                                "snippet": text[start:end].strip()[:120],
                            })
                            break
            except json.JSONDecodeError:
                continue
        return results

    @staticmethod
    def export_to_markdown(dirpath: str) -> str:
        """Export all turn files in a session directory as a single Markdown string."""
        import json

        from encre.crypto import decrypt
        d = pathlib.Path(dirpath)
        if not d.is_dir():
            raise ValueError(f"Session directory not found: {dirpath}")

        lines: list[str] = []
        lines.append("# Session Export\n")
        lines.append(f"_Session ID: {d.name}_\n")

        turn_files = sorted(
            [p for p in d.iterdir() if p.name.startswith("turn_") and p.suffix == ".json"],
            key=lambda p: p.name,
        )

        turn_num = 0
        for fpath in turn_files:
            raw = fpath.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            if not raw.startswith("["):
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            try:
                turn_msgs = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(turn_msgs, list):
                continue

            turn_num += 1
            lines.append(f"\n## Turn {turn_num}\n")

            for m in turn_msgs:
                role = m.get("role", "")
                content = m.get("content", "")

                if role == "system":
                    continue
                elif role == "user":
                    lines.append(f"### User\n\n{EncreSession._extract_text(content)}\n")
                elif role == "assistant":
                    reasoning = m.get("reasoning_content", "")
                    if reasoning:
                        lines.append(f"<details>\n<summary>Thinking</summary>\n\n{reasoning}\n\n</details>\n")
                    tool_calls = m.get("tool_calls")
                    if content:
                        lines.append(f"### Assistant\n\n{EncreSession._extract_text(content)}\n")
                    if tool_calls:
                        for tc in tool_calls:
                            func = tc.get("function", {})
                            tc_name = func.get("name", "unknown")
                            tc_args = func.get("arguments", "{}")
                            lines.append(f"#### 🔧 Tool: `{tc_name}`\n")
                            try:
                                args_parsed = json.loads(tc_args) if isinstance(tc_args, str) else tc_args
                                lines.append("```json\n" + json.dumps(args_parsed, indent=2, ensure_ascii=False) + "\n```\n")
                            except Exception:
                                lines.append(f"```\n{tc_args}\n```\n")
                elif role == "tool":
                    lines.append(f"#### 📋 Tool Result\n\n```\n{EncreSession._extract_text(content)}\n```\n")

        return "".join(lines)

    @staticmethod
    def _extract_text(content: str | list[dict[str, Any]]) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif b.get("type") == "image_url":
                        parts.append("[Image]")
            return "".join(parts)
        return str(content)

    @staticmethod
    def read_meta(dirpath: str) -> dict[str, Any] | None:
        """Read session meta from a turn directory."""
        import json

        from encre.crypto import decrypt
        mp = pathlib.Path(dirpath) / "meta.json"
        if not mp.exists():
            return None
        raw = mp.read_text(encoding="utf-8").strip()
        if raw and not raw.startswith("{"):
            with contextlib.suppress(Exception):
                raw = decrypt(raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _save_meta_file(self, dirpath: pathlib.Path) -> None:
        import json

        from encre.crypto import encrypt
        payload = json.dumps(self.to_meta_dict(), ensure_ascii=False, separators=(",", ":"))
        # Fail closed: never persist session metadata as plaintext.
        payload = encrypt(payload)
        _atomic_write_text(dirpath / "meta.json", payload)

    def _save_summary_db(self, dirpath: pathlib.Path) -> None:
        """Write summary-panel data into the per-session SQLite store."""
        from encre.session_db import SessionDB

        try:
            db = SessionDB(session_dir=dirpath)
            try:
                db.save(self.plan_items, self.artifacts, self.references)
            finally:
                db.close()
        except Exception:
            logger.exception("[session] failed to write summary db in %s", dirpath)

    def _load_summary_db(self, dirpath: pathlib.Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Read summary-panel data from the per-session SQLite store.

        Returns ``(plan_items, artifacts, references)`` or raises
        ``FileNotFoundError``/any sqlite error so the caller can fall back to
        the meta file (older sessions predate the summary DB).
        """
        from encre.session_db import SessionDB

        db = SessionDB(session_dir=dirpath)
        try:
            return db.load()
        finally:
            db.close()

    def _partition_messages_into_turns(
        self, messages: list[dict[str, Any]] | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Split messages into per‑turn chunks.

        Turn 0: system + first user message.
        Turn N: assistant + tool_results + user guidance messages.

        When *messages* is omitted the live ``self.messages`` list is used and
        the result is cached by ``_messages_version``; a caller-supplied list
        (e.g. the tombstone-filtered copy in ``save_to_dir``) bypasses the
        cache entirely.
        """
        if messages is None:
            if self._turn_partition_cache is not None and self._turn_partition_cache[0] == self._messages_version:
                return self._turn_partition_cache[1]
            messages = self.messages
        if not messages:
            return []

        turns: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        saw_user = False

        for msg in messages:
            role = msg.get("role", "")
            if role == "system" and not saw_user:
                current.append(msg)
                continue
            if role == "user" and not saw_user:
                current.append(msg)
                saw_user = True
                continue
            if role == "assistant" and current and saw_user:
                turns.append(current)
                current = []
            current.append(msg)

        if current:
            turns.append(current)

        if messages is self.messages:
            self._turn_partition_cache = (self._messages_version, turns)
        return turns

    def set_compact_archive(self, messages: list[dict[str, Any]]) -> None:
        self.compact_archive = list(messages)

    def get_compact_archive(self) -> list[dict[str, Any]] | None:
        return self.compact_archive

    def has_compact_archive(self) -> bool:
        return self.compact_archive is not None and len(self.compact_archive) > 0


def _guess_image_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/png")
