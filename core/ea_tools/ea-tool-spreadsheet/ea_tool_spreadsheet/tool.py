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

"""Spreadsheet read/write tool.

Handles CSV (via the stdlib ``csv`` module) and Excel ``.xlsx`` /
``.xls`` (via ``openpyxl``) files. Actions:

* ``read``        -- dump header plus up to 100 rows as
                     ``" | "``-separated text so the model can
                     scan the shape of the data without blowing
                     its context window.
* ``write``       -- create or overwrite the file. CSV writes accept
                     raw CSV text; Excel writes accept a JSON array
                     of arrays.
* ``list_sheets`` -- return the workbook's worksheet names for
                     Excel files. CSV files have a single sheet,
                     so this action returns a sentinel message for
                     them.

Design notes
------------

The dispatcher picks the handler purely by file extension
(``os.path.splitext(...)[1].lower()``). The extension is case-
insensitive because Windows filesystems are case-insensitive and
``File.XLSX`` is a legitimate file name on that platform.

The 100-row cap on ``read`` output is a hard invariant: an unbounded
dump of a large spreadsheet would fill the model's context with
repeated rows and hide the actual signal (the header + shape).
The ``"..."`` sentinel reports how many rows were dropped so the
caller can decide whether to filter with ``sheet_name`` or
pre-truncate the file.

``openpyxl`` is imported inside ``_handle_excel`` rather than at
module level so CSV-only workflows do not pay for the Excel
dependency. CSV is stdlib and always available; Excel is an
optional extra.

The ``range`` parameter is intentionally parsed but not enforced on
reads. Full enforcement would require translating Excel A1 notation
into ``openpyxl`` row/column filters and the benefit does not
justified the complexity given the 100-row cap already limits the
blast radius. The description calls this out so callers know the
parameter is advisory.
"""

import csv
import json
import os
from typing import Any

from encre.tools.base import build_tool


async def _spreadsheet_execute(**kwargs: Any) -> str:
    """Dispatch a spreadsheet action by file extension.

    The dispatcher uses only the file extension to route between
    ``_handle_csv`` and ``_handle_excel``. Both handlers return a
    human-readable string so the tool registry does not need to
    serialise a structured result.

    Args:
        kwargs: Tool input keyed by the JSON schema declared on
            ``spreadsheet_tool.input_schema`` below.

    Returns:
        A human-readable result string, or an ``"Error: ..."``
        string when the file format, action, or path is invalid.
    """
    action = kwargs.get("action", "read")
    file_path = kwargs.get("file_path", "")
    sheet_name = kwargs.get("sheet_name", "")
    data = kwargs.get("data", "")
    _range = kwargs.get("range", "")

    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext == ".csv":
            return await _handle_csv(action, file_path, data)
        elif ext in (".xlsx", ".xls"):
            return await _handle_excel(action, file_path, sheet_name, data, _range)
        else:
            return f"Error: Unsupported file format: {ext}. Supported: .csv, .xlsx, .xls"
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error processing spreadsheet: {e}"


