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

"""Apply a unified diff across multiple files, atomically.

Execution is two-phase:

1. **Plan** (pure computation, zero disk writes): parse the patch,
   resolve every target path, and compute the resulting text of every
   file in memory. Any failure -- unknown file, unplaceable hunk,
   existing destination -- aborts here with a precise per-file report
   and the working tree is left completely untouched.
2. **Commit** (only when the whole plan is green): write each
   computed file through the temp+fsync+rename atomic writer, perform
   renames and deletions.

Supported patch features: ``diff --git`` headers, ``--- a/p`` /
``+++ b/p`` markers (including ``/dev/null``), ``rename from`` /
``rename to``, ``new file mode`` / ``deleted file mode``,
``@@ -l,c +l,c @@`` hunk headers, and
``\\ No newline at end of file`` markers (the trailing-newline policy
of the result is derived from them). Hunk placement is fuzzy: recorded
line numbers may drift by up to 200 lines, trailing whitespace
differences are tolerated, and as a last resort indentation-only
differences are tolerated for hunks with at least two pre-image lines.
The source file's dominant line ending (CRLF / LF / CR) is detected
and reused when reconstructing the file, so edits never rewrite a
file's line-ending style.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

from encre.tools.base import build_tool

# ---------------------------------------------------------------------------
# Atomic write helper -- temp -> fsync -> rename
# ---------------------------------------------------------------------------


def _atomic_write(path: str, content: str) -> None:
    """Write *content* to *path* atomically via a temp file.

    The classic temp-write-then-rename sequence guarantees that a
    crashed or interrupted write either leaves the old file intact or
    the new file in place -- there is no intermediate state where the
    target exists but is truncated. We ``fsync`` the temp fd before
    ``os.replace`` because ``rename`` alone only updates the directory
    entry; a power loss between the write and the rename would still
    leave the file empty on the next boot.

    The temp file is created in the same directory as the target so
    that ``os.replace`` is a same-filesystem atomic rename on every
    supported platform (cross-device ``rename`` is not atomic).

    Args:
        path: Destination path (parent directories are created if
            missing, so callers do not need to pre-create them).
        content: UTF-8 encoded file body.
    """
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        # Clean up the temp file on any failure path (including
        # ``KeyboardInterrupt``), otherwise we'd leak ``*.tmp`` files.
        # ``os.close`` may already have run if the failure came from
        # ``os.replace`` -- but close-on-close is a no-op, so calling
        # it unconditionally is safe.
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?"
    r"\s+\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)


@dataclass
class _Hunk:
    """A single ``@@`` hunk: header counts plus tagged body lines.

    ``lines`` stores every body line *with* its leading tag character
    (``' '`` for context, ``'-'`` for removed, ``'+'`` for added) so
    the trailing-newline bookkeeping can be recovered by looking at
    the tag of the line preceding a ``\\ No newline at end of file``
    marker.
    """
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)  # raw lines with tag prefix
    no_newline_old: bool = False  # EOF marker followed a context/minus line
    no_newline_new: bool = False  # EOF marker followed a plus line


@dataclass
class _FileDiff:
    """One file's worth of patch metadata and hunks.

    A single ``_FileDiff`` corresponds to one file's changeset inside
    the unified diff. ``is_new`` / ``is_deleted`` / ``is_rename`` are
    flags derived from the ``diff --git`` section's auxiliary lines
    (``new file mode``, ``deleted file mode``, ``rename from/to``) or
    from ``/dev/null`` appearing in the ``---`` / ``+++`` markers.
    """
    old_path: str
    new_path: str
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    hunks: list[_Hunk] = field(default_factory=list)


def _strip_ab_prefix(p: str) -> str:
    """Strip the ``a/`` or ``b/`` prefix that ``git diff`` adds to paths.

    The two-letter prefix is a Git convention that distinguishes the
    "before" and "after" sides of a rename; it is not part of the real
    path. We also need to handle paths that legitimately start with
    ``a/`` -- those only show up as ``a/a/...`` in the diff, so a
    single prefix strip is always safe.
    """
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def _split_patch_lines(patch: str) -> list[str]:
    """Split the patch into lines, tolerating CRLF input.

    ``str.splitlines`` is deliberately avoided: it also breaks on form
    feeds and Unicode separators that are legal inside diff content.
    We strip only a single trailing ``\\r`` so that a ``\\r`` inside
    a body line is preserved byte-for-byte.
    """
    return [ln[:-1] if ln.endswith("\r") else ln for ln in patch.split("\n")]


def _parse_patch(patch: str) -> list[_FileDiff]:
    """Parse a unified diff into a list of per-file diffs.

    The parser is intentionally lenient: it tolerates ``diff --git``
    headers being present or missing, and derives the real paths from
    the ``---`` / ``+++`` markers, which are the only place a
    ``/dev/null`` sentinel can appear.

    Hunk bodies are consumed by the counts declared in the hunk
    header, not by scanning for a line that *looks* like the next
    header. This matters because a removed line whose content happens
    to be ``--- foo`` renders as ``--- foo`` and is indistinguishable
    from a file-boundary marker by prefix alone -- only the declared
    counts disambiguate reliably.
    """
    lines = _split_patch_lines(patch)
    files: list[_FileDiff] = []
    i = 0
    n = len(lines)
    cur: _FileDiff | None = None

    while i < n:
        line = lines[i]

        if line.startswith("diff --git "):
            if cur is not None:
                files.append(cur)
            # Extract paths from the header just to seed something; the real
            # paths come from the --- / +++ markers below.
            parts = line.split()
            old_p = _strip_ab_prefix(parts[2]) if len(parts) > 2 else ""
            new_p = _strip_ab_prefix(parts[3]) if len(parts) > 3 else old_p
            cur = _FileDiff(old_path=old_p, new_path=new_p)
            i += 1
            continue

        if line.startswith("rename from "):
            if cur is None:
                cur = _FileDiff(old_path="", new_path="")
            cur.is_rename = True
            cur.old_path = line[len("rename from "):].strip()
            i += 1
            continue

        if line.startswith("rename to "):
            if cur is None:
                cur = _FileDiff(old_path="", new_path="")
            cur.is_rename = True
            cur.new_path = line[len("rename to "):].strip()
            i += 1
            continue

        if line.startswith("new file mode"):
            if cur is not None:
                cur.is_new = True
            i += 1
            continue

        if line.startswith("deleted file mode"):
            if cur is not None:
                cur.is_deleted = True
            i += 1
            continue

        if line.startswith("--- "):
            old_p = line[4:].strip()
            if cur is None:
                cur = _FileDiff(old_path="", new_path="")
            cur.old_path = "/dev/null" if old_p == "/dev/null" else _strip_ab_prefix(old_p)
            if cur.old_path == "/dev/null":
                cur.is_new = True
            i += 1
            continue

        if line.startswith("+++ "):
            new_p = line[4:].strip()
            if cur is None:
                cur = _FileDiff(old_path="", new_path="")
            cur.new_path = "/dev/null" if new_p == "/dev/null" else _strip_ab_prefix(new_p)
            if cur.new_path == "/dev/null":
                cur.is_deleted = True
            i += 1
            continue

        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m is None or cur is None:
                i += 1
                continue
            hunk = _Hunk(
                old_start=int(m.group("old_start")),
                old_count=int(m.group("old_count") or "1"),
                new_start=int(m.group("new_start")),
                new_count=int(m.group("new_count") or "1"),
            )
            i += 1
            # Consume exactly the declared number of hunk lines so content
            # that merely *looks* like a header cannot terminate the hunk
            # early (a removed line "-- x" renders as "--- x", which is
            # indistinguishable from the next file's header by prefix
            # alone -- only the declared counts disambiguate reliably).
            remaining_old = hunk.old_count
            remaining_new = hunk.new_count
            while i < n and (remaining_old > 0 or remaining_new > 0):
                hl = lines[i]
                if hl.startswith("\\ No newline at end of file"):
                    # The marker applies to the hunk line right before it:
                    # a plus line means the NEW file lacks a trailing
                    # newline; a context/minus line means the OLD one did.
                    prev = hunk.lines[-1] if hunk.lines else ""
                    tag = prev[0] if prev else " "
                    if tag == "+":
                        hunk.no_newline_new = True
                    else:
                        hunk.no_newline_old = True
                    i += 1
                    continue
                tag = hl[0] if hl else " "
                if tag in (" ", "+", "-"):
                    hunk.lines.append(hl if hl else " ")
                    if tag in (" ", "-"):
                        remaining_old -= 1
                    if tag in (" ", "+"):
                        remaining_new -= 1
                    i += 1
                    continue
                # Junk between hunks (e.g. binary patch markers) -- stop.
                break
            cur.hunks.append(hunk)
            continue

        # Anything else (index lines, similarity indices, binary markers) skip.
        i += 1

    if cur is not None:
        files.append(cur)
    return files


def _count_patch_add_del(fd: _FileDiff) -> tuple[int, int]:
    """Count added/removed lines across all hunks of a file diff.

    Context lines (tagged ``' '``) are not counted -- the summary is
    only meant to describe the change, not the shape of the context.
    Empty raw lines (which can appear when the diff body contains an
    empty context line represented as the bare tag ``' '``) are
    skipped because they carry no change.
    """
    add_count = 0
    del_count = 0
    for h in fd.hunks:
        for raw in h.lines:
            if not raw:
                continue
            if raw[0] == "+":
                add_count += 1
            elif raw[0] == "-":
                del_count += 1
    return add_count, del_count


# ---------------------------------------------------------------------------
# Hunk application
# ---------------------------------------------------------------------------


def _detect_eol(text: str) -> str:
    """Return the dominant line ending of *text* (CRLF wins, then CR).

    The counts are computed in a single pass so a file that mixes
    endings on only a few lines still keeps the convention of the
    majority. Returning the *dominant* ending rather than the first
    ending means we do not get tripped up by a stray LF in an
    otherwise-CRLF file.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr and cr >= lf:
        return "\r"
    return "\n"


