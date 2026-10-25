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

"""Configuration hot reload for an already-built agent loop.

:func:`apply_config_reload` re-applies a new
:class:`~encre.config.EncreConfig` to a live
:class:`~encre.loop.runner.EncreLoop` **without rebuilding the loop**.  The
loop owns this through :meth:`~encre.loop.wiring._LoopWiringMixin.reload`,
so embedders and the local admin API have one entry point::

    report = loop.reload(EncreConfig.from_file())
    if report.config_changed:
        ...

Reload boundary
---------------

Everything here is safe to re-derive from disk while a session is live,
because nothing in-flight holds a reference to the old value:

* **config** -- the :class:`~encre.config.EncreConfig` in force, both on the
  loop and on its session (they are the same object by construction, and a
  reload keeps them that way).
* **mode profile** -- ``get_mode_profile(mode)`` plus the tool set derived
  from it.
* **thinking** -- the resolved :class:`~encre.thinking.config.ThinkingConfig`
  for the active model.
* **system prompt** -- the memoised prompt and its cache key are dropped, so
  the next turn rebuilds from the reloaded config.
* **rules** -- the :class:`~encre.rules.loader.RulesLoader` cache is cleared;
  project/global rule files are re-read on the next prompt build.
* **skills** -- project and user skill definitions are re-scanned from disk and
  replace the copies already registered.
* **learned policies** -- the persisted permission policies are re-pushed into
  the Rust permission engine.

Deliberately **out of scope** -- these need a rebuild, and a reload must never
do it behind the caller's back:

* the **backend client**: a turn in flight streams through it, so swapping it
  under the loop would tear the stream down.  ``EncreAgent.rebuild_backend()``
  is the supported path for that.
* the **tool registry**, the **phase pipeline**, the **plugin registry** and
  the **memory / soul / profile collaborators**: they are wired exactly once in
  :mod:`encre.loop.wiring` and are replaced by rebuilding the loop.
* **MCP servers**: reconciled by ``EncreMcpManager.reload()``.
* **bundled / EA-package skills**: they ship with the build, so a restart (or
  a plugin reload, see :mod:`encre.plugins.hot_reload`) is what changes them.

Idempotency
-----------

Every call re-reads from disk, so calling ``reload()`` twice in a row leaves
exactly the state a single call leaves -- the surfaces are *derived*, never
accumulated.  :attr:`ReloadReport.config_changed` reports whether the new
configuration differs from the one that was already in force, which is what
lets a caller skip downstream work on a no-op reload.

Fault containment
-----------------

A reload runs against a live loop, so a fault in one surface must not abort
the others or kill the loop: each surface is refreshed in its own guarded step
and a failure is recorded as a warning on the report *and* logged with its
traceback -- surfaced, never swallowed silently, never fatal.
"""


import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from encre.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "RELOADED_SURFACES",
    "ReloadReport",
    "apply_config_reload",
    "config_fingerprint",
]

#: The surfaces a reload refreshes, in application order.  A report lists the
#: same tuple so a consumer can render "what was refreshed" generically.
RELOADED_SURFACES: tuple[str, ...] = (
    "config",
    "mode_profile",
    "thinking",
    "system_prompt",
    "rules",
    "skills",
    "learned_policies",
)


