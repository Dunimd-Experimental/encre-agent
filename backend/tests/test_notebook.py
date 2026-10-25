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

"""Tests for encre.notebook.session -- EncreNotebookSession.

Covers session construction (default and custom kernel names, UUID
session IDs, initial stopped state, empty cell map, and the kernel
bootstrap script markers), cell lifecycle management (creation, editing,
deletion, output/error lookups, and ordered multi-cell notebooks),
state serialisation via get_state, and idempotent shutdown through close.
"""

import uuid


# ---- EncreNotebookSession construction ----


class TestEncreNotebookSessionConstruction:
    """Validate EncreNotebookSession construction invariants.

    The class confirms that sessions default to the python3 kernel,
    accept arbitrary kernel names, generate a unique UUID session ID per
    instance, start in a stopped state with an empty cell map, and
    generate a kernel bootstrap script containing the required
    import sys / exec( / __SHUTDOWN__ markers.
    """

    def test_verify_session_construction_default_kernel(self):
        """Validate that EncreNotebookSession defaults to the python3 kernel."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        assert sess is not None
        assert sess.kernel_name == "python3"

    def test_verify_session_construction_custom_kernel(self):
        """Validate that EncreNotebookSession accepts custom kernel names."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession(kernel_name="python3.12")
        assert sess.kernel_name == "python3.12"

    def test_verify_session_construction_other_python_versions(self):
        """Validate that various python3.X kernel names are accepted verbatim."""
        from encre.notebook.session import EncreNotebookSession
        for kname in ["python3.9", "python3.10", "python3.11", "python3.13"]:
            sess = EncreNotebookSession(kernel_name=kname)
            assert sess.kernel_name == kname

    def test_verify_session_id_is_uuid_string(self):
        """Validate that session_id is a valid UUID string."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        assert isinstance(sess.session_id, str)
        # Parsing must not raise; a malformed ID would fail here.
        uuid.UUID(sess.session_id)

    def test_verify_session_each_has_unique_id(self):
        """Validate that each session instance gets a unique session ID."""
        from encre.notebook.session import EncreNotebookSession
        a = EncreNotebookSession()
        b = EncreNotebookSession()
        assert a.session_id != b.session_id

    def test_verify_session_initial_state_not_started(self):
        """Validate that a new session starts in stopped state with no process."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        assert sess._started is False
        assert sess._process is None

    def test_verify_session_initial_cells_empty(self):
        """Validate that a new session starts with an empty cell map and order list."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        assert sess._cells == {}
        assert sess._cell_order == []

    def test_verify_session_kernel_script_is_python_code(self):
        """Validate that _kernel_script contains the required bootstrap markers."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        assert "import sys" in sess._kernel_script
        assert "exec(" in sess._kernel_script
        assert "__SHUTDOWN__" in sess._kernel_script


# ---- EncreNotebookSession cell management ----