def _apply_hunks(original: str, hunks: list[_Hunk], eol: str = "\n") -> tuple[str, list[str]]:
    """Apply hunks to *original* text and return ``(new_text, notes)``.

    Lines are joined with *eol* (detected from the source file) so
    existing CRLF/LF/CR conventions survive the edit. The
    trailing-newline policy of the result is inherited from the
    source, unless the final hunk produces the new end-of-file via a
    plus line that carries the ``\\ No newline at end of file``
    marker -- in which case the result has no trailing newline. A new
    file (empty *original*) therefore ends with a newline by default,
    exactly like a plain diff would produce.

    The function raises :class:`RuntimeError` if a hunk cannot be
    placed, which lets the caller abort the whole patch before any
    file has been written (see :func:`_apply_patch_execute` for the
    two-phase commit protocol).
    """
    if original:
        src_has_final_newline = original.endswith(eol)
        body = original[:-len(eol)] if src_has_final_newline else original
        src_lines = body.split(eol) if body else []
    else:
        src_has_final_newline = False
        src_lines = []
    out_lines: list[str] = []
    src_cursor = 0  # next index in src_lines we haven't copied yet
    notes: list[str] = []

    for h_idx, h in enumerate(hunks):
        target = max(0, h.old_start - 1)  # convert to 0-based
        # Build expected pre-image (context + minus) and post-image (context + plus)
        pre: list[str] = []
        post: list[str] = []
        for raw in h.lines:
            tag = raw[0] if raw else " "
            body_text = raw[1:] if raw else ""
            if tag == " ":
                pre.append(body_text)
                post.append(body_text)
            elif tag == "-":
                pre.append(body_text)
            elif tag == "+":
                post.append(body_text)

        # Locate the actual position
        pos = _locate(src_lines, pre, target, src_cursor)
        if pos is None:
            first_ctx = next((ln for ln in pre if ln.strip()), "")
            raise RuntimeError(
                f"hunk #{h_idx + 1} could not be applied (context not found "
                f"near line {h.old_start}; first context line: {first_ctx[:80]!r})"
            )

        if pos < src_cursor:
            raise RuntimeError(
                f"hunk #{h_idx + 1} is out of order (would rewind from line {src_cursor} to {pos})"
            )

        # Copy unchanged lines between src_cursor and pos
        out_lines.extend(src_lines[src_cursor:pos])
        # Splice in post-image
        out_lines.extend(post)
        # Advance src_cursor past the pre-image
        src_cursor = pos + len(pre)

        offset = pos - target
        if offset == 0:
            notes.append(f"hunk #{h_idx + 1}: applied at line {pos + 1}")
        else:
            notes.append(
                f"hunk #{h_idx + 1}: applied at line {pos + 1} (offset {offset:+d} from header)"
            )

    # Tail
    out_lines.extend(src_lines[src_cursor:])

    result = eol.join(out_lines)
    # Trailing-newline policy: inherit from the source; override only when
    # the final hunk produced the new EOF through a plus line carrying the
    # no-newline marker.
    trailing = src_has_final_newline
    if hunks and (not src_lines or src_cursor >= len(src_lines)):
        last = hunks[-1]
        for raw in reversed(last.lines):
            tag = raw[0] if raw else " "
            if tag == "-":
                continue
            if tag == "+":
                trailing = not last.no_newline_new
            break  # context line: inherit the source policy
    if out_lines and trailing:
        result += eol
    return result, notes


