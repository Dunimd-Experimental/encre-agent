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

"""Plugin manifest and factory for the ``ea-tool-pdf`` tool.

The plugin loader imports this module and calls ``create_plugin()`` once
per session to obtain a fresh ``EncrePlugin``. The manifest attached to
the plugin class is the source of truth for what the desktop market and
``encre-plugin verify`` render to users; ``get_tools()`` is what the tool
registry iterates to expose the ``pdf`` tool to the model.

``danger_levels`` is set to ``{"pdf": "safe"}`` because this tool only
reads files -- it never mutates the PDF on disk -- so destructive-aware
UIs can render it without an explicit confirmation prompt. The
underlying tool is still flagged ``is_readonly=True`` for the same
reason; the two flags are complementary and both are honored by
different parts of the agent stack.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource
from ea_tool_pdf.tool import pdf_tool


def create_plugin() -> EncrePlugin:
    """Return a fresh ``EncrePlugin`` instance exposing the pdf tool.

    Uses the factory pattern rather than a module-level singleton so each
    session has its own plugin instance. This matches the convention the
    plugin loader expects and keeps the door open for per-session state
    without breaking the current API.

    Returns:
        A new ``_PdfPlugin`` instance. The manifest is a shared class
        attribute; only the returned instance is fresh per call.
    """
    return _PdfPlugin()


class _PdfPlugin(EncrePlugin):
    """Internal plugin container for the pdf tool.

    The underscore prefix signals that callers should go through
    ``create_plugin()``. The class identity is intentionally an
    implementation detail so future refactors can split this file without
    changing import paths for consumers.

    Manifest invariants:
        * ``source=BUNDLED`` -- ships with the Encre Agent distribution
          rather than being user-installed from the market, so the
          desktop does not offer to uninstall it.
        * ``tier="system-default"`` -- visible in the default UI tier
          without extra user configuration.
        * ``engines={"encre": ">=0.5"}`` -- advertises the minimum
          Encre Agent version required so older clients can refuse to
          load the plugin rather than half-installing a broken
          integration.
    """

    manifest = PluginManifest(
        name="ea-tool-pdf",
        display_name="PDF",
        icon="file-type-2",
        version="0.5.0",
        description="EA (Encre Agent) system-default tools: pdf.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "office"],
        provides_tools=['pdf'],
            danger_levels={"pdf": "safe"},
        provides_skills=["tool-pdf"],
        activation_events=[],
        permissions=[],
    )

    def get_tools(self) -> list:
        """Return the list of tools this plugin contributes to the agent.

        Returning a fresh list each call keeps the contract honest: the
        tool registry may concatenate and mutate the returned list, and
        those mutations must not leak back into the plugin's state.

        Returns:
            A one-element list containing the ``pdf_tool`` instance.
        """
        return [pdf_tool]
