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

import asyncio
import json
import os
import re
import threading
import time
from typing import Any

from encre.capabilities.search.codebase.document_manager import EncreDocumentManager
from encre.capabilities.search.codebase.indexer import EncreCodeIndex
from encre.config import EncreConfig
from encre.git.repo import EncreGitRepo


class _CacheMiss:
    """Sentinel returned by :meth:`PromptCache.get` on a miss.

    A dedicated object (not ``None``) because several builders legitimately
    cache falsy values such as ``""``.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "PromptCache.MISS"


class PromptCache:
    """The single prompt cache behind every :class:`ContextBuilder` builder.

    One implementation replaces the former per-builder
    ``(cache_key, built_at, value)`` tuples: entries live in one dict keyed by
    a hashable ``("namespace", ...)`` key, freshness is delegated to the
    ``cache_fresh`` callback wired from the loop (identical TTL semantics --
    the callback carries its own default TTL), and invalidation is per-key,
    per-namespace or whole-cache.

    Memory profile matches the old single-slot idiom: a stale entry is
    evicted on read, and ``set`` sweeps expired entries once the map grows
    past :attr:`_MAX_ENTRIES_BEFORE_SWEEP`, so high-cardinality keys (e.g.
    recall queries) cannot accumulate unboundedly.

    Thread-safety: the builders are normally driven by the loop task, but a
    ``ContextBuilder`` can also be reached from worker threads (tool
    executors, sub-agents); a lock guards the entry map.  It is uncontended
    on the common path, so the cost is negligible.
    """

    MISS: Any = _CacheMiss()
    # Above this many entries a set() sweeps expired ones (bounds memory for
    # high-cardinality keys while keeping the common path O(1)).
    _MAX_ENTRIES_BEFORE_SWEEP = 128

    def __init__(self, cache_fresh: Any) -> None:
        self._cache_fresh = cache_fresh
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        """Return the cached value for ``key``, or :attr:`MISS`.

        A hit requires an entry stored under exactly ``key`` whose build
        timestamp still passes the ``cache_fresh`` check.  A stale entry is
        evicted on read (the old single-slot tuples were overwritten in
        place; a keyed map needs explicit eviction to stay bounded).
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return self.MISS
            built_at, value = entry
        if not self._cache_fresh(built_at):
            with self._lock:
                self._entries.pop(key, None)
            return self.MISS
        return value

    def set(self, key: Any, value: Any) -> None:
        """Store ``value`` under ``key``, stamped with the current time."""
        with self._lock:
            self._entries[key] = (time.time(), value)
            if len(self._entries) > self._MAX_ENTRIES_BEFORE_SWEEP:
                self._entries = {
                    k: v for k, v in self._entries.items()
                    if self._cache_fresh(v[0])
                }

    def invalidate(self, key: Any) -> None:
        """Drop the entry stored under ``key`` (no-op when absent)."""
        with self._lock:
            self._entries.pop(key, None)

    def invalidate_namespace(self, namespace: str) -> None:
        """Drop every entry whose key is a tuple starting with ``namespace``.

        Used where the old single-slot caches were cleared without a key
        available (a freshly injected code index invalidates every
        codebase-context entry regardless of its exact key).
        """
        with self._lock:
            stale = [k for k in self._entries
                     if isinstance(k, tuple) and k and k[0] == namespace]
            for key in stale:
                del self._entries[key]

    def invalidate_all(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._entries.clear()


class ContextBuilder:
    """Builds all context / prompt blocks injected into the system message.

    Owns the unified :class:`PromptCache` (one keyed cache behind every
    prompt builder) and related state (``_document_manager``, ``_code_index``,
    etc.) so EncreLoop no longer carries them directly.
    """

    def __init__(self, config: EncreConfig, session: Any, *,
                 cache_fresh: Any,
                 memory_system: Any = None,
                 recall_engine: Any = None,
                 soul_system: Any = None,
                 profile_system: Any = None,
                 git: Any = None,
                 rules_loader: Any = None,
                 device_context_manager: Any = None) -> None:
        self._config = config
        self._session = session
        # The freshness callback (TTL semantics live on the wired callable);
        # the shared PromptCache owns the entries.
        self._prompt_cache = PromptCache(cache_fresh)

        self.memory_system = memory_system
        # The single retrieval path: associative recall over the layer store.
        # May be attached after construction (the loop builds its memory
        # subsystem later than this builder) or stay ``None`` when the memory
        # subsystem is unavailable -- the prompt path then degrades to "".
        self.recall_engine = recall_engine
        self.soul_system = soul_system
        self.profile_system = profile_system
        self._git = git
        self._rules_loader = rules_loader

        self._document_manager: EncreDocumentManager | None = None
        self._document_manager_data_dir: str | None = None
        self._code_index: EncreCodeIndex | None = None
        self._device_context_manager = device_context_manager

    # 鈹€鈹€ Directory tree (static helper) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_directory_tree(self, ws_path: str, max_depth: int = 4, max_entries: int = 200) -> str:
        skip_dirs = {"node_modules", "__pycache__", ".git", ".venv", "venv",
                     "target", "build", "dist", ".tox", ".eggs",
                     ".mypy_cache", ".pytest_cache", ".ruff_cache",
                     ".svn", ".hg", ".idea", ".vscode"}
        skip_ext = {".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe"}
        lines: list[str] = []
        total_files = 0
        try:
            for root, dirs, files in os.walk(ws_path):
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d not in skip_dirs]
                rel = os.path.relpath(root, ws_path)
                if rel == ".":
                    rel = ""
                depth = rel.count(os.sep) + 1 if rel else 0
                if depth > max_depth:
                    continue
                indent = "  " * depth
                if depth == 0:
                    lines.append("\U0001f4c1 workspace/")
                else:
                    basename = os.path.basename(root)
                    lines.append(f"{indent}\U0001f4c1 {basename}/")
                for fname in sorted(files):
                    if fname.startswith("."):
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in skip_ext:
                        continue
                    if len(lines) >= max_entries:
                        break
                    lines.append(f"{indent}  \U0001f4c4 {fname}")
                    total_files += 1
                if len(lines) >= max_entries:
                    lines.append(f"  ... (truncated at {max_entries} entries)")
                    break
        except (OSError, PermissionError):
            pass
        if not lines:
            return ""
        header = (
            f"## Workspace Structure\n"
            f"{total_files} files shown (tree depth \u2264{max_depth}). "
            f"Index is still building \u2014 full file contents coming soon.\n"
            f"```\n" + "\n".join(lines) + "\n```"
        )
        return header

    # 鈹€鈹€ Workspace info 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def workspace_info(self) -> tuple[str, str, str]:
        """Return (workspace_root, workspace_name, project_summary) for the prompt builder.

        Returns ("", "", "") when not running inside a workspace.

        Cache key includes the git branch so that switching branches
        automatically refreshes the workspace context.
        """
        ws_path = getattr(self._config, "workspace", "") or ""
        if not ws_path or not os.path.isdir(ws_path):
            self._prompt_cache.invalidate(("workspace", ws_path))
            return "", "", ""
        cache_key = ws_path
        try:
            repo = self._git
            if repo is not None:
                branch = repo.get_branch() if hasattr(repo, "get_branch") else ""
                if branch:
                    cache_key = f"{ws_path}@{branch}"
        except Exception:
            pass
        cached = self._prompt_cache.get(("workspace", cache_key))
        if cached is not PromptCache.MISS:
            return cached

        ws_name = os.path.basename(ws_path)

        yim_dir = os.path.join(ws_path, ".encre")
        ws_config_path = os.path.join(yim_dir, "config.json")
        ws_config: dict[str, Any] = {}
        if os.path.isfile(ws_config_path):
            try:
                with open(ws_config_path, encoding="utf-8") as f:
                    ws_config = json.load(f)
            except Exception:
                pass

        summary_lines: list[str] = []

        custom_prompt = ws_config.get("system_prompt", "")
        if custom_prompt:
            summary_lines.append("Project-specific instructions:")
            summary_lines.append(custom_prompt)
            summary_lines.append("")

        try:
            visible: list[tuple[str, bool]] = []
            with os.scandir(ws_path) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(".") and name != ".encre":
                        continue
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    visible.append((name, is_dir))
            visible.sort(key=lambda item: (not item[1], item[0]))
            if visible:
                summary_lines.append("Top-level entries:")
                for name, is_dir in visible[:40]:
                    prefix = "/" if is_dir else " "
                    summary_lines.append(f"  {prefix}{name}")
                if len(visible) > 40:
                    summary_lines.append(f"  ... and {len(visible) - 40} more entries")
        except Exception:
            pass

        try:
            git_repo = EncreGitRepo(ws_path)
            if git_repo.is_in_repo():
                state = git_repo.get_state()
                summary_lines.append("")
                summary_lines.append("Git status:")
                summary_lines.append(f"  branch: {state.branch}")
                summary_lines.append(f"  clean: {'yes' if state.is_clean else 'no'}")
                if state.changed_files:
                    summary_lines.append(f"  changed: {', '.join(state.changed_files[:20])}")
                if state.untracked_files:
                    summary_lines.append(f"  untracked: {', '.join(state.untracked_files[:10])}")
                if state.recent_commits:
                    summary_lines.append("  recent commits:")
                    for commit in state.recent_commits[:5]:
                        summary_lines.append(f"    {commit}")
        except Exception:
            pass

        # Project profile: tech-stack detection + entry points.  The agent's
        # understanding of an unfamiliar workspace is weak because the tree
        # alone does not tell it what language / framework / build tool the
        # project uses.  Detecting the stack from manifest files gives the
        # model a concrete picture before it starts globbing.
        _profile = self._detect_project_profile(ws_path)
        if _profile:
            summary_lines.append("")
            summary_lines.append("Project profile:")
            for _line in _profile:
                summary_lines.append(f"  {_line}")

        result = (ws_path, ws_name, "\n".join(summary_lines))
        self._prompt_cache.set(("workspace", cache_key), result)
        return result

    def _detect_project_profile(self, ws_path: str) -> list[str]:
        """Detect the project's tech stack and key entry points.

        Reads manifest files (pyproject.toml, package.json, Cargo.toml,
        go.mod, requirements.txt, etc.) and well-known entry points
        (README*, Makefile, main.*, index.*).  Returns a list of summary
        lines or ``[]`` when nothing is detectable.
        """
        _MANIFESTS: dict[str, str] = {
            "pyproject.toml": "Python (pyproject.toml)",
            "setup.py": "Python (setup.py)",
            "requirements.txt": "Python (requirements.txt)",
            "package.json": "Node.js",
            "tsconfig.json": "TypeScript",
            "Cargo.toml": "Rust (Cargo.toml)",
            "go.mod": "Go",
            "pom.xml": "Java (Maven)",
            "build.gradle": "Java (Gradle)",
            "build.gradle.kts": "Kotlin/Gradle",
            "Gemfile": "Ruby",
            "composer.json": "PHP (Composer)",
            "CMakeLists.txt": "C/C++ (CMake)",
            "Makefile": "Make",
            "Dockerfile": "Docker",
        }
        _ENTRY_NAMES = ("README.md", "README", "README.rst", "readme.md",
                        "CONTRIBUTING.md", "CHANGELOG.md", "main.py",
                        "index.py", "main.js", "index.js", "main.ts",
                        "index.ts", "app.py", "manage.py", "cli.py",
                        "bin/", "src/", "lib/", "tests/", "test/")

        try:
            entries = set(os.listdir(ws_path))
        except OSError:
            return []

        lines: list[str] = []
        stacks: list[str] = []
        for _name in _MANIFESTS:
            if _name in entries:
                stacks.append(_MANIFESTS[_name])
        if stacks:
            lines.append("stack: " + "; ".join(dict.fromkeys(stacks)))

        entry_points = [n for n in _ENTRY_NAMES if n in entries or os.path.isfile(os.path.join(ws_path, n))]
        if entry_points:
            lines.append("key files: " + ", ".join(entry_points[:12]))

        pyproject = os.path.join(ws_path, "pyproject.toml")
        if os.path.isfile(pyproject):
            try:
                import tomllib
                with open(pyproject, "rb") as _f:
                    _data = tomllib.load(_f)
                _proj = _data.get("project", {}) or {}
                _deps = _proj.get("dependencies", []) or []
                _scripts = _proj.get("scripts", {}) or {}
                if _deps:
                    lines.append("python deps: " + ", ".join(str(d).split("[")[0].strip() for d in _deps[:12]))
                if _scripts:
                    lines.append("console scripts: " + ", ".join(list(_scripts)[:8]))
            except Exception:
                pass

        package_json = os.path.join(ws_path, "package.json")
        if os.path.isfile(package_json):
            try:
                with open(package_json, encoding="utf-8") as _f:
                    _pkg = json.load(_f)
                _scripts = _pkg.get("scripts", {}) or {}
                if _scripts:
                    lines.append("npm scripts: " + ", ".join(list(_scripts)[:8]))
                _deps = _pkg.get("dependencies", {}) or {}
                _dev = _pkg.get("devDependencies", {}) or {}
                if _deps or _dev:
                    _all = list(_deps) + list(_dev)
                    lines.append("js deps: " + ", ".join(_all[:12]))
            except Exception:
                pass

        return lines

    # 鈹€鈹€ Codebase index 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def inject_code_index(self, idx: Any) -> None:
        """Inject a fully-built code index from the background IndexManager."""
        self._code_index = idx
        # The index changed: drop every cached codebase-context entry (the
        # exact key is not reconstructible here -- it carries index stats).
        self._prompt_cache.invalidate_namespace("codebase")
        ws_path = getattr(idx, "workspace", "")
        if ws_path and self._session.messages:
            new_ctx = self._build_codebase_context_sync(ws_path, idx)
            if new_ctx:
                for m in self._session.messages:
                    if m.get("role") == "system":
                        old = m.get("content", "")
                        if "## Codebase Index" in old:
                            pass
                        elif "## Workspace Structure" in old or \
                             "Codebase index is still being built" in old:
                            m["content"] = old + "\n\n" + new_ctx
                            self._session.mark_messages_dirty()
                        break

    def _build_codebase_context_sync(self, _ws_path: str, idx: Any) -> str:
        try:
            modules = idx.list_all_modules()
            total = len(modules)
            if total == 0:
                return ""
            by_lang: dict[str, int] = {}
            for mod in modules:
                lang = getattr(mod, "language", None) or "other"
                by_lang[lang] = by_lang.get(lang, 0) + 1
            lang_items = sorted(by_lang.items(), key=lambda x: (-x[1], x[0]))
            lines = ["## Codebase Index",
                     f"Indexed {total} source files in the workspace.",
                     "Use `codebase_search` to find relevant code, or "
                     "`codebase_context` to view a specific file's details."]
            if lang_items:
                lines.append("Language breakdown: " +
                             ", ".join(f"{lang}: {count}" for lang, count in lang_items))
            return "\n".join(lines)
        except Exception:
            return ""

    async def build_codebase_context(self) -> str:
        """Build codebase context from the workspace index when available."""
        ws_path = getattr(self._config, "workspace", "") or ""
        if not ws_path or not os.path.isdir(ws_path):
            return ""

        loop = asyncio.get_running_loop()

        if self._code_index is None or getattr(self._code_index, "workspace", "") != ws_path:
            _t0 = time.time()
            try:
                idx = await loop.run_in_executor(None, EncreCodeIndex, ws_path)
                self._code_index = idx
                if not idx._indexed:
                    return self.build_directory_tree(ws_path)
            except Exception:
                return self.build_directory_tree(ws_path)
        elif not self._code_index._indexed:
            return self.build_directory_tree(ws_path)

        if self._code_index is None:
            return ""

        modules = self._code_index.list_all_modules()
        total = len(modules)
        if total == 0:
            return ""

        by_lang: dict[str, int] = {}
        for mod in modules:
            lang = mod.language or "other"
            by_lang[lang] = by_lang.get(lang, 0) + 1
        lang_summary_items = tuple(sorted(by_lang.items(), key=lambda x: (-x[1], x[0])))
        cache_key = ("codebase", ws_path, total, int(self._code_index._indexed),
                     lang_summary_items)
        cached = self._prompt_cache.get(cache_key)
        if cached is not PromptCache.MISS:
            return cached

        lines: list[str] = []
        lines.append("## Codebase Index")
        lines.append(f"Indexed {total} source files in the workspace.")
        lines.append("Use `codebase_search` to find relevant code, or `codebase_context` to view a specific file's details.")

        if lang_summary_items:
            lang_summary = ", ".join(f"{lang}: {count}" for lang, count in lang_summary_items)
            lines.append(f"Language breakdown: {lang_summary}")

        result = "\n".join(lines)
        self._prompt_cache.set(cache_key, result)
        return result

    # 鈹€鈹€ Architecture contract 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_architecture_contract(self) -> str:
        """Return the persisted architecture contract blocks for the workspace.

        Loads ``.encre/contracts/PLAN.md`` and ``ARCHITECTURE.md`` (when
        present) and returns a single markdown block ready to be appended to
        the system prompt.  Cached on the newest contract file mtime so a new
        contract written mid-session is picked up on the next turn.
        """
        from encre.contract import latest_contract_mtime, load_architecture_contract

        ws_path = getattr(self._config, "workspace", "") or ""
        if not ws_path or not os.path.isdir(ws_path):
            self._prompt_cache.invalidate(("contract", ""))
            return ""
        mtime = latest_contract_mtime(ws_path)
        cache_key = ("contract", f"{ws_path}:{mtime}")
        cached = self._prompt_cache.get(cache_key)
        if cached is not PromptCache.MISS:
            return cached
        prompt = load_architecture_contract(ws_path)
        self._prompt_cache.set(cache_key, prompt)
        return prompt

    # 鈹€鈹€ Document context 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_document_context(self) -> str:
        from encre.config import get_data_dir

        try:
            data_dir = str(get_data_dir())
            index_path = os.path.join(data_dir, "documents", "index.json")
            try:
                st = os.stat(index_path)
                cache_key = f"{data_dir}:{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                cache_key = data_dir
            cache_key = ("document", cache_key)
            cached = self._prompt_cache.get(cache_key)
            if cached is not PromptCache.MISS:
                return cached

            if self._document_manager is None or self._document_manager_data_dir != data_dir:
                self._document_manager = EncreDocumentManager(data_dir)
                self._document_manager_data_dir = data_dir
            else:
                self._document_manager._load()
            prompt = self._document_manager.build_context()
            self._prompt_cache.set(cache_key, prompt)
            return prompt
        except Exception:
            return ""

    # 鈹€鈹€ Memory recall prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_recall_prompt(self, query: str) -> str:
        """Recall memories associated with ``query`` and render them as a block.

        Retrieval goes through the single path
        (:class:`~encre.memdir.recall.RecallEngine`): similarity seeds plus
        spreading activation over explicit links.  Each rendered hit carries its
        memory type, its content, and -- when it was reached by spreading rather
        than by direct similarity -- the link path it was reached through.

        The human-curated ``MEMORY.md`` entrypoint is rendered ahead of the
        hits.  It is an *index* the user maintains by hand, not a second
        retrieval path: it is loaded verbatim (never searched), so the single
        retrieval path stays :class:`RecallEngine`.  Without it the curated
        entrypoint would reach no prompt at all, since ``MEMORY.md`` is
        deliberately excluded from ingestion.

        Returns ``""`` when no recall engine is attached, the query is blank, and
        no curated entrypoint exists.
        """
        engine = self.recall_engine
        cache_key = ("recall", query)
        if engine is not None and query:
            cached = self._prompt_cache.get(cache_key)
            if cached is not PromptCache.MISS:
                return cached

        lines: list[str] = []
        entrypoint = self._load_memory_entrypoint()
        if entrypoint:
            lines.append("## Curated Memory Index")
            lines.append("")
            lines.append(entrypoint)
            lines.append("")

        # The curated index is rendered even when the recall engine is
        # unavailable (degraded memory subsystem), so a hand-maintained
        # entrypoint never silently stops reaching the model.
        hits = engine.recall(query) if (engine is not None and query) else []
        if not hits:
            # An empty result is never cached: a memory ingested after this
            # call (the ``refresh`` ingestion trigger) must be visible on the
            # next identical query, not hidden behind a stale empty entry.
            # The curated index is cheap to re-read, so it is not cached either.
            return "\n".join(lines).rstrip() if lines else ""

        lines.append("## Relevant Memories")
        lines.append("")
        for hit in hits:
            entry = hit.entry
            lines.append(f"### [{entry.type}] (score: {hit.score:.2f})")
            lines.append(entry.content)
            if hit.via_links:
                path = " -> ".join(
                    f"{link.get('relation', '')}:{link.get('target_id', '')}"
                    for link in hit.via_links
                )
                lines.append(f"(recalled via {path})")
            lines.append("")

        prompt = "\n".join(lines).rstrip()
        # Only reached with non-empty hits: an empty result is never cached
        # (see the early return above), so a memory ingested after this call
        # stays visible on the next identical query.
        self._prompt_cache.set(cache_key, prompt)
        return prompt

    def _load_memory_entrypoint(self) -> str:
        """Return the curated ``MEMORY.md`` content, or ``""`` when unavailable."""
        system = self.memory_system
        loader = getattr(system, "load_entrypoint", None)
        if loader is None:
            return ""
        try:
            return (loader().content or "").strip()
        except Exception:
            return ""

    # 鈹€鈹€ Soul prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_soul_prompt(self) -> str:
        if self.soul_system is None:
            return ""

        soul_dir = self.soul_system.get_soul_dir()
        cache_key = ("soul", soul_dir)
        cached = self._prompt_cache.get(cache_key)
        if cached is not PromptCache.MISS:
            return cached

        prompt = self.soul_system.build_prompt()
        self._prompt_cache.set(cache_key, prompt)
        return prompt

    # 鈹€鈹€ Device context prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_device_context_prompt(self) -> str:
        """Return the L1 device catalog block, or '' if disabled/unavailable.

        The returned text is loaded from the ``device_context.prompt`` block
        template with the catalog injected via the ``{{device_context}}``
        placeholder.
        """
        if self._device_context_manager is None:
            return ""
        try:
            catalog = self._device_context_manager.get_catalog()
            if not catalog:
                return ""
            from encre.prompts.loader import PromptLoader
            loader = PromptLoader()
            return loader.load_with_context("device_context", device_context=catalog)
        except Exception:
            return ""

    # 鈹€鈹€ Profile prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def refresh_profile_in_system(self) -> None:
        if self.profile_system is None:
            return
        if not self._session.messages or self._session.messages[0].get("role") != "system":
            return
        try:
            query = ""
            for m in reversed(self._session.messages):
                if m.get("role") == "user":
                    query = m.get("content", "")
                    break
            fresh = self.profile_system.build_relevant_prompt(query=query, threshold=0.0)
            if not fresh:
                return
            content = self._session.messages[0].get("content", "")
            content = re.sub(
                r"\n+## User Profile.*?(?=\n+## |\Z)",
                "",
                content,
                count=1,
                flags=re.DOTALL,
            )
            content = content.rstrip() + "\n\n" + fresh
            self._session.messages[0]["content"] = content
            self._session.mark_messages_dirty()
        except Exception:
            pass

    def build_profile_prompt(self, query: str) -> str:
        if self.profile_system is None:
            return ""
        cache_key = ("profile", getattr(self.profile_system, "_profile_path", ""), query)
        cached = self._prompt_cache.get(cache_key)
        if cached is not PromptCache.MISS:
            return cached
        prompt = self.profile_system.build_relevant_prompt(query=query, threshold=0.0)
        self._prompt_cache.set(cache_key, prompt)
        return prompt

    # 鈹€鈹€ Rules prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

    def build_rules_prompt(self) -> str:
        ws_root = getattr(self._config, "workspace", "") or ""
        disabled = set(getattr(self._config, "disabled_context_files", None) or [])
        cache_key = (
            "rules",
            ws_root,
            bool(self._config.enable_project_rules),
            bool(self._config.enable_global_rules),
            tuple(sorted(disabled)),
        )
        cached = self._prompt_cache.get(cache_key)
        if cached is not PromptCache.MISS:
            return cached
        prompt = self._rules_loader.build_rules_prompt(
            ws_root,
            enable_project=self._config.enable_project_rules,
            enable_global=self._config.enable_global_rules,
            disabled_files=disabled,
        )
        self._prompt_cache.set(cache_key, prompt)
        return prompt