class TestEncreNotebookSessionCells:
    """Validate cell lifecycle operations on EncreNotebookSession.

    Tests cover cell creation (ID generation, default code type, explicit
    markdown type), ordered multi-cell creation, cell editing (code
    update and status/output reset), cell deletion (including preservation
    of sibling order), and non-existent-cell error handling (return False
    or empty string rather than raising).
    """

    def test_verify_cell_create_returns_id(self):
        """Validate that create_cell returns an 8-character string ID."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        cell_id = sess.create_cell(code="print('hello')")
        assert isinstance(cell_id, str)
        assert len(cell_id) == 8

    def test_verify_cell_create_default_type_code(self):
        """Validate that create_cell defaults cell_type to 'code'."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        sess.create_cell(code="x = 1")
        state = sess.get_state()
        cells = state["cells"]
        assert len(cells) == 1
        assert cells[0]["cell_type"] == "code"

    def test_verify_cell_create_markdown_type(self):
        """Validate that create_cell accepts explicit 'markdown' cell type."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        sess.create_cell(code="# Title", cell_type="markdown")
        state = sess.get_state()
        cells = state["cells"]
        assert cells[0]["cell_type"] == "markdown"

    def test_verify_cell_create_multiple_preserves_order(self):
        """Validate that multiple cells preserve creation order in state."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        id1 = sess.create_cell(code="a = 1")
        id2 = sess.create_cell(code="b = 2")
        id3 = sess.create_cell(code="c = 3")
        state = sess.get_state()
        cell_ids = [c["id"] for c in state["cells"]]
        assert cell_ids == [id1, id2, id3]

    def test_verify_cell_edit_changes_code(self):
        """Validate that edit_cell updates the code and returns True."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        cell_id = sess.create_cell(code="original")
        result = sess.edit_cell(cell_id, code="modified")
        assert result is True
        state = sess.get_state()
        assert state["cells"][0]["code"] == "modified"

    def test_verify_cell_edit_nonexistent_returns_false(self):
        """Validate that edit_cell returns False for non-existent cell IDs."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        result = sess.edit_cell("nonexistent", code="x = 1")
        assert result is False

    def test_verify_cell_edit_resets_status_and_outputs(self):
        """Validate that edit_cell resets status to idle and clears output/error."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        cell_id = sess.create_cell(code="print('hi')")
        sess.edit_cell(cell_id, code="print('hello')")
        state = sess.get_state()
        cell = state["cells"][0]
        assert cell["status"] == "idle"
        assert cell["output"] == ""
        assert cell["error"] == ""

    def test_verify_cell_delete_removes_from_state(self):
        """Validate that delete_cell removes the cell and updates cell_count."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        cell_id = sess.create_cell(code="x = 1")
        assert sess.delete_cell(cell_id) is True
        state = sess.get_state()
        assert state["cell_count"] == 0
        assert state["cells"] == []

    def test_verify_cell_delete_nonexistent_returns_false(self):
        """Validate that delete_cell returns False for non-existent cell IDs."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        result = sess.delete_cell("no_such_cell")
        assert result is False

    def test_verify_cell_delete_preserves_order(self):
        """Validate that delete_cell preserves the order of remaining cells."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        id1 = sess.create_cell(code="a = 1")
        id2 = sess.create_cell(code="b = 2")
        id3 = sess.create_cell(code="c = 3")
        sess.delete_cell(id2)
        state = sess.get_state()
        cell_ids = [c["id"] for c in state["cells"]]
        assert cell_ids == [id1, id3]

    def test_verify_cell_get_output_nonexistent_returns_empty(self):
        """Validate that get_output returns empty string for non-existent cells."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        result = sess.get_output("no_such_cell")
        assert result == ""

    def test_verify_cell_get_error_nonexistent_returns_empty(self):
        """Validate that get_error returns empty string for non-existent cells."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        result = sess.get_error("no_such_cell")
        assert result == ""

    def test_verify_cell_get_output_for_existing_cell(self):
        """Validate that get_output returns empty string before execution."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        cell_id = sess.create_cell(code="x = 1")
        # The cell has not been executed yet, so there is no output.
        assert sess.get_output(cell_id) == ""


# ---- EncreNotebookSession state ----


class TestEncreNotebookSessionState:
    """Validate the session state serialisation contract.

    Tests confirm that get_state returns a dict with the required keys
    (session_id, kernel_name, cells, cell_count), that cell entries
    contain all expected fields (id, code, cell_type, output, error,
    status, execution_time), and that newly-created cells start in idle
    status with zero execution time.
    """

    def test_verify_state_get_state_initial(self):
        """Validate that get_state returns correct initial values."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        state = sess.get_state()
        assert state["session_id"] == sess.session_id
        assert state["kernel_name"] == "python3"
        assert state["cells"] == []
        assert state["cell_count"] == 0

    def test_verify_state_get_state_after_creating_cells(self):
        """Validate that get_state reflects created cells with correct code."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        sess.create_cell(code="x = 1")
        sess.create_cell(code="y = 2")
        state = sess.get_state()
        assert state["cell_count"] == 2
        assert len(state["cells"]) == 2
        assert state["cells"][0]["code"] == "x = 1"
        assert state["cells"][1]["code"] == "y = 2"

    def test_verify_state_get_state_keys(self):
        """Validate that get_state returns all required top-level keys."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        state = sess.get_state()
        for key in ["session_id", "kernel_name", "cells", "cell_count"]:
            assert key in state

    def test_verify_state_cell_state_keys(self):
        """Validate that each cell entry contains all required fields."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        sess.create_cell(code="print('hi')")
        state = sess.get_state()
        cell = state["cells"][0]
        for key in ["id", "code", "cell_type", "output", "error", "status", "execution_time"]:
            assert key in cell

    def test_verify_state_cell_initial_status_idle(self):
        """Validate that new cells start in idle status with zero execution time."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        sess.create_cell(code="print('hi')")
        state = sess.get_state()
        assert state["cells"][0]["status"] == "idle"
        assert state["cells"][0]["execution_time"] == 0.0


# ---- EncreNotebookSession close ----


class TestEncreNotebookSessionClose:
    """Validate session shutdown behaviour.

    Tests assert that calling close before the kernel is started is safe
    (no exception, state remains stopped), and that repeated calls are
    idempotent -- a second close must not re-raise or corrupt state.
    """

    def test_verify_close_before_kernel_started_does_not_raise(self):
        """Validate that close before kernel start is safe and idempotent."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        sess.close()
        assert sess._started is False
        assert sess._process is None

    def test_verify_close_is_idempotent(self):
        """Validate that repeated close calls do not raise or corrupt state."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        sess.close()
        sess.close()
        assert sess._started is False
