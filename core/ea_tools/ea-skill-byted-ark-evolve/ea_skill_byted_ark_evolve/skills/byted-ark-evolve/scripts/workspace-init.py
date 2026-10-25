#!/usr/bin/env python3
# ruff: noqa: E402
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

"""
Workspace initialization for Evolution Skill.
Scans workspace files, classifies status, generates file-registry.json.
Read-only: does NOT modify any workspace files.
"""

import os
import sys
import json
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime, timezone

for stream in [sys.stdout, sys.stderr, sys.stdin]:
    if stream and hasattr(stream, "reconfigure") and stream.encoding != "utf-8":
        stream.reconfigure(encoding="utf-8")

# --- Paths ---
from _workspace import resolve_workspace_root, claw_exclude_dirs

WORKSPACE = Path(resolve_workspace_root())
EVOLUTION_DIR = WORKSPACE / "evolution-data"
REGISTRY_PATH = EVOLUTION_DIR / "file-registry.json"
SKILL_VERSION = "0.1.0"

# --- Exclusions ---
EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    "evolution-data",
} | claw_exclude_dirs()

# --- Known file semantic map (hardcoded, mirrors file-semantic-map.md) ---
KNOWN_FILES = {
    "SOUL.md": {
        "layer": "identity",
        "governs": ["tone", "values", "style", "boundaries"],
        "write_tier": "high",
        "baseline_lines": 37,
    },
    "IDENTITY.md": {
        "layer": "identity",
        "governs": ["name", "persona", "appearance"],
        "write_tier": "high",
        "baseline_lines": 24,
    },
    "USER.md": {
        "layer": "context",
        "governs": ["user-prefs", "timezone", "conventions"],
        "write_tier": "medium",
        "baseline_lines": 20,
    },
    "MEMORY.md": {
        "layer": "context",
        "governs": ["long-term-memory"],
        "write_tier": "medium",
        "baseline_lines": 10,
    },
    "AGENTS.md": {
        "layer": "protocol",
        "governs": ["rules", "permissions", "workflows"],
        "write_tier": "high",
        "baseline_lines": 210,
    },
    "TOOLS.md": {
        "layer": "protocol",
        "governs": ["device-config", "search-prefs", "local-env"],
        "write_tier": "low",
        "baseline_lines": 41,
    },
    "HEARTBEAT.md": {
        "layer": "protocol",
        "governs": ["periodic-checks"],
        "write_tier": "low",
        "baseline_lines": 10,
    },
    "BOOTSTRAP.md": {
        "layer": "protocol",
        "governs": ["first-run-setup"],
        "write_tier": "low",
        "baseline_lines": 30,
    },
}

# Pattern-based known paths
KNOWN_PATTERNS = {
    "memory/": {
        "layer": "context",
        "governs": ["daily-logs", "short-term-memory"],
        "write_tier": "low",
    },
    "skills/byted-ark-evolve/": {
        "status_override": "skill-owned",
        "layer": "capability",
        "governs": ["evolution-logic"],
        "write_tier": "medium",
    },
    "skills/": {
        "layer": "capability",
        "governs": ["skill-logic"],
        "write_tier": "medium",
    },
}

# --- Placeholder keywords (indicate default template) ---
PLACEHOLDERS = [
    "_(待定)_",
    "_(optional)_",
    "_(未设置)_",
    "Fill this in",
    "Add whatever helps",
    "_(What do they care about?",
    "Make it yours",
    "This is a starting point",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()[:16]}"