async def _handle_csv(action: str, file_path: str, data: str) -> str:
    """Handle CSV read/write/list_sheets actions using the stdlib csv module.

    Reads are capped at 100 rows in the returned output so a large
    CSV does not blow up the model's context window. The remaining
    row count is reported in a ``"..."`` sentinel line so the
    caller knows how much data was truncated.

    Args:
        action: One of ``"read"``, ``"write"``, ``"list_sheets"``.
        file_path: Path to the ``.csv`` file.
        data: For ``write`` actions, the raw CSV text to persist.

    Returns:
        A human-readable result string, or an ``"Error: ..."``
        string on failure.
    """
    if action == "read":
        with open(file_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
        header = rows[0] if rows else []
        data_rows = rows[1:] if len(rows) > 1 else []
        return f"Header: {header}\nRows: {len(data_rows)}\n\n" + "\n".join(
            " | ".join(f"{c}" for c in row) for row in data_rows[:100]
        ) + (f"\n... ({len(data_rows) - 100} more rows)" if len(data_rows) > 100 else "")

    elif action == "write":
        if not data:
            return "Error: No data provided for write"
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            f.write(data)
        return f"Written CSV to {file_path}"

    elif action == "list_sheets":
        return "CSV files have a single sheet only"

    else:
        return f"Error: unknown action {action!r}; supported: read, write, list_sheets"


async def _handle_excel(action: str, file_path: str, sheet_name: str, data: str, _range: str) -> str:
    """Handle Excel read/write/list_sheets actions using openpyxl.

    ``openpyxl`` is imported at call time so CSV-only workflows do
    not require the library. Reads use ``read_only=True`` and
    ``data_only=True`` so the workbook is opened lazily and
    formula results (rather than formula strings) are returned.
    Writes always create a fresh workbook rather than editing in
    place, which is intentional: partial-write semantics with
    ``openpyxl`` are error-prone, and callers can always read the
    existing workbook first if they need to preserve state.

    Args:
        action: One of ``"read"``, ``"write"``, ``"list_sheets"``.
        file_path: Path to the ``.xlsx`` / ``.xls`` file.
        sheet_name: Target worksheet name; empty string selects the
            active sheet on read.
        data: For ``write`` actions, a JSON array of arrays of
            values.
        _range: Cell-range hint (advisory only, not enforced on
            reads).

    Returns:
        A human-readable result string, or an ``"Error: ..."``
        string on failure.
    """
    import openpyxl

    if action == "read":
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        sheet = wb[sheet_name] if sheet_name else wb.active
        rows_data = []
        for row in sheet.iter_rows(values_only=True):
            rows_data.append([str(c) if c is not None else "" for c in row])
        header = rows_data[0] if rows_data else []
        data_rows = rows_data[1:] if len(rows_data) > 1 else []
        output = f"Sheet: {sheet.title}\nHeader: {header}\nRows: {len(data_rows)}\n\n"
        output += "\n".join(" | ".join(row) for row in data_rows[:100])
        if len(data_rows) > 100:
            output += f"\n... ({len(data_rows) - 100} more rows)"
        wb.close()
        return output

    elif action == "write":
        try:
            new_data = json.loads(data)
        except json.JSONDecodeError:
            return "Error: data must be valid JSON (array of arrays)"
        wb = openpyxl.Workbook()
        if sheet_name:
            wb.active.title = sheet_name
        sheet = wb.active
        for row in new_data:
            sheet.append(row)
        wb.save(file_path)
        return f"Written {len(new_data)} rows to {file_path}"

    elif action == "list_sheets":
        wb = openpyxl.load_workbook(file_path, read_only=True)
        sheets = wb.sheetnames
        wb.close()
        return f"Sheets: {json.dumps(sheets)}"

    else:
        return f"Error: unknown action {action!r}; supported: read, write, list_sheets"


spreadsheet_tool = build_tool(
    name="spreadsheet",
    description=(
        "Read, write, or inspect CSV and Excel (.xlsx/.xls) spreadsheet files. "
        "Use this to extract tabular data, dump rows into a sheet, or list "
        "worksheets in a workbook; Excel handling requires openpyxl. "
        "Do NOT use this for Office documents (use document) or PDFs (use pdf); "
        "and avoid it for heavy analytics better suited to pandas/SQL. "
        "Tips: for Excel writes, supply `data` as a JSON array of arrays; CSV writes "
        "accept raw CSV text; reading caps output at 100 rows with a trailing count. "
        "Pitfalls: the `range` parameter is parsed but not enforced on reads - filter "
        "large sheets via `sheet_name` or pre-truncate the file."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write", "list_sheets"],
                "description": "Spreadsheet operation: read (header + up to 100 rows), write (create/overwrite the file), or list_sheets (Excel worksheet names).",
            },
            "file_path": {
                "type": "string",
                "description": "Path to the spreadsheet file; the extension selects the handler (.csv via the csv module, .xlsx/.xls via openpyxl).",
            },
            "sheet_name": {
                "type": "string",
                "description": "Excel worksheet to target; defaults to the active sheet when omitted. Ignored for CSV files.",
            },
            "data": {
                "type": "string",
                "description": "Payload for write: raw CSV text for .csv, or a JSON array of arrays for Excel (e.g. '[[\"a\",1],[\"b\",2]]').",
            },
            "range": {
                "type": "string",
                "description": "Cell range hint for Excel reads (e.g. 'A1:C10'); Excel only and not enforced on read.",
            },
        },
        "required": ["action", "file_path"],
    },
    execute=_spreadsheet_execute,
    intents=["data", "research"],
    category="data",
    semantic_type="read",
    # Write actions overwrite the file in place, so they are
    # marked destructive. Reads and list_sheets are safe.
    is_destructive=lambda args: args.get("action", "") == "write",
    # Reads and writes target distinct files; there is no shared
    # state that could be corrupted by concurrent invocations.
    is_concurrency_safe=lambda _: True,
)
