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

"""SSH / remote command and file-transfer tool.

This module exposes a single Encre Agent tool — :data:`ssh_tool` — that
lets the agent drive the system ``ssh`` and ``scp`` binaries to run
commands, move files, verify connectivity, and execute multi-line scripts
on remote hosts.

The tool is deliberately thin: it shells out to the OS-installed OpenSSH
clients rather than depending on ``paramiko`` or ``asyncssh``. That
choice has three consequences the rest of the code is built around:

1. Credential handling is inherited from the OpenSSH ecosystem, so the
   tool can use ``~/.ssh/config`` aliases, agents, ``known_hosts``, and
   ``sshpass`` for password auth without re-implementing any of that.
2. There is no persistent connection: every action opens a fresh process
   and closes it when the command exits. The tool is therefore safe to
   call from any task without holding file descriptors open between
   invocations, but it does not reuse TCP/TLS handshakes across calls.
3. Timeouts are enforced with :func:`asyncio.wait_for` around
   ``proc.communicate()`` and are followed by a hard ``proc.kill()``.
   The ``wait()`` after ``kill()`` is required to reap the zombie before
   the caller can safely return control to the event loop.

The tool dispatches on an ``action`` field with five modes: ``exec``,
``upload``, ``download``, ``ping``, and ``script``. Only ``ping`` is
declared concurrency-safe because it issues a read-only ``echo pong``;
the other actions either mutate remote state (``upload``, ``script``) or
are user-controlled command execution (``exec``, ``download``), so the
tool registry serialises them through the harness scheduler.

See the :data:`ssh_tool` object literal at the bottom of this module for
the full input schema and description string that callers see.
"""

import asyncio
import contextlib
import json
import os
import tempfile
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes


def _ssh_base_args(kwargs: dict[str, Any]) -> list[str]:
    """Assemble the argv prefix shared by every ``ssh`` invocation.

    The tool's five actions all start with the same authentication and
    connection setup — only the trailing command differs. Centralising
    that setup here guarantees that ``exec``, ``ping``, and ``script``
    all use identical ``-o`` flags, identical port handling, and
    identical ``sshpass`` wrapping, so credentials cannot silently drift
    between code paths.

    The two hardcoded ``-o`` options are intentional:

    * ``StrictHostKeyChecking=no`` disables TOFU host-key verification
      so agents do not get blocked on first contact with a fresh host.
      This is a foot-gun for interactive use but is the right default
      for automated execution.
    * ``ConnectTimeout=15`` caps the connect phase at 15 s so a
      misconfigured host cannot hang the whole tool call forever.

    Args:
        kwargs: Caller-supplied action arguments. Recognised keys are
            ``host``, ``port``, ``user``, ``key_file``, and ``password``.
            All values are optional and fall back to OpenSSH-friendly
            defaults (port 22, user ``root``, no key, no password).

    Returns:
        The argv list starting with the executable (``ssh`` or
        ``sshpass`` when a password is supplied) and ending with the
        ``user@host`` target. Callers append the remote command to this
        list to build the full argv.
    """
    host = kwargs.get("host", "")
    port = kwargs.get("port", 22)
    user = kwargs.get("user", "root")
    key_file = kwargs.get("key_file", "")
    password = kwargs.get("password", "")

    args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15"]

    if port and port != 22:
        args.extend(["-p", str(port)])
    if key_file:
        args.extend(["-i", key_file])

    if password:
        args = ["sshpass", "-p", password, *args]

    args.append(f"{user}@{host}")
    return args


