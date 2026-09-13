"""EA File Edit Tool 鈥?mandatory builtin package."""
from __future__ import annotations

import re
from typing import Any

from encre.native import compute_diff as _native_diff
from encre.native import read_file as _native_read
from encre.native import write_file as _native_write
from encre.tools.base import build_tool
from encre.capabilities.process import remap_tool_path

_WS_RE = re.compile(r"[ \t]+")
_DIFF_ADD_RE = re.compile(r"^\+(?!\+\+)", re.MULTILINE)
_DIFF_DEL_RE = re.compile(r"^-(?!--)", re.MULTILINE)


def _normalize_ws(text: str) -> str:
    return "\n".join(_WS_RE.sub(" ", line).rstrip() for line in text.split("\n"))


def _map_norm_to_orig(orig: str, norm_pos: int) -> int | None:
    cur_norm = 0
    i = 0
    while i < len(orig):
        if cur_norm == norm_pos:
            return i
        ch = orig[i]
        if ch in (" ", "\t"):
            j = i
            while j < len(orig) and orig[j] in (" ", "\t"):
                j += 1
            cur_norm += 1; i = j; continue
        if ch == "\n":
            cur_norm += 1; i += 1; continue
        cur_norm += 1; i += 1
    return len(orig) if cur_norm == norm_pos else None


def _find_with_fallback(haystack: str, needle: str) -> tuple[int, int, str] | None:
    idx = haystack.find(needle)
    if idx != -1:
        return idx, idx + len(needle), needle
    norm_hay, norm_needle = _normalize_ws(haystack), _normalize_ws(needle)
    if not norm_needle:
        return None
    n_idx = norm_hay.find(norm_needle)
    if n_idx == -1:
        return None
    s, e = _map_norm_to_orig(haystack, n_idx), _map_norm_to_orig(haystack, n_idx + len(norm_needle))
    if s is None or e is None or e <= s:
        return None
    return s, e, haystack[s:e]


def _apply_one_edit(content: str, old_str: str, new_str: str, replace_all: bool) -> tuple[str, int, str]:
    if not old_str:
        raise ValueError("old_str must not be empty")
    if replace_all:
        if old_str not in content:
            count, cursor, buf = 0, 0, []
            while cursor < len(content):
                hit = _find_with_fallback(content[cursor:], old_str)
                if hit is None:
                    buf.append(content[cursor:]); break
                s, e, _ = hit
                buf.append(content[cursor:cursor + s]); buf.append(new_str); cursor += e; count += 1
            if count == 0:
                raise ValueError("old_str not found (replace_all)")
            return "".join(buf), count, "replace_all+ws"
        return content.replace(old_str, new_str), content.count(old_str), "replace_all"
    exact_count = content.count(old_str)
    if exact_count == 1:
        return content.replace(old_str, new_str, 1), 1, "exact"
    if exact_count > 1:
        raise ValueError(f"old_str matched {exact_count} times. Use replace_all=true or add context.")
    hit = _find_with_fallback(content, old_str)
    if hit is None:
        raise ValueError("old_str not found (tried whitespace-normalized match)")
    s, e, _ = hit
    if _find_with_fallback(content[e:], old_str) is not None:
        raise ValueError("Match is ambiguous. Add more context or set replace_all=true.")
    return content[:s] + new_str + content[e:], 1, "ws_fallback"


async def _file_edit_execute(**kwargs: Any) -> str:
    file_path = kwargs.get("file_path", "")
    if not file_path:
        return "Error: file_path is required"
    file_path = remap_tool_path(file_path)
    if not file_path:
        return "Error: Path rejected by sandbox"
    dry_run = bool(kwargs.get("dry_run", False))
    tool_call_id = str(kwargs.get("tool_call_id", ""))

    edits_param = kwargs.get("edits")
    if edits_param:
        if not isinstance(edits_param, list):
            return "Error: 'edits' must be an array"
        edits = [(str(e["old_str"]), str(e["new_str"]), bool(e.get("replace_all", False)))
                 for e in edits_param if isinstance(e, dict) and "old_str" in e and "new_str" in e]
        if len(edits) != len(edits_param):
            return "Error: each edit must have 'old_str' and 'new_str'"
    else:
        old_str, new_str = kwargs.get("old_str"), kwargs.get("new_str")
        if old_str is None or new_str is None:
            return "Error: provide either 'edits' or both 'old_str' and 'new_str'"
        edits = [(str(old_str), str(new_str), bool(kwargs.get("replace_all", False)))]

    try:
        original = _native_read(file_path, 0, 0)
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except PermissionError:
        return f"Error: Permission denied: {file_path}"
    except Exception as e:
        return f"Error reading file: {e}"

    content, report = original, []
    for i, (o, n, r) in enumerate(edits):
        try:
            content, count, note = _apply_one_edit(content, o, n, r)
        except ValueError as exc:
            return f"Error: Edit #{i+1} failed: {exc}"
        report.append(f"edit #{i+1}: {count} replacement(s) ({note})")

    if content == original:
        return "No-op: edits produced no change."

    summary = "; ".join(report)
    compute_full_diff = dry_run or len(original) <= 100_000
    add_count = del_count = 0
    if compute_full_diff:
        diff_text = _native_diff(original, content)
        add_count = len(_DIFF_ADD_RE.findall(diff_text))
        del_count = len(_DIFF_DEL_RE.findall(diff_text))
    else:
        orig_lines, new_lines = original.splitlines(), content.splitlines()
        delta = len(new_lines) - len(orig_lines)
        add_count, del_count = max(delta, 0), max(-delta, 0)
        if delta == 0 and original != content:
            changed = sum(1 for a, b in zip(orig_lines, new_lines) if a != b)
            add_count = del_count = changed

    if dry_run:
        import json
        proposal = {"kind": "edit_proposal", "tool_call_id": tool_call_id, "file_path": file_path,
                    "diff_text": diff_text if compute_full_diff else None, "original": original,
                    "proposed": content, "added": add_count, "removed": del_count, "summary": summary}
        return "```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"

    try:
        _native_write(file_path, content)
    except PermissionError:
        return f"Error: Permission denied writing file: {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"

    out = f"Applied {len(edits)} edit(s) to {file_path}.\n{add_count} insertions(+), {del_count} deletions(-)\n" + "\n".join(report)
    if compute_full_diff:
        out += f"\n```diff\n{diff_text}\n```"
    else:
        out += "\n(large file 鈥?diff omitted)"
    return out


file_edit_tool = build_tool(
    name="file_edit",
    description=(
        "Apply search-and-replace edits to an existing file. Supports unique single-match, "
        "replace_all, multi-hunk via 'edits' array, and whitespace-tolerant fallback. "
        "Returns a diff. Use dry_run=true to preview without writing. "
        "Read the file via file_read first; do not re-read if already in context."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file to edit (required)."},
            "old_str": {"type": "string", "description": "Text to replace (single-edit mode)."},
            "new_str": {"type": "string", "description": "Replacement text (single-edit mode)."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
            "edits": {"type": "array", "description": "List of {old_str, new_str, replace_all} hunks."},
            "dry_run": {"type": "boolean", "default": False, "description": "Preview without writing."},
            "tool_call_id": {"type": "string", "description": "Optional tool-call id for UI routing."},
        },
        "required": ["file_path"],
    },
    execute=_file_edit_execute,
    intents=["general", "coding", "data"], category="filesystem",
    triggers=["edit file", "replace text", "search replace"],
    semantic_type="write", is_destructive=True, cost_level="high", retryability="guarded",
    get_effective_path=lambda self, args: args.get("file_path") or None,
)
