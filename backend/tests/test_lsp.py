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

"""Tests for the LSP protocol dataclasses and the client and manager classes.

Covers the wire-shape records used by the search capability (Position, Range,
Location, Diagnostic, HoverResult, LSPState), the EncreLSPClient constructor
and its initial internal state, and the EncreLSPManager construction path, so
that the public surface of encre.lsp is pinned as a data contract.
"""


# ----------------------------------------------------------------------
# Position dataclass
# ----------------------------------------------------------------------

class TestPosition:
    """Validates Position construction and value-equality semantics.

    Exercises the ``Position`` record and checks that line and character fields
    are stored verbatim, that large coordinates do not overflow, that the type
    is a real dataclass, and that value-based equality holds for identical
    coordinates while a differing character breaks it. Position is the atomic
    coordinate type used by every other LSP structure in this module.
    """

    def test_verify_position_creation_stores_fields(self):
        """Checks that Position stores line and character exactly as passed."""
        from encre.capabilities.search.lsp.protocol import Position
        p = Position(line=10, character=5)
        assert p.line == 10, "line must be stored verbatim."
        assert p.character == 5, "character must be stored verbatim."

    def test_verify_zero_position_is_valid(self):
        """Checks that the zero-based origin coordinate (0, 0) is accepted."""
        from encre.capabilities.search.lsp.protocol import Position
        p = Position(line=0, character=0)
        assert p.line == 0, "line=0 must be accepted."
        assert p.character == 0, "character=0 must be accepted."

    def test_verify_large_coordinate_values(self):
        """Checks that large line and character values are stored without overflow."""
        from encre.capabilities.search.lsp.protocol import Position
        p = Position(line=99999, character=999)
        assert p.line == 99999, "Large line value must be stored verbatim."
        assert p.character == 999, "Large character value must be stored verbatim."

    def test_verify_position_is_dataclass(self):
        """Checks that the stdlib dataclass checker recognises Position."""
        from dataclasses import is_dataclass
        from encre.capabilities.search.lsp.protocol import Position
        assert is_dataclass(Position), "Position must be a dataclass."

    def test_verify_position_equality(self):
        """Checks that identical Positions compare equal and differing ones do not."""
        from encre.capabilities.search.lsp.protocol import Position
        p1 = Position(line=5, character=10)
        p2 = Position(line=5, character=10)
        p3 = Position(line=5, character=11)
        assert p1 == p2, "Positions with identical fields must compare equal."
        assert p1 != p3, "Positions with differing character values must not compare equal."


# ----------------------------------------------------------------------
# Range dataclass
# ----------------------------------------------------------------------

class TestRange:
    """Validates Range construction and its span invariants.

    Exercises the ``Range`` record and checks that start and end positions are
    stored verbatim, that a single-line range keeps equal lines with an
    increasing character, and that the type is a dataclass. Range is the
    primary span type used by Diagnostic, Location, and HoverResult.
    """

    def test_verify_range_creation_stores_positions(self):
        """Checks that Range stores start and end Position objects exactly as passed."""
        from encre.capabilities.search.lsp.protocol import Position, Range
        start = Position(line=0, character=0)
        end = Position(line=10, character=20)
        r = Range(start=start, end=end)
        assert r.start.line == 0, "start.line must be stored verbatim."
        assert r.start.character == 0, "start.character must be stored verbatim."
        assert r.end.line == 10, "end.line must be stored verbatim."
        assert r.end.character == 20, "end.character must be stored verbatim."

    def test_verify_single_line_range_preserves_line_equality(self):
        """Checks that a single-line Range keeps equal lines with an increasing character."""
        from encre.capabilities.search.lsp.protocol import Position, Range
        start = Position(line=5, character=3)
        end = Position(line=5, character=15)
        r = Range(start=start, end=end)
        assert r.start.line == r.end.line, "Single-line range must have equal start and end line."
        assert r.end.character > r.start.character, "End character must be greater than start character."

    def test_verify_range_is_dataclass(self):
        """Checks that the stdlib dataclass checker recognises Range."""
        from dataclasses import is_dataclass
        from encre.capabilities.search.lsp.protocol import Range
        assert is_dataclass(Range), "Range must be a dataclass."


