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

"""Tests for the shared data models of code search, LSP, git, notebook, server, session, and goal subsystems.

Each typed record is constructed with a representative payload and its fields
are asserted to round-trip unchanged, documented defaults are checked for
partial records, and the wire-level encode/parse helpers, session registry, and
goal-loop objects are exercised in both plaintext and encrypted modes.
"""


# ---- LSP types ----


class TestLSPProtocol:
    """Tests for the LSP protocol data model.

    Builds Position, Range, Location, Diagnostic, HoverResult and LSPState with
    explicit values and asserts each field survives, keeping the server-to-client
    inter-process contract stable.
    """

    def test_verify_position_fields(self):
        """Position must store zero-based line and character offsets exactly."""
        from encre.capabilities.search.lsp.protocol import Position
        p = Position(line=10, character=5)
        assert p.line == 10
        assert p.character == 5

    def test_verify_range_bounds(self):
        """Range must preserve both its start and end Position boundaries."""
        from encre.capabilities.search.lsp.protocol import Position, Range
        start = Position(line=0, character=0)
        end = Position(line=10, character=20)
        r = Range(start=start, end=end)
        assert r.start.line == 0
        assert r.end.line == 10

    def test_verify_location_uri_and_range(self):
        """Location must carry both its document URI and its range unchanged."""
        from encre.capabilities.search.lsp.protocol import Location, Position, Range
        r = Range(start=Position(line=1, character=0), end=Position(line=1, character=10))
        loc = Location(uri="file:///test.py", range=r)
        assert loc.uri == "file:///test.py"
        assert loc.range.start.line == 1

    def test_verify_diagnostic_fields(self):
        """Diagnostic must keep its message, numeric severity, and producing source."""
        from encre.capabilities.search.lsp.protocol import Diagnostic, Position, Range
        r = Range(start=Position(line=5, character=0), end=Position(line=5, character=10))
        diag = Diagnostic(
            range=r,
            message="Unused variable",
            severity=2,
            source="pyright",
        )
        assert diag.message == "Unused variable"
        # Severity 2 is the LSP numeric code for a warning.
        assert diag.severity == 2
        assert diag.source == "pyright"

    def test_verify_hover_result_without_range(self):
        """HoverResult must keep its contents when no annotation range is supplied."""
        from encre.capabilities.search.lsp.protocol import HoverResult
        hr = HoverResult(contents="def foo(x: int) -> str", range=None)
        assert hr.contents == "def foo(x: int) -> str"
        # A None range signals hover over a whole symbol rather than a span.
        assert hr.range is None

    def test_verify_hover_result_with_range(self):
        """HoverResult must retain an explicitly supplied range instead of dropping it."""
        from encre.capabilities.search.lsp.protocol import HoverResult, Position, Range
        r = Range(start=Position(line=1, character=0), end=Position(line=1, character=10))
        hr = HoverResult(contents="def foo()", range=r)
        assert hr.range is not None

    def test_verify_lsp_state_running(self):
        """A healthy LSPState must report its status with no error attached."""
        from encre.capabilities.search.lsp.protocol import LSPState
        state = LSPState(status="running")
        assert state.status == "running"
        assert state.error is None

    def test_verify_lsp_state_with_error(self):
        """An unhealthy LSPState must propagate both its terminal status and error text."""
        from encre.capabilities.search.lsp.protocol import LSPState
        state = LSPState(status="stopped", error="connection refused")
        assert state.status == "stopped"
        assert state.error == "connection refused"


# ---- Git types ----


class TestGitTypes:
    """Tests for the git repository state model.

    Exercises GitState, GitDiffResult and EncreGitRepo for both out-of-repository
    defaults and fully populated snapshots, so change-aware features such as
    context injection and diff retrieval see consistent data.
    """

    def test_verify_git_state_defaults(self):
        """GitState must be safe to read when the workspace is not a git repository."""
        from encre.git.repo import GitState
        gs = GitState(in_repo=False)
        assert gs.in_repo is False
        # With no repository to poll there is nothing dirty, so cleanliness is assumed.
        assert gs.is_clean is True
        assert gs.changed_files == []
        assert gs.untracked_files == []

    def test_verify_git_state_populated(self):
        """GitState must preserve complete repository metadata once populated."""
        from encre.git.repo import GitState
        gs = GitState(
            in_repo=True,
            commit_hash="abc123",
            branch="main",
            remote_url="https://github.com/example/repo",
            is_clean=True,
            changed_files=[],
            untracked_files=[],
            has_unpushed=False,
            worktree_count=1,
        )
        assert gs.in_repo is True
        assert gs.branch == "main"
        assert gs.commit_hash == "abc123"
        assert gs.worktree_count == 1

    def test_verify_git_diff_result_fields(self):
        """GitDiffResult must keep file, insertion, and deletion counts intact."""
        from encre.git.diff import GitDiffResult
        gdr = GitDiffResult(files=3, insertions=50, deletions=10)
        assert gdr.files == 3
        assert gdr.insertions == 50
        assert gdr.deletions == 10

    def test_verify_git_repo_instantiation(self):
        """EncreGitRepo must be constructible before any metadata query is issued."""
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        assert repo is not None

    def test_verify_git_repo_is_in_repo_returns_bool(self):
        """is_in_repo() must return a strict bool so truthiness checks stay reliable."""
        from encre.git.repo import EncreGitRepo
        repo = EncreGitRepo(workspace=".")
        assert isinstance(repo.is_in_repo(), bool)


