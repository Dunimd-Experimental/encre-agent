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

"""HTTP admin surface for the running Encre server.

This module is the non-WebSocket half of the server.  :func:`handle_admin` is
invoked by the ``process_request`` hook installed in
:func:`encre.server.app.EncreServer.start`, on the same port and interface as
the agent WebSocket, and returns an HTTP response before any upgrade happens.
Everything here is JSON in and JSON out, and everything here is meant to be
called by the local Encre Agent Desktop rather than by the network at large.

The routes fall into three groups:

* read-only operational data -- ``/health``, ``/`` , ``/config``,
  ``/sessions``, ``/stats``, ``/datacenter/*``;
* server-wide control -- ``/sessions/<id>/cancel`` and ``/reload``;
* workspace git operations -- ``/git/status``, ``/git/diff``, ``/git/commit``,
  ``/git/push``, ``/git/pull``, ``/git/behind``, ``/git/pr``.

Two protections are enforced in :func:`handle_admin` itself, before any route
is reached, because the port is bound on ``localhost`` and therefore reachable
by anything else running on the machine:

* :func:`_origin_is_local` rejects cross-site browser requests (drive-by CSRF
  and CORS-based secret reads).  Legitimate callers come from the Node main
  process, which sends no ``Origin`` header.
* :func:`_redact_secrets` masks credential-shaped values before any config
  payload crosses the boundary.

Concurrency: :mod:`encre.server.app` offloads this whole function to a worker
thread via ``asyncio.to_thread`` because the git routes shell out with timeouts
of up to 60 seconds.  Anything here that touches an asyncio primitive must
therefore marshal back onto the loop explicitly -- see the cancel route.

Public API
    * :func:`handle_admin` -- the single route dispatch entry point.

Also imported for the handshake gate in :mod:`encre.server.app`:
    * :func:`_origin_is_local`
"""

import json
import os
import time
from typing import Any
from urllib.parse import urlparse, parse_qs

from encre.server.session_manager import SessionManager, SessionState

_start_time = time.time()

#: Hosts considered local.  Requests whose ``Origin`` header names any other
#: host come from a browser page (drive-by CSRF / CORS reading) and are
#: rejected.  The Encre Agent Desktop calls these endpoints from the Node main
#: process, which never sends an ``Origin`` header.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

#: Substrings (lowercase) of config keys whose values must never leave the
#: process in plaintext via the admin API.
_SECRET_MARKERS = ("api_key", "token", "secret", "password")


def _redact_secrets(data: Any) -> Any:
    """Recursively mask secret-looking values in a config dict.

    The key name decides, not the value: any key whose lowercased name
    contains one of :data:`_SECRET_MARKERS` is treated as a credential.
    Matching on the value would be guessable and would miss an empty-string
    key that still identifies a credential slot.

    Scalar strings are replaced outright.  Lists are walked element-wise so a
    ``tokens: [t1, t2]`` field is fully masked instead of being dropped or
    left as a marker string that breaks a caller expecting a list.  Values
    under a secret key that are not strings or lists (numbers, booleans,
    ``None``) pass through unchanged -- they carry no secret material.

    Args:
        data: Any nested dict/list/scalar structure, typically the decoded
            configuration from :mod:`encre.config_store`.

    Returns:
        A new structure of identical shape with secret values masked.  The
        input is never mutated, which matters because the caller may hold
        the only decoded copy in memory.
    """
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            lk = str(k).lower()
            if any(m in lk for m in _SECRET_MARKERS):
                if isinstance(v, str) and v:
                    out[k] = "***"
                elif isinstance(v, list):
                    out[k] = ["***" if isinstance(i, str) and i else i for i in v]
                else:
                    out[k] = v
            else:
                out[k] = _redact_secrets(v)
        return out
    if isinstance(data, list):
        return [_redact_secrets(i) for i in data]
    return data


