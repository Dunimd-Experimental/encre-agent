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

"""Interactive terminal REPL channel.

Implements :class:`TerminalChannel`, a stdin/stdout REPL for direct
conversation with the agent from a shell. It exists for two narrow uses:
headless server deployments where no browser is available, and CI smoke
tests that need to prove a run produces output end to end.

This is not a user-facing surface. Encre's presentation layer is the
Electron Encre Agent Desktop, so :class:`TerminalChannel` is gated behind the
``ENCRE_DESKTOP_ONLY`` environment variable: when it is set,
:meth:`TerminalChannel.start` prints a notice and returns without entering
the read loop, so a deployment cannot accidentally present two different
UIs for the same agent.

The channel is the one transport in the project that blocks. It reads lines
synchronously from ``sys.stdin`` and therefore has to run that read through
:func:`asyncio.loop.run_in_executor` so the event loop keeps servicing the
other channels. Everything else -- the router, the session manager, the
Encre Agent Core behind it -- is ordinary asyncio.

Slash commands are parsed by
:func:`encre.slash_commands.parse_terminal_slash_command`, the shared parser
that the desktop uses as well, so command spelling stays consistent across
surfaces.

Public API:
    :class:`TerminalChannel` -- the REPL channel.
"""

import asyncio
import sys

from encre.channels.base import Channel
from encre.protocol.session_router import EventRouter
from encre.slash_commands import parse_terminal_slash_command
from encre.utils.types import Finish, TextDelta, ToolResult


class TerminalChannel(Channel):
    """Interactive terminal REPL channel over stdin/stdout.

    .. deprecated::
        Encre ships a desktop-only presentation layer (Electron + React), so
        this REPL is retained for headless server deployments and CI smoke
        tests rather than as a user-facing surface. When the
        ``ENCRE_DESKTOP_ONLY`` environment variable is set,
        :meth:`start` prints a notice and exits without entering the REPL
        loop.

    One :class:`TerminalChannel` instance corresponds to one process and one
    persistent agent session, so a conversation keeps its context across
    prompts. The session is created in :meth:`start` and removed at the end
    of the loop, whether the loop ended by command, EOF, or
    :meth:`stop`.

    Recognised commands:

    - ``/new`` -- drop the current session and open a fresh one.
    - ``/exit`` and ``/quit`` -- leave the REPL. Both are registered by
      :func:`encre.slash_commands.parse_terminal_slash_command`; the handler
      tests only for ``exit``, and ``/quit`` falls through the
      command block and is treated as a no-op line.
    - ``/clear`` -- registered by the same table but not branched on here, so
      it likewise consumes the line without doing anything.

    Args:
        None. The channel has no configurable state; the router is passed
        in at :meth:`start` time.

    Attributes:
        _running: Loop flag. Set ``False`` by ``/exit``, by EOF, and by
            :meth:`stop`; checked at the top of each iteration so an external
            :meth:`stop` takes effect on the next read boundary.
        _task: Reserved handle for an externally-owned task. The loop here
            runs inline inside :meth:`start`, so this stays ``None`` in the
            current implementation.
    """

    name = "terminal"

    def __init__(self) -> None:
        """Initialise the loop flag and the reserved task handle.

        Wiring is intentionally light: this channel has no constructor
        arguments because the :class:`encre.protocol.session_router.EventRouter`
        it needs is only known at :meth:`start` time, so it is passed in there
        instead. Both fields are stored as inert defaults -- the flag is
        cleared and the handle left unset -- and the first real assignment to
        either happens inside :meth:`start`.

        Invariant: ``self._running`` is ``False`` on return, so an instance
        cannot be mistaken for a live REPL before ``start()`` runs, and
        ``self._task`` stays ``None`` because the loop body executes inline in
        :meth:`start` rather than in a detached coroutine.

        Args:
            None.
        """
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self, router: EventRouter) -> None:
        """Run the REPL until the user leaves, the stream closes, or stop() fires.

        This coroutine is the entire lifetime of the channel: the caller
        awaits it and treats its return as channel shutdown. The loop
        alternates between blocking on stdin and streaming one agent run, so
        only one prompt is ever in flight -- terminal users expect
        request/response semantics, not concurrent turns.

        Args:
            router: Shared router. Used for session creation/removal via
                ``router.session_manager`` and for dispatching each prompt
                via :meth:`encre.protocol.session_router.EventRouter.submit_stream`.

        Returns:
            ``None``, always -- including the desktop-only short circuit and
            on EOF. Callers do not need to distinguish these outcomes.

        Raises:
            Exception: Only failures outside the loop body (session creation,
                final session removal) escape. Per-prompt failures are caught
                and swallowed so one bad turn never kills the REPL.
        """
        import os
        if os.environ.get("ENCRE_DESKTOP_ONLY", "").strip().lower() in ("1", "true", "yes"):
            print(
                "Encre: terminal channel is disabled in desktop-only mode. "
                "Use the desktop UI to interact with the agent.",
                flush=True,
            )
            return
        self._running = True
        loop = asyncio.get_event_loop()

        info = router.session_manager.create_session()
        session_id = info.session_id

        print(f"iClaw terminal -- session {session_id[:8]}", flush=True)
        print("Type /new for fresh session, /exit to quit", flush=True)

        while self._running:
            try:
                # Blocking readline pushed off the event loop so the other
                # channels keep being serviced while the user thinks.
                prompt = await loop.run_in_executor(None, sys.stdin.readline)
                # readline returns "" on EOF rather than raising; treat it as
                # a closed stdin so the loop exits cleanly.
                if not prompt:
                    self._running = False
                    break
                prompt = prompt.strip()
                if not prompt:
                    continue

                slash_command = parse_terminal_slash_command(prompt)
                if slash_command:
                    if slash_command.name == "exit":
                        self._running = False
                        break
                    if slash_command.name == "new":
                        await router.session_manager.remove_session(session_id)
                        info = router.session_manager.create_session()
                        session_id = info.session_id
                        print(f"[new session: {session_id[:8]}]", flush=True)
                    continue

                async for event in router.submit_stream(
                    self.name, prompt, session_id=session_id
                ):
                    if isinstance(event, TextDelta) and event.text:
                        sys.stdout.write(event.text)
                        sys.stdout.flush()
                    elif isinstance(event, ToolResult):
                        preview = event.content[:200].replace("\n", " ")
                        sys.stdout.write(f"\n[Tool: {preview}]\n")
                        sys.stdout.flush()
                    elif isinstance(event, Finish):
                        if event.reason not in ("stop",):
                            sys.stdout.write(f"\n[{event.reason}]\n")
                        else:
                            sys.stdout.write("\n")
                        sys.stdout.flush()
            except EOFError:
                self._running = False
                break
            except Exception:
                # One failed turn must not terminate the session; the next
                # prompt gets a clean slate.
                continue

        # Release the session slot so the shared session manager does not
        # accumulate one entry per REPL invocation.
        await router.session_manager.remove_session(session_id)

    async def stop(self) -> None:
        """Signal the REPL loop to exit at the next read boundary.

        Only flips the flag; it does not cancel anything. The in-flight
        ``run_in_executor`` readline finishes on its own, the loop condition
        fails, and the session is cleaned up by :meth:`start`. This keeps
        the shutdown path free of cancellations into a blocking C call.

        Idempotent: calling it twice, or before :meth:`start`, is a no-op.

        Returns:
            ``None``.
        """
        self._running = False