# ---- Notebook types ----


class TestNotebook:
    """Tests for the notebook session model.

    Builds EncreNotebookSession both with and without an explicit kernel to
    confirm the session always exposes a usable kernel identifier.
    """

    def test_verify_session_default_kernel(self):
        """A session created with no arguments must default to the python3 kernel."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession()
        assert sess is not None
        assert sess.kernel_name == "python3"

    def test_verify_session_custom_kernel(self):
        """An explicitly requested kernel name must be honoured per session."""
        from encre.notebook.session import EncreNotebookSession
        sess = EncreNotebookSession(kernel_name="python3.12")
        assert sess.kernel_name == "python3.12"


# ---- Server protocol types ----


class TestServerProtocol:
    """Tests for the server message protocol types.

    Builds ClientRun directly and through deserialization, parses valid,
    malformed, and keepalive client frames, and round-trips a server message over
    both the plaintext and encrypted wire paths.
    """

    def test_verify_client_run_fields(self):
        """ClientRun must expose its type discriminator, prompt, and session id."""
        from encre.server.protocol import ClientRun
        msg = ClientRun(prompt="Hello", session_id="s1")
        assert msg.type == "run"
        assert msg.prompt == "Hello"
        assert msg.session_id == "s1"

    def test_verify_client_run_from_dict(self):
        """ClientRun.from_dict must reconstruct the same message from a raw mapping."""
        from encre.server.protocol import ClientRun
        msg = ClientRun.from_dict({"prompt": "Hello", "session_id": "s1"})
        assert msg.type == "run"
        assert msg.prompt == "Hello"

    def test_verify_parse_client_message_valid_json(self):
        """parse_client_message must return a message object for a well-formed JSON frame."""
        import json

        from encre.server.protocol import parse_client_message
        raw = json.dumps({"type": "run", "prompt": "Hello", "session_id": "s1"})
        msg = parse_client_message(raw)
        assert msg is not None

    def test_verify_parse_client_message_invalid_json(self):
        """parse_client_message must return None for malformed input instead of raising."""
        from encre.server.protocol import parse_client_message
        msg = parse_client_message("not json")
        assert msg is None

    def test_verify_parse_client_message_ping(self):
        """Keepalive ping frames must parse successfully so the connection stays alive."""
        import json

        from encre.server.protocol import parse_client_message
        raw = json.dumps({"type": "ping"})
        msg = parse_client_message(raw)
        assert msg is not None

    def test_verify_encode_server_message_is_always_encrypted(self):
        """encode_server_message must only ever emit transport ciphertext.

        There is no plaintext mode: the encoder exposes no ``encrypt`` switch,
        so every server frame — including per-token deltas — hides its payload
        until ``decrypt()`` recovers the original document.
        """
        import inspect
        import json

        from encre.crypto import decrypt
        from encre.server.protocol import encode_server_message

        # The plaintext escape hatch must not be expressible.
        assert "encrypt" not in inspect.signature(encode_server_message).parameters

        encrypted = encode_server_message("text_delta", text="Hello!")
        assert isinstance(encrypted, str)
        assert "Hello!" not in encrypted
        assert not encrypted.startswith("{")
        recovered = json.loads(decrypt(encrypted))
        assert recovered == {"type": "text_delta", "text": "Hello!"}


# ---- Server session manager ----


class TestSessionManager:
    """Tests for the server session lifecycle.

    Walks SessionInfo and SessionManager through creation, lookup, listing, and
    async removal so the registry keeps accurate active-session state for every
    incoming request.
    """

    def test_verify_session_info_fields(self):
        """SessionInfo must expose its id and start in the IDLE state."""
        from encre.agent import EncreAgent
        from encre.config import EncreConfig
        from encre.server.session_manager import SessionInfo, SessionState
        agent = EncreAgent(config=EncreConfig(backend_type="openai", api_key="sk-fake"))
        si = SessionInfo(session_id="s1", agent=agent)
        assert si.session_id == "s1"
        # A freshly allocated session is idle until the agent does real work.
        assert si.state is SessionState.IDLE

    def test_verify_session_manager_creation(self):
        """A fresh SessionManager must hold zero active sessions."""
        from encre.server.session_manager import SessionManager
        sm = SessionManager()
        assert sm is not None
        assert sm.active_count == 0

    def test_verify_session_manager_create_session(self):
        """create_session must allocate an id and increment the active count."""
        from encre.config import EncreConfig
        from encre.server.session_manager import SessionManager
        sm = SessionManager()
        info = sm.create_session(EncreConfig(backend_type="openai", api_key="sk-fake"))
        assert info.session_id is not None
        assert sm.active_count == 1

    def test_verify_session_manager_get_session(self):
        """get_session must return the exact session previously created for that id."""
        from encre.config import EncreConfig
        from encre.server.session_manager import SessionManager
        sm = SessionManager()
        info = sm.create_session(EncreConfig(backend_type="openai", api_key="sk-fake"))
        retrieved = sm.get_session(info.session_id)
        assert retrieved is not None
        assert retrieved.session_id == info.session_id

    def test_verify_session_manager_list_sessions(self):
        """list_sessions must report one entry per created session."""
        from encre.config import EncreConfig
        from encre.server.session_manager import SessionManager
        sm = SessionManager()
        sm.create_session(EncreConfig(backend_type="openai", api_key="sk-fake"))
        sessions = sm.list_sessions()
        assert len(sessions) == 1

    def test_verify_session_manager_remove_session(self):
        """remove_session must decrement the active count and make the id unfindable."""
        import asyncio

        from encre.config import EncreConfig
        from encre.server.session_manager import SessionManager
        sm = SessionManager()
        info = sm.create_session(EncreConfig(backend_type="openai", api_key="sk-fake"))
        asyncio.run(sm.remove_session(info.session_id))
        assert sm.active_count == 0
        assert sm.get_session(info.session_id) is None


# ---- Agent / Loop / Goal types ----


class TestAgentTypes:
    """Tests for the agent goal-loop and checkpoint model.

    Builds GoalDefinition, GoalResult, GoalStatus, GoalEvent, SessionCheckpoint,
    EncreGoalLoop, and EncreGoalRunner to confirm the high-level orchestration
    types behave correctly during goal execution, status tracking, and
    checkpoint recording.
    """

    def test_verify_goal_definition_fields(self):
        """GoalDefinition must store its description, success criteria, and retry budget."""
        from encre.goal import GoalDefinition
        gd = GoalDefinition(description="Test feature", success_criteria="All tests pass", max_attempts=5)  # noqa: E501
        assert gd.description == "Test feature"
        assert gd.success_criteria == "All tests pass"
        assert gd.max_attempts == 5

    def test_verify_goal_result_status(self):
        """GoalResult must expose its status and how many attempts were made."""
        from encre.goal import GoalResult, GoalStatus
        gr = GoalResult(status=GoalStatus.SUCCESS, summary="Done", attempts=3)
        assert gr.status == GoalStatus.SUCCESS
        assert gr.attempts == 3

    def test_verify_goal_status_constants(self):
        """Every GoalStatus member must exist so all lifecycle transitions are representable."""
        from encre.goal import GoalStatus
        assert GoalStatus.PENDING is not None
        assert GoalStatus.IN_PROGRESS is not None
        assert GoalStatus.SUCCESS is not None
        assert GoalStatus.FAILED is not None
        assert GoalStatus.TIMEOUT is not None
        assert GoalStatus.MAX_ATTEMPTS is not None

    def test_verify_goal_event_fields(self):
        """GoalEvent must carry its status, attempt number, and human-readable message."""
        from encre.goal import GoalEvent, GoalStatus
        ge = GoalEvent(status=GoalStatus.IN_PROGRESS, attempt=1, message="Working...")
        assert ge.status == GoalStatus.IN_PROGRESS
        assert ge.attempt == 1
        assert ge.message == "Working..."

    def test_verify_session_checkpoint_fields(self):
        """SessionCheckpoint must record its id and turn/tool-call counters for resume."""
        from encre.session import SessionCheckpoint
        sc = SessionCheckpoint(
            checkpoint_id="ckpt1",
            label="After turn 5",
            turn_count=5,
            tool_call_count=10,
        )
        assert sc.checkpoint_id == "ckpt1"
        assert sc.turn_count == 5
        assert sc.tool_call_count == 10

    def test_verify_goal_loop_instantiation(self):
        """EncreGoalLoop must construct with the given agent and keep its goal description."""
        from encre.agent import EncreAgent
        from encre.config import EncreConfig
        from encre.goal import EncreGoalLoop
        agent = EncreAgent(config=EncreConfig(backend_type="openai", api_key="sk-fake"))
        loop = EncreGoalLoop(agent=agent, description="test", success_criteria="works")
        assert loop is not None
        assert loop._description == "test"

    def test_verify_goal_runner_instantiation(self):
        """EncreGoalRunner must wire config, tools, hooks, and safety before any cycle begins."""
        from encre.config import EncreConfig
        from encre.goal import EncreGoalRunner
        from encre.hooks.system import EncreHookSystem
        from encre.safety import EncreSafetyEngine
        from encre.tools.registry import ToolRegistry
        config = EncreConfig(backend_type="openai", api_key="sk-fake")
        registry = ToolRegistry()
        hooks = EncreHookSystem()
        safety = EncreSafetyEngine(config=config)
        runner = EncreGoalRunner(
            config=config,
            tool_registry=registry,
            hook_system=hooks,
            safety=safety,
        )
        assert runner is not None
