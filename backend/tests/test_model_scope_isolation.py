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

"""Model selection is scoped: per conversation in general mode, per workspace
in iwork mode.

Covered here:

1. a persisted model identity survives a model-list reorder;
2. an ambiguous ``model_id`` resolves by endpoint;
3. a workspace's stored model wins over the pool's first member;
4. a workspace's stored model outside the pool is ignored;
5. a general-mode session reloads on its own model, not the connection default.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from encre.config import EncreConfig, ModelConfig
from encre.model_selection import model_identity, resolve_model_index
from encre.protocol.handlers.workspace_store import _apply_workspace_config


def _model(model_id: str, backend: str = "openai", base: str = "https://a.example") -> ModelConfig:
    return ModelConfig(
        name=model_id,
        model_id=model_id,
        backend_type=backend,
        api_key="sk-test",
        base_url=base,
    )


def _config(*models: ModelConfig) -> EncreConfig:
    cfg = EncreConfig(api_key="sk-test", backend_type="openai", max_tokens=256)
    cfg.models = list(models)
    cfg.active_model_index = 0
    cfg.apply_active_model()
    return cfg


def _workspace(tmp_path, payload: dict) -> str:
    folder = tmp_path / "project"
    (folder / ".encre").mkdir(parents=True)
    (folder / ".encre" / "config.json").write_text(
        json.dumps(payload), encoding="utf-8")
    return str(folder)


def test_identity_survives_reorder():
    """The same model is found again after the flat list is rebuilt."""
    first, second = _model("m1"), _model("m2")
    cfg = _config(first, second)
    identity = model_identity(cfg.models[1])

    cfg.models = [second, first]

    assert resolve_model_index(cfg, identity) == 0


def test_identity_disambiguates_same_model_id_by_endpoint():
    """Two providers exposing one model id are told apart by their endpoint."""
    a = _model("shared", base="https://a.example")
    b = _model("shared", base="https://b.example")
    cfg = _config(a, b)
    identity = model_identity(cfg.models[1])

    assert resolve_model_index(cfg, identity) == 1


def test_workspace_model_wins_over_pool_pin(tmp_path):
    """The workspace's remembered model beats "first eligible pool member"."""
    m1, m2, m3 = _model("m1"), _model("m2"), _model("m3")
    cfg = _config(m1, m2, m3)
    folder = _workspace(tmp_path, {
        "models_enabled": True,
        "models": ["m1", "m2"],
        "active_model": model_identity(m2),
    })

    _apply_workspace_config(cfg, folder)

    assert cfg.workspace_pool_state == "on"
    assert cfg.models[cfg.active_model_index].model_id == "m2"


def test_workspace_model_outside_pool_is_ignored(tmp_path):
    """A stored model that left the pool cannot pull the session out of it."""
    m1, m2, m3 = _model("m1"), _model("m2"), _model("m3")
    cfg = _config(m1, m2, m3)
    folder = _workspace(tmp_path, {
        "models_enabled": True,
        "models": ["m1", "m2"],
        "active_model": model_identity(m3),
    })

    _apply_workspace_config(cfg, folder)

    assert cfg.models[cfg.active_model_index].model_id == "m1"


