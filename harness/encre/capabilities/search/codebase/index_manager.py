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

"""Background index manager -- owns workspace code indices independently of
any WebSocket connection.  Lives at the EncreServer level and survives
window open/close cycles.

Indexing runs in a **subprocess** to avoid blocking the server's event loop.
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from typing import Any

from .indexer import EncreCodeIndex

logger = logging.getLogger("encre.capabilities.search.codebase.index_manager")

_INDEXER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_data_dir() -> str:
    """Return the Encre data directory (from env or the default ``~/.dunimd``)."""
    return os.environ.get("ENCRE_DATA_DIR", os.path.join(os.path.expanduser("~"), ".dunimd", "encre"))


def _progress_path(ws_id: str) -> str:
    """Return the path of the JSON progress file for workspace *ws_id*."""
    return os.path.join(_get_data_dir(), "iwork", ws_id, "index_progress.json")


def _metadata_path(ws_id: str) -> str:
    """Return the path of the cached index metadata file for *ws_id*."""
    return os.path.join(_get_data_dir(), "iwork", ws_id, "index_metadata.json")


class IndexManager:
    """Manages code indices per workspace, with background building and
    progress subscribers.

    Thread safety: all access to ``self._indices`` is protected by
    ``self._lock``.
    """

    def __init__(self) -> None:
        """Initialise per-workspace index registries and synchronisation state."""
        self._lock = threading.Lock()
        self._indices: dict[str, dict[str, Any]] = {}
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._failed: set[str] = set()  # ws_ids whose last index attempt permanently failed
        # Optional secondary indices: AST and embedding.  They are loaded
        # lazily on demand so workspaces that do not need them pay no
        # startup cost.  We keep them per-workspace, mirroring the
        # primary BM25 index.
        self._ast_indices: dict[str, Any] = {}
        self._embedding_indices: dict[str, Any] = {}
        self._enable_file_watcher: bool = False
        # Callback fired when a workspace's code index finishes loading.
        # Signature: ``f(ws_id: str, index: EncreCodeIndex) -> None``.
        # Used by the WebSocket handler to inject the index into the
        # running agent so the conversation never blocks on indexing.
        self._on_index_ready: Callable[[str, Any], None] | None = None

    def set_on_index_ready(self, callback: Callable[[str, Any], None] | None) -> None:
        """Register a callback invoked when a code index finishes loading.

        The callback receives ``(ws_id, EncreCodeIndex)``.  Pass
        ``None`` to clear.
        """
        self._on_index_ready = callback

    def set_file_watcher(self, enabled: bool) -> None:
        """Enable or disable the auto-start file watcher on index ready."""
        self._enable_file_watcher = enabled

    # 鈹€鈹€ Public API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def start_index(self, ws_id: str, ws_path: str, force: bool = False) -> None:
        """Start building index for *ws_path* in a background subprocess.

        If the workspace already has a built index in memory and no index
        task is running, this is a no-op (the index is already ready).

        If *force* is ``True``, the existing index is discarded and rebuilt
        from scratch (used by reindex).  Failed workspaces are only retried  # noqa: E402
        when *force* is ``True``.

        If the workspace is already being indexed, this is a no-op.
        """
        with self._lock:
            # Never auto-retry a workspace whose subprocess already failed
            if not force and ws_id in self._failed:
                logger.debug("[index_manager] ws=%s is in failed state, skipping auto-retry", ws_id)
                return
            entry = self._indices.get(ws_id)
            if entry and entry.get("subprocess") is not None:
                logger.debug("[index_manager] already indexing ws=%s", ws_id)
                return
            # Already has a built index -- skip rebuild unless forced
            if not force and entry and entry.get("index") is not None:
                logger.debug("[index_manager] already indexed ws=%s, skipping", ws_id)
                return
            existing = self._indices.get(ws_id, {})
            existing_subs = list(existing.get("subscribers", []))
            self._indices[ws_id] = {
                "index": None,
                "subprocess": None,
                "progress": {"progress": 0, "status": "indexing", "files": 0},
                "subscribers": existing_subs,
            }
        # Notify subscribers immediately that indexing has started
        self._notify(ws_id, {"progress": 0, "status": "indexing", "files": 0})
        asyncio.get_running_loop()
        task = asyncio.ensure_future(self._spawn_indexer(ws_id, ws_path))
        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                entry["task"] = task

    def reindex(self, ws_id: str, ws_path: str) -> None:
        """Force a full re-index: cancel any in-flight task, start fresh."""
        self.cancel_index(ws_id)
        # Drop the on-disk cache (metadata/progress/index files) so the
        # spawned indexer actually rebuilds instead of taking the cache-hit
        # path and instantly reporting 100%.
        self._remove_index_files(ws_id, ws_path)
        with self._lock:
            self._failed.discard(ws_id)
            entry = self._indices.get(ws_id)
            if entry:
                entry["index"] = None
        self.start_index(ws_id, ws_path, force=True)

    def delete_index(self, ws_id: str, ws_path: str) -> None:
        """Delete on-disk index files and metadata, clear in-memory state."""
        self.cancel_index(ws_id)
        self._remove_index_files(ws_id, ws_path)
        with self._lock:
            self._indices.pop(ws_id, None)
            self._ast_indices.pop(ws_id, None)
            self._embedding_indices.pop(ws_id, None)

    def cancel_index(self, ws_id: str) -> None:
        """Cancel any running index subprocess for *ws_id*."""
        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                proc = entry.get("subprocess")
                if proc and proc.returncode is None:
                    proc.kill()
                entry["subprocess"] = None
        # Cancel the asyncio task that monitors the subprocess
        task = self._poll_tasks.pop(ws_id, None)
        if task and not task.done():
            task.cancel()

    def shutdown(self) -> None:
        """Cancel all running index tasks (called during server shutdown)."""
        with self._lock:
            for ws_id in list(self._indices.keys()):
                entry = self._indices[ws_id]
                proc = entry.get("subprocess")
                if proc and proc.returncode is None:
                    proc.kill()
        for task in self._poll_tasks.values():
            if not task.done():
                task.cancel()
        self._poll_tasks.clear()

    # 鈹€鈹€ Subscriber API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def subscribe(self, ws_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a progress callback for *ws_id*.

        The callback receives a dict with keys ``progress``, ``status``,
        ``files``.  The current state is sent immediately on subscribe.
        """
        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                entry["subscribers"].append(callback)
                try:
                    data = dict(entry["progress"])
                    proc = entry.get("subprocess")
                    is_running = proc is not None and proc.returncode is None
                    has_index = entry["index"] is not None
                    if is_running:
                        data["status"] = "indexing"
                    elif has_index:
                        data["status"] = "ready"
                    else:
                        data["status"] = "idle"
                    callback(data)
                except Exception:
                    pass

    def unsubscribe(self, ws_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            entry = self._indices.get(ws_id)
            if entry and callback in entry["subscribers"]:
                entry["subscribers"].remove(callback)

    # 鈹€鈹€ Query API 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def get_index(self, ws_id: str) -> EncreCodeIndex | None:
        """Return the built ``EncreCodeIndex`` for *ws_id*, or ``None``."""
        with self._lock:
            entry = self._indices.get(ws_id)
            return entry["index"] if entry else None

    def get_ast_index(self, ws_id: str) -> Any | None:
        """Return the cached AST index for *ws_id* if one is available.

        The AST index is a heavyweight structure (tree-sitter parse
        trees) that we only instantiate when a caller actually asks
        for it.  This method does **not** block on a running build --
        it returns whatever is currently in memory, or ``None`` if no
        AST index has been built yet.  Callers that want to build one
        should call :meth:`ensure_ast_index` (which dispatches to a
        thread) or :meth:`start_index` first.
        """
        with self._lock:
            return self._ast_indices.get(ws_id)

    def get_embedding_index(self, ws_id: str) -> Any | None:
        """Return the cached embedding index for *ws_id* if one is available.

        The embedding index requires an OpenAI-compatible embedding
        backend.  When no API key is configured, this method returns
        ``None`` and no embedding index is ever instantiated for this
        workspace.
        """
        with self._lock:
            return self._embedding_indices.get(ws_id)

    async def ensure_ast_index(self, ws_id: str, ws_path: str) -> Any | None:
        """Build (or load from cache) the AST index for *ws_id* on demand.

        Returns the populated :class:`EncreASTIndex` or ``None`` if
        tree-sitter is not installed in the current environment.
        """
        from encre.capabilities.search.codebase.ast_index import EncreASTIndex
        with self._lock:
            existing = self._ast_indices.get(ws_id)
            if existing is not None and getattr(existing, "_indexed", False):
                return existing
        loop = asyncio.get_running_loop()
        try:
            ast_idx = await loop.run_in_executor(None, lambda: EncreASTIndex(ws_path))
        except Exception as exc:
            logger.warning("[index_manager] failed to build AST index ws=%s: %s", ws_id, exc)
            return None
        if not ast_idx.available:
            # tree-sitter not installed -- store the no-op shell so we
            # don't repeatedly try to build it
            with self._lock:
                self._ast_indices[ws_id] = ast_idx
            return ast_idx
        try:
            await loop.run_in_executor(None, ast_idx.scan)
        except Exception as exc:
            logger.warning("[index_manager] AST scan failed ws=%s: %s", ws_id, exc)
        with self._lock:
            self._ast_indices[ws_id] = ast_idx
        return ast_idx

    async def ensure_embedding_index(
        self,
        ws_id: str,
        ws_path: str,
        embedding_fn: Any | None = None,
    ) -> Any | None:
        """Build (or load from cache) the embedding index for *ws_id*.

        Returns the populated :class:`EncreEmbeddingIndex` or ``None``
        when no embedding backend is configured (i.e. no API key).
        """
        from encre.capabilities.search.codebase.embedding_index import EncreEmbeddingIndex
        if embedding_fn is None and not os.environ.get("OPENAI_API_KEY"):
            # No embedding backend -- nothing to build.
            return None
        with self._lock:
            existing = self._embedding_indices.get(ws_id)
            if existing is not None and getattr(existing, "_indexed", False):
                return existing
        loop = asyncio.get_running_loop()
        try:
            emb_idx = await loop.run_in_executor(
                None,
                lambda: EncreEmbeddingIndex(ws_path, embedding_fn=embedding_fn),
            )
        except Exception as exc:
            logger.warning(
                "[index_manager] failed to build embedding index ws=%s: %s", ws_id, exc,
            )
            return None
        if not emb_idx.available:
            await loop.run_in_executor(None, emb_idx.scan)
        with self._lock:
            self._embedding_indices[ws_id] = emb_idx
        return emb_idx

    def get_task(self, ws_id: str) -> asyncio.Task | None:
        """Return the active build task for *ws_id*, or ``None``."""
        with self._lock:
            entry = self._indices.get(ws_id)
            return entry.get("task") if entry else None

    def get_status(self, ws_id: str) -> dict[str, Any]:
        """Return current indexing status dict."""
        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                proc = entry.get("subprocess")
                is_running = proc is not None and proc.returncode is None
                has_index = entry["index"] is not None
                if is_running:
                    status = "indexing"
                elif has_index:
                    status = "ready"
                elif ws_id in self._failed:
                    status = "error"
                else:
                    status = "idle"
                prog = entry["progress"]
                return {
                    "status": status,
                    "progress": prog.get("progress", 0),
                    "files": prog.get("files", 0),
                }
            return {"status": "no_workspace", "progress": 0, "files": 0}

    # 鈹€鈹€ Internal 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    async def _spawn_indexer(self, ws_id: str, ws_path: str) -> None:
        """Spawn ``index_worker.py`` as a subprocess and poll its progress."""
        loop = asyncio.get_running_loop()
        data_dir = _get_data_dir()

        prog_path = _progress_path(ws_id)
        os.makedirs(os.path.dirname(prog_path), exist_ok=True)
        # Clear old progress
        with contextlib.suppress(OSError):
            os.remove(prog_path)

        # Check for cached metadata (skip re-index if already done)
        meta_path = _metadata_path(ws_id)
        if os.path.isfile(meta_path):
            logger.info("[index_manager] cache hit ws=%s", ws_id)
            try:
                idx = await loop.run_in_executor(None, lambda: EncreCodeIndex(ws_path))
                if idx and idx._indexed:
                    await loop.run_in_executor(None, idx.scan_incremental)
                    await loop.run_in_executor(None, idx.prepare_query)
            except Exception:
                idx = None
            if idx and not idx._need_reindex:
                self._notify(ws_id, {"progress": 100, "status": "ready", "files": len(idx._modules)})
                with self._lock:
                    entry = self._indices.get(ws_id)
                    if entry:
                        entry["index"] = idx
                self._fire_index_ready(ws_id, idx)
                return

        logger.info("[index_manager] cache miss ws=%s, spawning subprocess", ws_id)

        # Spawn the worker subprocess
        python = sys.executable
        worker_script = os.path.join(_INDEXER_ROOT, "codebase", "index_worker.py")
        env = os.environ.copy()
        if _INDEXER_ROOT not in env.get("PYTHONPATH", ""):
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{_INDEXER_ROOT}{os.pathsep}{existing}" if existing else _INDEXER_ROOT
        env["ENCRE_DATA_DIR"] = data_dir

        # Redirect stderr to a log file so crashes are debuggable
        stderr_path = os.path.join(os.path.dirname(prog_path), "index_stderr.log")
        try:
            os.makedirs(os.path.dirname(stderr_path), exist_ok=True)
            stderr_file = open(stderr_path, "wb")  # noqa: SIM115
        except Exception:
            stderr_file = None

        from encre.capabilities.process import hidden_subprocess_kwargs
        kwargs = hidden_subprocess_kwargs()

        proc = await asyncio.create_subprocess_exec(
            python, worker_script,
            "--ws-id", ws_id,
            "--ws-path", ws_path,
            "--data-dir", data_dir,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_file or asyncio.subprocess.DEVNULL,
            **kwargs,
        )

        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                entry["subprocess"] = proc

        self._notify(ws_id, {"progress": 0, "status": "indexing", "files": 0})

        # Poll the progress file while the subprocess runs
        async def _poll_progress():
            while True:
                await asyncio.sleep(2)
                try:
                    with open(prog_path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._notify(ws_id, data)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                # Check if subprocess exited
                if proc.returncode is not None:
                    break

        poller = asyncio.create_task(_poll_progress())
        self._poll_tasks[ws_id] = poller

        try:
            await asyncio.wait_for(proc.wait(), timeout=3600)
        except TimeoutError:
            logger.warning("[index_manager] subprocess TIMEOUT after 600s ws=%s", ws_id)
            proc.kill()
        finally:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
            self._poll_tasks.pop(ws_id, None)
            if stderr_file:
                with contextlib.suppress(Exception):
                    stderr_file.close()

        # Read final result or error
        try:
            with open(prog_path, encoding="utf-8") as f:
                final = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            final = {}

        status = final.get("status", "idle")
        if status == "ready":
            # Load the index from disk
            try:
                idx = await loop.run_in_executor(None, lambda: EncreCodeIndex(ws_path))
                with self._lock:
                    entry = self._indices.get(ws_id)
                    if entry:
                        entry["index"] = idx
                self._fire_index_ready(ws_id, idx)
            except Exception as e:
                logger.warning("[index_manager] failed to load index after build: %s", e)
                status = f"error: {e}"

        is_ready = status == "ready"
        self._notify(ws_id, {
            "progress": final.get("progress", 0),
            "status": status,
            "files": final.get("files", 0),
        })

        # Persist metadata so the next workspace open treats this as a cache hit
        # instead of re-spawning the subprocess and clearing the progress file.
        if is_ready:
            try:
                from encre.protocol.handlers.workspace_store import _save_index_metadata

                _save_index_metadata(ws_id, final.get("files", 0))
            except Exception:
                logger.warning("[index_manager] failed to save index metadata for ws=%s", ws_id, exc_info=True)

        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                entry["subprocess"] = None
            if not is_ready:
                logger.warning("[index_manager] subprocess FAILED ws=%s status=%s", ws_id, status)
                self._failed.add(ws_id)
            else:
                self._failed.discard(ws_id)

    def _remove_index_files(self, ws_id: str, ws_path: str) -> None:
        """Remove on-disk index files for a workspace."""
        code_index_path = os.path.join(ws_path, ".encre", "code_index.json")
        try:
            if os.path.isfile(code_index_path):
                os.remove(code_index_path)
        except Exception:
            pass

        meta_path = _metadata_path(ws_id)
        try:
            if os.path.isfile(meta_path):
                os.remove(meta_path)
        except Exception:
            pass

        prog_path = _progress_path(ws_id)
        try:
            if os.path.isfile(prog_path):
                os.remove(prog_path)
        except Exception:
            pass

    def _notify(self, ws_id: str, data: dict[str, Any]) -> None:
        """Notify all subscribers of *ws_id* with *data*."""
        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                entry["progress"] = {
                    "progress": data.get("progress", 0),
                    "status": data.get("status", "indexing"),
                    "files": data.get("files", 0),
                }
                for cb in list(entry["subscribers"]):
                    with contextlib.suppress(Exception):
                        cb(data)

    def _fire_index_ready(self, ws_id: str, idx: Any) -> None:
        """Fire the ``_on_index_ready`` callback (registered by the WS handler).

        Called after a code index has been fully built and stored in
        ``self._indices[ws_id]["index"]``.  The callback runs **outside**
        ``self._lock`` so the handler can safely call back into the
        manager without deadlocking.
        """
        cb = self._on_index_ready
        if cb is not None:
            try:
                cb(ws_id, idx)
            except Exception:
                logger.warning("[index_manager] on_index_ready callback failed ws=%s", ws_id, exc_info=True)
        # Auto-start file watcher when the feature flag is enabled.
        if self._enable_file_watcher:
            try:
                import asyncio
                asyncio.ensure_future(idx.watch())
                logger.info("[index_manager] auto-started file watcher ws=%s", ws_id)
            except Exception:
                logger.warning("[index_manager] failed to start file watcher ws=%s", ws_id, exc_info=True)

    async def _load_cached_index(self, ws_id: str, ws_path: str) -> None:
        """Load a previously-built index from disk without spawning a subprocess."""
        loop = asyncio.get_running_loop()
        try:
            idx = await loop.run_in_executor(None, lambda: EncreCodeIndex(ws_path))
        except Exception as e:
            logger.warning("[index_manager] failed to load cached index ws=%s: %s", ws_id, e)
            idx = None
        with self._lock:
            entry = self._indices.get(ws_id)
            if entry:
                entry["index"] = idx
                if idx is not None:
                    entry["progress"] = {"progress": 100, "status": "ready", "files": len(idx._modules)}
                else:
                    entry["progress"] = {"progress": 0, "status": "error", "files": 0}
            if idx:
                self._notify(ws_id, {"progress": 100, "status": "ready", "files": len(idx._modules)})
                self._fire_index_ready(ws_id, idx)
            else:
                self._notify(ws_id, {"progress": 0, "status": "error", "files": 0})
