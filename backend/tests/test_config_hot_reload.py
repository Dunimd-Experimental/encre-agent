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

"""P1.6 -- configuration hot reload.

These tests exercise the **real** runtime: a real
:class:`~encre.loop.runner.EncreLoop`, a real
:class:`~encre.session.EncreSession`, a real
:class:`~encre.skills.registry.EncreSkillRegistry`, the real append-only
:class:`~encre.events.EventSourcedBus` and the real
:func:`~encre.server.admin.handle_admin` entry point.  The only substituted
object is the *model backend* -- a genuine
:class:`~encre.backends.base.BaseBackend` subclass scripting a deterministic
stream -- plus two doubles that exist purely to drive branches production
cannot be asked to produce on demand: a rules loader whose ``clear_cache()``
raises (fault containment) and a policy reflow that counts its refresh calls
(so "every reload re-pushes" is observable).

Covered invariants:

1. ``loop.reload`` puts the new configuration in force on the loop *and* on its
   session, and reports whether the configuration actually changed;
2. the derived surfaces are re-derived -- mode profile, tool set, thinking
   config, system-prompt cache, rules cache, skill definitions, learned
   policies -- so an edit made between two reloads is picked up;
3. reload is idempotent: the surfaces are *derived*, never accumulated, so a
   second call leaves the same state and reports ``config_changed=False``;
4. every reload is audited as a ``config_reloaded`` entry on the append-only
   backbone and mirrored as a typed event on the stream;
5. a failing surface degrades the reload to *partial* -- reported as a warning
   -- instead of aborting the other surfaces or killing the loop;
6. the documented boundary holds: the backend client is never hot-swapped;
7. ``handle_admin("/reload")`` re-reads the store, applies it to idle sessions,
   and skips -- while reporting -- a session whose turn is in flight, keeping
   the existing local-origin authorization.

The data root is redirected to ``tmp_path`` via ``ENCRE_DATA_DIR`` so the real
user data directory is never touched.
"""

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from encre.backends.base import BaseBackend
from encre.config import EncreConfig, ModelConfig
from encre.events import KIND_CONFIG_RELOADED, ConfigReloaded
from encre.loop import EncreLoop
from encre.loop.hot_reload import (
    RELOADED_SURFACES,
    ReloadReport,
    config_fingerprint,
)
from encre.session import EncreSession
from encre.skills.registry import EncreSkillRegistry
from encre.skills.types import SkillSource
from encre.tools.registry import ToolRegistry
from encre.utils.types import BackendFinish, BackendText


class _ScriptedBackend(BaseBackend):
    """A real backend that scripts a deterministic stream per request."""

    def __init__(self, text: str = "reloaded") -> None:
        """Store the scripted reply text and zero the call and close counters."""
        self.text = text
        self.chat_calls = 0
        self.closed = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
        enable_caching: bool = False,
        cache_edits_state: Any = None,
    ):
        """Count the call and stream the scripted text followed by a stop finish."""
        self.chat_calls += 1
        yield BackendText(text=self.text)
        yield BackendFinish(reason="stop", usage={"input_tokens": 1, "output_tokens": 1})

    def supports_tool_calling(self) -> bool:
        """Advertise full tool-calling support."""
        return True

    def context_window_size(self) -> int:
        """Report a one-million-token context window."""
        return 1_000_000

    def supports_prompt_caching(self) -> bool:
        """Report that prompt caching is unavailable."""
        return False

    async def aclose(self) -> None:
        """Record the close request instead of tearing down real resources."""
        self.closed += 1


class _WedgedRulesLoader:
    """Fault injector: a rules loader whose cache cannot be cleared.

    Used to prove the reload contains a per-surface fault instead of aborting
    the surfaces that come after it.
    """

    def clear_cache(self) -> None:
        """Always fail, simulating a rules cache that cannot be flushed."""
        raise RuntimeError("rules cache is wedged")