# ----------------------------------------------------------------------
# Location dataclass
# ----------------------------------------------------------------------

class TestLocation:
    """Validates Location construction and file-URI handling.

    Exercises the ``Location`` record and checks that the file URI and the
    embedded Range are stored verbatim, that file:// URIs are accepted, and
    that the type is a dataclass. Location bridges LSP references to actual
    filesystem paths.
    """

    def test_verify_location_creation_stores_uri_and_range(self):
        """Checks that Location stores uri and range exactly as passed."""
        from encre.capabilities.search.lsp.protocol import Location, Position, Range
        r = Range(start=Position(line=1, character=0), end=Position(line=1, character=10))
        loc = Location(uri="file:///test.py", range=r)
        assert loc.uri == "file:///test.py", "URI must be stored verbatim."
        assert loc.range.start.line == 1, "Range start line must be preserved."

    def test_verify_location_file_uri_format(self):
        """Checks that a file:/// URI is accepted and preserved on a Location."""
        from encre.capabilities.search.lsp.protocol import Location, Position, Range
        r = Range(start=Position(line=0, character=0), end=Position(line=0, character=5))
        loc = Location(uri="file:///home/user/project/main.py", range=r)
        assert loc.uri.startswith("file:///"), "URI must use the file:// scheme."

    def test_verify_location_is_dataclass(self):
        """Checks that the stdlib dataclass checker recognises Location."""
        from dataclasses import is_dataclass
        from encre.capabilities.search.lsp.protocol import Location
        assert is_dataclass(Location), "Location must be a dataclass."


# ----------------------------------------------------------------------
# Diagnostic dataclass
# ----------------------------------------------------------------------

class TestDiagnostic:
    """Validates Diagnostic construction and severity handling.

    Exercises the ``Diagnostic`` record and checks that message, severity, and
    source are stored verbatim, that the default source is the empty string,
    that all four standard LSP severity levels (1-4) are accepted, and that the
    type is a dataclass. Diagnostics are the primary error and warning surface
    returned by the LSP server.
    """

    def test_verify_diagnostic_creation_stores_fields(self):
        """Checks that Diagnostic stores message, severity, and source exactly as passed."""
        from encre.capabilities.search.lsp.protocol import Diagnostic, Position, Range
        r = Range(start=Position(line=5, character=0), end=Position(line=5, character=10))
        diag = Diagnostic(
            range=r,
            message="Unused variable 'x'",
            severity=2,
            source="pyright",
        )
        assert diag.message == "Unused variable 'x'", "message must be stored verbatim."
        assert diag.severity == 2, "severity must be stored verbatim."
        assert diag.source == "pyright", "source must be stored verbatim."

    def test_verify_diagnostic_default_source_is_empty_string(self):
        """Checks that source defaults to the empty string when omitted."""
        from encre.capabilities.search.lsp.protocol import Diagnostic, Position, Range
        r = Range(start=Position(line=1, character=0), end=Position(line=1, character=5))
        diag = Diagnostic(range=r, message="Error", severity=1)
        assert diag.source == "", "Default source must be an empty string."

    def test_verify_diagnostic_severity_levels(self):
        """Checks that all four standard severity codes are accepted without mutation."""
        from encre.capabilities.search.lsp.protocol import Diagnostic, Position, Range
        r = Range(start=Position(line=0, character=0), end=Position(line=0, character=1))
        for sev in [1, 2, 3, 4]:
            diag = Diagnostic(range=r, message=f"Level {sev}", severity=sev)
            assert diag.severity == sev, f"severity {sev} must be stored verbatim."

    def test_verify_diagnostic_is_dataclass(self):
        """Checks that the stdlib dataclass checker recognises Diagnostic."""
        from dataclasses import is_dataclass
        from encre.capabilities.search.lsp.protocol import Diagnostic
        assert is_dataclass(Diagnostic), "Diagnostic must be a dataclass."


