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

"""Standing goal for plan and spec modes.

An approved plan or specification is not a one-shot approval: it becomes the
session's standing objective.  The goal carries the work checklist and
outlives every run, turn and reload because it is persisted on
``Session.metadata`` under :data:`GOAL_META_KEY`.  The stop path reads it: while
items remain the turn is pushed to the next one, and the mode is left only once
every item is done -- or when the user exits the mode by hand.

Item statuses reuse the ``todo`` tool vocabulary (``pending`` / ``active`` /
``done``) so a live task list and the goal describe the same work with the same
words; :meth:`ActiveGoal.sync_items` merges the tool's list into the goal rather
than keeping a competing record.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from encre.logging_config import get_logger

logger = get_logger(__name__)

#: ``Session.metadata`` key holding the serialised goal.
GOAL_META_KEY = "active_goal"

PENDING = "pending"
ACTIVE = "active"
DONE = "done"

#: Cap on checklist size.  A plan that decomposes past this is truncated: the
#: goal is a stop gate, and an unbounded item list would make the gate
#: meaningless (and the prompt block enormous).
MAX_ITEMS = 40

#: Per-file read cap when deriving items from artifacts (bytes).
_MAX_ARTIFACT_BYTES = 400_000

_RE_CHECKBOX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[([ xX~\-])\]\s*(.+?)\s*$")
_RE_HEADING = re.compile(r"^#{2,4}\s+(.+?)\s*$")
_RE_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_RE_BULLET = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")

#: UI setting key, per goal kind, holding the user-configured turn budget for
#: the state machine (对话流 panel).  Semantics: unset/empty = automatic
#: default (the checklist-scaled stall budget below), 0 = never auto-push,
#: -1 = unlimited, a positive integer = that many pushes at most.
_TURN_LIMIT_KEYS: dict[str, str] = {
    "plan": "plan_mode_max_turns",
    "spec": "spec_mode_max_turns",
}


def _configured_turn_limit(kind: str) -> int | None:
    """User-configured turn budget for *kind*, read live from the UI store.

    ``None`` means "not configured" — the automatic default policy applies.
    Read on every call rather than cached: a standing goal outlives runs and
    reloads, and a cached value could survive a settings change indefinitely.
    Any failure degrades to ``None`` so a broken store can never wedge the
    drive loop.
    """
    key = _TURN_LIMIT_KEYS.get(kind)
    if not key:
        return None
    try:
        from encre.config_store import load_settings_file
        raw = load_settings_file().get(key)
    except Exception:
        return None
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_STATUS_ALIASES: dict[str, str] = {
    "": PENDING,
    "pending": PENDING,
    "todo": PENDING,
    "open": PENDING,
    "not_started": PENDING,
    "in_progress": ACTIVE,
    "active": ACTIVE,
    "doing": ACTIVE,
    "wip": ACTIVE,
    "completed": DONE,
    "complete": DONE,
    "done": DONE,
    "finished": DONE,
    "resolved": DONE,
    "x": DONE,
}

# Section headings that are document furniture rather than deliverables; they
# are skipped when headings are all a spec gives us to work with.
_STRUCTURAL_HEADINGS = frozenset({
    "overview", "summary", "feedback", "status", "checklist", "notes",
    "appendix", "reference", "references", "revision history",
})


def normalize_status(value: Any) -> str:
    """Map any status spelling onto ``pending`` / ``active`` / ``done``."""
    return _STATUS_ALIASES.get(str(value or "").strip().lower(), PENDING)


def normalize_text(value: Any) -> str:
    """Fold whitespace/punctuation so two spellings of one item compare equal."""
    return " ".join(str(value or "").split()).strip().rstrip(".;:。；：,，").lower()


@dataclass
class GoalItem:
    """One deliverable of the standing goal."""

    id: str
    text: str
    status: str = PENDING

    @property
    def done(self) -> bool:
        return self.status == DONE

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "status": self.status}

    @classmethod
    def from_dict(cls, raw: Any) -> GoalItem | None:
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("text") or raw.get("content") or "").strip()
        if not text:
            return None
        return cls(
            id=str(raw.get("id") or "").strip() or f"goal-{normalize_text(text)[:24]}",
            text=text,
            status=normalize_status(raw.get("status")),
        )


@dataclass
class ActiveGoal:
    """The session's approved objective, plus the drive bookkeeping.

    ``nudges`` counts consecutive pushes that produced no progress (no item
    turned done).  Any progress resets it, so a long objective keeps being
    driven for as long as it advances, while a model that has genuinely stalled
    is stopped with an explicit ``goal_incomplete`` finish instead of an
    endless push loop.
    """

    kind: str  # "plan" | "spec"
    title: str = ""
    items: list[GoalItem] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    approved: bool = True
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    nudges: int = 0
    progress_mark: int = 0
    #: Total pushes ever recorded (survives reloads via ``to_dict``).  The
    #: user-configured turn cap counts against this, not the stall streak.
    pushes_total: int = 0

    # ── Progress ─────────────────────────────────────────────────────

    @property
    def total(self) -> int:
        return len(self.items)

    def done_count(self) -> int:
        return sum(1 for i in self.items if i.done)

    def pending_items(self) -> list[GoalItem]:
        return [i for i in self.items if not i.done]

    def next_pending(self) -> GoalItem | None:
        pending = self.pending_items()
        return pending[0] if pending else None

    def is_complete(self) -> bool:
        return bool(self.items) and not self.pending_items()

    @property
    def drives(self) -> bool:
        """True when this goal is a live stop condition (approved + items)."""
        return bool(self.approved and self.items)

    # ── Live sync with the todo list ─────────────────────────────────

    def sync_items(self, raw_items: Any) -> bool:
        """Merge a live plan-items list into the goal's checklist.

        Statuses follow the incoming list by id (falling back to normalised
        text) so checking an item off in the ``todo`` tool advances the goal.
        Items the list adds are adopted as pending work.  Items the list drops
        keep their recorded status: the goal is a superset of any single
        snapshot, so a model that trims its list cannot silently shorten the
        objective it was approved for.
        """
        if not isinstance(raw_items, list):
            return False
        by_id = {i.id: i for i in self.items}
        by_text = {normalize_text(i.text): i for i in self.items}
        changed = False
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or raw.get("content") or "").strip()
            if not text:
                continue
            status = normalize_status(raw.get("status"))
            item = by_id.get(str(raw.get("id") or "").strip()) or by_text.get(normalize_text(text))
            if item is None:
                if len(self.items) >= MAX_ITEMS:
                    continue
                item = GoalItem(
                    id=str(raw.get("id") or "").strip() or f"goal-{len(self.items) + 1}",
                    text=text,
                    status=status,
                )
                self.items.append(item)
                by_id[item.id] = item
                by_text[normalize_text(item.text)] = item
                changed = True
                continue
            if status != item.status:
                item.status = status
                changed = True
        if changed:
            self.updated_at = time.time()
        return changed

    # ── Push budget ──────────────────────────────────────────────────

    @property
    def nudge_budget(self) -> int:
        """Pushes allowed before a no-progress run counts as stalled."""
        return max(2 * max(self.total, 1), 8)

    def note_push(self) -> bool:
        """Record one push; ``False`` once the drive may no longer continue.

        Gates, in order: an unset budget keeps the automatic stall budget
        below (the checklist-scaled default); ``0`` disables auto-push
        entirely; ``-1`` runs unlimited; a positive value caps the pushes
        this goal may ever make.  Exceeding any gate stops the drive with
        the usual ``goal_incomplete`` path.
        """
        limit = _configured_turn_limit(self.kind)
        if limit is not None and limit == 0:
            return False
        done = self.done_count()
        if done > self.progress_mark:
            self.progress_mark = done
            self.nudges = 0
        self.nudges += 1
        self.pushes_total += 1
        self.updated_at = time.time()
        if limit is not None:
            if limit < 0:
                return True
            if self.pushes_total > limit:
                return False
            return True
        return self.nudges <= self.nudge_budget

    # ── Serialisation ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "items": [i.to_dict() for i in self.items],
            "files": list(self.files),
            "approved": bool(self.approved),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "nudges": self.nudges,
            "progress_mark": self.progress_mark,
            "pushes_total": self.pushes_total,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ActiveGoal | None:
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "").strip()
        if kind not in ("plan", "spec"):
            return None
        items: list[GoalItem] = []
        for entry in raw.get("items") or []:
            item = GoalItem.from_dict(entry)
            if item is not None:
                items.append(item)
        return cls(
            kind=kind,
            title=str(raw.get("title") or ""),
            items=items,
            files=[str(f) for f in (raw.get("files") or [])],
            approved=bool(raw.get("approved", True)),
            started_at=float(raw.get("started_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            nudges=int(raw.get("nudges") or 0),
            progress_mark=int(raw.get("progress_mark") or 0),
            pushes_total=int(raw.get("pushes_total") or 0),
        )


# ── Item extraction ──────────────────────────────────────────────────


def _read_artifact_lines(files: Any) -> list[str]:
    """Read the reviewed artifacts, returning their lines (never raises)."""
    lines: list[str] = []
    for path in files or []:
        try:
            with open(str(path), encoding="utf-8", errors="replace") as fh:
                text = fh.read(_MAX_ARTIFACT_BYTES)
        except OSError:
            continue
        lines.extend(text.splitlines())
    return lines


def _dedupe(items: list[GoalItem]) -> list[GoalItem]:
    seen: set[str] = set()
    out: list[GoalItem] = []
    for i, item in enumerate(items):
        key = normalize_text(item.text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(GoalItem(id=item.id or f"goal-{i + 1}", text=item.text, status=item.status))
    return out


def _from_plan_items(raw_items: Any) -> list[GoalItem]:
    out: list[GoalItem] = []
    for raw in raw_items or []:
        item = GoalItem.from_dict(raw)
        if item is not None:
            out.append(item)
    return out


def _from_checkboxes(lines: list[str]) -> list[GoalItem]:
    out: list[GoalItem] = []
    for line in lines:
        m = _RE_CHECKBOX.match(line)
        if not m:
            continue
        mark = m.group(1)
        status = DONE if mark.lower() == "x" else (ACTIVE if mark == "~" else PENDING)
        out.append(GoalItem(id="", text=m.group(2).strip(), status=status))
    return out


def _from_sections(spec_sections: Any) -> list[GoalItem]:
    out: list[GoalItem] = []
    for section in spec_sections or []:
        title = (
            str(section.get("title") or "").strip()
            if isinstance(section, dict)
            else str(getattr(section, "title", "") or "").strip()
        )
        if title:
            out.append(GoalItem(id="", text=title, status=PENDING))
    return out


def _from_headings(lines: list[str]) -> list[GoalItem]:
    out: list[GoalItem] = []
    for line in lines:
        m = _RE_HEADING.match(line)
        if not m:
            continue
        title = m.group(1).strip()
        if normalize_text(title) in _STRUCTURAL_HEADINGS:
            continue
        out.append(GoalItem(id="", text=title, status=PENDING))
    return out


def _from_numbered(lines: list[str]) -> list[GoalItem]:
    out: list[GoalItem] = []
    for line in lines:
        m = _RE_NUMBERED.match(line) or _RE_BULLET.match(line)
        if not m:
            continue
        text = m.group(1).strip()
        if len(text) < 3:
            continue
        out.append(GoalItem(id="", text=text, status=PENDING))
    return out


def extract_items(
    *,
    files: Any = None,
    plan_items: Any = None,
    spec_sections: Any = None,
) -> list[GoalItem]:
    """Build the goal checklist from the best available source.

    Priority: the live ``todo`` list the model already maintains (session
    persisted, UI visible, statuses current), then markdown checkboxes in the
    reviewed artifacts, then the approved spec's sections, then artifact
    headings, then a numbered/bulleted list.  Returns an empty list when the
    artifacts declare no work -- in that case the caller keeps the old
    single-pass behaviour instead of inventing items.
    """
    items = _from_plan_items(plan_items)
    if not items:
        items = _from_checkboxes(_read_artifact_lines(files))
    if not items:
        items = _from_sections(spec_sections)
    if not items:
        lines = _read_artifact_lines(files)
        items = _from_headings(lines) or _from_numbered(lines)
    return _dedupe(items)[:MAX_ITEMS]


# ── Prompt / error text ──────────────────────────────────────────────


def _pending_preview(goal: ActiveGoal, limit: int) -> list[str]:
    return [i.text for i in goal.pending_items()[1 : limit + 1]]


def build_continue_message(goal: ActiveGoal) -> str:
    """The nudge pushed when the model tries to stop with items outstanding."""
    nxt = goal.next_pending()
    if nxt is None:
        return ""
    lines = [
        f"[GOAL {goal.kind.upper()}] {goal.done_count()}/{goal.total} items done -- keep going.",
        f"Next item: {nxt.text}",
    ]
    rest = _pending_preview(goal, 12)
    if rest:
        lines.append("Still outstanding after that:")
        lines.extend(f"  - {t}" for t in rest)
    lines.append(
        "Do that item now. This goal is not finished until every item is done "
        "and verified -- do not stop to summarise, do not ask whether to "
        "continue, do not hand back a partial result."
    )
    return "\n".join(lines)


def build_complete_message(goal: ActiveGoal) -> str:
    """The note left in context when every item is done."""
    return (
        f"[GOAL {goal.kind.upper()}] All {goal.total} items are done. The "
        f"{goal.kind} mode is now off and this goal is closed; report the "
        "outcome of the whole objective in one short message."
    )


def build_incomplete_message(goal: ActiveGoal) -> str:
    """The finish error when the drive stalled with items outstanding."""
    nxt = goal.next_pending()
    remaining = goal.total - goal.done_count()
    return (
        f"goal_incomplete: {remaining} of {goal.total} items are unfinished and "
        f"the last {goal.nudges} continued turns made no progress. "
        f"Next unfinished item: {nxt.text if nxt else '(unknown)'}. "
        f"The {goal.kind} mode and this goal stay active until the items are "
        "done or the user exits the mode."
    )
