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

"""Rust-backed semantic code embedding index wrapper."""

# This module implements semantic ("vector") search over code.  It splits the
# workspace into embedding slices (often one per symbol) and stores vectors
# produced by an OpenAI-compatible embedding model.  At query time it asks the
# native index (Rust) for the nearest slices.  When no embedding backend is
# configured the index degrades to storing zero vectors.

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from encre.capabilities.search.codebase.ast_index import EncreASTIndex
from encre.native import build_embedding_slices as native_build_embedding_slices
from encre.native import load_embedding_index as native_load_embedding_index
from encre.native import save_embedding_index as native_save_embedding_index
from encre.native import search_embedding_index as native_search_embedding_index

logger = logging.getLogger("encre.capabilities.search.codebase.embedding_index")

EmbeddingFn = Callable[[list[str]], list[list[float]]]


@dataclass
class EmbeddingSlice:
    """A contiguous chunk of code (often one symbol) queued for embedding."""
    file: str
    start_line: int
    end_line: int
    symbol: str
    kind: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise the embedding slice to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmbeddingSlice:
        """Reconstruct an :class:`EmbeddingSlice` from a dictionary."""
        return cls(
            file=str(data["file"]),
            start_line=int(data["start_line"]),
            end_line=int(data["end_line"]),
            symbol=str(data.get("symbol", "")),
            kind=str(data.get("kind", "module")),
            text=str(data.get("text", "")),
        )


@dataclass
class EmbeddingHit:
    """A search result slice with its similarity ``score`` to the query."""
    file: str
    start_line: int
    end_line: int
    symbol: str
    kind: str
    score: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise the search hit to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmbeddingHit:
        """Reconstruct an :class:`EmbeddingHit` from a dictionary."""
        return cls(
            file=str(data["file"]),
            start_line=int(data["start_line"]),
            end_line=int(data["end_line"]),
            symbol=str(data.get("symbol", "")),
            kind=str(data.get("kind", "module")),
            score=float(data.get("score", 0.0)),
            text=str(data.get("text", "")),
        )