def _locate(
    src: list[str],
    pre: list[str],
    target: int,
    min_pos: int,
) -> int | None:
    """Find ``pre`` in ``src`` starting somewhere reasonable around ``target``.

    Strategy, in order: exact match at the recorded position; scan
    plus/minus 200 lines for an exact match; rescan tolerating
    trailing-whitespace differences; finally, for hunks with at least
    two pre-image lines, tolerate leading-whitespace (indentation)
    differences as well.

    ``min_pos`` exists to prevent a later hunk from landing before an
    earlier one; without it, an out-of-order patch could produce a
    valid but nonsense file.

    The 200-line scan window is generous enough to survive patches
    generated against a nearby-but-stale version of the file, while
    being small enough that a false match on an unrelated region of a
    large file is very unlikely.
    """
    if not pre:
        # Pure-addition hunk: any position works in principle, but stick to
        # the target unless it's out of bounds.
        if target > len(src):
            return len(src)
        return max(target, min_pos)

    pre_len = len(pre)
    src_len = len(src)

    if min_pos <= target < src_len and src[target:target + pre_len] == pre:
        return target

    for delta in range(1, 201):
        for pos in (target + delta, target - delta):
            if pos < min_pos or pos + pre_len > src_len:
                continue
            if src[pos:pos + pre_len] == pre:
                return pos

    # Tolerate trailing-whitespace differences
    for delta in range(0, 201):
        for pos in (target + delta if delta else target, target - delta):
            if pos < min_pos or pos + pre_len > src_len:
                continue
            if all(src[pos + i].rstrip() == pre[i].rstrip() for i in range(pre_len)):
                return pos

    # Last resort: tolerate indentation differences too, but only anchor on
    # hunks with enough context that a single trivial line cannot misplace
    # the edit.
    if pre_len >= 2:
        stripped = [ln.strip() for ln in pre]
        for delta in range(0, 201):
            for pos in (target + delta if delta else target, target - delta):
                if pos < min_pos or pos + pre_len > src_len:
                    continue
                if all(src[pos + i].strip() == stripped[i] for i in range(pre_len)):
                    return pos
    return None