class _CountingReflow:
    """Collaborator double: counts how often learned policies are re-pushed."""

    def __init__(self) -> None:
        """Start with a zero refresh counter."""
        self.refreshes = 0

    def refresh(self) -> None:
        """Increment the counter of learned-policy refresh calls."""
        self.refreshes += 1


def _config(**overrides: Any) -> EncreConfig:
    """A minimal real configuration for an isolated run."""
    base: dict[str, Any] = {
        "model": "reload-model",
        "backend_type": "openai",
        "api_key": "sk-not-a-real-key",
        "permission_mode": "bypass",
        "max_turns": 0,
        "max_tokens": 512,
        "log_level": "ERROR",
        "enable_prompt_caching": False,
        "checkpoint_max_count": 1,
        "workspace": "",
    }
    base.update(overrides)
    return EncreConfig(**base)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Isolate the encrypted data root to ``tmp_path``."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _loop(
    data_root,
    config: EncreConfig | None = None,
    *,
    skill_registry: EncreSkillRegistry | None = None,
) -> EncreLoop:
    """Build a real loop whose backend is the scripted stream."""
    cfg = config if config is not None else _config()
    with patch("encre.loop.create_backend", return_value=_ScriptedBackend()):
        return EncreLoop(
            cfg,
            EncreSession(cfg),
            ToolRegistry(),
            skill_registry=skill_registry,
        )


def _write_skill(workspace, name: str, description: str, body: str):
    """Create ``<workspace>/.encre/skills/<name>/SKILL.md``."""
    skill_dir = workspace / ".encre" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


# ----------------------------------------------------------------------
# Fingerprint
# ----------------------------------------------------------------------


def test_config_fingerprint_tracks_content_not_identity():
    """Two equal configurations share a fingerprint; a changed one does not."""
    first = _config(model="same")
    second = _config(model="same")

    assert first is not second
    assert config_fingerprint(first) == config_fingerprint(second)
    assert config_fingerprint(first) != config_fingerprint(_config(model="other"))
    assert config_fingerprint(None) == ""


def test_config_fingerprint_ignores_api_key_ciphertext():
    """Re-encrypting the same key must not look like a configuration change.

    ``to_dict`` encrypts ``api_key`` with a fresh AES-GCM nonce every call, so
    a fingerprint taken over the encrypted form would report a change on every
    reload of an unchanged configuration.
    """
    config = _config(api_key="sk-not-a-real-key")

    first_ciphertext = config.to_dict(encrypt_api_keys=True)["api_key"]
    second_ciphertext = config.to_dict(encrypt_api_keys=True)["api_key"]

    assert first_ciphertext and second_ciphertext
    assert first_ciphertext != second_ciphertext
    assert config_fingerprint(config) == config_fingerprint(
        _config(api_key="sk-not-a-real-key")
    )
    assert config_fingerprint(config) != config_fingerprint(
        _config(api_key="sk-a-different-key")
    )


# ----------------------------------------------------------------------
# Sampling overrides on the initial backend
# ----------------------------------------------------------------------


def _sampling_config(**overrides: Any) -> EncreConfig:
    """A configuration whose active model carries sampling overrides."""
    base: dict[str, Any] = {
        "backend_type": "openai",
        "api_key": "sk-not-a-real-key",
        "permission_mode": "bypass",
        "max_turns": 0,
        "log_level": "ERROR",
        "enable_prompt_caching": False,
        "checkpoint_max_count": 1,
        "workspace": "",
    }
    base.update(overrides)
    cfg = EncreConfig(**base)
    cfg.models = [
        ModelConfig(
            name="sampling-model",
            model_id="sampling-model",
            backend_type="openai",
            api_key="sk-not-a-real-key",
            temperature=1.0,
            top_p=0.9,
            top_k=40,
        )
    ]
    return cfg


