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

"""Regression tests for multi-connector (multi-adapter) gateway operation.

Three independent defects each let a second -- and third -- connected
platform silently break the ones already connected.  Each test below pins
the fixed behaviour:

1. ``HandlerBase._persist_config`` saved whatever ``adapter_configs``
   snapshot the saving session happened to hold.  Because
   ``config_store.save_config`` replaces the ``adapters`` group wholesale
   (that is what makes unbinding stick), a session loaded from disk -- whose
   snapshot predates a connector bound on another connection -- erased that
   connector's credentials on its next save.  Symptom: connect QQ, WeChat
   and WeCom, save any setting, restart, and only the last-saved platform
   comes back up.
2. ``EventRouter.iclaw_context(timeout=...)`` wrapped the *whole* ``async
   with`` block, body included, in ``asyncio.timeout``.  Any platform turn
   longer than the timeout was cancelled mid-run, and the ``except`` branch
   then yielded a second time -- surfacing to the platform as
   ``RuntimeError: async generator didn't stop after athrow()``.
3. ``GatewayRunner.start_adapter`` exported every config field to
   ``os.environ`` under its bare name.  Adapters share generic field names
   (``token``, ``app_id``, ``secret``, ``server_url``, ``webhook_url``, ...),
   so the last-started adapter overwrote the values every earlier adapter's
   SDK would read.
"""

import asyncio
import os
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from encre import config_store
from encre.config import EncreConfig
from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platform_registry import PlatformEntry, platform_registry
from encre.gateway.run import GatewayRunner, _export_adapter_env
from encre.protocol.handlers.base import HandlerBase
from encre.protocol.session_router import EventRouter
from encre.server.session_manager import SessionManager


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the whole Encre data tree at an isolated temporary directory."""
    monkeypatch.setenv("ENCRE_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def clean_env():
    """Snapshot ``os.environ`` and restore it verbatim after the test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _session_info(config: EncreConfig) -> SimpleNamespace:
    """Minimal SessionInfo stand-in: ``_persist_config`` reads ``.agent.config``."""
    return SimpleNamespace(agent=SimpleNamespace(config=config))


# ---- 1. Persistence must not wipe other connectors ---------------------------


def test_persist_config_keeps_connectors_bound_elsewhere(data_dir):
    """A stale session snapshot must not erase other connectors on save.

    ``save_config`` replaces the ``adapters`` group wholesale, so before the
    fix a session whose snapshot predates the WeChat/WeCom binds persisted
    an ``adapters.json`` holding QQ only -- the other two platforms showed
    "connected" in the UI until the next restart, then came back dead.
    """
    canonical = EncreConfig()
    canonical.adapter_configs = {
        "qqbot": {"app_id": "qq-app", "client_secret": "qq-secret", "enabled": True},
        "weixin": {"account_id": "wx-acc", "token": "wx-token", "enabled": True},
        "wecom": {"bot_id": "ww-bot", "secret": "ww-secret", "enabled": True},
    }
    # Snapshot a session loaded from disk *before* weixin/wecom were bound.
    stale = replace(canonical)
    stale.adapter_configs = {"qqbot": dict(canonical.adapter_configs["qqbot"])}

    # ``_persist_config`` reads exactly one attribute off ``self`` -- the
    # server-canonical ``_default_config`` -- so the method is exercised
    # unbound against a minimal stand-in instead of a full handler stack.
    handler = SimpleNamespace(_default_config=canonical)
    HandlerBase._persist_config(handler, _session_info(stale))

    on_disk = config_store.adapter_flat_view()
    assert on_disk.get("adapter_qqbot_app_id") == "qq-app"
    assert on_disk.get("adapter_weixin_token") == "wx-token"
    assert on_disk.get("adapter_wecom_secret") == "ww-secret"