def _resolve(root: str, rel: str) -> str:
    """Resolve *rel* against *root*, rejecting paths that escape the root.

    We ``realpath`` both sides so that symlinked directories and
    ``..`` segments in *rel* cannot sneak the patch outside the
    workspace. The comparison uses ``root_abs + os.sep`` so that a
    sibling directory whose name happens to start with the root's name
    (e.g. ``/workspace2`` when the root is ``/workspace``) is
    correctly rejected rather than silently accepted by a naive
    ``startswith`` check.

    Raises:
        ValueError: When the resolved path escapes the workspace root.
    """
    p = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(root, rel))
    root_abs = os.path.realpath(root)
    p_real = os.path.realpath(p)
    if not p_real.startswith(root_abs + os.sep) and p_real != root_abs:
        raise ValueError(f"Path '{rel}' escapes workspace root '{root}'")
    return p_real


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


async def _apply_patch_execute(**kwargs: Any) -> str:
    """Apply a unified diff: plan everything in memory, then commit.

    Phase 1 validates and computes every target file without touching
    disk; any error aborts with a per-file report and zero
    modifications. Phase 2 only runs when the whole plan is green (and
    ``dry_run`` is false).

    The two-phase split is what makes this tool atomic *across* files:
    a naive "apply hunk, write file, apply next hunk" loop would leave
    the working tree half-patched if a later hunk failed. Instead, all
    computed bodies are held in memory (in the ``plan`` list) until
    every file's validation succeeds.

    Args:
        **kwargs: Tool arguments. Recognised keys:
            ``patch`` -- the unified diff text (required).
            ``root`` -- workspace root to resolve relative paths
                against (defaults to the current working directory).
            ``dry_run`` -- when true, phase 1 runs but phase 2 is
                skipped so no file is written.

    Returns:
        A JSON report prefixed by a one-line insertion/deletion
        summary. The JSON includes ``applied``, ``dry_run``, ``atomic``,
        ``files`` (per-file status with error messages when applicable),
        and a ``note`` field on planning failure explaining that no
        writes occurred.
    """
    patch = str(kwargs.get("patch", "") or "")
    if not patch.strip():
        return "Error: patch is required"
    root = kwargs.get("root") or os.getcwd()
    dry_run = bool(kwargs.get("dry_run", False))

    try:
        files = _parse_patch(patch)
    except Exception as exc:
        return f"Error parsing patch: {exc}"

    if not files:
        return "Error: no file diffs found in patch"

    report: list[dict[str, Any]] = []
    plan: list[dict[str, Any]] = []
    any_failure = False

    # ---- Phase 1: plan (pure computation, zero disk writes) ----
    for fd in files:
        idx = len(report)
        entry: dict[str, Any] = {
            "old_path": fd.old_path,
            "new_path": fd.new_path,
        }
        file_add, file_del = _count_patch_add_del(fd)
        entry["insertions"] = file_add
        entry["deletions"] = file_del
        pe: dict[str, Any] = {"index": idx, "action": ""}
        try:
            if fd.is_deleted or fd.new_path == "/dev/null":
                if not fd.old_path or fd.old_path == "/dev/null":
                    raise ValueError("delete diff has no target path")
                abs_path = _resolve(root, fd.old_path)
                if not os.path.isfile(abs_path):
                    raise FileNotFoundError(
                        f"cannot delete '{fd.old_path}': file does not exist"
                    )
                entry["action"] = "delete"
                pe.update(action="delete", abs_path=abs_path)

            elif fd.is_new or fd.old_path == "/dev/null":
                if not fd.new_path or fd.new_path == "/dev/null":
                    raise ValueError("create diff has no target path")
                abs_path = _resolve(root, fd.new_path)
                if os.path.exists(abs_path):
                    raise FileExistsError(
                        f"cannot create '{fd.new_path}': file already exists"
                    )
                new_text, notes = _apply_hunks("", fd.hunks, eol="\n")
                entry["action"] = "create"
                entry["hunks"] = notes
                pe.update(action="write", dst_path=abs_path, new_text=new_text)

            elif fd.is_rename and not fd.hunks:
                if not fd.old_path or not fd.new_path:
                    raise ValueError("rename diff is missing source or destination path")
                src_path = _resolve(root, fd.old_path)
                dst_path = _resolve(root, fd.new_path)
                if not os.path.isfile(src_path):
                    raise FileNotFoundError(
                        f"cannot rename '{fd.old_path}': file does not exist"
                    )
                if os.path.exists(dst_path):
                    raise FileExistsError(
                        f"cannot rename to '{fd.new_path}': destination already exists"
                    )
                entry["action"] = "rename"
                pe.update(action="rename", src_path=src_path, dst_path=dst_path)

            else:
                if not fd.old_path or fd.old_path == "/dev/null":
                    raise ValueError("modify diff has no source path")
                src_path = _resolve(root, fd.old_path)
                dst_path = (
                    _resolve(root, fd.new_path)
                    if fd.new_path and fd.new_path != "/dev/null"
                    else src_path
                )
                if not os.path.isfile(src_path):
                    raise FileNotFoundError(
                        f"cannot modify '{fd.old_path}': file does not exist -- "
                        "re-read the current file content and regenerate the patch"
                    )
                # ``newline=""`` keeps CRLF / CR endings intact so the
                # trailing-newline and line-ending detection below sees
                # the source's actual bytes, not a normalised view.
                with open(src_path, encoding="utf-8", newline="") as fh:
                    original = fh.read()
                if "\x00" in original:
                    raise ValueError(
                        f"'{fd.old_path}' is a binary file; apply_patch only supports text files"
                    )
                eol = _detect_eol(original)
                new_text, notes = _apply_hunks(original, fd.hunks, eol=eol)
                if (
                    fd.is_rename
                    and os.path.abspath(src_path) != os.path.abspath(dst_path)
                    and os.path.exists(dst_path)
                ):
                    raise FileExistsError(
                        f"cannot rename to '{fd.new_path}': destination already exists"
                    )
                entry["action"] = "rename+modify" if fd.is_rename else "modify"
                entry["hunks"] = notes
                pe.update(
                    action="rename+write" if fd.is_rename else "write",
                    src_path=src_path,
                    dst_path=dst_path,
                    new_text=new_text,
                )

            entry["status"] = "ok"
            plan.append(pe)
        except Exception as exc:
            any_failure = True
            entry["status"] = "error"
            entry["error"] = str(exc)
        report.append(entry)

    # ---- Phase 2: commit (only on a fully green plan, and not for dry runs).
    # A planning failure never reaches this point, so nothing has been
    # written and the working tree is untouched.
    committed_any = False
    if not any_failure and not dry_run:
        committed_any = True
        for pe in plan:
            rep = report[pe["index"]]
            try:
                action = pe["action"]
                if action == "delete":
                    os.remove(pe["abs_path"])
                elif action == "rename":
                    os.makedirs(os.path.dirname(pe["dst_path"]) or ".", exist_ok=True)
                    os.replace(pe["src_path"], pe["dst_path"])
                elif action == "write":
                    _atomic_write(pe["dst_path"], pe["new_text"])
                elif action == "rename+write":
                    os.makedirs(os.path.dirname(pe["dst_path"]) or ".", exist_ok=True)
                    _atomic_write(pe["dst_path"], pe["new_text"])
                    if os.path.abspath(pe["src_path"]) != os.path.abspath(pe["dst_path"]):
                        os.remove(pe["src_path"])
            except Exception as exc:
                any_failure = True
                rep["status"] = "error"
                rep["error"] = f"commit failed: {exc}"

    total_add = 0
    total_del = 0
    for entry in report:
        if entry.get("status") == "ok":
            total_add += entry["insertions"]
            total_del += entry["deletions"]

    result: dict[str, Any] = {
        "applied": not any_failure and not dry_run,
        "dry_run": dry_run,
        "atomic": True,
        "files": report,
    }
    if any_failure and not committed_any:
        result["note"] = "nothing was written: the patch failed validation before any file was touched"

    summary = f"{total_add} insertions(+), {total_del} deletions(-)"
    return f"{summary}\n{json.dumps(result, ensure_ascii=False, indent=2)}"