class OpenAICompatibleEmbedding:
    """Thin client for an OpenAI-compatible embeddings API (used to vectorise text)."""
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        timeout: float = 60.0,
    ) -> None:
        """Validate the API key and store endpoint/model configuration."""
        self.api_key: str = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "OpenAICompatibleEmbedding requires an API key. Pass "
                "api_key=... or set the OPENAI_API_KEY environment variable."
            )
        self.base_url: str = base_url.rstrip("/")
        self.model: str = model
        self.timeout: float = timeout

    def __call__(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into a list of float vectors via the API."""
        if not texts:
            return []
        import httpx

        payload: dict[str, Any] = {"model": self.model, "input": texts}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/embeddings"
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Embedding request to {url} failed: {e}") from e
        if resp.status_code >= 400:
            raise RuntimeError(f"Embedding API error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        items = data.get("data", [])
        if not isinstance(items, list) or len(items) != len(texts):
            raise RuntimeError(f"Embedding API returned unexpected payload shape: {data!r}")
        return [list(item["embedding"]) for item in items]


class EncreEmbeddingIndex:
    """Semantic embedding index over a workspace's code slices."""
    _META_NAME: str = "embedding_index.json"

    def __init__(
        self,
        workspace: str,
        ast_index: EncreASTIndex | None = None,
        embedding_fn: EmbeddingFn | None = None,
        embedding_dim: int | None = None,
        max_text_chars: int = 4000,
    ) -> None:
        """Create the embedding index, loading any cached state from disk."""
        self.workspace: str = workspace
        self._ast: EncreASTIndex = ast_index if ast_index is not None else EncreASTIndex(workspace)
        self._embedding_fn: EmbeddingFn | None = embedding_fn
        self._embedding_dim: int | None = embedding_dim
        self._max_text_chars: int = max_text_chars
        self._slices: list[EmbeddingSlice] = []
        self._vectors: list[list[float]] = []
        self._file_mtimes: dict[str, float] = {}
        self._indexed: bool = False
        self._lock = threading.Lock()
        self.load()

    @property
    def available(self) -> bool:
        """``True`` once at least one slice has been embedded."""
        return len(self._slices) > 0 and len(self._vectors) > 0

    @property
    def slice_count(self) -> int:
        """Number of embedding slices currently held in the index."""
        return len(self._slices)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* using the configured backend (zero-vectors if none)."""
        if not texts:
            return []
        if self._embedding_fn is None:
            dim = self._embedding_dim or 0
            return [[0.0] * dim for _ in texts]
        raw = self._embedding_fn(texts)
        if not raw:
            return []
        if self._embedding_dim is None:
            self._embedding_dim = len(raw[0])
        return [list(map(float, row)) for row in raw]

    def scan(self) -> None:
        """Build the embedding index from scratch and persist it."""
        if not self._ast._indexed:
            self._ast.scan()
        payload = json.loads(native_build_embedding_slices(self.workspace, self._max_text_chars))
        slices = [EmbeddingSlice.from_dict(item) for item in payload.get("slices", [])]
        vectors = self._embed([sl.text for sl in slices]) if slices else []
        with self._lock:
            self._slices = slices
            self._vectors = vectors
            self._file_mtimes = {
                str(k): float(v) for k, v in payload.get("file_mtimes", {}).items()
            }
            self._indexed = True
        native_save_embedding_index(
            self.workspace,
            json.dumps([sl.to_dict() for sl in self._slices], ensure_ascii=False),
            json.dumps(self._vectors, ensure_ascii=False),
            json.dumps(self._file_mtimes, ensure_ascii=False),
            int(self._embedding_dim or 0),
        )

    def scan_incremental(self) -> None:
        """Update the embedding index, re-embedding only changed/deleted files."""
        if not self._indexed:
            self.scan()
            return
        if not self._ast._indexed:
            self._ast.scan()

        payload = json.loads(native_build_embedding_slices(self.workspace, self._max_text_chars))
        next_file_mtimes = {
            str(k): float(v) for k, v in payload.get("file_mtimes", {}).items()
        }
        next_slices = [EmbeddingSlice.from_dict(item) for item in payload.get("slices", [])]

        changed_files = {
            rel for rel, mtime in next_file_mtimes.items()
            if self._file_mtimes.get(rel) != mtime
        }
        deleted_files = set(self._file_mtimes) - set(next_file_mtimes)
        touched_files = changed_files | deleted_files
        if not touched_files:
            return

        kept_slices: list[EmbeddingSlice] = []
        kept_vectors: list[list[float]] = []
        for idx, sl in enumerate(self._slices):
            if sl.file in touched_files:
                continue
            kept_slices.append(sl)
            if idx < len(self._vectors):
                kept_vectors.append(self._vectors[idx])

        changed_slices = [sl for sl in next_slices if sl.file in changed_files]
        changed_vectors = self._embed([sl.text for sl in changed_slices]) if changed_slices else []

        with self._lock:
            self._slices = kept_slices + changed_slices
            self._vectors = kept_vectors + changed_vectors
            self._file_mtimes = next_file_mtimes
            self._indexed = True

        self.save()

    def search(self, query: str, k: int = 10) -> list[EmbeddingHit]:
        """Return the *k* most semantically similar slices for *query*."""
        if not query:
            return []
        if not self._indexed:
            self.scan()
        q_vec = self._embed([query])
        if not q_vec:
            return []
        raw = json.loads(native_search_embedding_index(self.workspace, json.dumps(q_vec[0]), k))
        return [EmbeddingHit.from_dict(item) for item in raw]

    def _meta_path(self) -> Path:
        """Return the on-disk location of the cached embedding index."""
        return Path(self.workspace) / ".encre" / self._META_NAME

    def save(self) -> None:
        """Persist slices, vectors, mtimes and dim to the native index on disk."""
        native_save_embedding_index(
            self.workspace,
            json.dumps([sl.to_dict() for sl in self._slices], ensure_ascii=False),
            json.dumps(self._vectors, ensure_ascii=False),
            json.dumps(self._file_mtimes, ensure_ascii=False),
            int(self._embedding_dim or 0),
        )

    def load(self) -> bool:
        """Load a cached embedding index from disk, returning ``True`` on success."""
        meta = self._meta_path()
        if not meta.exists():
            return False
        try:
            payload = json.loads(native_load_embedding_index(self.workspace))
        except Exception:
            return False
        with self._lock:
            self._slices = [EmbeddingSlice.from_dict(item) for item in payload.get("slices", [])]
            self._vectors = [
                [float(value) for value in row]
                for row in payload.get("vectors", [])
            ]
            self._file_mtimes = {
                str(k): float(v) for k, v in payload.get("file_mtimes", {}).items()
            }
            dim = int(payload.get("embedding_dim") or 0)
            self._embedding_dim = dim or self._embedding_dim
            self._indexed = True
        return True