# ----------------------------------------------------------------------
# HoverResult dataclass
# ----------------------------------------------------------------------

class TestHoverResult:
    """Validates HoverResult construction and its optional range field.

    Exercises the ``HoverResult`` record and checks that contents is stored
    verbatim, that range is None when omitted and populated when provided,
    that markdown-formatted contents are accepted, and that the type is a
    dataclass. HoverResult is the payload for hover requests in the LSP protocol.
    """

    def test_verify_hover_result_without_range(self):
        """Checks that HoverResult stores contents and leaves range as None."""
        from encre.capabilities.search.lsp.protocol import HoverResult
        hr = HoverResult(contents="def foo(x: int) -> str")
        assert hr.contents == "def foo(x: int) -> str", "contents must be stored verbatim."
        assert hr.range is None, "range must be None when not provided."

    def test_verify_hover_result_with_range(self):
        """Checks that HoverResult stores contents and populates range when provided."""
        from encre.capabilities.search.lsp.protocol import HoverResult, Position, Range
        r = Range(start=Position(line=1, character=0), end=Position(line=1, character=10))
        hr = HoverResult(contents="A string value", range=r)
        assert hr.contents == "A string value", "contents must be stored verbatim."
        assert hr.range is not None, "range must be populated when provided."
        assert hr.range.start.line == 1, "Range start line must be preserved."

    def test_verify_hover_result_accepts_markdown_contents(self):
        """Checks that markdown-formatted contents are kept verbatim."""
        from encre.capabilities.search.lsp.protocol import HoverResult
        md = "```python\ndef foo() -> int: ...\n```"
        hr = HoverResult(contents=md)
        assert "```" in hr.contents, "Markdown fence characters must be preserved in contents."

    def test_verify_hover_result_is_dataclass(self):
        """Checks that the stdlib dataclass checker recognises HoverResult."""
        from dataclasses import is_dataclass
        from encre.capabilities.search.lsp.protocol import HoverResult
        assert is_dataclass(HoverResult), "HoverResult must be a dataclass."


# ----------------------------------------------------------------------
# LSPState dataclass
# ----------------------------------------------------------------------

class TestLSPState:
    """Validates LSPState construction and its canonical status values.

    Exercises the ``LSPState`` record and checks that status and error are
    stored verbatim, that all four canonical status strings ("starting",
    "running", "stopped", "error") are accepted, and that the type is a
    dataclass. LSPState is the top-level status container returned by the
    client health check.
    """

    def test_verify_lsp_state_creation_running(self):
        """Checks that LSPState stores status='running' and leaves error as None."""
        from encre.capabilities.search.lsp.protocol import LSPState
        state = LSPState(status="running")
        assert state.status == "running", "status must be stored verbatim."
        assert state.error is None, "error must be None when not provided."

    def test_verify_lsp_state_creation_with_error(self):
        """Checks that LSPState stores status='stopped' alongside the error string."""
        from encre.capabilities.search.lsp.protocol import LSPState
        state = LSPState(status="stopped", error="connection refused")
        assert state.status == "stopped", "status must be stored verbatim."
        assert state.error == "connection refused", "error must be stored verbatim."

    def test_verify_lsp_state_accepts_all_canonical_status_values(self):
        """Checks that all four canonical status strings are accepted without mutation."""
        from encre.capabilities.search.lsp.protocol import LSPState
        for status in ["starting", "running", "stopped", "error"]:
            s = LSPState(status=status)
            assert s.status == status, f"status '{status}' must be stored verbatim."

    def test_verify_lsp_state_is_dataclass(self):
        """Checks that the stdlib dataclass checker recognises LSPState."""
        from dataclasses import is_dataclass
        from encre.capabilities.search.lsp.protocol import LSPState
        assert is_dataclass(LSPState), "LSPState must be a dataclass."


