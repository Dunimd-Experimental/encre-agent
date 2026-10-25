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

"""Headless JSONL transport: one JSON object per line over stdin/stdout.

The desktop client drives the agent over a WebSocket, which needs a listening
port, a handshake, and a client library. A script or a CI job has none of that
and does not need any of it, so this module is the alternative transport: the
runtime speaks the same event vocabulary, but the wire is two plain text
streams.

::

    python -m encre.headless --model gpt-5.6 --backend-type openai

Frame projection is shared with the WebSocket bridge through
:func:`encre.events.project_event`. Because both transports call the same
projection, a frame means the same thing on either and a newly added event type
cannot be delivered by one and silently dropped by the other.

Protocol
--------

Every stdout line is a **response frame** shaped ``{"type": ..., ...}`` and
using the same vocabulary as the WebSocket protocol. Every stdin line is a
**request**:

``{"type": "run", "prompt": "..."}``
    Start a turn. Optional fields: ``system_prompt``,
    ``custom_instructions``, ``slash_command_mode``. The turn is streamed as
    response frames and terminates with a ``finish`` frame. A second ``run``
    while one is in flight is rejected with ``code: "busy"``, because a
    session is a single conversation.

``{"type": "permission", "decision": true, "remember": false}``
    Answer a pending ``permission_request`` frame.

``{"type": "question", "answers": "..."}``
    Answer a pending ``question_request`` frame.

``{"type": "plan", "action": "approve"|"reject", "proposal_id": ""}``
    Resolve a pending plan proposal.

``{"type": "engine_install", "request_id": "...", "choice": "..."}``
    Resolve a pending ``engine_install_request`` frame.

``{"type": "cancel"}``
    Ask the running turn to stop. It terminates with ``finish`` and
    ``reason: "cancelled"``.

``{"type": "close"}``
    Shut down cleanly. Reaching EOF on stdin has the same effect.

One process serves exactly one session, because a session is one conversation.
Branching, workspaces, and history browsing stay on the WebSocket transport,
where the desktop UI drives them.

Public API:

- :class:`HeadlessSession` -- the runtime plus protocol loop.
- :func:`main` -- the ``python -m encre.headless`` entry point.
"""

import argparse
import asyncio
import json
import sys
import threading
from dataclasses import dataclass
from typing import Any, TextIO

from encre.computer.engine_bridge import EngineRequester
from encre.config import EncreConfig
from encre.events import (
    SessionSnapshot,
    project_event,
    session_snapshot_need,
)
from encre.logging_config import get_logger
from encre.protocol.render import renderer_session_messages
from encre.sdk import EncreSDK
from encre.tools.runtime import get_engine_requester, set_engine_requester
from encre.utils.types import BackendError, create_finish

logger = get_logger(__name__)

#: Protocol revision announced in the ``ready`` frame. Bumped whenever a
#: request or response frame changes shape, so a client can compare it with
#: its own expectation and refuse to talk to a runtime it does not understand.
PROTOCOL_VERSION = 1

#: Request type discriminators. These are the ``"type"`` values accepted on
#: stdin; :meth:`HeadlessSession._handle` dispatches on exactly this set, so
#: adding a request here without adding a branch there would silently drop it.
REQ_RUN = "run"
REQ_PERMISSION = "permission"
REQ_QUESTION = "question"
REQ_PLAN = "plan"
REQ_ENGINE_INSTALL = "engine_install"
REQ_CANCEL = "cancel"
REQ_CLOSE = "close"

#: Error frame codes. ``error`` frames reuse the ``BackendError`` shape
#: (``type``/``code``/``message``) so a client needs exactly one error path
#: regardless of whether the failure came from the protocol layer, the agent
#: loop, or the backend provider.
ERR_BAD_JSON = "bad_json"
ERR_BAD_REQUEST = "bad_request"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_BUSY = "busy"
ERR_INTERNAL = "internal"


@dataclass
class _Request:
    """A parsed stdin line.

    Holding the whole mapping alongside the discriminator keeps dispatch flat:
    :meth:`HeadlessSession._handle` branches on ``kind`` and reads the
    type-specific fields straight out of ``data`` instead of re-parsing.

    Attributes:
        kind: The request type, taken from the ``"type"`` field.
        data: The full request mapping, for type-specific fields.
    """

    kind: str
    data: dict[str, Any]