def _origin_is_local(request_headers: Any) -> bool:
    """Return True when the request is not a cross-site browser request.

    Node (Electron main) requests carry no ``Origin`` header at all; browser
    pages always do.  Any ``Origin`` that is not one of the loopback hosts is
    treated as a drive-by attempt and rejected.

    The function is deliberately permissive in the missing-data case: a
    header mapping that cannot be read, an empty origin, or the literal
    ``"null"`` (opaque origins from ``file://`` and ``data://`` documents,
    which is how the desktop renderer itself connects) all pass.  Only a
    parseable ``http``/``https`` origin naming a foreign host fails.  That
    ordering keeps a header-object type mismatch from locking the machine out
    of its own server.

    Args:
        request_headers: The ASGI-style scope headers, mapping names to
            values, or ``None`` when the caller did not pass any.

    Returns:
        ``True`` when the request may proceed.
    """
    if request_headers is None:
        return True
    try:
        origin = request_headers.get("Origin")
    except Exception:
        return True
    if not origin or origin == "null":
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return True  # custom/desktop-app schemes
    return (parsed.hostname or "").lower() in _LOCAL_HOSTS


def _json_response(data: dict[str, Any], status: int = 200) -> tuple[int, str, list[tuple[str, str]]]:
    """Serialise *data* as a JSON HTTP response tuple.

    Every route in this module returns the same ``(status, body, headers)``
    shape so :mod:`encre.server.app` can apply one uniform normalisation
    (encoding the body, converting the header list) no matter which route
    produced it.

    Args:
        data: JSON-serialisable payload.
        status: HTTP status code.

    Returns:
        The response tuple.

    Note:
        No ``Access-Control-Allow-Origin`` header is emitted: cross-origin
        browser reads are never legitimate for these local admin endpoints
        (and are blocked upstream by the Origin check in handle_admin).
        Without the header the browser enforces the default deny.
    """
    body = json.dumps(data, ensure_ascii=False, indent=2)
    # No Access-Control-Allow-Origin: cross-origin browser reads are never
    # legitimate for these local admin endpoints (and are blocked upstream
    # by the Origin check in handle_admin).
    headers = [("Content-Type", "application/json")]
    return status, body, headers