# ----------------------------------------------------------------------
# EncreLSPClient
# ----------------------------------------------------------------------

class TestEncreLSPClient:
    """Validates EncreLSPClient construction and its initial internal state.

    Exercises the client and checks that server_name is stored, that the
    internal flags (_initialized, _process, _reader_task, _request_id) start
    at their documented initial values, that close() before start() is a safe
    no-op, and that every public symbol is importable from the package
    namespace. Together these form the contract boundary for the client API.
    """

    def test_verify_construction_stores_server_name_and_initial_flags(self):
        """Checks that the client stores server_name and starts in a clean pre-start state."""
        from encre.capabilities.search.lsp.client import EncreLSPClient
        client = EncreLSPClient(server_name="pylsp")
        assert client is not None, "Client must be instantiated without error."
        assert client._server_name == "pylsp", "server_name must be stored verbatim."
        assert client._initialized is False, "_initialized must start as False."
        assert client._request_id == 0, "_request_id must start at 0."

    def test_verify_construction_with_various_server_names(self):
        """Checks that any well-known LSP server name is accepted and stored."""
        from encre.capabilities.search.lsp.client import EncreLSPClient
        for name in ["pylsp", "pyright", "rust-analyzer", "gopls", "typescript-language-server"]:
            client = EncreLSPClient(server_name=name)
            assert client._server_name == name, f"server_name '{name}' must be stored verbatim."

    def test_verify_initial_state_has_no_process_or_reader_task(self):
        """Checks that a fresh client has no subprocess and no reader task."""
        from encre.capabilities.search.lsp.client import EncreLSPClient
        client = EncreLSPClient(server_name="test")
        assert client._process is None, "_process must be None before start()."
        assert client._initialized is False, "_initialized must be False before start()."
        assert client._reader_task is None, "_reader_task must be None before start()."

    def test_verify_close_before_start_does_not_raise(self):
        """Checks that close() is safe on a client that was never started."""
        import asyncio
        from encre.capabilities.search.lsp.client import EncreLSPClient

        async def _test():
            """Closes a freshly constructed, never-started client."""
            client = EncreLSPClient(server_name="test")
            await client.close()

        asyncio.run(_test())

    def test_verify_public_api_exports(self):
        """Checks that every documented public symbol is importable from the package."""
        from encre.capabilities.search.lsp import (
            Diagnostic,
            EncreLSPClient,
            EncreLSPManager,
            HoverResult,
            Location,
            LSPState,
            Position,
            Range,
        )
        assert EncreLSPClient is not None, "EncreLSPClient must be exported."
        assert EncreLSPManager is not None, "EncreLSPManager must be exported."
        assert Position is not None, "Position must be exported."
        assert Range is not None, "Range must be exported."
        assert Location is not None, "Location must be exported."
        assert Diagnostic is not None, "Diagnostic must be exported."
        assert HoverResult is not None, "HoverResult must be exported."
        assert LSPState is not None, "LSPState must be exported."


# ----------------------------------------------------------------------
# EncreLSPManager
# ----------------------------------------------------------------------

class TestEncreLSPManager:
    """Validates that EncreLSPManager can be constructed without error.

    Establishes the baseline that construction of the manager -- the lifecycle
    orchestrator for one or more EncreLSPClient instances -- is safe and
    yields a usable object ready for client registration.
    """

    def test_verify_manager_construction(self):
        """Checks that EncreLSPManager instantiates without raising."""
        from encre.capabilities.search.lsp.manager import EncreLSPManager
        manager = EncreLSPManager()
        assert manager is not None, "EncreLSPManager must be instantiated without error."
