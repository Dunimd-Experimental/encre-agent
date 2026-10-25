#!/usr/bin/env python3

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

"""Session memory index: pure-math BM25 retrieval over past sessions.

Builds one retrievable document per session under ``data/sessions/``.
The document text is assembled from real on-disk sources only: the
``title``/``summary`` fields of ``meta.json`` when the file exists, plus
the user/assistant ``message.appended`` events of the session's
append-only trajectory stream (``events.jsonl``).  Tokenisation is mixed
Chinese/English and stdlib-only: ASCII alphanumeric runs become
lower-cased word tokens, CJK runs are split into 2-grams.  Scoring is
classical BM25 (k1=1.5, b=0.75) over an inverted index; no vectors, no
embeddings, no external dependencies.

The index persists to ``data/memory_index.json`` with a build timestamp
and a source signature (per-session file mtime + size).  ``build()``
reuses the on-disk index when the signature is unchanged and reuses the
in-process structures when ``build()`` is called again on the same
signature, so repeated turns pay at most a directory stat scan.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from encre.logging_config import get_logger

# Rebuilding walks every session on disk and rewrites the whole index; doing
# that before every user message turns recall into a per-turn disk scan. The
# source signature only needs to be re-checked occasionally -- recall is a
# best-effort hint, so a short window of staleness costs nothing.
_MIN_REBUILD_INTERVAL_S = 30.0
# Serialises rebuilds across agents in one process: they all point at the same
# sessions directory and the same persisted index file.
_BUILD_LOCK = threading.Lock()

logger = get_logger(__name__)

#: BM25 free parameters (classical Robertson values).
BM25_K1 = 1.5
BM25_B = 0.75

#: Persisted index file name, resolved under the data root.
INDEX_FILENAME = "memory_index.json"

# --- Document extraction limits (bounds index size and memory use) ---------

#: Max recorded messages per session document.
_MAX_MESSAGES_PER_DOC = 60
#: Max characters kept per recorded message.
_MAX_CHARS_PER_MESSAGE = 1200
#: Max characters per session document.
_MAX_CHARS_PER_DOC = 20_000
#: Snippet window / sliding step in characters.
_SNIPPET_WINDOW = 240
_SNIPPET_STEP = 120

# --- Mixed Chinese/English tokenisation (stdlib only) ----------------------

#: ASCII alphanumeric runs -> word tokens.
_WORD_RE = re.compile(r"[0-9A-Za-z]+")
#: CJK ideograph + kana + hangul ranges treated as "no spaces" scripts.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x30FF),  # hiragana / katakana
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xAC00, 0xD7AF),  # hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
)


def _is_cjk(ch: str) -> bool:
    """Return True when ``ch`` belongs to a no-space (CJK) script range."""
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _flush_cjk(buf: list[str], tokens: list[str]) -> None:
    """Emit 2-grams (or the single character) for a buffered CJK run."""
    if len(buf) > 1:
        tokens.extend(buf[i] + buf[i + 1] for i in range(len(buf) - 1))
    elif len(buf) == 1:
        tokens.append(buf[0])


def _tokenize(text: str) -> list[str]:
    """Mixed Chinese/English tokenisation, standard library only.

    ASCII alphanumeric runs become lower-cased word tokens; contiguous
    CJK runs are split into overlapping 2-grams (a lone CJK character
    becomes a 1-gram) so Chinese text is retrievable without a
    segmentation dependency.
    """
    tokens: list[str] = [chunk.lower() for chunk in _WORD_RE.findall(text)]
    buf: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            buf.append(ch)
            continue
        _flush_cjk(buf, tokens)
        buf = []
    _flush_cjk(buf, tokens)
    return tokens


@dataclass
class RetrievedFragment:
    """One retrieved past-session fragment handed to the context compiler."""

    #: Session id the fragment was retrieved from.
    doc_id: str
    #: BM25 relevance score.
    score: float
    #: Query-hit-centred excerpt of the session document.
    snippet: str


class SessionMemoryIndex:
    """BM25 inverted index over past-session retrievable documents."""

    def __init__(self, data_root: str | Path = "data") -> None:
        """Bind the index to a data root (default: the repo ``data/`` dir)."""
        self._root = Path(data_root)
        self._sessions_dir = self._root / "sessions"
        self._index_path = self._root / INDEX_FILENAME
        self._docs: list[dict[str, Any]] = []  # {"doc_id": str, "text": str}
        self._postings: dict[str, list[list[int]]] = {}  # term -> [[doc_idx, tf], ...]
        self._df: dict[str, int] = {}  # term -> document frequency
        self._doc_len: list[int] = []  # doc_idx -> token count
        self._avgdl: float = 0.0
        self._signature: Any = None
        self._built = False
        self._last_build = 0.0

    # --- Build / persistence ------------------------------------------------

    def build(self, *, force: bool = False) -> None:
        """(Re)build or refresh the index; cheap when nothing changed.

        Runs under a process-wide lock and is rate-limited: within
        :data:`_MIN_REBUILD_INTERVAL_S` of the last attempt the scan is
        skipped entirely, because the active session rewrites its own
        transcript continuously and would otherwise invalidate the signature
        on every turn. Callers should invoke this off the event loop.

        In-process reuse: an unchanged source signature keeps the current
        structures.  On-disk reuse: a changed signature first tries to
        load a persisted index with the same signature before rescanning
        the transcripts.  Failures degrade to an empty index with one
        log line -- retrieval is best-effort and must never block the
        loop.
        """
        with _BUILD_LOCK:
            now = time.monotonic()
            if self._built and not force and now - self._last_build < _MIN_REBUILD_INTERVAL_S:
                return
            self._last_build = now
            try:
                signature = self._scan_signature()
                if self._built and signature == self._signature:
                    return
                if self._load(signature):
                    self._signature = signature
                    self._built = True
                    return
                self._docs = self._collect_docs()
                self._build_index()
                self._signature = signature
                self._built = True
                self._save(signature)
            except Exception:
                logger.debug("[memory_index] build failed; retrieval disabled", exc_info=True)
                self._built = True  # avoid retry storms; search() returns []

    def _scan_signature(self) -> dict[str, list[int | None]]:
        """Return ``{session_id: [events_mtime_ns, events_size, meta_mtime_ns, meta_size]}``."""
        signature: dict[str, list[int | None]] = {}
        if not self._sessions_dir.is_dir():
            return signature
        for entry in sorted(self._sessions_dir.iterdir()):
            if not entry.is_dir():
                continue
            row: list[int | None] = []
            for name in ("events.jsonl", "meta.json"):
                path = entry / name
                if path.is_file():
                    stat = path.stat()
                    row.extend([stat.st_mtime_ns, stat.st_size])
                else:
                    row.extend([None, None])
            signature[entry.name] = row
        return signature

    def _collect_docs(self) -> list[dict[str, Any]]:
        """Extract one retrievable document per session directory."""
        docs: list[dict[str, Any]] = []
        if not self._sessions_dir.is_dir():
            return docs
        for entry in sorted(self._sessions_dir.iterdir()):
            if not entry.is_dir():
                continue
            text = self._extract_doc_text(entry)
            if text:
                docs.append({"doc_id": entry.name, "text": text})
        return docs

    def _extract_doc_text(self, session_dir: Path) -> str:
        """Assemble one session document from real on-disk sources."""
        parts: list[str] = []
        meta_path = session_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            if isinstance(meta, dict):
                for key in ("title", "summary", "name", "description"):
                    value = meta.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip()[:_MAX_CHARS_PER_MESSAGE])
        events_path = session_dir / "events.jsonl"
        if events_path.is_file():
            taken = 0
            try:
                with events_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        if taken >= _MAX_MESSAGES_PER_DOC:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except Exception:
                            continue
                        if event.get("type") != "message.appended":
                            continue
                        if event.get("role") not in ("user", "assistant"):
                            continue
                        content = event.get("content")
                        if not isinstance(content, str) or not content.strip():
                            continue
                        parts.append(content.strip()[:_MAX_CHARS_PER_MESSAGE])
                        taken += 1
            except Exception:
                logger.debug(
                    "[memory_index] failed reading %s", events_path, exc_info=True
                )
        return "\n".join(parts)[:_MAX_CHARS_PER_DOC]

    def _build_index(self) -> None:
        """Tokenise documents and build the inverted index + df/length tables."""
        self._postings = {}
        self._df = {}
        self._doc_len = []
        total_len = 0
        for doc_idx, doc in enumerate(self._docs):
            tokens = _tokenize(doc["text"])
            self._doc_len.append(len(tokens))
            total_len += len(tokens)
            tf: dict[str, int] = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1
            for token, count in tf.items():
                self._postings.setdefault(token, []).append([doc_idx, count])
                self._df[token] = len(self._postings[token])
        self._avgdl = (total_len / len(self._docs)) if self._docs else 0.0

    def _save(self, signature: Any) -> None:
        """Persist the index atomically; failures degrade to a log line."""
        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "built_at": time.time(),
                "signature": signature,
                "avgdl": self._avgdl,
                "doc_len": self._doc_len,
                "docs": self._docs,
                "postings": self._postings,
            }
            tmp_path = self._index_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            tmp_path.replace(self._index_path)
        except Exception:
            logger.debug("[memory_index] persist failed", exc_info=True)

    def _load(self, signature: Any) -> bool:
        """Load a persisted index whose source signature matches; else False."""
        try:
            if not self._index_path.is_file():
                return False
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
            if payload.get("signature") != signature:
                return False
            docs = payload.get("docs")
            postings = payload.get("postings")
            doc_len = payload.get("doc_len")
            if not isinstance(docs, list) or not isinstance(postings, dict):
                return False
            if not isinstance(doc_len, list) or len(doc_len) != len(docs):
                return False
            self._docs = docs
            self._postings = postings
            self._df = {term: len(plist) for term, plist in postings.items()}
            self._doc_len = doc_len
            self._avgdl = float(payload.get("avgdl") or 0.0)
            return True
        except Exception:
            logger.debug("[memory_index] load failed", exc_info=True)
            return False

    # --- Retrieval ----------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 3,
        min_score: float = 0.0,
    ) -> list[RetrievedFragment]:
        """BM25-ranked retrieval over session documents.

        Args:
            query: Free-form mixed Chinese/English query.
            top_k: Maximum number of fragments returned.
            min_score: Minimum BM25 score threshold.

        Returns:
            Fragments sorted by descending score; empty when the index is
            empty, the query has no in-vocabulary terms, or building failed.
        """
        if not self._built:
            self.build()
        # A rebuild in another thread swaps the posting/doc tables in place;
        # reading them concurrently can pair a new doc list with old indices.
        # Skip recall for this turn rather than block the event loop.
        if not _BUILD_LOCK.acquire(blocking=False):
            return []
        try:
            return self._search_locked(query, top_k, min_score)
        finally:
            _BUILD_LOCK.release()

    def _search_locked(
        self,
        query: str,
        top_k: int,
        min_score: float,
    ) -> list[RetrievedFragment]:
        """BM25 retrieval; caller holds :data:`_BUILD_LOCK`."""
        if not self._docs or not self._postings or self._avgdl <= 0.0:
            return []
        query_terms = [
            t for t in dict.fromkeys(_tokenize(query)) if t in self._postings
        ]
        if not query_terms:
            return []
        n_docs = len(self._docs)
        scores: dict[int, float] = {}
        for term in query_terms:
            plist = self._postings[term]
            df = self._df[term]
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            for doc_idx, tf in plist:
                dl = self._doc_len[doc_idx] or 1
                denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / self._avgdl)
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * tf * (BM25_K1 + 1.0) / denom
        ranked = sorted(
            ((score, idx) for idx, score in scores.items() if score > min_score),
            reverse=True,
        )[: max(top_k, 0)]
        return [
            RetrievedFragment(
                doc_id=self._docs[idx]["doc_id"],
                score=round(score, 4),
                snippet=self._snippet(idx, query_terms),
            )
            for score, idx in ranked
        ]

    def _snippet(self, doc_idx: int, query_terms: list[str]) -> str:
        """Return the window of the document holding the most query hits."""
        text = str(self._docs[doc_idx].get("text", ""))
        if not text:
            return ""
        lowered = text.lower()
        positions: list[int] = []
        for term in query_terms:
            start = 0
            while True:
                hit = lowered.find(term, start)
                if hit < 0:
                    break
                positions.append(hit)
                start = hit + 1
        if not positions:
            return text[:_SNIPPET_WINDOW]
        positions.sort()
        best_start, best_hits = 0, 0
        left = 0
        for right, pos in enumerate(positions):
            while pos - positions[left] >= _SNIPPET_WINDOW:
                left += 1
            hits = right - left + 1
            if hits > best_hits:
                best_hits = hits
                best_start = max(0, positions[left] - 20)
        window = text[best_start:best_start + _SNIPPET_WINDOW]
        prefix = "..." if best_start > 0 else ""
        suffix = "..." if best_start + _SNIPPET_WINDOW < len(text) else ""
        return prefix + window.strip() + suffix
