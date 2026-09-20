#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Lightweight HTTP admin endpoints for the Encre server.

These endpoints are served alongside the WebSocket upgrade by
:func:`encre.server.app.EncreServer.start` via the ``process_request`` hook.
They expose read-only operational data (health, config, session counts, stats)
and a single control action (cancel a running session).  All responses are JSON;
cross-site browser requests (non-local ``Origin``) are rejected to prevent
drive-by CSRF and CORS-based secret reads.
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
#: rejected.  The desktop app calls these endpoints from the Node main
#: process, which never sends an ``Origin`` header.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

#: Substrings (lowercase) of config keys whose values must never leave the
#: process in plaintext via the admin API.
_SECRET_MARKERS = ("api_key", "token", "secret", "password")


def _redact_secrets(data: Any) -> Any:
    """Recursively mask secret-looking values in a config dict."""
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
    body = json.dumps(data, ensure_ascii=False, indent=2)
    # No Access-Control-Allow-Origin: cross-origin browser reads are never
    # legitimate for these local admin endpoints (and are blocked upstream
    # by the Origin check in handle_admin).
    headers = [("Content-Type", "application/json")]
    return status, body, headers


def handle_admin(path: str, manager: SessionManager, request_headers: Any = None) -> tuple[int, str, list[tuple[str, str]]] | None:
    if not _origin_is_local(request_headers):
        return (403, json.dumps({"error": "cross-origin requests are not allowed"}), [("Content-Type", "application/json")])

    parsed = urlparse(path)
    base_path = parsed.path
    query = parse_qs(parsed.query)

    if base_path == "/health" or base_path == "/":
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

    if base_path == "/sessions":
        return _json_response({
            "sessions": manager.list_sessions(),
            "total": manager.active_count,
        })

    if base_path == "/stats":
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
        manager.set_session_state(session_id, SessionState.IDLE)
        return _json_response({"ok": True, "session_id": session_id})

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
                # No upstream 鈫?branch-branch=origin/HEAD else origin/<branch>
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
    """Basic sanity checks for the user-supplied workspace path."""
    if not workspace or not workspace.strip():
        return "workspace required"
    if "\x00" in workspace:
        return "invalid workspace"
    return None


def _build_compare_url(origin: str, branch: str) -> str:
    """Turn a git origin URL + branch into a web compare/pull-request URL."""
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