def test_loop_initial_backend_receives_sampling_overrides(data_root):
    """The loop's *first* backend must see the active model's sampling params.

    Relays that force-require ``temperature`` / ``top_p`` / ``top_k`` reject
    every turn when these never reach ``backend.sampling_overrides`` -- the
    rebuild-on-settings-change path alone is not enough.
    """
    cfg = _sampling_config()
    captured: dict[str, Any] = {}

    def _capture(_type: str, **kwargs: Any) -> _ScriptedBackend:
        captured.update(kwargs)
        return _ScriptedBackend()

    with patch("encre.loop.create_backend", side_effect=_capture):
        EncreLoop(cfg, EncreSession(cfg), ToolRegistry())

    assert captured.get("temperature") == 1.0
    assert captured.get("top_p") == 0.9
    assert captured.get("top_k") == 40


def test_loop_initial_backend_ignores_stale_backend_kwargs_sampling(data_root):
    """Model-level sampling wins over a stale backend_kwargs copy.

    ``backend_kwargs`` may still carry a legacy ``temperature`` key from an
    old config; spreading it after the explicit sampling kwargs would both
    collide (TypeError) and shadow the authoritative per-model value.
    """
    cfg = _sampling_config(backend_kwargs={"temperature": 0.3})
    captured: dict[str, Any] = {}

    def _capture(_type: str, **kwargs: Any) -> _ScriptedBackend:
        captured.update(kwargs)
        return _ScriptedBackend()

    with patch("encre.loop.create_backend", side_effect=_capture):
        EncreLoop(cfg, EncreSession(cfg), ToolRegistry())

    assert captured.get("temperature") == 1.0


# ----------------------------------------------------------------------
# Applying a reload
# ----------------------------------------------------------------------


def test_reload_puts_the_new_config_in_force(data_root):
    """The configuration is swapped on the loop and on its session."""
    loop = _loop(data_root)
    original = loop.config
    new = _config(model="reload-model-v2")

    report = loop.reload(new)

    assert isinstance(report, ReloadReport)
    assert loop.config is new
    assert loop.session.config is new
    assert loop.config is not original
    assert report.config_fingerprint == config_fingerprint(new)
    assert report.config_changed is True
    assert report.surfaces == RELOADED_SURFACES
    assert report.skills_reloaded == 0
    assert report.ok, report.warnings


def test_reload_without_an_argument_re_applies_the_held_config(data_root):
    """``reload()`` with no argument is the "re-read my files" case."""
    loop = _loop(data_root)
    held = loop.config

    report = loop.reload()

    assert loop.config is held
    assert report.config_changed is False
    assert report.ok, report.warnings


def test_reload_reports_no_change_for_an_equivalent_config(data_root):
    """A different object with the same content is not a change."""
    loop = _loop(data_root)
    loop.reload(_config(model="v2"))

    report = loop.reload(_config(model="v2"))

    assert report.config_changed is False
    assert loop.config.model == "v2"


def test_reload_does_not_swap_the_backend(data_root):
    """The documented boundary: the backend client outlives a reload.

    A turn in flight streams through this object, so replacing it would tear
    the stream down -- that is ``EncreAgent.rebuild_backend()``'s job.
    """
    loop = _loop(data_root)
    backend = loop.backend

    loop.reload(_config(model="boundary"))

    assert loop.backend is backend


# ----------------------------------------------------------------------
# Derived surfaces
# ----------------------------------------------------------------------


def test_reload_re_derives_the_mode_profile_and_tool_set(data_root):
    """A stale profile / tool set is replaced by the freshly derived one."""
    from encre.modes.mode_profiles import get_mode_profile

    loop = _loop(data_root)
    loop._profile = "stale"
    loop._tool_set_name = "stale"

    loop.reload(_config())

    assert loop._profile is get_mode_profile(loop.mode)
    assert loop._tool_set_name == loop._resolve_tool_set_for_mode()
    assert loop._tool_set_name != "stale"


def test_reload_re_resolves_thinking_and_drops_the_prompt_cache(data_root):
    """Thinking config and the memoised system prompt are both refreshed."""
    loop = _loop(data_root)
    sentinel = object()
    loop._thinking_config = sentinel
    loop._sys_prompt_cache = "stale prompt"
    loop._sys_prompt_cache_key = ("stale", 1)

    loop.reload(_config())

    assert loop._thinking_config is not sentinel
    assert loop._sys_prompt_cache is None
    assert loop._sys_prompt_cache_key is None