def config_fingerprint(config: Any) -> str:
    """Return a stable hash identifying the effective content of *config*.

    The fingerprint is taken over the config's own serialisation with
    ``sort_keys=True``, so key order never perturbs it, and with API keys left
    in cleartext (``encrypt_api_keys=False``) so that encrypting the same key a
    second time -- which yields different ciphertext -- is not mistaken for a
    configuration change.  The value is only ever compared and hashed in
    memory; it is never logged with its inputs and never persisted.

    Args:
        config: A configuration object exposing ``to_dict()``.

    Returns:
        The hex digest, or ``""`` when *config* is ``None`` or cannot be
        serialised (a fingerprint failure degrades to "unknown", it never
        raises).
    """
    if config is None:
        return ""
    to_dict = getattr(config, "to_dict", None)
    if not callable(to_dict):
        return ""
    try:
        try:
            payload = to_dict(encrypt_api_keys=False)
        except TypeError:
            # A config-like object whose ``to_dict`` takes no arguments.
            payload = to_dict()
    except Exception:
        logger.warning("[reload] config is not serialisable", exc_info=True)
        return ""
    try:
        canonical = json.dumps(
            payload, sort_keys=True, default=str, ensure_ascii=False,
        )
    except Exception:
        logger.warning("[reload] config fingerprint failed", exc_info=True)
        return ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReloadReport:
    """Outcome of one configuration reload.

    Args:
        config_fingerprint: The digest of the configuration now in force (see
            :func:`config_fingerprint`); ``""`` when it could not be computed.
        config_changed: Whether that configuration differs from the one the
            loop had before the reload.
        surfaces: The surfaces the reload refreshed.
        skills_reloaded: How many skill definitions were (re-)registered.
        warnings: One message per surface whose refresh failed.  A non-empty
            tuple means the reload was **partial** -- the listed surfaces still
            hold their previous value.
    """

    config_fingerprint: str
    config_changed: bool
    surfaces: tuple[str, ...] = RELOADED_SURFACES
    skills_reloaded: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every surface was refreshed."""
        return not self.warnings

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable event payload for this report."""
        return {
            "config_fingerprint": self.config_fingerprint,
            "config_changed": self.config_changed,
            "surfaces": list(self.surfaces),
            "skills_reloaded": self.skills_reloaded,
            "warnings": list(self.warnings),
        }


def apply_config_reload(loop: Any, new_config: Any) -> ReloadReport:
    """Re-apply *new_config* to the live *loop* in place.

    Each surface is refreshed in its own guarded step; a failing surface is
    recorded as a warning instead of aborting the reload.  See the module
    docstring for what is -- and is not -- covered.

    Args:
        loop: The live :class:`~encre.loop.runner.EncreLoop` to refresh.
        new_config: The configuration to put in force.

    Returns:
        The :class:`ReloadReport` describing what was refreshed.
    """
    failures: list[str] = []
    previous = config_fingerprint(getattr(loop, "config", None))

    _step(failures, "config", lambda: _swap_config(loop, new_config))
    # Fingerprint *after* the swap: putting a configuration in force may
    # legitimately re-derive flat fields (``apply_active_model``), and the digest
    # has to describe what is actually in force rather than the half-derived
    # object that was handed in.  A failed swap leaves the old configuration in
    # place, which correctly reports as "no change" alongside the warning.
    fingerprint = config_fingerprint(getattr(loop, "config", None))

    _step(failures, "mode_profile", lambda: _refresh_mode_profile(loop))
    _step(failures, "thinking", lambda: _refresh_thinking(loop))
    _step(failures, "system_prompt", lambda: _invalidate_system_prompt(loop))
    _step(failures, "rules", lambda: _clear_rule_cache(loop))
    skills = _step(failures, "skills", lambda: _rescan_skills(loop))
    _step(failures, "learned_policies", lambda: _refresh_learned_policies(loop))

    report = ReloadReport(
        config_fingerprint=fingerprint,
        config_changed=fingerprint != previous,
        skills_reloaded=int(skills or 0),
        warnings=tuple(failures),
    )
    logger.info(
        "[reload] config_changed=%s skills=%d warnings=%d",
        report.config_changed, report.skills_reloaded, len(report.warnings),
    )
    return report


def _step(
    failures: list[str], name: str, action: Callable[[], Any],
) -> Any:
    """Run one guarded refresh step.

    Args:
        failures: Accumulator for per-surface failure messages.
        name: The surface name, used in the log line and the message.
        action: The refresh to perform.

    Returns:
        Whatever *action* returned, or ``None`` when it raised.
    """
    try:
        return action()
    except Exception as exc:
        logger.warning("[reload] %s refresh failed", name, exc_info=True)
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        return None


def _swap_config(loop: Any, new_config: Any) -> None:
    """Put *new_config* in force on the loop and its session."""
    # A caller that mutated the config object in place may not have re-derived
    # the flat model fields; the derived payload must be correct before any
    # downstream surface reads it.
    apply_active = getattr(new_config, "apply_active_model", None)
    if callable(apply_active):
        apply_active()
    loop.config = new_config
    session = getattr(loop, "session", None)
    if session is not None:
        session.config = new_config