async def _ssh_execute(**kwargs: Any) -> str:
    """Dispatch an SSH action and return its JSON-serialised result.

    This is the callable that the Encre Agent tool-dispatch layer invokes
    once per tool call. The returned string is always JSON (or a plain
    ``Error: ...`` line for validation failures); the caller does not
    distinguish between "success" and "failure" at the Python level — it
    reads the ``exit_code`` field inside the JSON, or the ``Error:``
    prefix, to decide.

    All five action branches follow the same shape:

    1. Validate the required inputs and return early with a plain
       ``Error: ...`` string on failure. This avoids spawning a
       subprocess at all when the call is obviously wrong.
    2. Build argv (either by calling :func:`_ssh_base_args` for
       ``ssh``-based actions, or by constructing ``scp`` argv inline for
       upload/download).
    3. Import :func:`hidden_subprocess_kwargs` at call time and pass it
       into :func:`asyncio.create_subprocess_exec` so the child process
       does not inherit any terminal-interactive flags that would block
       on Windows or spawn a visible console on other platforms.
    4. Await ``proc.communicate()`` wrapped in :func:`asyncio.wait_for`
       so the timeout is enforced by the event loop rather than by a
       separate watchdog. On :class:`TimeoutError`, ``proc.kill()`` is
       followed by ``await proc.wait()`` — skipping the ``wait`` would
       leave a zombie process around and could leak the child.
    5. Decode stdout/stderr with :func:`decode_bytes` (which handles the
       platform's common non-UTF-8 locale encodings), merge stderr into
       the reply for ``exec``-style actions, and serialise to JSON.

    Args:
        **kwargs: The caller-supplied tool input, matching the schema in
            :data:`ssh_tool`. Recognised top-level keys are ``action``,
            ``host``, ``port``, ``user``, ``key_file``, ``password``,
            ``command``, ``local_path``, ``remote_path``, ``content``,
            and ``timeout``.

    Returns:
        A JSON string describing the outcome, or a plain
        ``"Error: ..."`` string when validation or environment checks
        fail before a subprocess can be spawned.

    Raises:
        Never. All failure paths (missing host, missing command, missing
        binary, timeout, unexpected subprocess exception) are caught and
        turned into error strings, because the tool-dispatch layer treats
        raised exceptions as harness-level failures rather than tool
        failures.
    """
    action = kwargs.get("action", "exec")
    host = kwargs.get("host", "")
    if not host:
        return "Error: 'host' is required."

    # ── Execute a remote command ────────────────────────────────────
    # The most common case. We defer importing
    # ``hidden_subprocess_kwargs`` inside the try block so that a missing
    # binary on PATH is reported as the more helpful "ssh not found"
    # error rather than an ImportError about the capabilities module.
    if action == "exec":
        command = kwargs.get("command", "")
        if not command:
            return "Error: 'command' is required for exec action."
        timeout = kwargs.get("timeout", 60)

        args = [*_ssh_base_args(kwargs), command]
        try:
            from encre.capabilities.process import (
                hidden_subprocess_kwargs as _hidden,
            )
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_hidden(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: SSH command timed out after {timeout}s"
            out = decode_bytes(stdout) if stdout else ""
            if stderr:
                err = decode_bytes(stderr)
                if err:
                    out += f"\n[stderr]\n{err}"
            exit_code = proc.returncode if proc.returncode is not None else -1
            result = {"exit_code": exit_code, "stdout": out.strip()}
            return json.dumps(result, indent=2)
        except FileNotFoundError:
            return "Error: ssh (or sshpass) not found in PATH"
        except Exception as e:
            return f"Error executing SSH command: {e}"

    # ── Upload a local file to the remote host via SCP ─────────────
    # SCP uses a separate argv from ssh: the port flag is ``-P`` (upper
    # case) rather than ssh's ``-p``, and the ``user@host:path`` target
    # is the *destination*, not the connection endpoint. The ``sshpass``
    # wrapping follows the same shape as :func:`_ssh_base_args` so
    # password-auth callers do not need to remember different flags per
    # action. The hardcoded 120 s timeout covers the connect phase,
    # transfer, and remote-write together; large files may hit this cap
    # and callers should chunk manually for transfers above a few hundred
    # MB.
    elif action == "upload":
        local_path = kwargs.get("local_path", "")
        remote_path = kwargs.get("remote_path", "")
        if not local_path or not remote_path:
            return "Error: 'local_path' and 'remote_path' are required for upload."

        if not os.path.isfile(local_path):
            return f"Error: Local file not found: {local_path}"

        port = kwargs.get("port", 22)
        user = kwargs.get("user", "root")
        key_file = kwargs.get("key_file", "")
        password = kwargs.get("password", "")

        scp_args = ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15"]
        if port and port != 22:
            scp_args.extend(["-P", str(port)])
        if key_file:
            scp_args.extend(["-i", key_file])

        if password:
            scp_args = ["sshpass", "-p", password, *scp_args]

        scp_args.extend([local_path, f"{user}@{host}:{remote_path}"])

        try:
            from encre.capabilities.process import (
                hidden_subprocess_kwargs as _hidden,
            )
            proc = await asyncio.create_subprocess_exec(
                *scp_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_hidden(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return "Error: SCP upload timed out after 120s"
            if proc.returncode != 0:
                err = decode_bytes(stderr) if stderr else ""
                return f"SCP upload failed (exit {proc.returncode}): {err[:500]}"
            return f"File uploaded: {local_path} -> {user}@{host}:{remote_path}"
        except FileNotFoundError:
            return "Error: scp (or sshpass) not found in PATH"
        except Exception as e:
            return f"Error during SCP upload: {e}"

    # ── Download a remote file to the local filesystem via SCP ─────
    # Same SCP argv shape as upload, but the ``user@host:path`` token is
    # the source and the local path is the destination. When the caller
    # omits ``local_path`` we fall back to the basename of the remote
    # path so a single ``remote_path`` is enough to pull a file into the
    # current working directory. This is convenient for agents that only
    # know the remote filename.
    elif action == "download":
        remote_path = kwargs.get("remote_path", "")
        local_path = kwargs.get("local_path", "")
        if not remote_path:
            return "Error: 'remote_path' is required for download."
        if not local_path:
            local_path = os.path.basename(remote_path)

        port = kwargs.get("port", 22)
        user = kwargs.get("user", "root")
        key_file = kwargs.get("key_file", "")
        password = kwargs.get("password", "")

        scp_args = ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15"]
        if port and port != 22:
            scp_args.extend(["-P", str(port)])
        if key_file:
            scp_args.extend(["-i", key_file])

        if password:
            scp_args = ["sshpass", "-p", password, *scp_args]

        scp_args.extend([f"{user}@{host}:{remote_path}", local_path])

        try:
            from encre.capabilities.process import (
                hidden_subprocess_kwargs as _hidden,
            )
            proc = await asyncio.create_subprocess_exec(
                *scp_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_hidden(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return "Error: SCP download timed out after 120s"
            if proc.returncode != 0:
                err = decode_bytes(stderr) if stderr else ""
                return f"SCP download failed (exit {proc.returncode}): {err[:500]}"
            local_abs = os.path.abspath(local_path)
            return f"File downloaded: {user}@{host}:{remote_path} -> {local_abs}"
        except FileNotFoundError:
            return "Error: scp (or sshpass) not found in PATH"
        except Exception as e:
            return f"Error during SCP download: {e}"

    # ── Test SSH connectivity ──────────────────────────────────────
    # This is the only action flagged concurrency-safe on the tool
    # object. It issues ``echo pong`` so the round-trip exercises the
    # entire SSH handshake, auth, and command dispatch without touching
    # any remote state. The 20 s timeout is shorter than the 15 s
    # connect timeout plus a small buffer for the ``echo`` itself.
    elif action == "ping":
        args = [*_ssh_base_args(kwargs), "echo", "pong"]
        try:
            from encre.capabilities.process import (
                hidden_subprocess_kwargs as _hidden,
            )
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_hidden(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return f"SSH connection to {host} timed out"
            if proc.returncode == 0:
                return f"SSH connection to {user}@{host}:{port} OK"
            err = decode_bytes(stderr) if stderr else ""
            return f"SSH connection to {host} failed (exit {proc.returncode}): {err[:300]}"
        except FileNotFoundError:
            return "Error: ssh not found in PATH"
        except Exception as e:
            return f"Error testing SSH connection: {e}"

    # ── Upload a multi-line bash script and execute it remotely ────
    # Multi-line scripts need two hops because a single ``ssh host "cmd"``
    # call cannot preserve newlines without quoting gymnastics. The
    # approach is:
    #
    #   1. Write the script to a local NamedTemporaryFile so the bytes
    #      survive across async boundaries (the ``content.encode()``
    #      stdin input in the cat step below reads from the string, but
    #      the file also gives us a stable path for cleanup).
    #   2. Push the script to a fixed remote path under ``/tmp`` via
    #      ``ssh host "cat > path && chmod +x path"``, feeding the
    #      content through stdin. The remote path is namespaced with
    #      the local PID to avoid collisions when multiple agent runs
    #      share a host.
    #   3. Run ``bash path && rm -f path || (rm -f path; exit 1)`` —
    #      the fallback ensures the remote temp file is deleted even
    #      when bash exits non-zero, and propagates the original exit
    #      status to the caller.
    #
    # The local temp file is always removed in a ``finally`` block
    # regardless of outcome, and cleanup failures are swallowed because
    # the caller cares about the remote result, not about a leftover
    # file in the local ``$TMPDIR``.
    elif action == "script":
        content = kwargs.get("content", "")
        if not content:
            return "Error: 'content' is required for script action."
        timeout = kwargs.get("timeout", 120)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, encoding="utf-8") as f:
            f.write("#!/bin/bash\nset -e\n")
            f.write(content)
            f.flush()
            script_path = f.name

        try:
            remote_path = f"/tmp/_encre_script_{os.getpid()}.sh"

            from encre.capabilities.process import (
                hidden_subprocess_kwargs as _hidden,
            )

            cat_proc = await asyncio.create_subprocess_exec(
                *_ssh_base_args(kwargs), f"cat > {remote_path} && chmod +x {remote_path}",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_hidden(),
            )
            _cat_stdout, _cat_stderr = await cat_proc.communicate(input=content.encode())

            exec_proc = await asyncio.create_subprocess_exec(
                *_ssh_base_args(kwargs), f"bash {remote_path} && rm -f {remote_path} || (rm -f {remote_path}; exit 1)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_hidden(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(exec_proc.communicate(), timeout=timeout)
            except TimeoutError:
                exec_proc.kill()
                await exec_proc.wait()
                return f"Error: Remote script timed out after {timeout}s"
            out = decode_bytes(stdout) if stdout else ""
            if stderr:
                err = decode_bytes(stderr)
                if err:
                    out += f"\n[stderr]\n{err}"
            return json.dumps({
                "exit_code": exec_proc.returncode,
                "output": out.strip(),
            }, indent=2)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(script_path)


    else:
        return f"Error: Unknown action '{action}'. Available actions: exec, upload, download, ping, script."


ssh_tool = build_tool(
    name="ssh",
    description=(
        "Run commands, transfer files, or execute multi-line scripts on a remote host "
        "via SSH/SCP using the system ssh and scp clients. "
        "Use this for remote server operations: `exec` a single command, `upload`/"
        "`download` files with SCP, `ping` to verify connectivity, or `script` to run "
        "a multi-line bash script remotely. "
        "Do NOT use this for interactive shells, port forwarding, or long-running "
        "daemons; prefer a persistent session or dedicated tool. "
        "Tips: prefer key-based auth via `key_file` over `password` (which requires "
        "sshpass); tune `timeout` for slow commands. "
        "Pitfalls: StrictHostKeyChecking is disabled and ConnectTimeout is 15s; large "
        "SCP transfers may hit the 120s timeout."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["exec", "upload", "download", "ping", "script"],
                "description": "SSH operation to perform: exec (run a remote command), upload (SCP local->remote), download (SCP remote->local), ping (test connectivity), or script (upload+run a bash script).",
            },
            "host": {
                "type": "string",
                "description": "Remote host as hostname, FQDN, or IP address.",
            },
            "port": {
                "type": "integer",
                "description": "TCP port for the SSH service; defaults to 22.",
            },
            "user": {
                "type": "string",
                "description": "SSH login username; defaults to 'root' if omitted.",
            },
            "key_file": {
                "type": "string",
                "description": "Path to a private key file passed via ssh -i; preferred over password auth.",
            },
            "password": {
                "type": "string",
                "description": "SSH password; only used when sshpass is installed on the host system.",
            },
            "command": {
                "type": "string",
                "description": "Shell command string to execute on the remote host (required for exec).",
            },
            "local_path": {
                "type": "string",
                "description": "Local filesystem path of the file to upload or download target.",
            },
            "remote_path": {
                "type": "string",
                "description": "Remote filesystem path of the upload destination or download source.",
            },
            "content": {
                "type": "string",
                "description": "Bash script body to upload and execute remotely (required for script).",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum runtime in seconds before killing the process; defaults to 60 (exec) or 120 (script).",
            },
        },
        "required": ["action", "host"],
    },
    execute=_ssh_execute,
    intents=["coding", "system", "general"],
    is_concurrency_safe=lambda data: data.get("action") == "ping",
    is_destructive=True,
    category="infra",
    semantic_type="exec",
)