def test_persist_config_still_removes_an_unbound_connector(data_dir):
    """Unbinding must keep working: the merge is one-way from canonical.

    The configure handler pops the adapter from *both* the session config and
    the server-canonical ``_default_config`` before persisting, so the sync
    finds nothing to re-add and the removal sticks.
    """
    canonical = EncreConfig()
    canonical.adapter_configs = {
        "qqbot": {"app_id": "qq-app", "client_secret": "qq-secret", "enabled": True},
        "weixin": {"account_id": "wx-acc", "token": "wx-token", "enabled": True},
    }
    handler = SimpleNamespace(_default_config=canonical)
    HandlerBase._persist_config(handler, _session_info(canonical))
    assert config_store.adapter_flat_view().get("adapter_weixin_token") == "wx-token"

    # Unbind weixin: popped from the session snapshot *and* the canonical set.
    session_cfg = replace(canonical)
    session_cfg.adapter_configs = {
        "qqbot": dict(canonical.adapter_configs["qqbot"]),
    }
    canonical.adapter_configs.pop("weixin")
    HandlerBase._persist_config(handler, _session_info(session_cfg))

    on_disk = config_store.adapter_flat_view()
    assert "adapter_weixin_token" not in on_disk
    assert on_disk.get("adapter_qqbot_app_id") == "qq-app"


# ---- 2. iclaw_context timeout bounds the lock wait, not the body -------------


def test_iclaw_context_long_body_is_not_cancelled(data_dir):
    """A turn longer than the timeout must run to completion.

    The old implementation wrapped the body in ``asyncio.timeout`` too, so
    every platform conversation slower than the timeout was aborted mid-run
    and the caller got a RuntimeError instead of the answer.
    """
    router = EventRouter(SessionManager())

    async def scenario() -> str:
        async with router.iclaw_context(timeout=0.1):
            await asyncio.sleep(0.3)  # three times the timeout
        return "body-completed"

    assert asyncio.run(scenario()) == "body-completed"


def test_iclaw_context_contended_lock_times_out_without_lock(data_dir):
    """A contended entrant proceeds without the lock after the timeout."""
    router = EventRouter(SessionManager())

    async def scenario() -> None:
        holder_released = asyncio.Event()

        async def holder() -> None:
            async with router.iclaw_context():
                await asyncio.sleep(0.4)
            holder_released.set()

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.05)  # let the holder take the lock first

        started = time.monotonic()
        async with router.iclaw_context(timeout=0.1):
            waited = time.monotonic() - started
            # The body runs while the holder still owns the lock: the
            # timed-out entrant proceeded without it.
            assert router._iclaw_lock.locked()
        # Gave up waiting at ~the timeout instead of blocking behind the
        # holder for its full 0.4s.
        assert 0.08 <= waited < 0.35

        await task
        assert holder_released.is_set()
        # Once the holder is done the lock is free -- the timed-out entrant
        # never left it held.
        assert not router._iclaw_lock.locked()

    asyncio.run(scenario())


# ---- 3. Adapter env export must be namespaced per adapter --------------------


def test_export_adapter_env_namespaces_generic_fields(clean_env):
    """Generic field names are namespaced; lifecycle keys are skipped."""
    for bare in ("app_id", "client_secret", "enabled", "APP_ID"):
        os.environ.pop(bare, None)

    _export_adapter_env("qqbot", {
        "app_id": "qq-app",
        "client_secret": "qq-secret",
        "enabled": True,
    })

    assert os.environ.get("ENCRE_ADAPTER_QQBOT_APP_ID") == "qq-app"
    assert os.environ.get("ENCRE_ADAPTER_QQBOT_CLIENT_SECRET") == "qq-secret"
    assert "app_id" not in os.environ
    assert "client_secret" not in os.environ
    assert "enabled" not in os.environ


def test_export_adapter_env_passes_vendor_namespaced_keys_through(clean_env):
    """Upper-snake keys are already vendor-namespaced and pass through."""
    os.environ.pop("WEIXIN_ACCOUNT_ID", None)
    os.environ.pop("ENCRE_ADAPTER_WEIXIN_TOKEN", None)

    _export_adapter_env("weixin", {
        "WEIXIN_ACCOUNT_ID": "wx-acc",
        "account_id": "wx-acc",
        "token": "wx-token",
    })

    assert os.environ.get("WEIXIN_ACCOUNT_ID") == "wx-acc"
    assert os.environ.get("ENCRE_ADAPTER_WEIXIN_TOKEN") == "wx-token"
    assert "token" not in os.environ