def test_reload_clears_the_rules_cache(data_root):
    """Rule files are re-read from disk on the next prompt build."""
    loop = _loop(data_root)
    loop._rules_loader._cache["global"] = ((1,), [])

    loop.reload(_config())

    assert not loop._rules_loader._cache


def test_reload_re_reads_an_edited_skill(data_root, tmp_path):
    """An edited ``SKILL.md`` replaces the definition already registered."""
    workspace = tmp_path / "ws"
    skill_file = _write_skill(workspace, "reload-probe", "before", "body-before")

    registry = EncreSkillRegistry()
    registry.load_from_dir(
        str(workspace / ".encre" / "skills"), source=SkillSource.PROJECT,
    )
    assert registry.lookup("reload-probe").body.strip() == "body-before"

    loop = _loop(
        data_root,
        _config(workspace=str(workspace)),
        skill_registry=registry,
    )
    skill_file.write_text(
        "---\nname: reload-probe\ndescription: after\n---\n\nbody-after\n",
        encoding="utf-8",
    )

    report = loop.reload(_config(workspace=str(workspace)))

    refreshed = registry.lookup("reload-probe")
    assert refreshed is not None
    assert refreshed.body.strip() == "body-after"
    assert refreshed.description == "after"
    assert report.skills_reloaded == 1


def test_reload_re_pushes_learned_policies_on_every_call(data_root):
    """The learned permission policies are re-pushed, once per reload."""
    loop = _loop(data_root)
    reflow = _CountingReflow()
    loop._policy_reflow = reflow

    loop.reload(_config())
    loop.reload(_config())

    assert reflow.refreshes == 2


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_reload_is_idempotent(data_root, tmp_path):
    """Repeating a reload converges instead of duplicating work.

    The surfaces are derived, so the second call must leave the same state --
    in particular the skill re-scan must replace the definition rather than
    register a second copy of it.
    """
    workspace = tmp_path / "ws"
    _write_skill(workspace, "reload-probe", "stable", "body")

    registry = EncreSkillRegistry()
    loop = _loop(
        data_root,
        _config(workspace=str(workspace)),
        skill_registry=registry,
    )

    first = loop.reload(_config(workspace=str(workspace)))
    second = loop.reload(_config(workspace=str(workspace)))

    assert first.config_fingerprint == second.config_fingerprint
    assert second.config_changed is False
    assert first.skills_reloaded == 1
    assert second.skills_reloaded == 1
    assert len(registry.list_all()) == 1
    assert registry.lookup("reload-probe") is not None


# ----------------------------------------------------------------------
# Audit trail
# ----------------------------------------------------------------------


def test_reload_is_audited_on_the_append_only_backbone(data_root):
    """The reload lands in the ledger with its outcome."""
    loop = _loop(data_root)
    seen: list[Any] = []
    loop.event_bus.subscribe(KIND_CONFIG_RELOADED, seen.append)

    report = loop.reload(_config(model="audited"))

    assert len(seen) == 1
    event = seen[0]
    assert event.kind == KIND_CONFIG_RELOADED
    assert event.session_id == (loop.session.id or "")
    assert event.payload["config_fingerprint"] == report.config_fingerprint
    assert event.payload["config_changed"] is True
    assert event.payload["surfaces"] == list(RELOADED_SURFACES)
    assert event.payload["warnings"] == []


async def test_reload_is_mirrored_on_the_typed_event_stream(data_root):
    """Plugin / telemetry taps see the reload through the typed contract."""
    loop = _loop(data_root)
    subscription = loop.event_stream.subscribe()

    report = loop.reload(_config(model="typed"))

    event = await asyncio.wait_for(subscription.__anext__(), timeout=5.0)
    assert isinstance(event, ConfigReloaded)
    assert event.config_fingerprint == report.config_fingerprint
    assert event.config_changed is True
    assert event.surfaces == RELOADED_SURFACES
    assert event.skills_reloaded == 0
    assert event.warnings == ()


