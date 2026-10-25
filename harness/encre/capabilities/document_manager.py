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

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse



from encre.logging_config import get_logger
logger = get_logger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover - httpx is optional at import time
    httpx = None

# This module implements the knowledge library: a hierarchical tree of
# spaces, folders and typed content leaves (rich text docs, sheets, web
# clips, markdown files, raw attachments).  Everything persists in a single
# ``<data_dir>/library.json`` manifest; leaf bodies live under
# ``<data_dir>/documents/files``.  The tree is the single source of truth —
# there is no separate flat document index any more.  ``build_context``
# renders the leaves (depth-first, tree order) into the retrieval context
# string consumed by the loop prompt builder.

LIBRARY_NODE_TYPES = ("space", "folder", "doc", "sheet", "clip", "md", "attachment")
_LIBRARY_CONTENT_TYPES = ("doc", "sheet", "clip", "md", "attachment")
_MAX_CRAWL_PAGES = 30


class LibraryNode:
    """One node of the knowledge-library tree.

    ``parent_id`` is ``""`` for top-level spaces.  Structural nodes
    (``space`` / ``folder``) carry no content; content leaves (``doc``,
    ``sheet``, ``clip``, ``md``, ``attachment``) store their body in
    ``files/<content_path>`` and keep source metadata for provenance.
    """

    def __init__(
        self,
        id: str = "",
        parent_id: str = "",
        type: str = "folder",
        name: str = "",
        order: int = 0,
        created_at: float = 0.0,
        status: str = "ready",
        original_url: str = "",
        original_path: str = "",
        content_path: str = "",
        file_type: str = "",
        size: int = 0,
    ) -> None:
        self.id = id
        self.parent_id = parent_id
        self.type = type
        self.name = name
        self.order = order
        self.created_at = created_at
        self.status = status
        self.original_url = original_url
        self.original_path = original_path
        self.content_path = content_path
        self.file_type = file_type
        self.size = size

    def to_dict(self) -> dict[str, Any]:
        """Serialise the node to the manifest payload (leaf fields included)."""
        d: dict[str, Any] = {
            "id": self.id,
            "parent_id": self.parent_id,
            "type": self.type,
            "name": self.name,
            "order": self.order,
            "created_at": self.created_at,
        }
        if self.type in _LIBRARY_CONTENT_TYPES:
            d.update({
                "status": self.status,
                "original_url": self.original_url,
                "original_path": self.original_path,
                "content_path": self.content_path,
                "file_type": self.file_type,
                "size": self.size,
            })
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LibraryNode":
        """Reconstruct a :class:`LibraryNode` from manifest JSON."""
        return cls(
            id=str(d.get("id", "")),
            parent_id=str(d.get("parent_id", "")),
            type=str(d.get("type", "folder")),
            name=str(d.get("name", "")),
            order=int(d.get("order", 0)),
            created_at=float(d.get("created_at", 0.0)),
            status=str(d.get("status", "ready")),
            original_url=str(d.get("original_url", "")),
            original_path=str(d.get("original_path", "")),
            content_path=str(d.get("content_path", "")),
            file_type=str(d.get("file_type", "")),
            size=int(d.get("size", 0)),
        )


