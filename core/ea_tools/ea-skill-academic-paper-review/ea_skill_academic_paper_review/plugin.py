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

"""Plugin entry point for ``ea-skill-academic-paper-review``.

This module defines the plugin object that the Encre Agent plugin loader
instantiates when the user installs (or Encre Agent ships) this skill.
The loader looks for a :func:`create_plugin` factory function here,
calls it, and receives an :class:`EncrePlugin` subclass instance whose
:attr:`manifest` attribute describes everything the loader needs to know
about the plugin.

The plugin itself is a *system-default* skill: it ships inside the Encre
Agent distribution rather than being downloaded from the plugin market
on first use. The ``source=PluginSource.BUNDLED`` and
``tier="system-default"`` fields on the manifest are what tell the
loader to treat it as built-in rather than user-installed — that means
it appears in the "Bundled" section of the plugin marketplace UI, and
uninstalling it goes through the different "restore defaults" flow
rather than a plain delete.

The skill this plugin provides (``academic-paper-review``, listed in
``provides_skills``) is where the actual review logic lives; the plugin
object itself is just the metadata carrier. When the harness registers
this plugin, it walks ``provides_skills`` and looks up each skill name
in the shared skill registry.
"""

from __future__ import annotations

from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource


def create_plugin() -> EncrePlugin:
    """Instantiate and return the academic-paper-review plugin.

    This is the factory the plugin loader imports and calls. It returns
    a fresh instance of :class:`_SkillPlugin`; the class is defined at
    module level (rather than inside this function) so its manifest is
    a true class attribute — that lets callers inspect
    ``_SkillPlugin.manifest`` without constructing an instance, which
    is handy for tooling that lists available plugins without loading
    them.

    Returns:
        A new :class:`_SkillPlugin` instance. The manifest is
        constructed at class definition time, not per call, so
        repeated :func:`create_plugin` invocations return plugins that
        share the same ``PluginManifest`` object — that is intentional
        and safe because :class:`PluginManifest` is immutable.
    """
    return _SkillPlugin()


class _SkillPlugin(EncrePlugin):
    """Concrete :class:`EncrePlugin` for the academic-paper-review skill.

    This class is a data-only carrier: it holds the plugin's
    :attr:`manifest` and inherits the loader-facing interface from
    :class:`EncrePlugin`. Subclassing is the extension point the loader
    uses to attach behaviour later (for example, lifecycle hooks such
    as :meth:`on_enable`, :meth:`on_disable`, or :meth:`on_install`);
    the current implementation does not override any of those because
    the skill registers itself through the shared skill registry at
    import time rather than through per-plugin hooks.

    Attributes:
        manifest: The plugin's metadata descriptor. See each field on
            :class:`PluginManifest` for the semantic of each entry.
            The important ones for the loader are ``name`` (unique
            plugin identifier), ``provides_skills`` (skill names to
            register), ``source`` (bundled vs. installed), and
            ``tier`` (system-default vs. user vs. system).
    """

    manifest = PluginManifest(
        # Unique plugin identifier. Must match the PyPI distribution
        # name (``ea-skill-academic-paper-review``) so the market index
        # can correlate an installed plugin with its catalog entry.
        name="ea-skill-academic-paper-review",
        display_name="Academic Paper Review",
        # Lucide icon name used by the desktop plugin marketplace UI.
        icon="graduation-cap",
        # SemVer version of the plugin. Bumped on every published
        # release; the desktop UI uses this to sort and filter.
        version="0.5.0",
        # Short description shown in the marketplace listing. Kept to
        # one sentence so it fits in the marketplace card.
        description="EA (Encre Agent) system-default skill: academic-paper-review.",
        # Author of the plugin. Uses the standard "Name <email>" format
        # so it round-trips with PyPI's author metadata.
        author="Dunimd Team <dunimd@outlook.com>",
        # SPDX license identifier. Must match the license file shipped
        # with the plugin distribution; CI validates this.
        license="Apache-2.0",
        # Project homepage. Displayed in the marketplace detail view;
        # also used by the desktop UI's "open on GitHub" affordance.
        homepage="https://github.com/mf2023/Encre",
        # Whether this plugin is bundled with Encre Agent itself
        # (``PluginSource.BUNDLED``) or was downloaded from the market
        # (``PluginSource.INSTALLED``). Bundled plugins are shipped
        # inside the Encre Agent wheel; installed plugins live in the
        # user's data directory.
        source=PluginSource.BUNDLED,
        # Human-readable tier label for the marketplace UI.
        # "system-default" means it is part of the base Encre Agent
        # distribution and cannot be uninstalled without restoring
        # defaults; "user" would be a downloaded plugin.
        tier="system-default",
        # Other plugins this one depends on. Empty for this plugin
        # because academic-paper-review is self-contained.
        dependencies=[],
        # Minimum Encre Agent engine version required. The loader
        # refuses to load a plugin whose engine constraint is not
        # satisfied by the running Encre Agent version.
        engines={"encre": ">=0.5"},
        # Free-form tags used for marketplace filtering and search.
        tags=["system-default", "skill", "research"],
        # Skill names this plugin registers with the shared skill
        # registry. Each name must resolve to a real skill module
        # somewhere on the search path; the loader validates this at
        # install time and refuses to load the plugin if any listed
        # skill cannot be found.
        provides_skills=["academic-paper-review"],
        # Events that should trigger automatic activation of this
        # plugin. Empty here because the skill is always loaded as
        # part of the bundled set — it does not need to react to
        # external events.
        activation_events=[],
        # Permissions the plugin requests at runtime. Empty because
        # academic-paper-review only reads files that the user has
        # explicitly pointed the agent at; it does not need extra
        # filesystem, network, or system access.
        permissions=[],
    )