def count_lines(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def has_placeholders(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return any(p in content for p in PLACEHOLDERS)
    except Exception:
        return False


def get_openclaw_version() -> str:
    try:
        result = subprocess.run(
            ["openclaw", "--version"], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def classify_file(rel_path: str, full_path: Path) -> dict:
    """Classify a single file."""
    filename = os.path.basename(rel_path)
    lines = count_lines(full_path)
    file_hash = sha256_file(full_path)

    # 1. Check pattern-based overrides (e.g. skills/byted-ark-evolve/*)
    for pattern, meta in KNOWN_PATTERNS.items():
        if rel_path.startswith(pattern):
            if "status_override" in meta:
                return {
                    "status": meta["status_override"],
                    "layer": meta["layer"],
                    "governs": meta["governs"],
                    "write_tier": meta["write_tier"],
                    "lines": lines,
                    "hash": file_hash,
                }
            # Known pattern but no status override - fall through to check
            known_meta = meta
            break
    else:
        known_meta = None

    # 2. Check known root files
    if filename in KNOWN_FILES:
        meta = KNOWN_FILES[filename]
        baseline = meta["baseline_lines"]

        # Determine if evolvable or user-owned
        if has_placeholders(full_path):
            status = "evolvable"
        elif lines < baseline * 1.2:
            status = "evolvable"
        else:
            status = "user-owned"

        return {
            "status": status,
            "layer": meta["layer"],
            "governs": meta["governs"],
            "write_tier": meta["write_tier"],
            "lines": lines,
            "hash": file_hash,
        }

    # 3. Known pattern (memory/*, skills/*)
    if known_meta:
        return {
            "status": "user-owned",  # pattern files with user content
            "layer": known_meta["layer"],
            "governs": known_meta["governs"],
            "write_tier": known_meta["write_tier"],
            "lines": lines,
            "hash": file_hash,
        }

    # 4. Unknown file
    return {
        "status": "needs_review",
        "layer": None,
        "governs": None,
        "write_tier": "unknown",
        "lines": lines,
        "hash": file_hash,
    }


def scan_workspace() -> dict:
    """Scan all workspace files and classify them."""
    files = {}

    for root, dirs, filenames in os.walk(WORKSPACE):
        # Prune excluded directories
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

        for fname in filenames:
            full_path = Path(root) / fname
            rel_path = str(full_path.relative_to(WORKSPACE)).replace("\\", "/")
            files[rel_path] = classify_file(rel_path, full_path)

    return files


def load_existing_registry() -> dict | None:
    """Load existing registry if present."""
    if REGISTRY_PATH.exists():
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def merge_with_existing(new_files: dict, existing: dict | None) -> dict:
    """Preserve user-owned status from previous init (don't downgrade)."""
    if not existing or "files" not in existing:
        return new_files

    for path, info in new_files.items():
        old = existing["files"].get(path)
        if old and old.get("status") == "user-owned" and info["status"] == "evolvable":
            # Don't downgrade user-owned to evolvable
            info["status"] = "user-owned"
        if old and old.get("layer") and info.get("layer") is None:
            # Preserve previously classified layer for needs_review files
            info["layer"] = old["layer"]
            info["governs"] = old.get("governs")
            info["write_tier"] = old.get("write_tier", "unknown")
            if info["status"] == "needs_review":
                info["status"] = old.get("status", "needs_review")

    return new_files


def build_registry(files: dict) -> dict:
    return {
        "initialized_at": datetime.now(timezone.utc).isoformat(),
        "agent_version": get_openclaw_version(),
        "skill_version": SKILL_VERSION,
        "files": files,
        "write_policy": {
            "evolvable": "section-edit",
            "user-owned": "append-only",
            "skill-owned": "skill-managed",
            "needs_review": "blocked-until-classified",
        },
    }


def print_summary(files: dict):
    """Print human-readable summary."""
    status_counts = {}
    for info in files.values():
        s = info["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    total = len(files)
    print("\nEvolution init complete:")
    print(f"  Files scanned: {total}")
    for status in ["evolvable", "user-owned", "skill-owned", "needs_review"]:
        count = status_counts.get(status, 0)
        if count:
            names = [p for p, i in files.items() if i["status"] == status]
            preview = ", ".join(names[:5])
            if len(names) > 5:
                preview += f" (+{len(names) - 5} more)"
            print(f"  {status}: {count} ({preview})")

    if REGISTRY_PATH.exists():
        print(f"  Registry: updated ({REGISTRY_PATH})")
    else:
        print(f"  Registry: created ({REGISTRY_PATH})")

    # Welcome message + command reference
    print("""
========================================
  欢迎使用进化系统
========================================

这个系统让 Agent 能从你的反馈中学习：
  - 你日常的纠正、建议会被自动记录  - 攒够一定数量后，系统会分析并提出改进方案  - 所有改动必须经你确认才会执行
可用命令：  /evolve           手动触发一次进化分析  /evolve scan      扫描历史对话提取反馈
  /evolve dashboard 生成进化 Dashboard
  /evolve help      查看完整引导
""")


def main():
    if not WORKSPACE.exists():
        print(f"Error: workspace not found at {WORKSPACE}", file=sys.stderr)
        sys.exit(1)

    # Ensure evolution-data dir exists
    EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing registry for merge
    existing = load_existing_registry()

    # Scan and classify
    files = scan_workspace()
    files = merge_with_existing(files, existing)

    # Build and save registry
    registry = build_registry(files)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)

    # Print summary
    print_summary(files)

    # Also output JSON for programmatic use
    if "--json" in sys.argv:
        print(json.dumps(registry, ensure_ascii=False, indent=2))

    # Check if DB needs init
    db_path = EVOLUTION_DIR / "evolution.db"
    if not db_path.exists():
        print(
            "\n  DB not found. Run: python skills/byted-ark-evolve/scripts/db-init.py"
        )
    else:
        print(f"  DB: exists ({db_path})")


if __name__ == "__main__":
    main()