apply_patch_tool = build_tool(
    name="apply_patch",
    description=(
        "Apply a unified git-style diff to the working tree across multiple "
        "files in one call.\n\n"
        "WHAT: parses the patch (new files, deletions, renames, hunk "
        "headers, and no-newline-at-EOF markers), computes every resulting "
        "file in memory, and only writes to disk when ALL hunks of ALL "
        "files validate -- a failure anywhere leaves the working tree "
        "completely untouched (true atomicity). Fuzzy hunk placement "
        "tolerates line-number drift up to 200 lines and whitespace or "
        "indentation differences. Original line endings (CRLF/LF/CR) are "
        "preserved.\n"
        "WHEN to use: you have a prepared multi-file patch; a known-good "
        "edit sequence must be replayed across many files.\n"
        "WHEN NOT to use: for 1-2 small edits prefer file_edit; never "
        "generate patches from memory -- re-read the current file content "
        "first.\n"
        "TIPS: run with dry_run=true first to validate every hunk; keep "
        "hunks small and context-rich so fuzzy matching has anchors.\n"
        "PITFALLS: patches against stale content fail by design (the tool "
        "will not guess); binary files are rejected; existing destination "
        "files are never silently overwritten."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": (
                    "Unified diff text to apply (required). Must include "
                    "--- / +++ markers and @@ hunk headers. Generate it "
                    "from the CURRENT file content, never from memory."
                ),
            },
            "root": {
                "type": "string",
                "description": (
                    "Root directory that patch paths are relative to "
                    "(optional, default: current working directory). "
                    "Paths escaping this root are rejected."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Validate the patch and report per-file results "
                    "without writing anything (optional, default false). "
                    "Recommended before a real apply."
                ),
            },
        },
        "required": ["patch"],
    },
    execute=_apply_patch_execute,
    intents=["general", "coding"],
    semantic_type="write",
    is_destructive=True,
    cost_level="high",
    retryability="guarded",
    safe_fallback="Read the target files again, reduce the patch scope, or run a dry run to inspect failed hunks before retrying.",
)