def test_export_adapter_env_has_no_cross_adapter_collision(clean_env):
    """Two adapters sharing a field name must not overwrite each other."""
    for bare in ("token", "secret", "TOKEN", "SECRET"):
        os.environ.pop(bare, None)

    _export_adapter_env("qqbot", {"token": "qq-token", "app_id": "qq-app"})
    _export_adapter_env("wecom", {"token": "ww-token", "secret": "ww-secret"})

    assert os.environ.get("ENCRE_ADAPTER_QQBOT_TOKEN") == "qq-token"
    assert os.environ.get("ENCRE_ADAPTER_WECOM_TOKEN") == "ww-token"
    assert os.environ.get("ENCRE_ADAPTER_WECOM_SECRET") == "ww-secret"
    assert "token" not in os.environ
    assert "secret" not in os.environ


# ---- 4. start_adapter wires the namespaced export ----------------------------


class _StubAdapter:
    """Minimal adapter satisfying everything ``start_adapter`` touches."""

    def __init__(self, config: PlatformConfig) -> None:
        self.config = config
        self.name = "stubplat"
        self.running = False
        self._fatal_error_message = None

    def set_message_handler(self, handler) -> None:
        self._message_handler = handler

    def set_fatal_error_handler(self, handler) -> None:
        self._fatal_error_handler = handler

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self.running = True
        return True

    async def disconnect(self) -> None:
        self.running = False


def test_start_adapter_exports_namespaced_env(data_dir, clean_env):
    """The runner's own start path must not leak bare credential names."""
    for bare in ("token", "app_id", "TOKEN", "APP_ID"):
        os.environ.pop(bare, None)

    entry = PlatformEntry(
        name="stubplat",
        label="Stub Platform",
        platform=Platform.WEBHOOK,
        adapter_factory=_StubAdapter,
        check_fn=lambda: True,
    )
    platform_registry.register(entry)
    try:
        runner = GatewayRunner()
        ok = asyncio.run(runner.start_adapter("stubplat", {
            "app_id": "stub-app",
            "token": "stub-token",
            "enabled": True,
        }))
        assert ok is True
        assert os.environ.get("ENCRE_ADAPTER_STUBPLAT_APP_ID") == "stub-app"
        assert os.environ.get("ENCRE_ADAPTER_STUBPLAT_TOKEN") == "stub-token"
        assert "token" not in os.environ
        assert "app_id" not in os.environ
    finally:
        platform_registry._entries.pop("stubplat", None)
        platform_registry._deferred.pop("stubplat", None)


def test_start_adapter_replaces_live_instance(data_dir, clean_env):
    """Re-binding a connected platform must not leave two live sockets."""
    entry = PlatformEntry(
        name="stubplat",
        label="Stub Platform",
        platform=Platform.WEBHOOK,
        adapter_factory=_StubAdapter,
        check_fn=lambda: True,
    )
    platform_registry.register(entry)
    try:
        runner = GatewayRunner()
        cfg = {"app_id": "stub-app", "token": "stub-token", "enabled": True}
        assert asyncio.run(runner.start_adapter("stubplat", cfg)) is True
        first = runner.instances["stubplat"]
        assert first.running is True

        # QR login on an already-connected platform: the old instance must
        # be disconnected, not leaked alongside the new one.
        assert asyncio.run(runner.start_adapter("stubplat", cfg)) is True
        second = runner.instances["stubplat"]
        assert second is not first
        assert first.running is False
        assert second.running is True
        assert len(runner.instances) == 1
    finally:
        platform_registry._entries.pop("stubplat", None)
        platform_registry._deferred.pop("stubplat", None)