def test_general_session_reloads_on_its_own_model(tmp_path, monkeypatch):
    """A reopened conversation keeps the model it ran on, not the default."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path / "data"))
    from encre.server.session_manager import SessionManager

    m1, m2, m3 = _model("m1"), _model("m2"), _model("m3")
    cfg = _config(m1, m2, m3)
    with patch("encre.loop.create_backend", return_value=SimpleNamespace(
        close=lambda: None, context_window_size=lambda: 128000,
    )):
        manager = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        info = manager.create_session(cfg)
        info.agent.config.active_model_index = 2
        info.agent.config.apply_active_model()
        info.agent.session.metadata["active_model"] = model_identity(
            info.agent.config.models[2])
        manager._save_session(info)
        sid = info.session_id

    fresh_default = _config(m1, m2, m3)
    fresh_default.active_model_index = 0
    fresh_default.apply_active_model()
    with patch("encre.loop.create_backend", return_value=SimpleNamespace(
        close=lambda: None, context_window_size=lambda: 128000,
    )):
        reloaded = SessionManager(
            sessions_dir=str(tmp_path / "sessions"),
        ).load_or_create_session(sid, config=fresh_default)

    assert reloaded.agent.config.models[
        reloaded.agent.config.active_model_index].model_id == "m3"


def _fake_info(cfg: EncreConfig, sid: str):
    """A session-shaped record exposing only what the scope helpers touch."""
    session = SimpleNamespace(metadata={})
    agent = SimpleNamespace(config=cfg, session=session, rebuild_backend=lambda: None)
    return SimpleNamespace(session_id=sid, agent=agent, agent_task=None)


def _fake_handler(workspace: str, sessions: list, default: EncreConfig):
    from encre.protocol.handlers.settings import SettingsHandlers

    manager = SimpleNamespace(
        iter_sessions=lambda: list(sessions),
        _schedule_save=lambda _info: None,
    )
    handler = SettingsHandlers.__new__(SettingsHandlers)
    handler._workspace_path = workspace
    handler._default_config = default
    handler._manager = manager
    return handler


def test_general_selection_stays_on_its_own_session():
    """Switching a general conversation never touches a sibling's config."""
    from encre.protocol.handlers.settings import SettingsHandlers

    a_cfg = _config(_model("m1"), _model("m2"), _model("m3"))
    b_cfg = _config(_model("m1"), _model("m2"), _model("m3"))
    a, b = _fake_info(a_cfg, "a"), _fake_info(b_cfg, "b")
    handler = _fake_handler("", [a, b], _config(_model("m1"), _model("m2"), _model("m3")))
    a_cfg.active_model_index = 2
    a_cfg.apply_active_model()

    SettingsHandlers._sync_model_scope(handler, a, 2)

    assert a_cfg.models[a_cfg.active_model_index].model_id == "m3"
    assert b_cfg.active_model_index == 0
    assert a.agent.session.metadata["active_model"]["model_id"] == "m3"
    assert handler._default_config.active_model_index == 2


def test_workspace_selection_moves_every_workspace_session(tmp_path):
    """Picking a model in one workspace conversation moves its siblings."""
    from encre.protocol.handlers.settings import SettingsHandlers

    folder = _workspace(tmp_path, {})
    a_cfg = _config(_model("m1"), _model("m2"), _model("m3"))
    a_cfg.workspace = folder
    b_cfg = _config(_model("m1"), _model("m2"), _model("m3"))
    b_cfg.workspace = folder
    other_cfg = _config(_model("m1"), _model("m2"), _model("m3"))
    other_cfg.workspace = str(tmp_path / "other")
    a = _fake_info(a_cfg, "a")
    b = _fake_info(b_cfg, "b")
    other = _fake_info(other_cfg, "other")
    default = _config(_model("m1"), _model("m2"), _model("m3"))
    handler = _fake_handler(folder, [a, b, other], default)
    a_cfg.active_model_index = 2
    a_cfg.apply_active_model()

    SettingsHandlers._sync_model_scope(handler, a, 2)

    assert a_cfg.models[a_cfg.active_model_index].model_id == "m3"
    assert b_cfg.models[b_cfg.active_model_index].model_id == "m3"
    assert other_cfg.active_model_index == 0
    saved = json.loads(
        (tmp_path / "project" / ".encre" / "config.json").read_text(encoding="utf-8"))
    assert saved["active_model"]["model_id"] == "m3"
    # The workspace choice must not become the global default.
    assert handler._default_config.active_model_index == 0