class HeadlessSession:
    """One agent runtime speaking the JSONL protocol on a pair of streams.

    Owns exactly one agent session for its whole lifetime: construct, serve,
    shut down. All state is touched from a single asyncio event loop, except
    the stdin reader, which is a daemon thread that only hands lines to an
    :class:`asyncio.Queue` via ``call_soon_threadsafe``. :meth:`_write` is
    therefore always called from the loop, which is what keeps the frame
    stream line-atomic -- no two frames can interleave.

    Concurrency rule: one turn at a time. A second ``run`` while the first is
    in flight is answered with an ``error`` frame carrying
    ``code: "busy"`` rather than being queued, because a session is a single
    conversation and interleaved turns would corrupt its history.

    Every failure is converted into a frame rather than an exception: the
    client must always be able to tell whether the process is still serving,
    and a dead stdout is indistinguish from an error otherwise.

    Attributes:
        sdk: The :class:`encre.sdk.EncreSDK` owning the agent runtime, the
            tool registry, and the config.
        _turn: The in-flight turn task, or ``None`` when idle. Only set from
            the event loop.
        _lines: Queue of raw stdin lines; ``None`` marks EOF.
        _engine: The engine-install requester surfaced as frames instead of
            blocking forever inside a tool call.
    """

    def __init__(
        self,
        config: EncreConfig | None = None,
        *,
        tools: Any = None,
        mode: Any = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        register_builtins: bool = True,
        **sdk_collaborators: Any,
    ) -> None:
        """Build the runtime and bind the request/response streams.

        Construction is deliberately synchronous: ``EncreSDK.from_config``
        resolves config, tools, and backend before any request can arrive, so
        the very first frame a client reads (``ready``) already describes a
        live runtime.

        Args:
            config: The configuration to run under; a default
                :class:`encre.config.EncreConfig` is used when omitted.
            tools: A tool registry, an iterable of tools, or ``None``.
            mode: The capability profile to run under.
            stdin: Request stream; defaults to ``sys.stdin``.
            stdout: Response stream; defaults to ``sys.stdout``.
            register_builtins: Register the harness's six builtin tools.
            **sdk_collaborators: Forwarded verbatim to
                :class:`encre.sdk.EncreSDK`.

        Raises:
            Exception: Anything raised by ``EncreSDK.from_config`` propagates.
        """
        # Streams are bound at construction so embedders can point the
        # protocol at pipes or StringIO instead of the real console.
        self._stdin: TextIO = stdin if stdin is not None else sys.stdin
        self._stdout: TextIO = stdout if stdout is not None else sys.stdout
        self.sdk = EncreSDK.from_config(
            config,
            tools,
            mode=mode,
            register_builtins=register_builtins,
            **sdk_collaborators,
        )
        self._turn: asyncio.Task[None] | None = None
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._previous_requester: Any = None
        # Browser/desktop tools ask this requester when a missing engine needs
        # a decision. Wiring it keeps those tools from hanging forever with
        # nobody to answer; the request is surfaced as a normal frame.
        self._engine = EngineRequester(emit=self._emit_engine_event)

    # ------------------------------------------------------------------
    # Stream plumbing
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """Return the id of the session this process serves.

        Every response frame carries this id so a client can correlate frames
        even though the process never multiplexes sessions.

        Returns:
            The session id string, or ``""`` when the session has not been
            assigned an id yet.
        """
        return self.sdk.session.id or ""

    def _write(self, message: dict[str, Any]) -> None:
        """Write one response frame as a single JSON line.

        Called from the event loop only, so the bytes of one frame can never
        interleave with another frame's and a reader can parse stdout line by
        line safely. Non-JSON values (a dataclass leaking into a payload) are
        degraded to ``str`` via ``default`` so one odd payload cannot kill the
        transport, and a total encoding failure falls back to an ``error``
        frame rather than a traceback on the wire.

        Args:
            message: The frame to write.

        Returns:
            None.
        """
        try:
            line = json.dumps(message, ensure_ascii=False, default=str)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("failed to encode frame: %s", exc, exc_info=True)
            line = json.dumps({
                "type": "error",
                "code": ERR_INTERNAL,
                "message": f"failed to encode frame: {exc}",
            })
        self._stdout.write(line + "\n")
        self._stdout.flush()

    def _emit_error(self, code: str, message: str) -> None:
        """Write an ``error`` frame.

        Args:
            code: A machine-readable code from the ``ERR_*`` constants.
            message: The human-readable detail.

        Returns:
            None.
        """
        self._write({"type": "error", "code": code, "message": message})

    def _snapshot(self, *, renderer: bool) -> SessionSnapshot:
        """Build the session snapshot for a snapshot-bearing frame.

        The renderer history is an O(n) projection, so it is only computed when
        the caller asks for it rather than on every snapshot.

        Args:
            renderer: Whether the O(n) renderer history is needed.

        Returns:
            The session snapshot.
        """
        session = self.sdk.session
        messages = session.messages
        return SessionSnapshot(
            renderer_messages=renderer_session_messages(session) if renderer else [],
            tail_message=messages[-1] if messages else None,
            plan_items=session.plan_items,
            artifacts=session.artifacts,
            references=session.references,
        )

    def _emit(self, event: Any) -> None:
        """Project one agent event and write its frame.

        Frames that carry session state are the only ones that pay for a
        snapshot, and only ``finish`` while compacted pays for the renderer
        history -- the same laziness rule the WebSocket bridge applies, which
        is why both transports behave identically.

        Args:
            event: The agent event to ship.

        Returns:
            None.
        """
        need = session_snapshot_need(event)
        snapshot = self._snapshot(renderer=need.renderer_history) if need.required else None
        frame = project_event(event, session_id=self.session_id, snapshot=snapshot)
        if frame is not None:
            self._write(frame.to_message())

    async def _emit_engine_event(self, event: Any) -> None:
        """Immediate-emit hook for the engine installer.

        Called the moment a tool needs an engine decision, *before* it starts
        awaiting the answer, so the request reaches the client even though the
        running turn is blocked inside the tool call. Without this the client
        would never learn that a choice is needed.

        Args:
            event: The engine install event.

        Returns:
            None.
        """
        self._emit(event)

    def _start_reader(self, event_loop: asyncio.AbstractEventLoop) -> None:
        """Pump stdin lines into the asyncio queue from a helper thread.

        ``sys.stdin`` is a blocking stream; iterating it inline would freeze
        the event loop and stop the running turn from streaming. A single
        daemon thread iterates it once and hands each line to the queue,
        then pushes ``None`` to signal EOF.

        Args:
            event_loop: The loop the queue belongs to.

        Returns:
            None.
        """

        def _read() -> None:
            """Drain ``sys.stdin`` line by line into the asyncio queue.

            Runs on the daemon thread spawned by :meth:`_start_reader` so that
            the blocking read of a terminal stream never occupies the event
            loop. Each line is scheduled back onto the loop with
            :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe` because the
            queue belongs to the loop's thread, not this one; calling
            ``put_nowait`` directly from here would race the queue's internal
            state.

            Every exception is logged rather than re-raised -- a dying thread
            would otherwise be silent -- but the ``finally`` clause always
            queues ``None`` so the consumer loop sees EOF even on a broken pipe
            or closed stdin and cannot hang waiting for a line that will never
            arrive.

            Args:
                None; the stdin stream, queue, and loop are closed over from
                    the enclosing :meth:`_start_reader` frame.

            Returns:
                ``None``; this is a thread target, not a producer of results.
            """
            try:
                for line in self._stdin:
                    event_loop.call_soon_threadsafe(self._lines.put_nowait, line)
            except Exception as exc:  # pragma: no cover - stream failure
                logger.error("stdin reader failed: %s", exc, exc_info=True)
            finally:
                # Always signal EOF, so the loop cannot hang on a broken pipe.
                event_loop.call_soon_threadsafe(self._lines.put_nowait, None)

        threading.Thread(
            target=_read, name="encre-headless-stdin", daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def _parse(self, line: str) -> _Request | None:
        """Parse one stdin line into a request, reporting failures itself.

        Blank lines are ignored rather than rejected so a client may send
        keep-alive newlines without producing an ``error`` frame. Every
        rejection writes its own ``error`` frame and returns ``None``, so the
        caller only ever has to handle the success case.

        Args:
            line: The raw line.

        Returns:
            The parsed request, or ``None`` when the line was rejected (an
            ``error`` frame has already been written).
        """
        text = line.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._emit_error(ERR_BAD_JSON, f"line is not valid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            self._emit_error(ERR_BAD_REQUEST, "request must be a JSON object")
            return None
        kind = data.get("type")
        if not isinstance(kind, str) or not kind:
            self._emit_error(ERR_BAD_REQUEST, "request is missing a string 'type'")
            return None
        return _Request(kind=kind, data=data)

    async def _handle(self, request: _Request) -> bool:
        """Dispatch one request.

        Every branch validates its own payload and emits an ``error`` frame
        before returning, so a malformed request never raises into the serve
        loop and the session keeps serving.

        Args:
            request: The parsed request.

        Returns:
            ``True`` when the session should shut down, ``False`` to keep
            serving.
        """
        kind = request.kind
        if kind == REQ_RUN:
            self._handle_run(request.data)
        elif kind == REQ_PERMISSION:
            # The decision must be a real bool: a truthy string like "false"
            # would approve the action.
            decision = request.data.get("decision")
            if not isinstance(decision, bool):
                self._emit_error(ERR_BAD_REQUEST, "'decision' must be a boolean")
                return False
            remember = bool(request.data.get("remember", False))
            self.sdk.loop.resolve_permission(decision, remember)
            self._write({"type": "permission_resolved", "decision": decision,
                         "remember": remember, "session_id": self.session_id})
        elif kind == REQ_QUESTION:
            answers = request.data.get("answers")
            if not isinstance(answers, str):
                self._emit_error(ERR_BAD_REQUEST, "'answers' must be a string")
                return False
            self.sdk.loop.resolve_question(answers)
            self._write({"type": "question_resolved", "session_id": self.session_id})
        elif kind == REQ_PLAN:
            self._handle_plan(request.data)
        elif kind == REQ_ENGINE_INSTALL:
            request_id = request.data.get("request_id")
            choice = request.data.get("choice")
            if not isinstance(request_id, str) or not isinstance(choice, str):
                self._emit_error(
                    ERR_BAD_REQUEST, "'request_id' and 'choice' must be strings",
                )
                return False
            resolved = self._engine.resolve(request_id, choice)
            self._write({"type": "engine_install_resolved", "request_id": request_id,
                         "resolved": resolved, "session_id": self.session_id})
        elif kind == REQ_CANCEL:
            self.sdk.cancel()
            self._write({"type": "cancel_acknowledged", "session_id": self.session_id})
        elif kind == REQ_CLOSE:
            return True
        else:
            self._emit_error(ERR_UNKNOWN_TYPE, f"unknown request type {kind!r}")
        return False

    def _handle_plan(self, data: dict[str, Any]) -> None:
        """Resolve a plan proposal from a ``plan`` request.

        Args:
            data: The raw request mapping.

        Returns:
            None.
        """
        action = data.get("action")
        if action not in ("approve", "reject"):
            self._emit_error(ERR_BAD_REQUEST, "'action' must be 'approve' or 'reject'")
            return
        proposal_id = data.get("proposal_id") or ""
        if not isinstance(proposal_id, str):
            self._emit_error(ERR_BAD_REQUEST, "'proposal_id' must be a string")
            return
        if action == "approve":
            self.sdk.loop.approve_plan(proposal_id)
        else:
            self.sdk.loop.reject_plan(proposal_id)
        self._write({"type": "plan_resolved", "action": action,
                     "proposal_id": proposal_id, "session_id": self.session_id})

    def _handle_run(self, data: dict[str, Any]) -> None:
        """Validate a ``run`` request and start its turn.

        The busy check runs before any field validation so a client learns
        immediately that it must wait, instead of getting a confusing
        validation error while a turn is mid-flight.

        Args:
            data: The raw request mapping.

        Returns:
            None.
        """
        # A session is one conversation, so a second in-flight turn is
        # rejected rather than queued.
        if self._turn is not None and not self._turn.done():
            self._emit_error(ERR_BUSY, "a turn is already running")
            return
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            self._emit_error(ERR_BAD_REQUEST, "'prompt' must be a non-empty string")
            return
        system_prompt = data.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            self._emit_error(ERR_BAD_REQUEST, "'system_prompt' must be a string")
            return
        custom_instructions = data.get("custom_instructions", "")
        if not isinstance(custom_instructions, str):
            self._emit_error(ERR_BAD_REQUEST, "'custom_instructions' must be a string")
            return
        slash_command_mode = data.get("slash_command_mode")
        if slash_command_mode is not None and not isinstance(slash_command_mode, str):
            self._emit_error(ERR_BAD_REQUEST, "'slash_command_mode' must be a string")
            return

        # The turn runs as a task so the serve loop keeps reading requests and
        # can still answer permission and cancel while it streams.
        self._turn = asyncio.create_task(
            self._run_turn(
                prompt,
                system_prompt,
                custom_instructions=custom_instructions,
                slash_command_mode=slash_command_mode,
            ),
            name="encre-headless-turn",
        )

    async def _run_turn(
        self,
        prompt: str,
        system_prompt: str | None,
        *,
        custom_instructions: str,
        slash_command_mode: str | None,
    ) -> None:
        """Stream one turn to the client.

        The loop owns backend-error recovery, so a provider failure surfaces
        as a ``finish`` frame carrying ``reason``/``error``/``error_code``
        rather than as an exception. This handler covers the residue: an
        exception that escapes the loop entirely is reported as an ``error``
        frame plus a ``finish`` frame with ``reason: "error"``, mirroring the
        WebSocket bridge. Either way a client always sees a terminal frame and
        never hangs waiting for one.

        Args:
            prompt: The user prompt.
            system_prompt: Optional system-prompt override.
            custom_instructions: Extra context instructions.
            slash_command_mode: Optional slash-command mode.

        Returns:
            None.

        Raises:
            asyncio.CancelledError: Re-raised for deliberate teardown so
                shutdown is not reported as a failure.
        """
        try:
            async for event in self.sdk.run(
                prompt,
                system_prompt,
                custom_instructions=custom_instructions,
                slash_command_mode=slash_command_mode,
            ):
                # Drain engine requests produced by a blocked tool before the
                # event that follows them, so the client can answer in time.
                async for pending in self._engine.drain():
                    self._emit(pending)
                self._emit(event)
            async for pending in self._engine.drain():
                self._emit(pending)
        except asyncio.CancelledError:
            # Deliberate teardown (``close`` request or process exit) tore the
            # turn down; stay silent rather than reporting a fake failure.
            # A user-requested stop goes through ``cancel`` instead, which the
            # loop turns into a normal ``finish``/reason: "cancelled".
            raise
        except Exception as exc:
            logger.error("headless turn failed: %s", exc, exc_info=True)
            self._emit(BackendError(error=f"{type(exc).__name__}: {exc}"))
            self._emit(create_finish("error", error=str(exc)))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _emit_ready(self) -> None:
        """Announce the runtime to the client.

        Sent immediately after the reader thread starts and before the first
        request is read, so a client that writes ``run`` as soon as it sees
        ``ready`` cannot race ahead of the runtime.

        Returns:
            None.
        """
        config = self.sdk.config
        self._write({
            "type": "ready",
            "protocol": PROTOCOL_VERSION,
            "session_id": self.session_id,
            "model": config.model,
            "backend_type": config.backend_type,
            "workspace": getattr(config, "workspace", "") or "",
            "permission_mode": config.permission_mode,
            "tools": sorted(self.sdk.tool_registry.list_tools()),
        })

    async def serve(self) -> int:
        """Serve requests until ``close`` or EOF on stdin.

        Registers this session's engine requester as the process-wide one for
        the duration of the call, so tools spawned outside the explicit
        ``_engine`` path still reach a live requester.

        Returns:
            A process exit code: ``0`` on a clean shutdown.
        """
        self._loop = asyncio.get_running_loop()
        self._previous_requester = get_engine_requester()
        set_engine_requester(self._engine)
        self._start_reader(self._loop)
        self._emit_ready()
        try:
            while True:
                line = await self._lines.get()
                if line is None:
                    break
                if await self.handle_line(line):
                    break
        finally:
            await self._shutdown()
        return 0

    async def handle_line(self, line: str) -> bool:
        """Parse and dispatch one protocol line.

        Exposed so an embedder (or a test) can drive the protocol without
        standing up the stdin reader thread; :meth:`serve` is a thin loop
        over this.

        Args:
            line: One raw JSONL request line.

        Returns:
            ``True`` when the session should shut down.
        """
        request = self._parse(line)
        if request is None:
            return False
        return await self._handle(request)

    async def wait_turn(self) -> None:
        """Wait for the in-flight turn to finish.

        A no-op when no turn is running. A turn reports its own failures as
        frames, so this only raises if the waiting task itself is cancelled.

        Returns:
            None.
        """
        turn = self._turn
        if turn is not None:
            await turn

    async def _shutdown(self) -> None:
        """Stop the running turn and release the runtime's resources.

        An in-flight turn is cancelled rather than awaited: a client that
        wants the turn's output sends ``close`` after its ``finish`` frame, and
        a client that wants to abort should not be able to hang the process by
        closing early.

        Returns:
            None.
        """
        self._engine.cancel_all("shutdown")
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            try:
                await self._turn
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("headless turn ended with an error: %s", exc)
        self._turn = None
        try:
            await self.sdk.aclose()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("sdk close failed: %s", exc)
        # Only restore the previous requester when this session still owns the
        # slot, so a nested session cannot steal the process-wide binding.
        if get_engine_requester() is self._engine:
            set_engine_requester(self._previous_requester)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the headless entry point.

    Only overrides are exposed; anything omitted falls back to the config file
    or the :class:`encre.config.EncreConfig` defaults.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m encre.headless",
        description="Drive the Encre agent runtime over JSONL stdin/stdout.",
    )
    parser.add_argument("--config", default=None,
                        help="Config file to load (default: the standard location).")
    parser.add_argument("--model", default=None, help="Override the model name.")
    parser.add_argument("--backend-type", default=None,
                        help="Override the backend type (e.g. openai, anthropic).")
    parser.add_argument("--api-key", default=None, help="Override the API key.")
    parser.add_argument("--base-url", default=None, help="Override the API base URL.")
    parser.add_argument("--workspace", default=None, help="Set the workspace root.")
    parser.add_argument("--permission-mode", default=None,
                        choices=["default", "accept_edits", "bypass", "dont_ask",
                                 "plan", "spec", "auto", "blacklist", "custom"],
                        help="How tool permissions are gated.")
    parser.add_argument("--mode", default=None,
                        help="Capability profile to run under.")
    parser.add_argument("--no-builtins", action="store_true",
                        help="Do not register the harness's six builtin tools.")
    return parser


def _load_config(args: argparse.Namespace) -> EncreConfig:
    """Load the configuration for a headless run.

    A config load failure is degraded to defaults with a warning rather than
    aborting, because a missing config file should never block a caller that
    passed every setting on the command line.

    Args:
        args: Parsed CLI arguments.

    Returns:
        The configuration, with any command-line overrides applied.
    """
    try:
        config = EncreConfig.from_file(args.config)
    except Exception as exc:
        logger.warning("config load failed (%s); using defaults", exc)
        config = EncreConfig()
    if args.model:
        config.model = args.model
    if args.backend_type:
        config.backend_type = args.backend_type
    if args.api_key:
        config.api_key = args.api_key
    if args.base_url:
        config.base_url = args.base_url
    if args.workspace:
        config.workspace = args.workspace
    if args.permission_mode:
        config.permission_mode = args.permission_mode
    return config


def main(argv: list[str] | None = None) -> int:
    """Run the headless JSONL transport.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """
    args = build_arg_parser().parse_args(argv)
    session = HeadlessSession(
        _load_config(args),
        mode=args.mode,
        register_builtins=not args.no_builtins,
    )
    return asyncio.run(session.serve())


if __name__ == "__main__":
    raise SystemExit(main())