def _refresh_mode_profile(loop: Any) -> None:
    """Re-derive the capability profile and the tool set it implies."""
    from encre.modes.mode_profiles import AgentMode, get_mode_profile

    raw = getattr(loop, "mode", None)
    mode = raw if isinstance(raw, AgentMode) else AgentMode.GENERAL
    loop.mode = mode
    loop._profile = get_mode_profile(mode)
    resolver = getattr(loop, "_resolve_tool_set_for_mode", None)
    if callable(resolver):
        loop._tool_set_name = resolver()


def _refresh_thinking(loop: Any) -> None:
    """Re-resolve the thinking config for the (possibly new) active model."""
    from encre.thinking.config import resolve_thinking_config

    config = loop.config
    active_model = config.get_active_model()
    thinking = active_model.thinking_config or config.thinking_config
    if thinking is None:
        thinking = resolve_thinking_config(
            None, config.model, backend_type=config.backend_type,
        )
    loop._thinking_config = thinking


def _invalidate_system_prompt(loop: Any) -> None:
    """Drop the memoised system prompt so the next turn rebuilds it."""
    loop._sys_prompt_cache = None
    loop._sys_prompt_cache_key = None


def _clear_rule_cache(loop: Any) -> None:
    """Clear the rules loader cache so rule files are re-read from disk."""
    loader = getattr(loop, "_rules_loader", None)
    clear = getattr(loader, "clear_cache", None)
    if callable(clear):
        clear()


def _rescan_skills(loop: Any) -> int:
    """Re-scan the project and user skill directories.

    The registry resolves same-name conflicts by *source priority* and refuses
    to overwrite an entry of equal or higher priority, so simply re-scanning a
    directory cannot refresh a skill whose ``SKILL.md`` changed.  Definitions
    that came from the directories being re-scanned are therefore dropped first
    and restored if the re-scan fails, which makes the step reversible.

    Bundled and EA-package skills are intentionally not re-scanned: they ship
    with the build (see the module docstring).

    Args:
        loop: The live loop whose ``skill_registry`` should be refreshed.

    Returns:
        The number of skill definitions registered by the re-scan.
    """
    registry = getattr(loop, "skill_registry", None)
    if registry is None:
        return 0

    from encre.kernel.services import PROJECT_SKILLS_CANDIDATE_DIRS as _PROJECT_SKILLS_CANDIDATE_DIRS
    from encre.config import get_data_dir
    from encre.skills.types import SkillSource

    workspace = getattr(loop.config, "workspace", "") or ""
    targets: list[tuple[str, Any]] = []
    if workspace:
        for rel in _PROJECT_SKILLS_CANDIDATE_DIRS:
            full = os.path.join(workspace, rel)
            if os.path.isdir(full):
                targets.append((full, SkillSource.PROJECT))
    try:
        user_dir = str(get_data_dir() / "skills")
    except Exception:  # pragma: no cover - defensive
        user_dir = ""
    if user_dir and os.path.isdir(user_dir):
        targets.append((user_dir, SkillSource.USER))

    loaded = 0
    for path, source in targets:
        loaded += _reload_skill_dir(registry, path, source)
    return loaded


def _reload_skill_dir(registry: Any, path: str, source: Any) -> int:
    """Re-scan one skill directory, dropping its previous definitions first."""
    dropped = _drop_skills_under(registry, path)
    before = len(registry.list_all())
    try:
        registry.load_from_dir(path, source=source)
    except Exception:
        for skill in dropped:
            registry.register(skill)
        raise
    return max(len(registry.list_all()) - before, 0)


def _drop_skills_under(registry: Any, path: str) -> list[Any]:
    """Unregister every skill whose definition file lives under *path*.

    Args:
        registry: The skill registry to prune.
        path: The directory being re-scanned.

    Returns:
        The definitions that were removed, newest-first ordering undefined --
        they are only ever re-registered as a group.
    """
    prefix = os.path.normcase(os.path.normpath(path)) + os.sep
    dropped: list[Any] = []
    for skill in list(registry.list_all()):
        file_path = getattr(skill, "file_path", "") or ""
        if not file_path:
            continue
        if os.path.normcase(os.path.normpath(file_path)).startswith(prefix):
            if registry.unregister(skill.name) is not None:
                dropped.append(skill)
    return dropped


def _refresh_learned_policies(loop: Any) -> None:
    """Re-push persisted learned permission policies into the Rust engine."""
    reflow = getattr(loop, "_policy_reflow", None)
    refresh = getattr(reflow, "refresh", None)
    if callable(refresh):
        refresh()