# ----------------------------------------------------------------------
# Fault containment
# ----------------------------------------------------------------------


def test_a_failing_surface_degrades_the_reload_to_partial(data_root):
    """One broken surface is reported, not fatal to the others."""
    loop = _loop(data_root)
    loop._rules_loader = _WedgedRulesLoader()
    loop._sys_prompt_cache = "stale"
    seen: list[Any] = []
    loop.event_bus.subscribe(KIND_CONFIG_RELOADED, seen.append)

    report = loop.reload(_config(model="partial"))

    assert report.ok is False
    assert len(report.warnings) == 1
    assert report.warnings[0].startswith("rules: RuntimeError:")
    assert "rules cache is wedged" in report.warnings[0]
    # The surfaces after the failing one still ran, and the configuration was
    # still applied -- a reload fault must never leave the loop half-dead.
    assert loop._sys_prompt_cache is None
    assert loop.config.model == "partial"
    assert len(seen) == 1
    assert seen[0].payload["warnings"] == list(report.warnings)


# ----------------------------------------------------------------------
# Admin endpoint
# ----------------------------------------------------------------------


def _manager(data_root, config: EncreConfig):
    """Build a real session manager holding one real agent session."""
    from encre.server.session_manager import SessionManager

    manager = SessionManager(sessions_dir=str(data_root / "sessions"))
    with patch("encre.loop.create_backend", return_value=_ScriptedBackend()):
        info = manager.create_session(config)
    return manager, info


def test_handle_admin_reload_applies_the_stored_config(data_root):
    """``/reload`` re-reads the store and puts it in force on idle sessions."""
    from encre.server.admin import handle_admin

    manager, info = _manager(data_root, _config(model="before-reload"))
    loop = info.agent.loop
    before = loop.config

    status, body, headers = handle_admin("/reload", manager, None)

    assert status == 200
    assert headers == [("Content-Type", "application/json")]
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["reloaded"] == 1
    assert payload["skipped"] == []
    assert payload["sessions"][0]["session_id"] == info.session_id
    assert payload["sessions"][0]["config_changed"] is True
    assert loop.config is not before


async def test_handle_admin_reload_skips_a_session_with_a_turn_in_flight(data_root):
    """A running turn is skipped and reported, not reloaded underneath."""
    from encre.server.admin import handle_admin

    manager, info = _manager(data_root, _config(model="busy"))
    loop = info.agent.loop
    before = loop.config
    info.agent_task = asyncio.get_running_loop().create_future()

    status, body, _headers = handle_admin("/reload", manager, None)

    assert status == 200
    payload = json.loads(body)
    assert payload["reloaded"] == 0
    assert payload["sessions"] == []
    assert payload["skipped"] == [
        {"session_id": info.session_id, "reason": "turn in flight"},
    ]
    assert loop.config is before


def test_handle_admin_reload_rejects_cross_origin(data_root):
    """The existing local-origin authorization still guards ``/reload``."""
    from encre.server.admin import handle_admin

    manager, info = _manager(data_root, _config())

    status, body, _headers = handle_admin(
        "/reload", manager, {"Origin": "https://evil.example"},
    )

    assert status == 403
    assert "cross-origin" in json.loads(body)["error"]
    assert info.agent.loop.config is not None


# ----------------------------------------------------------------------
# Session manager accessor
# ----------------------------------------------------------------------


def test_iter_sessions_returns_records_not_a_listing(data_root):
    """``iter_sessions`` yields the records ``list_sessions`` filters out."""
    manager, info = _manager(data_root, _config())
    info.metadata["archived"] = True

    assert manager.list_sessions() == []
    assert [record.session_id for record in manager.iter_sessions()] == [
        info.session_id
    ]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--no-header"]))
