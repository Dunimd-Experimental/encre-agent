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

"""Rust-backed AST workspace index wrapper."""

# This module wraps the native (Rust) tree-sitter based AST index exposed by
# ``encre.native``.  It provides a pure-Python API for symbol lookup, reference
# finding, outline generation, "go to definition" and relevance search, while
# caching parsed data on disk under ``.encre/ast_index.json``.

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from encre.native import ast_available as native_ast_available
from encre.native import ast_backend_name as native_ast_backend_name
from encre.native import ast_find_references as native_ast_find_references
from encre.native import ast_find_relevant as native_ast_find_relevant
from encre.native import ast_get_outline as native_ast_get_outline
from encre.native import ast_get_symbol as native_ast_get_symbol
from encre.native import ast_goto_definition as native_ast_goto_definition
from encre.native import ast_list_files as native_ast_list_files
from encre.native import build_ast_index as native_build_ast_index
from encre.native import load_ast_index as native_load_ast_index
from encre.native import update_ast_index as native_update_ast_index

logger = logging.getLogger("encre.capabilities.search.codebase.ast_index")


@dataclass
class Symbol:
    """A named code definition located in the workspace.

    Represents a function, class, method, variable, etc., along with its
    source location span and optional signature/docstring metadata.
    """
    name: str
    kind: str
    file: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    parent: str | None = None
    signature: str | None = None
    docstring: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise the symbol to a plain dictionary (for JSON storage)."""
        return asdict(self)


    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Symbol:
        """Reconstruct a :class:`Symbol` from a dictionary produced by :meth:`to_dict`."""
        return cls(
            name=str(data["name"]),
            kind=str(data["kind"]),
            file=str(data["file"]),
            start_line=int(data["start_line"]),
            start_col=int(data["start_col"]),
            end_line=int(data["end_line"]),
            end_col=int(data["end_col"]),
            parent=data.get("parent"),
            signature=data.get("signature"),
            docstring=data.get("docstring"),
        )


@dataclass
class Reference:
    """A single usage site of a symbol at a specific source location."""
    file: str
    line: int
    col: int
    name: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise the reference to a plain dictionary (for JSON storage)."""
        return asdict(self)


    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reference:
        """Reconstruct a :class:`Reference` from a dictionary produced by :meth:`to_dict`."""
        return cls(
            file=str(data["file"]),
            line=int(data["line"]),
            col=int(data["col"]),
            name=str(data["name"]),
            kind=str(data["kind"]),
        )