def handle_admin(path: str, manager: SessionManager, request_headers: Any = None) -> tuple[int, str, list[tuple[str, str]]] | None:
    """Dispatch one admin HTTP path to its route handler.

    This is the entire routing table: a flat chain of ``base_path ==``
    comparisons, each returning a complete response.  A flat chain keeps the
    protocol readable end to end -- every route's exact path, query contract
    and error status is visible in one place -- and short-circuits so a
    request never runs more work than the first matching route.

    The function may execute in a worker thread (see the module docstring),
    so it never awaits and never touches an asyncio primitive directly.

    Args:
        path: The raw request target, including its query string.
        manager: The server's session manager, the source of truth for
            session state, counts and cancellation.
        request_headers: Request headers, used only for the origin check.

    Returns:
        ``(status, body, headers)`` for a matched route, or ``None`` for an
        unknown path.  ``None`` is meaningful: the caller is responsible for
        turning it into a 404, which lets it also try the plugin route
        fallback before giving up.

    Raises:
        Exception: ``json.dumps`` failures or a broken session manager would
            propagate; the caller in :mod:`encre.server.app` is expected to
            treat any raise as a 500-class failure.
    """
    if not _origin_is_local(request_headers):
        return (403, json.dumps({"error": "cross-origin requests are not allowed"}), [("Content-Type", "application/json")])

    parsed = urlparse(path)
    base_path = parsed.path
    query = parse_qs(parsed.query)

    if base_path == "/health" or base_path == "/":
        # Liveness probe.  ``active_sessions`` is a snapshot, not a count of
        # in-flight turns; the desktop main process polls this to decide
        # whether the backend it spawned is worth talking to.
        uptime = time.time() - _start_time
        return _json_response({
            "status": "ok",
            "uptime_seconds": round(uptime, 1),
            "active_sessions": manager.active_count,
        })

    if base_path == "/config":
        # The configuration is split across encrypted JSON files under
        # <data_dir>/config/ -- read it through the store.  This endpoint
        # used to ``tomllib.load`` the monolithic encrypted blob, which
        # always raised and returned "{}" no matter what was configured.
        from encre import config_store
        return _json_response(_redact_secrets(config_store.load_config()))

    if base_path == "/reload":
        # Write-side counterpart of ``/config``: re-read the configuration from
        # the store and put it in force on the live sessions, without a
        # restart.  The store is what the settings UI writes, so this is the
        # "apply the settings I just saved" action.
        #
        # A turn in flight streams through its session's backend and
        # collaborators, so reloading that session would tear the stream down:
        # those sessions are skipped and reported as skipped rather than
        # silently ignored, so the caller can retry once the turn ends.
        from encre.config import EncreConfig

        try:
            fresh = EncreConfig.from_file()
        except Exception as exc:
            return _json_response(
                {"error": f"could not load configuration: {exc}"}, 500,
            )

        reloaded: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for info in manager.iter_sessions():
            # The reload entry point lives on the session's agent loop, not on
            # the agent itself, because it is the loop that owns the live
            # backend and collaborator set being replaced.
            reload_fn = getattr(getattr(info.agent, "loop", None), "reload", None)
            if not callable(reload_fn):
                skipped.append({
                    "session_id": info.session_id, "reason": "no reloadable loop",
                })
                continue
            if info.agent_task is not None and not info.agent_task.done():
                skipped.append({
                    "session_id": info.session_id, "reason": "turn in flight",
                })
                continue
            try:
                report = reload_fn(fresh)
            except Exception as exc:
                skipped.append({
                    "session_id": info.session_id,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            reloaded.append({"session_id": info.session_id, **report.to_payload()})

        return _json_response({
            "ok": True,
            "reloaded": len(reloaded),
            "skipped": skipped,
            "sessions": reloaded,
        })

    if base_path == "/sessions":
        return _json_response({
            "sessions": manager.list_sessions(),
            "total": manager.active_count,
        })

    if base_path == "/stats":
        # "Running" means anything whose persisted state is not idle, which
        # intentionally includes a session paused mid-turn rather than only
        # one actively streaming to a model.
        sessions = manager.list_sessions()
        running = sum(1 for s in sessions if s.get("state", "idle") != "idle")
        uptime = time.time() - _start_time
        return _json_response({
            "uptime_seconds": round(uptime, 1),
            "total_sessions": len(sessions),
            "running_sessions": running,
            "idle_sessions": len(sessions) - running,
        })

    if base_path.startswith("/sessions/") and base_path.endswith("/cancel"):
        session_id = base_path.split("/")[2]
        info = manager.get_session(session_id)
        if info is None:
            return _json_response({"error": "Session not found"}, 404)
        if info.agent_task and not info.agent_task.done():
            # handle_admin may run in a worker thread (offloaded from the
            # event loop by server.app); Task.cancel is not thread-safe, so
            # marshal it back onto the task's own loop.
            task = info.agent_task
            loop = task.get_loop()
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                task.cancel()
        # State is force-reset to IDLE regardless of whether the cancel
        # landed, because the task's own cleanup may not run if the loop is
        # already shutting down; leaving it stuck as running would block the
        # UI's retry.
        manager.set_session_state(session_id, SessionState.IDLE)
        return _json_response({"ok": True, "session_id": session_id})

    if base_path == "/datacenter/survey":
        # Data-center page: file/byte statistics over the data root.  Pure
        # filesystem observation -- no store is decrypted here.
        from encre.server.datacenter import survey
        return _json_response(survey())

    if base_path == "/datacenter/module":
        # Data-center module page: the records behind one store.  Browser
        # stores are decrypted here (key never leaves the process; password
        # values are masked inside core before serialization).
        from encre.server.datacenter import module_rows
        module_id = query.get("id", [""])[0]
        # Module ids are joined by string against the renderer registry and
        # used to build filesystem paths, so anything but alphanumerics and
        # underscores is rejected rather than sanitised.
        if not module_id or not module_id.replace("_", "").isalnum():
            return _json_response({"error": "bad module id"}, 400)
        try:
            return _json_response(module_rows(module_id))
        except Exception as exc:  # noqa: BLE001 - surface the failure honestly
            logger.error("datacenter module %s failed: %s", module_id, exc)
            return _json_response({"error": str(exc)}, 500)

    if base_path == "/git/status":
        workspace = query.get("workspace", [""])[0]
        _ws_err = _workspace_error(workspace)
        if _ws_err:
            return _json_response({"error": _ws_err}, 400)
        try:
            from encre.git.repo import EncreGitRepo
            repo = EncreGitRepo(workspace=workspace)
            output = repo.get_porcelain_status()
            return _json_response({"output": output})
        except Exception as e:
            return _json_response({"error": str(e)})

    if base_path == "/git/diff":
        workspace = query.get("workspace", [""])[0]
        _ws_err = _workspace_error(workspace)
        if _ws_err:
            return _json_response({"error": _ws_err}, 400)
        # ``filter`` selects staged/working/all; ``file`` narrows to one path.
        filter_type = query.get("filter", ["all"])[0]
        file_path = query.get("file", [None])[0]
        try:
            from encre.git.repo import EncreGitRepo
            repo = EncreGitRepo(workspace=workspace)
            output = repo.get_diff_ex(filter_type=filter_type, file_path=file_path)
            return _json_response({"output": output})
        except Exception as e:
            return _json_response({"error": str(e)})

    if base_path == "/git/commit":
        import subprocess
        workspace = query.get("workspace", [""])[0]
        _ws_err = _workspace_error(workspace)
        if _ws_err:
            return _json_response({"error": _ws_err}, 400)
        # The message arrives through a query string, so an empty value means
        # the caller forgot it: refuse rather than letting git fail with an
        # unhelpful error.
        message = query.get("message", [""])[0]
        if not message:
            return _json_response({"error": "message required"}, 400)
        try:
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=15,
            )
            if result.returncode != 0:
                return _json_response({"error": result.stderr.strip()})
            return _json_response({"output": result.stdout})
        except Exception as e:
            return _json_response({"error": str(e)})

    if base_path == "/git/push":
        import subprocess
        workspace = query.get("workspace", [""])[0]
        _ws_err = _workspace_error(workspace)
        if _ws_err:
            return _json_response({"error": _ws_err}, 400)
        try:
            # If no upstream tracking is set, auto-configure origin/HEAD.
            check = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=15,
            )
            if check.returncode != 0:
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=workspace, timeout=10,
                ).stdout.strip()
                if branch:
                    subprocess.run(
                        ["git", "push", "--set-upstream", "origin", branch],
                        capture_output=True, cwd=workspace, timeout=60,
                    )
            result = subprocess.run(
                ["git", "push"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=60,
            )
            if result.returncode != 0:
                return _json_response({"error": result.stderr.strip()})
            return _json_response({"output": result.stdout or "Pushed."})
        except Exception as e:
            return _json_response({"error": str(e)})

    if base_path == "/git/pull":
        import subprocess
        workspace = query.get("workspace", [""])[0]
        _ws_err = _workspace_error(workspace)
        if _ws_err:
            return _json_response({"error": _ws_err}, 400)
        try:
            # If no upstream tracking is set, auto-configure origin/HEAD.
            check = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=15,
            )
            if check.returncode != 0:
                # No upstream -> wire up branch -> origin/HEAD explicitly.
                branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=workspace, timeout=10,
                ).stdout.strip()
                if branch:
                    subprocess.run(
                        ["git", "branch", "--set-upstream-to", f"origin/{branch}", branch],
                        capture_output=True, cwd=workspace, timeout=15,
                    )
            result = subprocess.run(
                ["git", "pull"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=60,
            )
            if result.returncode != 0:
                return _json_response({"error": result.stderr.strip()})
            return _json_response({"output": result.stdout or "Pulled."})
        except Exception as e:
            return _json_response({"error": str(e)})

    if base_path == "/git/behind":
        import subprocess
        workspace = query.get("workspace", [""])[0]
        _ws_err = _workspace_error(workspace)
        if _ws_err:
            return _json_response({"error": _ws_err}, 400)
        try:
            branch_res = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=10,
            )
            branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""
            if not branch:
                return _json_response({"behind": -1, "error": "no branch"})
            # ls-remote before fetch so a branch that never existed remotely
            # fails fast, instead of spending the fetch timeout on nothing.
            remote_res = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", branch],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=30,
            )
            if remote_res.returncode != 0 or not remote_res.stdout.strip():
                return _json_response({"behind": -1, "error": "no remote"})
            subprocess.run(
                ["git", "fetch", "origin", branch],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=30,
            )
            behind_res = subprocess.run(
                ["git", "rev-list", "--count", f"HEAD..origin/{branch}"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=15,
            )
            behind = int(behind_res.stdout.strip()) if behind_res.returncode == 0 else 0
            return _json_response({"behind": behind})
        except Exception as e:
            return _json_response({"behind": -1, "error": str(e)})

    if base_path == "/git/pr":
        import subprocess
        workspace = query.get("workspace", [""])[0]
        _ws_err = _workspace_error(workspace)
        if _ws_err:
            return _json_response({"error": _ws_err}, 400)
        try:
            # Push first so the remote branch exists.
            push_res = subprocess.run(
                ["git", "push"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=60,
            )
            # Resolve the origin URL and current branch to build a compare link.
            url_res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=10,
            )
            branch_res = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=workspace, timeout=10,
            )
            origin = url_res.stdout.strip() if url_res.returncode == 0 else ""
            branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""
            # Translate an SSH/SCP-style origin into a browsable compare URL.
            compare_url = _build_compare_url(origin, branch)
            # The push can fail while the compare link is still valid and
            # useful (branch already pushed, message already up to date), so
            # the URL is returned in the error response as well.
            if push_res.returncode != 0 and "Everything up-to-date" not in (push_res.stderr or ""):
                return _json_response({
                    "error": push_res.stderr.strip(),
                    "compare_url": compare_url,
                })
            return _json_response({
                "output": push_res.stdout or "Pushed.",
                "compare_url": compare_url,
            })
        except Exception as e:
            return _json_response({"error": str(e)})

    return None


