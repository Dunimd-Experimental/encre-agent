#!/usr/bin/env python3
# Copyright 2026 Beijing Volcano Engine Technology Co., Ltd.
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

from __future__ import annotations

"""Read gene templates from the bundled static library.

Genes are pre-bundled at `references/gene-library.json` and shipped with the
skill release. Use `--list / --show / --summary` to inspect them.

This script intentionally does NOT do local gene matching nor any network I/O.
"""


import argparse
import json
import sys
from pathlib import Path

LIBRARY_PATH = (
    Path(__file__).resolve().parent.parent / "references" / "gene-library.json"
)

# Windows UTF-8 fix
for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")


def load_library():
    if not LIBRARY_PATH.exists():
        raise SystemExit(f"gene library not found: {LIBRARY_PATH}")
    return json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))


def _gene_summary(g: dict) -> dict:
    return {
        "id": g.get("id") or g.get("gene_id"),
        "summary": g.get("summary") or g.get("name") or g.get("gene_name"),
        "pattern_key": g.get("pattern_key"),
        "rule_text": (g.get("rule_text") or "")[:200],
        "created_at": g.get("created_at"),
    }


def list_genes():
    return [_gene_summary(g) for g in load_library()]


def show_gene(gene_id: str):
    for g in load_library():
        if str(g.get("id") or g.get("gene_id")) == str(gene_id):
            return g
    raise SystemExit(f"gene not found in library: {gene_id}")


def summary():
    data = load_library()
    pattern_keys = {}
    for g in data:
        key = str(g.get("pattern_key") or "")
        if key:
            pattern_keys[key] = pattern_keys.get(key, 0) + 1
    return {
        "backend": "static-library",
        "library_path": str(LIBRARY_PATH),
        "gene_count": len(data),
        "top_pattern_keys": sorted(
            pattern_keys.items(), key=lambda x: x[1], reverse=True
        )[:10],
    }


def main():
    parser = argparse.ArgumentParser(description="Inspect bundled gene templates")
    parser.add_argument(
        "--list", action="store_true", help="List all genes (summary form)"
    )
    parser.add_argument(
        "--show", metavar="GENE_ID", help="Show full content of one gene by id"
    )
    parser.add_argument(
        "--summary", action="store_true", help="Show gene library statistics"
    )
    args = parser.parse_args()

    if args.list:
        print(json.dumps(list_genes(), ensure_ascii=False, indent=2))
        return
    if args.show:
        print(json.dumps(show_gene(args.show), ensure_ascii=False, indent=2))
        return
    if args.summary:
        print(json.dumps(summary(), ensure_ascii=False, indent=2))
        return
    parser.print_help()


if __name__ == "__main__":
    main()