class EncreASTIndex:
    """Compatibility wrapper around the native Rust AST index."""

    def __init__(self, workspace: str) -> None:
        """Create the index, loading any cached index from ``.encre/`` on disk."""
        self.workspace: str = workspace
        self._symbols_by_file: dict[str, list[Symbol]] = {}
        self._global_index: dict[str, list[Symbol]] = {}
        self._file_mtimes: dict[str, float] = {}
        self._indexed: bool = False
        self.load()

    @property
    def available(self) -> bool:
        """``True`` when the native AST backend (e.g. tree-sitter) is usable."""
        return bool(native_ast_available())

    @property
    def backend(self) -> str | None:
        """Return the name of the active native AST backend (e.g. ``tree-sitter``)."""
        return str(native_ast_backend_name())

    def _load_from_native_payload(self, data: dict[str, Any]) -> None:
        """Populate the in-memory caches from a native index payload dict."""
        self._file_mtimes = {
            str(path): float(mtime) for path, mtime in data.get("file_mtimes", {}).items()
        }
        self._symbols_by_file = {
            str(path): [Symbol.from_dict(item) for item in symbols]
            for path, symbols in data.get("symbols_by_file", {}).items()
        }
        self._global_index.clear()
        for symbols in self._symbols_by_file.values():
            for sym in symbols:
                self._global_index.setdefault(sym.name, []).append(sym)
        self._indexed = True

    def scan(self, progress_cb: Callable[[str, int], None] | None = None) -> None:
        """Build the AST index from scratch for the whole workspace."""
        ws = Path(self.workspace).resolve()
        if not ws.exists():
            self._indexed = True
            return
        if progress_cb is not None:
            progress_cb("_build_native_ast_index", 0)
        data = json.loads(native_build_ast_index(self.workspace))
        self._load_from_native_payload(data)
        if progress_cb is not None:
            progress_cb("_done", len(self._file_mtimes))

    def scan_incremental(
        self, progress_cb: Callable[[str, int], None] | None = None
    ) -> None:
        """Update the index re-using prior state (full scan on first run)."""
        ws = Path(self.workspace).resolve()
        if not ws.exists():
            self._indexed = True
            return
        if not self._indexed:
            self.scan(progress_cb=progress_cb)
            return
        if progress_cb is not None:
            progress_cb("_update_native_ast_index", 0)
        data = json.loads(native_update_ast_index(self.workspace))
        self._load_from_native_payload(data)
        if progress_cb is not None:
            progress_cb("_done", len(self._file_mtimes))

    def _storage_path(self) -> Path:
        """Return the on-disk location of the cached AST index (``.encre/ast_index.json``)."""
        return Path(self.workspace) / ".encre" / "ast_index.json"

    def save(self) -> None:
        """Persist the current index to ``.encre/ast_index.json`` on disk."""
        data: dict[str, Any] = {
            "workspace": self.workspace,
            "file_mtimes": self._file_mtimes,
            "symbols_by_file": {
                file: [sym.to_dict() for sym in symbols]
                for file, symbols in self._symbols_by_file.items()
            },
        }
        storage = self._storage_path()
        storage.parent.mkdir(parents=True, exist_ok=True)
        storage.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self) -> bool:
        """Load a cached index from disk, returning ``True`` on success."""
        storage = self._storage_path()
        if not storage.exists():
            return False
        try:
            data = json.loads(native_load_ast_index(self.workspace))
            if data.get("workspace") != self.workspace:
                return False
            self._load_from_native_payload(data)
            return True
        except Exception:
            try:
                data = json.loads(storage.read_text(encoding="utf-8"))
                if data.get("workspace") != self.workspace:
                    return False
                self._load_from_native_payload(data)
                return True
            except Exception:
                self._symbols_by_file.clear()
                self._global_index.clear()
                self._file_mtimes.clear()
                return False

    def get_symbol(self, name: str) -> list[Symbol]:
        """Return all symbols in the workspace that match *name*."""
        if not self._indexed:
            self.scan()
        try:
            return [Symbol.from_dict(item) for item in json.loads(native_ast_get_symbol(self.workspace, name))]
        except Exception:
            return list(self._global_index.get(name, []))

    def get_outline(self, file: str) -> list[Symbol]:
        """Return the symbol outline (top-level definitions) for *file*."""
        if not self._indexed:
            self.scan()
        try:
            return [Symbol.from_dict(item) for item in json.loads(native_ast_get_outline(self.workspace, file))]
        except Exception:
            return list(self._symbols_by_file.get(file, []))

    def list_files(self) -> list[str]:
        """Return the list of indexed source file paths."""
        if not self._indexed:
            self.scan()
        try:
            return list(json.loads(native_ast_list_files(self.workspace)))
        except Exception:
            return list(self._file_mtimes.keys())

    def find_references(self, name: str) -> list[Reference]:
        """Return all references (usages) of the symbol named *name*."""
        if not self._indexed:
            self.scan()
        try:
            return [
                Reference.from_dict(item)
                for item in json.loads(native_ast_find_references(self.workspace, name))
            ]
        except Exception:
            return []

    def goto_definition(self, file: str, line: int, col: int) -> Symbol | None:
        """Resolve the symbol definition at a (file, line, col) position."""
        if not self._indexed:
            self.scan()
        try:
            raw = json.loads(native_ast_goto_definition(self.workspace, file, line, col))
        except Exception:
            return None
        if not raw:
            return None
        return Symbol.from_dict(raw)

    def find_relevant(self, name: str, limit: int = 10) -> list[Symbol]:
        """Return symbols most relevant to *name*, capped at *limit* results."""
        if not self._indexed:
            self.scan()
        if not name:
            return []
        try:
            return [
                Symbol.from_dict(item)
                for item in json.loads(native_ast_find_relevant(self.workspace, name, limit))
            ]
        except Exception:
            out: list[Symbol] = []
            for sym_name, syms in self._global_index.items():
                if name in sym_name:
                    out.extend(syms)
                    if len(out) >= limit:
                        return out[:limit]
            return out