def _workspace_error(workspace: str | None) -> str | None:
    """Basic sanity checks for the user-supplied workspace path.

    Every git route takes its working directory from a query string, i.e. it
    is caller-controlled and goes straight into ``subprocess.run(cwd=...)``.
    The checks here are deliberately minimal but non-negotiable: an empty
    value would make git run in the server's own directory, and a NUL byte
    would fail with a cryptic encoding error rather than a clear one.

    Args:
        workspace: The ``workspace`` query parameter.

    Returns:
        A human-readable error message to send back, or ``None`` if the path
        is acceptable.
    """
    if not workspace or not workspace.strip():
        return "workspace required"
    if "\x00" in workspace:
        return "invalid workspace"
    return None


def _build_compare_url(origin: str, branch: str) -> str:
    """Turn a git origin URL + branch into a web compare/pull-request URL.

    ``/git/pr`` only pushes; opening the PR must happen in the user's
    browser.  The URL is synthesised rather than read from git config so it
    works for a fresh clone with no remote metadata beyond ``origin``.

    Args:
        origin: The ``git remote get-url origin`` result.
        branch: The current branch name.

    Returns:
        A browsable ``compare/<branch>?expand=1`` URL, or ``""`` when the
        origin cannot be mapped -- SCP-style forms that are not host:path, or
        URLs that are not http(s).  An empty string is the renderer's signal
        to hide the button.
    """
    if not origin or not branch:
        return ""
    url = origin.strip()
    # git@github.com:owner/repo.git -> https://github.com/owner/repo
    if url.startswith("git@"):
        url = url.replace(":", "/").replace("git@", "https://")
    if url.endswith(".git"):
        url = url[:-4]
    if not url.startswith("http"):
        return ""
    sep = "" if url.endswith("/") else "/"
    return f"{url}{sep}compare/{branch}?expand=1"
