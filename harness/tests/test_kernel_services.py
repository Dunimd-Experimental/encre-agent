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

"""Kernel service locator: stable identity, explicit discovery, resettable."""

from __future__ import annotations

import pytest

from encre.kernel import services


@pytest.fixture(autouse=True)
def _isolate() -> None:
    services.reset_shared_services()
    yield
    services.reset_shared_services()


def test_plugin_registry_identity_is_stable() -> None:
    first = services.shared_plugin_registry_raw()
    second = services.shared_plugin_registry_raw()
    assert first is second


def test_skill_registry_identity_is_stable() -> None:
    first = services.shared_skill_registry_raw()
    second = services.shared_skill_registry_raw()
    assert first is second


def test_raw_accessor_does_not_run_discovery() -> None:
    class Registry:
        def __init__(self) -> None:
            self.calls = 0

        def discover_all(self) -> None:
            self.calls += 1

    registry = Registry()
    services.install_shared_plugin_registry(registry, discovered=False)
    assert services.shared_plugin_registry_raw() is registry
    assert registry.calls == 0

    services.shared_plugin_registry()
    assert registry.calls == 1
    services.shared_plugin_registry()
    assert registry.calls == 1, "discovery must run exactly once"


def test_ensure_discovered_is_once_only() -> None:
    class Registry:
        def __init__(self) -> None:
            self.calls = 0

        def discover_all(self) -> None:
            self.calls += 1

    registry = Registry()
    services.ensure_plugins_discovered(registry)
    services.ensure_plugins_discovered(registry)
    assert registry.calls == 1


def test_install_and_reset() -> None:
    class Registry:
        def discover_all(self) -> None:  # pragma: no cover - never called
            raise AssertionError("should not discover")

    registry = Registry()
    services.install_shared_plugin_registry(registry)
    assert services.shared_plugin_registry() is registry
    services.reset_shared_services()
    assert services.shared_plugin_registry_raw() is not registry


def test_candidate_dirs_are_shared_constants() -> None:
    assert ".encre/skills" in services.PROJECT_SKILLS_CANDIDATE_DIRS
    assert ".claude/skills" in services.PROJECT_SKILLS_CANDIDATE_DIRS
    assert ".encre/agents" in services.PROJECT_SUB_AGENT_CANDIDATE_DIRS
    assert ".codex/agents" in services.PROJECT_SUB_AGENT_CANDIDATE_DIRS


def test_agent_aliases_delegate_to_kernel() -> None:
    """``encre.agent`` keeps historical names but must not own the state."""
    import encre.agent as agent

    assert agent._get_shared_plugin_registry_raw is services.shared_plugin_registry_raw
    assert agent._PROJECT_SKILLS_CANDIDATE_DIRS is services.PROJECT_SKILLS_CANDIDATE_DIRS
