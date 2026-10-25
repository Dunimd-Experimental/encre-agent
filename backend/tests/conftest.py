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

"""Shared pytest fixtures for the Encre backend test suite.

This module exposes the reusable test infrastructure shared by every test
module in the package. Keeping the boilerplate in one place lets individual
test files focus on the behavior under verification instead of repeating
setup logic.

The fixtures cover three concerns: an isolated temporary filesystem tree
that mimics a small real-world project layout, a deliberately small
configuration instance suitable for fast unit tests, and a fully wired
tool registry that mirrors the production tool set.
"""

import os
import tempfile
from pathlib import Path

import pytest
from encre.config import EncreConfig
from encre.plugins.registry import PluginRegistry
from encre.tools.registry import ToolRegistry


@pytest.fixture
def temp_dir():
    """Provide an isolated temporary filesystem tree for file-tool tests.

    The fixture builds a deterministic directory layout holding a README, a
    top-level Python module, a nested ``src/`` package with helper utilities
    and a JSON config, an empty file, a hidden directory containing a secret
    file, and a binary-suffixed file. The working directory is switched into
    that tree before the test body and restored in a ``finally`` block, so
    every test observes the same starting directory without polluting the
    repository root.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)

        # Human-readable project readme used by listing and read tests.
        (base / "README.md").write_text("# Test Project\n\nHello, world!\n", encoding="utf-8")

        # Executable-looking Python module used by execution and import tests.
        (base / "main.py").write_text(
            "def hello():\n    return 'Hello, world!'\n\n\nif __name__ == '__main__':\n    print(hello())\n",  # noqa: E501
            encoding="utf-8",
        )

        # Nested package so recursive search and path-resolution tests have depth.
        (base / "src").mkdir(exist_ok=True)
        (base / "src" / "utils.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        (base / "src" / "config.json").write_text('{"key": "value", "debug": true}', encoding="utf-8")  # noqa: E501

        # Zero-length file for the empty-read edge case.
        (base / "empty.txt").write_text("", encoding="utf-8")

        # Hidden directory so dotfile-visibility behavior can be asserted.
        (base / ".hidden").mkdir(exist_ok=True)
        (base / ".hidden" / "secret.txt").write_text("secret content\n", encoding="utf-8")

        # Binary-suffixed file so extension filtering can be exercised.
        (base / "data.bin").write_text("binary\x00content", encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(str(base))
        try:
            yield str(base)
        finally:
            os.chdir(old_cwd)


@pytest.fixture
def sample_config():
    """Return a minimal :class:`EncreConfig` tuned for fast unit-test runs.

    A small token budget and a quiet log level keep fixture construction
    cheap and stop configuration chatter from leaking into test output.
    """
    return EncreConfig(
        model="gpt-5.6",
        permission_mode="default",
        max_turns=10,
        max_tokens=4096,
        log_level="WARNING",
    )


@pytest.fixture
def tool_registry():
    """Return a :class:`ToolRegistry` pre-populated with every builtin tool.

    Pre-registering the tools removes per-test registration boilerplate and
    guarantees that regression tests run against a registry shaped like the
    production wiring rather than against a hand-built stub.
    """
    registry = ToolRegistry()
    plugin_registry = PluginRegistry()
    plugin_registry.discover_all()
    for tool in plugin_registry.get_all_tools():
        registry.register(tool)
    return registry