def _strip_html(html: str) -> str:
    """Strip scripts/styles/nav/footer and tags from HTML, returning plain text."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:100000]


def _extract_same_domain_links(html: str, base_url: str, domain: str) -> set[str]:
    """Extract same-domain HTTP(S) links from an HTML page (for crawling)."""
    links: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc == domain and parsed.scheme in ("http", "https"):
            clean = parsed._replace(fragment="").geturl()
            links.add(clean)
    return links


def crawl_url_to_text(name: str, url: str) -> str:
    """Fetch a web page and crawl up to ``_MAX_CRAWL_PAGES`` same-domain links.

    Returns a Markdown-ish concatenation of each page's stripped text.
    """
    if httpx is None:
        raise RuntimeError("httpx is required for URL clips. Install with: pip install httpx")
    parsed = urlparse(url)
    domain = parsed.netloc
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    html = resp.text
    links = _extract_same_domain_links(html, url, domain)
    lines: list[str] = []
    lines.append(f"# {name or domain}")
    lines.append(f"Source: {url}")
    lines.append("")
    lines.append(f"## {url}")
    lines.append(_strip_html(html))
    visited = {url}
    count = 0
    for link in sorted(links):
        if count >= _MAX_CRAWL_PAGES:
            break
        if link in visited:
            continue
        visited.add(link)
        try:
            sub = httpx.get(link, follow_redirects=True, timeout=15)
            sub.raise_for_status()
            lines.append("")
            lines.append(f"## {link}")
            lines.append(_strip_html(sub.text))
            count += 1
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
    return "\n".join(lines)


class EncreLibraryManager:
    """Manage the knowledge-library tree: CRUD, persistence and retrieval."""

    def __init__(self, data_dir: str) -> None:
        """Initialise storage paths and load the persisted tree manifest."""
        self._base_dir = Path(data_dir) / "documents"
        self._files_dir = self._base_dir / "files"
        self._manifest_path = self._base_dir / "library.json"
        self._legacy_index_path = self._base_dir / "index.json"
        self._nodes: dict[str, LibraryNode] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        """Create the ``files/`` storage directory if it does not exist."""
        self._files_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        """Load the library manifest, migrating a legacy flat index once."""
        if self._manifest_path.exists():
            try:
                data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
                for item in data.get("nodes", []):
                    node = LibraryNode.from_dict(item)
                    self._nodes[node.id] = node
                return
            except Exception:
                self._nodes.clear()
        self._migrate_legacy_index()

    def _migrate_legacy_index(self) -> None:
        """Fold a pre-tree flat ``index.json`` into the tree, zero-cost.

        Every legacy document becomes a leaf under one default space, with
        the leaf type inferred from its source and file extension.  The
        legacy manifest is left untouched on disk; the tree simply stops
        reading it.
        """
        docs: list[dict[str, Any]] = []
        if self._legacy_index_path.exists():
            try:
                data = json.loads(self._legacy_index_path.read_text(encoding="utf-8"))
                docs = data.get("documents", []) or []
            except Exception:
                docs = []
        root = LibraryNode(
            id=uuid.uuid4().hex[:12],
            parent_id="",
            type="space",
            name="",
            order=0,
            created_at=time.time(),
        )
        self._nodes[root.id] = root
        for idx, item in enumerate(docs):
            source = str(item.get("source", ""))
            file_type = str(item.get("file_type", ""))
            if source == "url":
                node_type = "clip"
            elif file_type in (".md", ".markdown"):
                node_type = "md"
            else:
                node_type = "attachment"
            node = LibraryNode(
                id=uuid.uuid4().hex[:12],
                parent_id=root.id,
                type=node_type,
                name=str(item.get("name", "")),
                order=idx,
                created_at=float(item.get("added_at", 0.0)) or time.time(),
                status=str(item.get("status", "ready")),
                original_url=str(item.get("original_url", "")),
                original_path=str(item.get("original_path", "")),
                content_path=str(item.get("content_path", "")),
                file_type=file_type,
                size=int(item.get("size", 0)),
            )
            self._nodes[node.id] = node
        self._save()

    def _save(self) -> None:
        """Persist the tree manifest to ``library.json``."""
        self._ensure_dirs()
        data = {"nodes": [n.to_dict() for n in self._nodes.values()]}
        self._manifest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Tree internals ────────────────────────────────────────────────

    def _children_of(self, parent_id: str) -> list[LibraryNode]:
        """Return the children of ``parent_id`` ordered by their ``order``."""
        kids = [n for n in self._nodes.values() if n.parent_id == parent_id]
        kids.sort(key=lambda n: (n.order, n.created_at, n.id))
        return kids

    def _next_order(self, parent_id: str) -> int:
        """Return the next sibling order value under ``parent_id``."""
        kids = self._children_of(parent_id)
        return (kids[-1].order + 1) if kids else 0

    def _is_descendant(self, node_id: str, maybe_ancestor_id: str) -> bool:
        """Whether ``node_id`` sits inside the subtree of ``maybe_ancestor_id``."""
        seen: set[str] = set()
        cur = self._nodes.get(node_id)
        while cur is not None and cur.parent_id and cur.parent_id not in seen:
            seen.add(cur.parent_id)
            if cur.parent_id == maybe_ancestor_id:
                return True
            cur = self._nodes.get(cur.parent_id)
        return False

    def _require_parent(self, parent_id: str) -> None:
        """Validate that ``parent_id`` is root or an existing space/folder."""
        if not parent_id:
            return
        parent = self._nodes.get(parent_id)
        if parent is None or parent.type not in ("space", "folder"):
            raise ValueError("Parent must be an existing space or folder")

    # ── Public tree API ───────────────────────────────────────────────

    def nodes(self) -> list[dict[str, Any]]:
        """Return every tree node for the panel (already plain dicts)."""
        return [n.to_dict() for n in self._nodes.values()]

    def get(self, node_id: str) -> dict[str, Any] | None:
        """Return one node's dict, or ``None`` when unknown."""
        node = self._nodes.get(node_id)
        return node.to_dict() if node else None

    def create_node(self, parent_id: str, node_type: str, name: str) -> dict[str, Any]:
        """Create a structural node or a self-contained content leaf.

        Covers ``space`` / ``folder`` (pure structure) and ``doc`` /
        ``sheet`` (leaf with a freshly initialised body file).  URL clips
        and local files go through :meth:`add_clip_pending` and
        :meth:`add_file` instead.
        """
        if node_type not in ("space", "folder", "doc", "sheet"):
            raise ValueError(f"Unsupported node type: {node_type}")
        self._require_parent(parent_id)
        node_id = uuid.uuid4().hex[:12]
        if node_type == "doc":
            content_path = f"{node_id}.html"
            self._ensure_dirs()
            body = "<p></p>"
            (self._files_dir / content_path).write_text(body, encoding="utf-8")
            node = LibraryNode(
                id=node_id, parent_id=parent_id, type=node_type, name=name,
                order=self._next_order(parent_id), created_at=time.time(),
                content_path=content_path, file_type=".html",
                size=len(body.encode("utf-8")),
            )
        elif node_type == "sheet":
            content_path = f"{node_id}.json"
            self._ensure_dirs()
            grid = json.dumps({"rows": [["", "", ""] for _ in range(3)]}, ensure_ascii=False)
            (self._files_dir / content_path).write_text(grid, encoding="utf-8")
            node = LibraryNode(
                id=node_id, parent_id=parent_id, type=node_type, name=name,
                order=self._next_order(parent_id), created_at=time.time(),
                content_path=content_path, file_type=".json",
                size=len(grid.encode("utf-8")),
            )
        else:
            node = LibraryNode(
                id=node_id, parent_id=parent_id, type=node_type, name=name,
                order=self._next_order(parent_id), created_at=time.time(),
            )
        self._nodes[node_id] = node
        self._save()
        return node.to_dict()

    def add_file(self, parent_id: str, name: str, file_path: str) -> dict[str, Any]:
        """Import a local file as a ``md`` or ``attachment`` leaf.

        The bytes are copied under ``files/`` so the content survives even
        if the user later moves or deletes the source file.
        """
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        self._require_parent(parent_id)
        node_id = uuid.uuid4().hex[:12]
        ext = src.suffix.lower()
        node_type = "md" if ext in (".md", ".markdown") else "attachment"
        size = src.stat().st_size
        content_path = f"{node_id}{ext}"
        try:
            (self._files_dir / content_path).write_bytes(src.read_bytes())
        except Exception:
            content_path = ""
        node = LibraryNode(
            id=node_id, parent_id=parent_id, type=node_type,
            name=name or src.stem, order=self._next_order(parent_id),
            created_at=time.time(), original_path=str(src.resolve()),
            content_path=content_path, file_type=ext, size=size,
        )
        self._nodes[node_id] = node
        self._save()
        return node.to_dict()

    def add_clip_pending(self, parent_id: str, name: str, url: str) -> dict[str, Any]:
        """Register a web-clip leaf with ``status="loading"`` for async crawl."""
        self._require_parent(parent_id)
        node_id = uuid.uuid4().hex[:12]
        parsed = urlparse(url)
        ext = Path(url.split("?")[0].split("#")[0]).suffix.lower() or ".html"
        node = LibraryNode(
            id=node_id, parent_id=parent_id, type="clip",
            name=name or parsed.netloc, order=self._next_order(parent_id),
            created_at=time.time(), status="loading",
            original_url=url, file_type=ext,
        )
        self._nodes[node_id] = node
        self._save()
        return node.to_dict()

    def finish_clip(self, node_id: str, text: str) -> dict[str, Any] | None:
        """Finalise a pending web clip once crawling completes."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        content_path = f"{node_id}.txt"
        self._ensure_dirs()
        (self._files_dir / content_path).write_text(text, encoding="utf-8")
        node.content_path = content_path
        node.status = "ready"
        node.size = len(text.encode("utf-8"))
        self._save()
        return node.to_dict()

    def move(self, node_id: str, new_parent_id: str, index: int) -> dict[str, Any]:
        """Move a node (with its subtree) under ``new_parent_id`` at ``index``.

        Moving a node into its own descendant is rejected; spaces may only
        live at the root level.
        """
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError("Node not found")
        self._require_parent(new_parent_id)
        if new_parent_id and self._is_descendant(new_parent_id, node_id):
            raise ValueError("Cannot move a node into its own subtree")
        if node.type == "space" and new_parent_id:
            raise ValueError("Spaces can only live at the root level")
        siblings = [n for n in self._children_of(new_parent_id) if n.id != node_id]
        index = max(0, min(index, len(siblings)))
        siblings.insert(index, node)
        for i, sib in enumerate(siblings):
            sib.parent_id = new_parent_id
            sib.order = i
        self._save()
        return node.to_dict()

    def rename(self, node_id: str, name: str) -> dict[str, Any]:
        """Rename a tree node."""
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError("Node not found")
        node.name = name.strip()
        self._save()
        return node.to_dict()

    def delete(self, node_id: str) -> bool:
        """Delete a node and its whole subtree (leaf body files included)."""
        node = self._nodes.get(node_id)
        if node is None:
            return False
        doomed = [node_id]
        while doomed:
            cur = doomed.pop()
            n = self._nodes.get(cur)
            if n is None:
                continue
            doomed.extend(c.id for c in self._children_of(cur))
            if n.content_path:
                cf = self._files_dir / n.content_path
                if cf.exists():
                    try:
                        cf.unlink()
                    except Exception:
                        logger.debug("exception suppressed", exc_info=True)
            del self._nodes[cur]
        self._save()
        return True

    # ── Content ───────────────────────────────────────────────────────

    def read_content(self, node_id: str) -> str:
        """Read a leaf node's body text from its stored file."""
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError("Node not found")
        if node.type not in _LIBRARY_CONTENT_TYPES:
            raise ValueError("Node has no content")
        if not node.content_path:
            if node.original_path:
                try:
                    p = Path(node.original_path)
                    if p.exists():
                        return p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    logger.debug("exception suppressed", exc_info=True)
            return ""
        content_file = self._files_dir / node.content_path
        if content_file.exists():
            try:
                return content_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        return ""

    def write_content(self, node_id: str, content: str) -> dict[str, Any]:
        """Overwrite a leaf node's body file and refresh its size."""
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError("Node not found")
        if node.type not in _LIBRARY_CONTENT_TYPES:
            raise ValueError("Node has no content")
        if not node.content_path:
            raise ValueError("Node has no stored content file")
        self._ensure_dirs()
        (self._files_dir / node.content_path).write_text(content, encoding="utf-8")
        node.size = len(content.encode("utf-8"))
        if node.status != "ready":
            node.status = "ready"
        self._save()
        return node.to_dict()

    # ── Retrieval ─────────────────────────────────────────────────────

    def build_context(self) -> str:
        """Render all leaves (depth-first, tree order) into a context string."""
        parts: list[str] = []

        def walk(parent_id: str) -> None:
            for node in self._children_of(parent_id):
                if node.type in ("space", "folder"):
                    walk(node.id)
                    continue
                content = self.read_content(node.id)
                if not content:
                    content = f"(Location: {node.original_path or node.original_url})"
                title = f"[{node.name}]"
                if node.original_path:
                    title += f" ({node.original_path})"
                elif node.original_url:
                    title += f" ({node.original_url})"
                parts.append(f"--- {title} ---")
                parts.append(content[:8000])
                parts.append("")

        walk("")
        if not parts:
            return ""
        return "=== Reference Documents ===\n" + "\n".join(parts)