class _CommandHost:
    """Minimal host that can drive ``_h_set_active_model`` for real.

    It wires the real session manager, agent config and UI-notice recording,
    and stubs only the transport and disk-persistence edges.
    """

    def __init__(self, manager, info, workspace: str, default: EncreConfig) -> None:
        from encre.protocol.handlers.settings import SettingsHandlers

        self._handler = SettingsHandlers.__new__(SettingsHandlers)
        self._handler._manager = manager
        self._handler._info = info
        self._handler._workspace_path = workspace
        self._handler._default_config = default
        self._handler._current_session_id = info.session_id
        self._handler._get_or_create_session = lambda: info
        self._handler._persist_config = lambda _info: None
        self.sent: list[tuple[str, dict]] = []

        async def _send(_ws, kind: str, **payload) -> None:
            self.sent.append((kind, payload))

        async def _context(_ws, _info) -> None:
            return None

        self._handler._send = _send
        self._handler._send_context_usage_frame = _context
        # Metadata is set before the save is scheduled; stubbing the debounced
        # writer keeps the test loop free of dangling tasks.
        manager._schedule_save = lambda _info: None

    def set_model(self, index: int) -> None:
        import asyncio

        from encre.server.protocol import ClientSetActiveModel

        with patch("encre.backend.create_backend", return_value=SimpleNamespace(
            close=lambda: None, context_window_size=lambda: 128000,
        )):
            asyncio.run(self._handler._h_set_active_model(
                object(), ClientSetActiveModel(type="set_active_model", model_index=index)))


def _manager_with(*cfgs: EncreConfig, sessions_dir: str):
    from encre.server.session_manager import SessionManager

    with patch("encre.loop.create_backend", return_value=SimpleNamespace(
        close=lambda: None, context_window_size=lambda: 128000,
    )):
        manager = SessionManager(sessions_dir=sessions_dir)
        infos = [manager.create_session(cfg) for cfg in cfgs]
    return manager, infos


def test_set_active_model_is_per_conversation_in_general_mode(tmp_path):
    """Two general conversations keep independent models across back-and-forth."""
    m1, m2, m3 = _model("m1"), _model("m2"), _model("m3")
    a_cfg, b_cfg = _config(m1, m2, m3), _config(m1, m2, m3)
    manager, (a, b) = _manager_with(a_cfg, b_cfg, sessions_dir=str(tmp_path / "sessions"))
    default = _config(m1, m2, m3)

    host = _CommandHost(manager, a, "", default)
    host.set_model(2)
    host._handler._get_or_create_session = lambda: b
    host.set_model(1)

    a_cfg, b_cfg = a.agent.config, b.agent.config
    assert a_cfg.models[a_cfg.active_model_index].model_id == "m3"
    assert b_cfg.models[b_cfg.active_model_index].model_id == "m2"
    assert a.agent.session.metadata["active_model"]["model_id"] == "m3"
    assert b.agent.session.metadata["active_model"]["model_id"] == "m2"


def test_set_active_model_is_per_workspace(tmp_path):
    """A switch in one workspace conversation moves every sibling with it."""
    folder = _workspace(tmp_path, {})
    m1, m2, m3 = _model("m1"), _model("m2"), _model("m3")
    a_cfg, b_cfg = _config(m1, m2, m3), _config(m1, m2, m3)
    a_cfg.workspace = b_cfg.workspace = folder
    manager, (a, b) = _manager_with(a_cfg, b_cfg, sessions_dir=str(tmp_path / "sessions"))
    default = _config(m1, m2, m3)

    host = _CommandHost(manager, a, folder, default)
    host.set_model(2)

    a_cfg, b_cfg = a.agent.config, b.agent.config
    assert a_cfg.models[a_cfg.active_model_index].model_id == "m3"
    assert b_cfg.models[b_cfg.active_model_index].model_id == "m3"
    saved = json.loads(
        (tmp_path / "project" / ".encre" / "config.json").read_text(encoding="utf-8"))
    assert saved["active_model"]["model_id"] == "m3"
    assert default.active_model_index == 0


def test_model_switch_notice_waits_for_first_real_turn(tmp_path):
    """An empty conversation records no switch line; the first turn unlocks it."""
    m1, m2 = _model("m1"), _model("m2")
    cfg = _config(m1, m2)
    manager, (info,) = _manager_with(cfg, sessions_dir=str(tmp_path / "sessions"))
    host = _CommandHost(manager, info, "", _config(m1, m2))

    host.set_model(1)

    assert info.agent.session.get_ui_notices() == []
    assert not any(kind == "model_switch" for kind, _ in host.sent)

    info.agent.session.add_message("user", "hello")
    host.set_model(0)

    notices = info.agent.session.get_ui_notices()
    assert len(notices) == 1
    assert notices[0]["to_model"] == "m1"
    assert any(kind == "model_switch" for kind, _ in host.sent)
