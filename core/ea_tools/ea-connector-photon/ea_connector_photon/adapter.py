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

"""
Photon Spectrum (iMessage) platform adapter for Encre.



Both directions of traffic flow through a small supervised Node sidecar

(see ``sidecar/index.mjs``) that runs the ``spectrum-ts`` SDK -- the SDK is

TypeScript-only and there is no public HTTP message API, so a sidecar is

unavoidable.



Inbound:

    The SDK's ``app.messages`` is a long-lived **gRPC** stream. The sidecar

    serializes each message to a normalized JSON event and streams it to this

    adapter over a loopback ``GET /inbound`` (NDJSON). A background task here

    consumes that stream, dedupes on ``messageId``, and dispatches a

    ``MessageEvent`` to the gateway via ``BasePlatformAdapter.handle_message``.

    No webhook, no public URL, no signing secret.



Outbound:

    ``send`` / ``send_typing`` are loopback POSTs to the sidecar's control

    endpoints, authenticated with a shared bearer token.  Outbound media

    (images, voice notes, video, documents) goes through spectrum-ts'

    ``attachment()`` / ``voice()`` content builders via the sidecar's

    ``/send-attachment`` endpoint.

"""


import asyncio

import base64

import json

import logging

import os

import re

import secrets
import shutil

import signal

 
import subprocess

import sys

import time

from datetime import datetime, timezone

from pathlib import Path

from typing import Any, Dict, List, Optional


import httpx

HTTPX_AVAILABLE = True


from encre.gateway.config import Platform, PlatformConfig

from encre.gateway.platforms.base import (

    BasePlatformAdapter,

    MessageEvent,

    MessageType,

    ProcessingOutcome,

    SendResult,

)

from encre.gateway.platforms.helpers import strip_markdown
from encre.tools._encoding import decode_bytes


from .auth import load_project_credentials


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------

# Constants


_DEFAULT_SIDECAR_PORT = 8789

_DEFAULT_SIDECAR_BIND = "127.0.0.1"


# Photon iMessage messages from the SDK side have no documented hard

# limit, but the underlying iMessage protocol limits practical message

# size to ~16 KB.  Keep a conservative cap that matches BlueBubbles.

_MAX_MESSAGE_LENGTH = 8000


# Dedup parameters -- the gRPC stream is at-least-once, and a sidecar

# reconnect can replay, so keep at least 1k ids for ~48h.

_DEDUP_MAX_SIZE = 4000

_DEDUP_WINDOW_SECONDS = 48 * 3600


_SIDECAR_DIR = Path(__file__).parent / "sidecar"


# Cap on a self-heal `npm ci`/`npm install` of the sidecar deps. A cold

# install of the pinned spectrum-ts tree normally takes well under a minute;

# a wedged npm (dead registry, network blackhole) must not stall the photon

# connect path indefinitely.

_NPM_REINSTALL_TIMEOUT = 600


# Photon / Envoy / spectrum-ts error substrings that indicate a transient

# upstream overload rather than a permanent failure.  These are not in the

# core _RETRYABLE_ERROR_PATTERNS because they are specific to this adapter.

_PHOTON_RETRYABLE_PATTERNS = (

    "internal sidecar error",

    "upstream connect error",

    "upstream unavailable",

    "connection dropped",

    "reset reason: overflow",

    "upstream_overflow",

    "upstream_unavailable",

)


# Minimum seconds between typing-indicator calls for the same chat.

# iMessage is a personal channel -- suppressing rapid repeats reduces

# upstream gRPC pressure during Photon overflow events.

_TYPING_COOLDOWN_SECONDS = 5.0


# Group-chat mention wake words. When ``require_mention`` is enabled, group

# messages are ignored unless they match one of these patterns -- same

# behavior and defaults as the BlueBubbles iMessage channel so the two

# iMessage adapters gate group chats identically.

_DEFAULT_MENTION_PATTERNS = [

    r"(?<![\w@])@?encre\s+agent\b[,:\-]?",

    r"(?<![\w@])@?encre\b[,:\-]?",

]


# ---------------------------------------------------------------------------

# Module-level helpers -- also used by check_fn / standalone send


def _coerce_port(value: Any, default: int) -> int:
    """Coerce a loosely typed port value to an ``int``, falling back to ``default``.

    ``config.extra`` and environment ports arrive as strings (or ``None``), so
    every sidecar-port read funnels through here instead of repeating the
    ``int()`` conversion and its error handling.

    Args:
        value: Raw port value from config extra or an environment variable.
        default: Port used when ``value`` is ``None`` or not int-coercible.

    Returns:
        The coerced port number, or ``default`` when coercion fails.
    """

    try:

        return int(value)

    except (TypeError, ValueError):

        return default


def check_requirements() -> bool:

    """Return True when both Python deps and the Node sidecar are available."""

    if not HTTPX_AVAILABLE:

        return False

    if not shutil.which(os.getenv("PHOTON_NODE_BIN") or "node"):

        return False

    if not (_SIDECAR_DIR / "node_modules").exists():

        # spectrum-ts not installed yet -- `encre photon setup` will

        # install it.  check_fn still returns False so the gateway

        # surfaces the missing-deps state in `encre setup` / status.

        return False

    return True


def _sidecar_deps_stale() -> bool:

    """True when node_modules exists but is older than the committed lockfile.



    `encre update` rewrites ``package-lock.json`` when the spectrum-ts pin is

    bumped, but does not reinstall ``node_modules``. npm records the state of

    the last install in ``node_modules/.package-lock.json``; when the top-level

    lockfile is newer than that marker, the install is out of date. This is the

    same signal ``npm ci`` uses. Returns False (do nothing) if either file is

    missing or unreadable, so a first-run or odd filesystem never blocks start.

    """

    lockfile = _SIDECAR_DIR / "package-lock.json"

    marker = _SIDECAR_DIR / "node_modules" / ".package-lock.json"

    try:

        return lockfile.stat().st_mtime > marker.stat().st_mtime

    except OSError:

        return False


def _reinstall_sidecar_deps() -> None:

    """Reinstall the sidecar's node_modules from the lockfile (blocking).



    Mirrors ``encre photon install-sidecar``: ``npm ci`` for an exact,

    reproducible install, falling back to ``npm install`` if the lockfile is

    missing or drifted. Runs the postinstall patch as part of the install.

    Best-effort -- a failure here just leaves the (stale) deps in place and the

    normal ``_start_sidecar`` readiness check reports the real error.

    """

    npm = shutil.which("npm")

    if not npm:

        logger.warning("[photon] cannot reinstall stale sidecar deps: npm not on PATH")

        return

    # Windows: suppress the console flash these short-lived npm runs would

    # otherwise pop (0 elsewhere). Same helper as the sidecar spawn below.

    from encre._subprocess_compat import windows_hide_flags

    try:

        result = subprocess.run(  # noqa: S603

            [npm, "ci"],

            cwd=str(_SIDECAR_DIR),

            capture_output=True,

            text=True,

            check=False,

            timeout=_NPM_REINSTALL_TIMEOUT,

            creationflags=windows_hide_flags(),

        )

        if result.returncode != 0:

            logger.warning(

                "[photon] sidecar `npm ci` failed; falling back to `npm install`"

            )

            result = subprocess.run(  # noqa: S603

                [npm, "install"],

                cwd=str(_SIDECAR_DIR),

                capture_output=True,

                text=True,

                check=False,

                timeout=_NPM_REINSTALL_TIMEOUT,

                creationflags=windows_hide_flags(),

            )

    except subprocess.TimeoutExpired:

        # A wedged npm (dead registry, network blackhole) must not stall the

        # photon connect forever -- give up, leave the stale deps in place, and

        # let the readiness check report the real error. Retried on the next

        # reconnect tick.

        logger.error(

            "[photon] sidecar dependency reinstall timed out after %ss",

            _NPM_REINSTALL_TIMEOUT,

        )

        return

    if result.returncode != 0:

        logger.error(

            "[photon] sidecar dependency reinstall failed: %s",

            (result.stderr or result.stdout or "").strip(),

        )

    else:

        logger.info("[photon] sidecar dependencies reinstalled from lockfile")


def validate_config(cfg: PlatformConfig) -> bool:
    """Report whether Photon project credentials are resolvable for ``cfg``.

    Lookup order is ``config.extra`` -> ``PHOTON_PROJECT_ID`` /
    ``PHOTON_PROJECT_SECRET`` -> the on-disk credential store written by
    ``encre photon setup`` via
    :func:`ea_connector_photon.auth.load_project_credentials`.

    Args:
        cfg: Platform config under test.

    Returns:
        True when both a project id and a project secret can be resolved from
        any source, False otherwise.
    """

    extra = cfg.extra or {}

    project_id = extra.get("project_id") or os.getenv("PHOTON_PROJECT_ID")

    project_secret = extra.get("project_secret") or os.getenv("PHOTON_PROJECT_SECRET")

    if not project_id or not project_secret:

        # Fall back to auth.json

        stored_id, stored_sec = load_project_credentials()

        return bool(stored_id and stored_sec)

    return True


def is_connected(cfg: PlatformConfig) -> bool:
    """Module-level connectivity probe used by ``encre gateway status``.

    This hook is answered without constructing an adapter, so it cannot
    observe whether the sidecar is actually up. Photon keeps no persistent
    session token of its own -- spectrum-ts re-authenticates from the project
    secret on every connect -- so credential resolvability is the strongest
    offline signal available here.

    Args:
        cfg: Platform config under test.

    Returns:
        True when :func:`validate_config` finds complete credentials.
    """

    return validate_config(cfg)


def _env_enablement() -> Optional[dict]:

    """Seed PlatformConfig.extra from env so env-only setups appear in status.



    The special ``home_channel`` key is handled by the core plugin hook and

    becomes a proper ``HomeChannel`` on ``PlatformConfig``.

    """

    project_id, project_secret = load_project_credentials()

    if not (project_id and project_secret):

        return None

    seed: dict = {"project_id": project_id, "project_secret": project_secret}

    home = os.getenv("PHOTON_HOME_CHANNEL", "").strip()

    if home:

        seed["home_channel"] = {

            "chat_id": home,

            "name": os.getenv("PHOTON_HOME_CHANNEL_NAME", "Home"),

        }

    return seed


def _markdown_enabled() -> bool:

    """Send agent replies as markdown (spectrum-ts ``markdown()`` builder).



    iMessage renders it natively; other Spectrum platforms degrade to

    readable plain text. On-device rendering can't be unit-tested, so

    ``PHOTON_MARKDOWN=false`` is the kill-switch back to stripped plain

    text without a release.

    """

    return os.getenv("PHOTON_MARKDOWN", "true").strip().lower() not in {

        "false", "0", "no",

    }


# ---------------------------------------------------------------------------

# Adapter


class PhotonAdapter(BasePlatformAdapter):

    """Bidirectional bridge to Photon Spectrum via the Node spectrum-ts sidecar.



    Inbound: consume the sidecar's ``/inbound`` gRPC stream.

    Outbound: loopback POSTs to the sidecar's control channel.

    """

    MAX_MESSAGE_LENGTH = _MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):

        """Configure credentials, sidecar settings, and runtime state.

        Credential precedence is environment -> ``config.extra`` -> the
        on-disk store written by ``encre photon setup``, so an env-only
        install and a config-file install behave identically. The sidecar is
        only described here (port, bind address, bearer token, autostart flag,
        Node binary) and is spawned later by :meth:`connect`.

        The bearer token is generated randomly when unset, which is what makes
        a sidecar started outside the gateway unaddressable by default.

        Args:
            config: The platform config slice for this adapter.

        Note:
            Group-mention gating reads ``config.extra`` before
            ``PHOTON_REQUIRE_MENTION``, matching the BlueBubbles iMessage
            channel so the two gate group chats identically.
        """

        super().__init__(config, Platform("photon"))

        extra = config.extra or {}

        # Project credentials (env wins, then config.extra, then auth.json).

        # ``project_id`` here is the project's spectrumProjectId -- the value

        # the spectrum-ts SDK authenticates with.

        stored_id, stored_sec = load_project_credentials()

        self._project_id: str = (

            os.getenv("PHOTON_PROJECT_ID")

            or extra.get("project_id")

            or stored_id

            or ""

        )

        self._project_secret: str = (

            os.getenv("PHOTON_PROJECT_SECRET")

            or extra.get("project_secret")

            or stored_sec

            or ""

        )

        # Sidecar

        self._sidecar_port = _coerce_port(

            extra.get("sidecar_port") or os.getenv("PHOTON_SIDECAR_PORT"),

            _DEFAULT_SIDECAR_PORT,

        )

        self._sidecar_bind = _DEFAULT_SIDECAR_BIND

        self._sidecar_token = (

            os.getenv("PHOTON_SIDECAR_TOKEN") or secrets.token_hex(16)

        )

        self._autostart_sidecar = str(

            os.getenv("PHOTON_SIDECAR_AUTOSTART", "true")

        ).lower() not in ("0", "false", "no")

        self._node_bin = os.getenv("PHOTON_NODE_BIN") or shutil.which("node") or "node"

        # With markdown on, format_message preserves fences and the sidecar's

        # markdown() builder renders them (or degrades them readably).

        self.supports_code_blocks = _markdown_enabled()

        # Runtime state

        self._sidecar_proc: Optional[subprocess.Popen] = None

        self._sidecar_supervisor_task: Optional[asyncio.Task] = None

        self._inbound_task: Optional[asyncio.Task] = None

        self._sidecar_health_task: Optional[asyncio.Task] = None

        self._inbound_running = False

        self._http_client: Optional["httpx.AsyncClient"] = None

        self._sidecar_health_interval = 15.0

        # Lightweight in-memory dedup. The gRPC stream is at-least-once, so we

        # may see the same messageId more than once (e.g. after a reconnect).

        self._seen_messages: Dict[str, float] = {}

        # Ids of messages WE sent (bounded, insertion-order eviction). Inbound

        # reaction events are only routed to the agent when they target one of

        # these -- a tapback on a human->human message is not addressed to us.

        self._sent_message_ids: Dict[str, float] = {}

        # Latest inbound message id per chat (bounded). Lets the agent-facing

        # react action default to "the message that triggered me" without

        # requiring the model to thread message ids through tool calls.

        self._last_inbound_by_chat: Dict[str, str] = {}

        # Last time we sent a typing indicator per chat, for cooldown gating.

        self._typing_last_sent: Dict[str, float] = {}

        # Group-chat mention gating (parity with BlueBubbles). When enabled,

        # group messages are ignored unless they match a wake word; DMs are

        # always processed. Config key wins, then env var.

        _require_mention = extra.get("require_mention")

        if _require_mention is None:

            _require_mention = os.getenv("PHOTON_REQUIRE_MENTION")

        self.require_mention = str(_require_mention).strip().lower() in {

            "true", "1", "yes", "on",

        }

        self._mention_patterns = self._compile_mention_patterns(

            extra["mention_patterns"]

            if "mention_patterns" in extra

            else os.getenv("PHOTON_MENTION_PATTERNS")

        )

    # -- Group-mention gating (parity with BlueBubbles) -------------------

    @staticmethod

    def _compile_mention_patterns(raw: Any) -> "list[re.Pattern]":

        """Compile group-mention wake words from config/env.



        ``raw`` is a list (config or env JSON), a string (env var: JSON

        list, or comma/newline-separated), or None (use Encre defaults).

        Mirrors the BlueBubbles implementation so both iMessage channels

        accept the same configuration shapes.

        """

        if raw is None:

            patterns = list(_DEFAULT_MENTION_PATTERNS)

        elif isinstance(raw, str):

            text = raw.strip()

            try:

                loaded = json.loads(text) if text else []

            except Exception:

                loaded = None

            patterns = loaded if isinstance(loaded, list) else [

                part.strip()

                for line in text.splitlines()

                for part in line.split(",")

            ]

        elif isinstance(raw, list):

            patterns = raw

        else:

            patterns = [raw]

        compiled: "list[re.Pattern]" = []

        for pattern in patterns:

            text = str(pattern).strip()

            if not text:

                continue

            try:

                compiled.append(re.compile(text, re.IGNORECASE))

            except re.error as exc:

                logger.warning("[photon] Invalid mention pattern %r: %s", text, exc)

        return compiled

    def _message_matches_mention_patterns(self, text: str) -> bool:

        """Report whether ``text`` contains any configured group wake word.

        Args:
            text: The inbound message text to test.

        Returns:
            True when at least one compiled pattern matches. Empty text and an
            empty pattern list both return False, so a misconfigured
            ``require_mention`` drops every group message rather than leaking
            them through.
        """

        if not text or not self._mention_patterns:

            return False

        return any(pattern.search(text) for pattern in self._mention_patterns)

    def _clean_mention_text(self, text: str) -> str:

        """Strip a leading wake word before dispatch.



        Custom mention patterns are regexes, so we only strip a leading

        match to avoid deleting ordinary words later in the prompt.

        """

        if not text:

            return text

        for pattern in self._mention_patterns:

            match = pattern.match(text.lstrip())

            if match:

                cleaned = text.lstrip()[match.end():].lstrip(" ,:-")

                return cleaned or text

        return text

    # -- Connection lifecycle ---------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:

        """Start the sidecar and the inbound stream, or record a fatal error.

        Follows the :meth:`BasePlatformAdapter.connect` contract: report
        problems through :meth:`_set_fatal_error` and return False instead of
        raising, so the gateway's reconnect policy owns the backoff. Sidecar
        startup is a prerequisite for both directions of traffic, not outbound
        only, because the sidecar owns the upstream gRPC stream.

        Args:
            is_reconnect: True when the gateway retries after a prior drop.
                Sidecar autostart and credential checks run on both paths.

        Returns:
            True only when the adapter is connected and the inbound stream
            plus the sidecar health monitor are both running.
        """

        if not HTTPX_AVAILABLE:

            self._set_fatal_error(

                "MISSING_DEP", "httpx not installed", retryable=False

            )

            return False

        if not self._project_id or not self._project_secret:

            self._set_fatal_error(

                "MISSING_CREDENTIALS",

                "PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET are required. "

                "Run: encre photon setup",

                retryable=False,

            )

            return False

        client = httpx.AsyncClient(timeout=30.0)

        self._http_client = client

        # The sidecar holds the gRPC stream for BOTH directions, so it is

        # required now (not just for outbound).

        if self._autostart_sidecar:

            try:

                await self._start_sidecar()

            except Exception as e:

                self._set_fatal_error(

                    "SIDECAR_FAILED",

                    f"failed to start Photon sidecar: {e}",

                    retryable=True,

                )

                await client.aclose()

                self._http_client = None

                return False

        else:

            logger.warning(

                "[photon] sidecar autostart disabled 鈥?inbound + outbound will fail"

            )

        # Start consuming the inbound gRPC stream from the sidecar.

        self._inbound_running = True

        self._inbound_task = asyncio.get_event_loop().create_task(

            self._inbound_loop()

        )

        self._sidecar_health_task = asyncio.get_event_loop().create_task(

            self._monitor_sidecar_health()

        )

        self._mark_connected()

        logger.info(

            "[photon] connected 鈥?sidecar on %s:%d, streaming inbound over gRPC",

            self._sidecar_bind, self._sidecar_port,

        )

        return True

    async def disconnect(self) -> None:

        """Stop the inbound stream, the sidecar, and the HTTP client in order.

        Reverses :meth:`connect`: cancel the health monitor and inbound tasks
        first so no event is dispatched while the sidecar is going down, then
        shut the sidecar down, then close the shared client. Idempotent, and
        safe to call before :meth:`connect` ever succeeded -- every step
        guards on the task or handle being non-None.

        Note:
            Cancelled tasks are awaited with :exc:`asyncio.CancelledError`
            swallowed, so a half-closed task cannot raise out of shutdown.
        """

        self._inbound_running = False

        if self._sidecar_health_task is not None:

            task = self._sidecar_health_task

            self._sidecar_health_task = None

            task.cancel()

            if task is not asyncio.current_task():

                try:

                    await task

                except asyncio.CancelledError:

                    pass

                except Exception:

                    pass

        if self._inbound_task is not None:

            self._inbound_task.cancel()

            try:

                await self._inbound_task

            except asyncio.CancelledError:

                pass

            except Exception:

                pass

            self._inbound_task = None

        await self._stop_sidecar()

        if self._http_client is not None:

            try:

                await self._http_client.aclose()

            except Exception:

                pass

            self._http_client = None

        self._mark_disconnected()

    # -- Inbound stream consumer ------------------------------------------

    async def _inbound_loop(self) -> None:

        """Consume the sidecar's ``/inbound`` NDJSON stream, with reconnect.



        The sidecar owns the gRPC reconnect/heartbeat to Photon; this loop

        only has to re-open the loopback HTTP stream if it drops (e.g. the

        sidecar restarts).

        """

        client = self._http_client

        if client is None:

            return

        url = f"http://{self._sidecar_bind}:{self._sidecar_port}/inbound"

        headers = {"X-Encre-Sidecar-Token": self._sidecar_token}

        backoff = 1.0

        while self._inbound_running:

            try:

                async with client.stream(

                    "GET", url, headers=headers, timeout=None,

                ) as resp:

                    if resp.status_code != 200:

                        raise RuntimeError(f"/inbound returned {resp.status_code}")

                    backoff = 1.0  # reset on a successful connect

                    async for line in resp.aiter_lines():

                        if not self._inbound_running:

                            break

                        line = line.strip()

                        if not line:

                            continue  # heartbeat

                        await self._on_inbound_line(line)

            except asyncio.CancelledError:

                raise

            except Exception as e:

                if not self._inbound_running:

                    break

                logger.warning(

                    "[photon] inbound stream dropped (%s); reconnecting in %.1fs",

                    e, backoff,

                )

                await asyncio.sleep(backoff)

                backoff = min(backoff * 2, 30.0)

    async def _monitor_sidecar_health(self) -> None:

        """Promote degraded upstream Photon stream health into reconnect.



        The sidecar HTTP process can stay alive while spectrum-ts repeatedly

        fails to maintain the upstream inbound gRPC stream. Polling `/healthz`

        keeps that from becoming a silent inbound outage.

        """

        while self._inbound_running:

            await asyncio.sleep(self._sidecar_health_interval)

            if not self._inbound_running:

                break

            try:

                data = await self._sidecar_call("/healthz", {})

            except asyncio.CancelledError:

                raise

            except Exception as exc:

                logger.debug("[photon] sidecar health check failed: %s", exc)

                continue

            stream = data.get("stream") if isinstance(data, dict) else None

            if not isinstance(stream, dict) or stream.get("ok") is not False:

                continue

            state = str(stream.get("state") or "unknown")

            degraded_for_ms = stream.get("degradedForMs")

            last_issue = str(stream.get("lastIssue") or "unknown stream issue")

            message = (

                "Photon upstream stream degraded"

                f" (state={state}, degradedForMs={degraded_for_ms}): "

                f"{last_issue}"

            )

            logger.error("[photon] %s", message)

            self._set_fatal_error(

                "UPSTREAM_STREAM_DEGRADED",

                message,

                retryable=True,

            )

            try:

                await self._notify_fatal_error()

            except Exception as exc:  # pragma: no cover - defensive

                logger.warning("[photon] fatal-error notification failed: %s", exc)

            break

    async def _on_inbound_line(self, line: str) -> None:

        """Parse and dedupe one line of the sidecar's inbound NDJSON stream.

        Blank lines are heartbeats and non-JSON lines are dropped at debug
        level, so a noisy or partially corrupted stream cannot wedge the
        consumer. Duplicate ``messageId`` values are dropped before dispatch,
        because the sidecar's gRPC source is at-least-once.

        Args:
            line: One raw line from ``GET /inbound``, already stripped.
        """

        try:

            event = json.loads(line)

        except json.JSONDecodeError:

            logger.debug("[photon] skipping non-JSON inbound line")

            return

        msg_id = event.get("messageId")

        if msg_id and self._is_duplicate(msg_id):

            return

        try:

            await self._dispatch_inbound(event)

        except Exception:

            logger.exception("[photon] inbound dispatch failed")

    def _is_duplicate(self, msg_id: str) -> bool:

        """Report whether ``msg_id`` was already seen, recording it otherwise.

        Enforces the two bounds that make dedupe safe: ids expire after
        ``_DEDUP_WINDOW_SECONDS``, and the dict is hard-capped at
        ``_DEDUP_MAX_SIZE`` by evicting the oldest entries. The size cap is
        independent of expiry, so a burst of unique ids cannot grow memory
        without limit inside the window.

        Args:
            msg_id: Inbound ``messageId`` from a sidecar event.

        Returns:
            True when the id was seen within the window, False (and recorded)
            otherwise.
        """

        now = time.time()

        seen = self._seen_messages

        t = seen.get(msg_id)

        if t is not None and now - t < _DEDUP_WINDOW_SECONDS:

            return True  # seen, unexpired

        # New or expired: record and enforce a HARD size bound (evict oldest,

        # insertion-order) so a burst of unique ids within the window can't grow

        # the dict without limit -- not just the expired-only prune.

        if msg_id in seen:

            del seen[msg_id]  # refresh insertion order

        seen[msg_id] = now

        if len(seen) > _DEDUP_MAX_SIZE:

            for old in list(seen.keys())[: len(seen) - _DEDUP_MAX_SIZE]:

                del seen[old]

        return False

    async def _dispatch_inbound(self, event: Dict[str, Any]) -> None:

        """Normalize a sidecar inbound event and dispatch it to the encre.gateway.



        Event shape (from ``sidecar/index.mjs``)::



            {

              "messageId": "...",

              "platform": "iMessage",

              "space": {"id": "...", "type": "dm"|"group", "phone": "+E164"},

              "sender": {"id": "+E164"},

              "content": {"type": "text", "text": "..."}

                       | {"type": "attachment"|"voice", "id", "name",

                          "mimeType", "size", "duration"?, "data"?,

                          "encoding"?}

                       | {"type": "reaction", "emoji": "<tapback>",

                          "targetMessageId": "..." | null,

                          "targetDirection": "inbound"|"outbound" | null,

                          "targetText": "..." | null},

              "timestamp": "2026-05-14T19:06:32.000Z"



        Attachment and voice content carry the bytes inline as base64 ``data``

        (with ``encoding == "base64"``) when the sidecar could read them

        within its size cap; otherwise only metadata is present and we surface

        a marker.

            }

        """

        space = event.get("space") or {}

        sender = event.get("sender") or {}

        content = event.get("content") or {}

        space_id = space.get("id") or ""

        if not space_id:

            logger.warning("[photon] inbound missing space.id")

            return

        # iMessage spaces carry their type directly -- no id string-sniffing.

        chat_type = "group" if space.get("type") == "group" else "dm"

        sender_id = sender.get("id") or space.get("phone") or space_id

        ts_str = event.get("timestamp") or ""

        try:

            timestamp = (

                datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

                if ts_str

                else datetime.now(tz=timezone.utc)

            )

        except ValueError:

            timestamp = datetime.now(tz=timezone.utc)

        # Media attachments (local cached paths) handed to the agent via the

        # gateway's image-routing path, exactly like the BlueBubbles channel.

        media_urls: List[str] = []

        media_types: List[str] = []

        def _normalize_binary_payload(

            payload: Dict[str, Any]

        ) -> tuple[str, MessageType, List[str], List[str]]:

            """Coerce one inbound attachment or voice payload into a dispatch tuple.

            Resolves the display text, the :class:`MessageType`, and the media URL
            and MIME lists that :meth:`_dispatch_inbound` folds into the
            :class:`encre.gateway.platforms.base.MessageEvent` it dispatches.
            Bytes the sidecar inlined as base64 are decoded and cached through
            :func:`_cache_inbound_attachment`, so the gateway's media-routing
            path can hand them to the agent; payloads whose bytes the sidecar
            could not read degrade to a self-describing text marker.

            Args:
                payload: The sidecar ``content`` dict for one ``attachment`` or
                    ``voice`` item: ``type``, ``name``, ``mimeType``,
                    ``duration``, and optionally ``data``.

            Returns:
                ``(text, message_type, media_urls, media_types)``. ``media_urls``
                holds one cached absolute path when the bytes were recoverable and
                is empty otherwise, in which case ``text`` carries the marker so
                the message still reaches the agent.

            Note:
                ``type == "voice"`` short-circuits
                :func:`_attachment_message_type` and pins the result to
                ``MessageType.VOICE``, defaulting to ``audio/mp4`` when the
                sidecar omitted a MIME.
            """

            is_voice = payload.get("type") == "voice"

            name = payload.get("name") or ("voice" if is_voice else "(unnamed)")

            mime = payload.get("mimeType") or ""

            mtype = MessageType.VOICE if is_voice else _attachment_message_type(mime)

            cached = _cache_inbound_attachment(

                payload, name, mime, force_audio=is_voice

            )

            if cached:

                return (

                    "(voice)" if is_voice else "(attachment)",

                    mtype,

                    [cached],

                    [mime or ("audio/mp4" if is_voice else "application/octet-stream")],

                )

            label = "voice" if is_voice else "attachment"

            duration = payload.get("duration")

            duration_text = (

                f", duration: {duration}s"

                if isinstance(duration, (int, float))

                else ""

            )

            return (

                f"[Photon {label} received: {name} "

                f"({mime or 'unknown MIME'}{duration_text})]",

                mtype,

                [],

                [],

            )

        ctype = content.get("type")

        if ctype == "reaction":

            # Route only tapbacks on messages WE sent -- those are implicitly

            # addressed to the bot (feishu precedent: synthetic text event).

            # Reactions on human->human messages are not for us. Checked before

            # the mention gate: a tapback never carries a wake word.

            target_id = content.get("targetMessageId")

            is_ours = content.get("targetDirection") == "outbound" or (

                target_id and target_id in self._sent_message_ids

            )

            if not is_ours:

                logger.debug(

                    "[photon] ignoring reaction on a message we didn't send"

                )

                return

            emoji = content.get("emoji") or ""

            source = self.build_source(

                chat_id=space_id,

                chat_name=space_id,

                chat_type=chat_type,

                user_id=sender_id,

                user_name=sender_id or None,

            )

            # Correlate the tapback to the message it reacted to, so the agent

            # sees WHAT was reacted to. `is_ours` above guarantees the target is

            # one of the bot's own messages, so reply_to_is_own_message holds and

            # the gateway injects `[Replying to your previous message: "..."]`.

            # reply_to_text comes from the sidecar (hydrated reaction target);

            # it's None for attachment/voice-only targets, and the gateway only

            # injects the pointer when both id and text are present.

            await self.handle_message(

                MessageEvent(

                    text=f"reaction:added:{emoji}",

                    message_type=MessageType.TEXT,

                    source=source,

                    message_id=event.get("messageId"),

                    reply_to_message_id=target_id,

                    reply_to_text=content.get("targetText") or None,

                    reply_to_is_own_message=True,

                    raw_message=event,

                    timestamp=timestamp,

                )

            )

            return

        # Anything past here is a real (reactable) message -- remember it as

        # the chat's latest inbound so `add_reaction` can target it when the

        # caller doesn't pass an explicit message id. Recorded before the

        # mention gate: a reaction to a non-wake-word group message is valid.

        self._record_last_inbound(space_id, event.get("messageId"))

        if ctype == "text":

            text = content.get("text") or ""

            mtype = MessageType.TEXT

        elif ctype in {"attachment", "voice"}:

            text, mtype, media_urls, media_types = _normalize_binary_payload(content)

        elif ctype == "group":

            text_parts: List[str] = []

            mtype = MessageType.TEXT

            for item in content.get("items") or []:

                if not isinstance(item, dict):

                    continue

                item_content = item.get("content") or {}

                if not isinstance(item_content, dict):

                    continue

                item_type = item_content.get("type")

                if item_type == "text":

                    item_text = item_content.get("text") or ""

                    if item_text:

                        text_parts.append(item_text)

                    continue

                if item_type in {"attachment", "voice"}:

                    marker, item_mtype, item_urls, item_types = _normalize_binary_payload(

                        item_content

                    )

                    if mtype == MessageType.TEXT:

                        mtype = item_mtype

                    media_urls.extend(item_urls)

                    media_types.extend(item_types)

                    if not item_urls:

                        text_parts.append(marker)

                    continue

                if item_type:

                    text_parts.append(f"[Photon content type not handled: {item_type}]")

            if media_urls and mtype == MessageType.TEXT:

                mtype = MessageType.DOCUMENT

            text = "\n".join(part for part in text_parts if part).strip()

            if not text:

                text = "(attachment)" if media_urls else "[Photon empty group received]"

        else:

            text = f"[Photon content type not handled: {ctype}]"

            mtype = MessageType.TEXT

        # Group-mention gating (parity with BlueBubbles). In group chats with

        # require_mention enabled, drop messages that don't hit a wake word;

        # strip the leading wake word from the ones that do. DMs are never

        # gated.

        if chat_type == "group" and self.require_mention:

            if not self._message_matches_mention_patterns(text):

                logger.debug(

                    "[photon] ignoring group message "

                    "(require_mention=true, no mention pattern matched)"

                )

                return

            text = self._clean_mention_text(text)

        source = self.build_source(

            chat_id=space_id,

            chat_name=space_id,

            chat_type=chat_type,

            user_id=sender_id,

            user_name=sender_id or None,

        )

        message_event = MessageEvent(

            text=text,

            message_type=mtype,

            source=source,

            message_id=event.get("messageId"),

            raw_message=event,

            timestamp=timestamp,

            media_urls=media_urls,

            media_types=media_types,

        )

        await self.handle_message(message_event)

    # -- Sidecar lifecycle -------------------------------------------------

    @staticmethod

    def _find_listener_pids(port: int) -> List[int]:

        """PIDs listening on a local TCP port (empty if none/undeterminable)."""

        try:

            out = subprocess.run(  # noqa: S603, S607

                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],

                capture_output=True, text=True, timeout=5.0, check=False,

            )

        except (OSError, subprocess.TimeoutExpired):

            return []

        return [int(tok) for tok in out.stdout.split() if tok.strip().isdigit()]

    @staticmethod

    def _pid_is_sidecar(pid: int) -> bool:

        """True if ``pid``'s command line is a Photon sidecar process."""

        try:

            out = subprocess.run(  # noqa: S603, S607

                ["ps", "-p", str(pid), "-o", "command="],

                capture_output=True, text=True, timeout=5.0, check=False,

            )

        except (OSError, subprocess.TimeoutExpired):

            return False

        # Checkout-agnostic: any Encre checkout's sidecar entry point.

        return "photon/sidecar/index.mjs" in out.stdout

    @staticmethod

    def _pid_alive(pid: int) -> bool:

        """Probe whether a process still exists, without signalling it.

        Args:
            pid: The pid to probe.

        Returns:
            True when the process exists, False when the probe raises
            ``OSError`` (gone, or not ours to signal).

        Note:
            Signal 0 is a liveness probe only. Callers must treat False as
            "not alive", not as "permission denied".
        """

        try:

            os.kill(pid, 0)  # windows-footgun: ok -- only called from _reap_stale_sidecar which win32-guards early

            return True

        except OSError:

            return False

    async def _reap_stale_sidecar(self) -> None:

        """Kill an orphaned sidecar squatting our port before spawning ours.



        A hard gateway exit (crash, SIGKILL, supervisor restart) used to leave

        the detached sidecar running with a token the new gateway doesn't

        know, so it can't be told to ``/shutdown`` -- and every replacement

        spawn died on EADDRINUSE, failing each reconnect attempt. The

        stdin-EOF watch prevents new orphans; this reclaims the port from

        orphans that predate it (or survived it). Listeners are verified by

        command line before being signalled.

        """

        if sys.platform == "win32":  # lsof/ps; orphaning is a POSIX-only path

            return

        try:

            async with httpx.AsyncClient(timeout=2.0) as client:

                await client.post(

                    f"http://{self._sidecar_bind}:{self._sidecar_port}/healthz",

                    headers={"X-Encre-Sidecar-Token": self._sidecar_token},

                )

        except httpx.RequestError:

            return  # nothing listening -- the normal case

        pids = self._find_listener_pids(self._sidecar_port)

        stale = [pid for pid in pids if self._pid_is_sidecar(pid)]

        foreign = [pid for pid in pids if pid not in stale]

        if not stale:

            raise RuntimeError(

                f"port {self._sidecar_port} is in use by another process "

                f"(pids: {foreign or 'unknown'}, not a Photon sidecar) 鈥?"

                f"free it or set PHOTON_SIDECAR_PORT to a different port"

            )

        for pid in stale:

            logger.warning(

                "[photon] reaping orphaned sidecar (pid %d) on port %d",

                pid, self._sidecar_port,

            )

            try:

                os.kill(pid, signal.SIGTERM)

            except OSError:

                pass

        deadline = time.time() + 3.0

        while time.time() < deadline and any(self._pid_alive(p) for p in stale):

            await asyncio.sleep(0.1)

        for pid in stale:

            if self._pid_alive(pid):

                try:

                    os.kill(pid, signal.SIGKILL)  # windows-footgun: ok -- unreachable on win32 (early return above)

                except OSError:

                    pass

        # Give the OS a beat to release the listening socket.

        await asyncio.sleep(0.2)

        if foreign:

            raise RuntimeError(

                f"port {self._sidecar_port} is also held by non-sidecar "

                f"processes (pids: {foreign}) 鈥?free it or set "

                f"PHOTON_SIDECAR_PORT to a different port"

            )

    async def _start_sidecar(self) -> None:

        """Spawn and wait for the sidecar to answer ``/healthz``.

        Order matters: verify deps are installed, self-heal a stale
        ``node_modules`` off the event loop, reclaim the port from an orphan
        before binding, apply the spectrum-ts patch, spawn the child, then
        poll readiness with a bounded deadline.

        Raises:
            RuntimeError: Deps missing, patch failed, the child exited before
                becoming ready, or the readiness deadline was exceeded.
        """

        if not (_SIDECAR_DIR / "node_modules").exists():

            raise RuntimeError(

                f"Photon sidecar deps not installed. Run: "

                f"cd {_SIDECAR_DIR} && npm install   (or `encre photon setup`)"

            )

        # A `encre update` that bumps the spectrum-ts pin rewrites

        # package-lock.json but never reinstalls node_modules, so the sidecar

        # spawns against stale deps and dies on every reconnect (the v8 patch

        # script can't find @spectrum-ts/imessage/dist that only v8 ships).

        # Self-heal by reinstalling when the lockfile is newer than npm's

        # install marker. Runs off the event loop so a cold install can't

        # freeze every other platform's traffic.

        if _sidecar_deps_stale():

            logger.warning(

                "[photon] sidecar deps are stale (lockfile newer than install); "

                "reinstalling before start"

            )

            await asyncio.to_thread(_reinstall_sidecar_deps)

        await self._reap_stale_sidecar()

        env = os.environ.copy()

        env["PHOTON_PROJECT_ID"] = self._project_id

        env["PHOTON_PROJECT_SECRET"] = self._project_secret

        env["PHOTON_SIDECAR_PORT"] = str(self._sidecar_port)

        env["PHOTON_SIDECAR_BIND"] = self._sidecar_bind

        env["PHOTON_SIDECAR_TOKEN"] = self._sidecar_token

        # The sidecar exits when its stdin (the pipe below) hits EOF, so a

        # gateway death of ANY kind -- including SIGKILL, where disconnect()

        # never runs -- can't leave it orphaned on the port.

        env["PHOTON_SIDECAR_WATCH_STDIN"] = "1"

        # Windows: hide the child console (0 elsewhere). Same helper the

        # discord/whatsapp adapters use for their sidecar spawns.

        from encre._subprocess_compat import windows_hide_flags

        try:

            patch = subprocess.run(  # noqa: S603

                [

                    self._node_bin,

                    str(_SIDECAR_DIR / "patch-spectrum-mixed-attachments.mjs"),

                    str(_SIDECAR_DIR),

                ],

                capture_output=True,

                text=True,

                timeout=10,

                check=False,

                # Windows: suppress the brief console flash this short-lived

                # node patch run would otherwise pop on every sidecar start.

                creationflags=windows_hide_flags(),

            )

            if patch.returncode != 0:

                raise RuntimeError((patch.stderr or patch.stdout or "").strip())

            if patch.stderr.strip():

                logger.debug("[photon] %s", patch.stderr.strip())

        except Exception as exc:

            logger.warning(

                "[photon] failed to apply Spectrum mixed attachment patch: %s",

                exc,

            )

        self._sidecar_proc = subprocess.Popen(  # noqa: S603

            [self._node_bin, str(_SIDECAR_DIR / "index.mjs")],

            stdin=subprocess.PIPE,

            stdout=subprocess.PIPE,

            stderr=subprocess.STDOUT,

            env=env,

            start_new_session=(sys.platform != "win32"),

            # Windows: run the persistent sidecar headless so it does not open

            # (or leave) a visible console window. CREATE_NO_WINDOW only (no

            # DETACHED_PROCESS) so the stdin/stdout pipes above stay usable.

            creationflags=windows_hide_flags(),

        )

        # Pump sidecar stderr/stdout into our logger so users see crashes.

        loop = asyncio.get_event_loop()

        self._sidecar_supervisor_task = loop.create_task(

            self._supervise_sidecar(self._sidecar_proc)

        )

        # Wait for /healthz to come up -- give it up to 15s on cold start.

        deadline = time.time() + 15.0

        last_err: Optional[Exception] = None

        async with httpx.AsyncClient(timeout=2.0) as client:

            while time.time() < deadline:

                if self._sidecar_proc.poll() is not None:

                    raise RuntimeError(

                        f"Photon sidecar exited with code "

                        f"{self._sidecar_proc.returncode} before becoming ready"

                    )

                try:

                    resp = await client.post(

                        f"http://{self._sidecar_bind}:{self._sidecar_port}/healthz",

                        headers={"X-Encre-Sidecar-Token": self._sidecar_token},

                    )

                    if resp.status_code == 200:

                        return

                except httpx.RequestError as e:

                    last_err = e

                await asyncio.sleep(0.2)

        raise RuntimeError(

            f"Photon sidecar did not become ready within 15s: {last_err}"

        )

    async def _supervise_sidecar(self, proc: subprocess.Popen) -> None:

        """Pump the sidecar's stdout/stderr into our logger."""

        if proc.stdout is None:  # subprocess was launched without stdout=PIPE

            return

        stdout = proc.stdout

        loop = asyncio.get_event_loop()

        try:

            while True:

                line = await loop.run_in_executor(None, stdout.readline)

                if not line:

                    break

                logger.info("[photon-sidecar] %s", decode_bytes(line).rstrip())

        except Exception as e:  # pragma: no cover - defensive

            logger.warning("[photon-sidecar] supervisor exited: %s", e)

        if self._inbound_running:

            exit_code = proc.poll()

            logger.error(

                "[photon] sidecar exited unexpectedly (code %s) 鈥?triggering reconnect",

                exit_code,

            )

            self._set_fatal_error(

                "SIDECAR_CRASHED",

                f"Photon sidecar exited unexpectedly (code {exit_code})",

                retryable=True,

            )

            try:

                await self._notify_fatal_error()

            except Exception as exc:  # pragma: no cover - defensive

                logger.warning("[photon] fatal-error notification failed: %s", exc)

    async def _stop_sidecar(self) -> None:

        """Shut the sidecar down, escalating from polite to forced.

        Layered teardown so no layer alone is load-bearing: closing the stdin
        pipe is itself an EOF shutdown signal, then the ``/shutdown`` HTTP
        call, then a bounded ``wait``, then a process-group ``SIGTERM``, then
        ``SIGKILL``. Idempotent -- a second call returns immediately.

        Note:
            Always clears ``_sidecar_proc`` and cancels the supervisor task in
            the ``finally`` block, so a failed teardown cannot leak a zombie
            handle into the next ``connect``.
        """

        proc = self._sidecar_proc

        if proc is None:

            return

        try:

            # Closing our end of the stdin pipe is itself a shutdown signal

            # (the sidecar watches for EOF), and covers the case where the

            # HTTP call below can't get through.

            if proc.stdin is not None:

                try:

                    proc.stdin.close()

                except Exception:

                    pass

            # Polite shutdown first.

            if self._http_client is not None:

                try:

                    await self._http_client.post(

                        f"http://{self._sidecar_bind}:{self._sidecar_port}/shutdown",

                        headers={"X-Encre-Sidecar-Token": self._sidecar_token},

                        timeout=2.0,

                    )

                except Exception:

                    pass

            try:

                proc.wait(timeout=3.0)

            except subprocess.TimeoutExpired:

                if sys.platform != "win32":

                    try:

                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # windows-footgun: ok

                    except (ProcessLookupError, PermissionError):

                        proc.terminate()

                else:

                    proc.terminate()

                try:

                    proc.wait(timeout=2.0)

                except subprocess.TimeoutExpired:

                    proc.kill()

        finally:

            self._sidecar_proc = None

            if self._sidecar_supervisor_task is not None:

                self._sidecar_supervisor_task.cancel()

                self._sidecar_supervisor_task = None

    # -- Outbound ----------------------------------------------------------

    async def send(

        self,

        chat_id: str,

        content: str,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

    ) -> SendResult:

        """Send one text message through the live sidecar.

        This is the path taken when the gateway is co-resident with the
        adapter: :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`
        calls it to relay agent output. Content is formatted by
        :meth:`format_message` before being handed to :meth:`_sidecar_send`,
        which enforces the length cap and records the created message id for
        later tapback correlation.

        Cron deliveries made when the gateway is not running do not reach here;
        they use :func:`_standalone_send` instead.

        Args:
            chat_id: Spectrum space id to post to.
            content: Agent output text, before formatting.
            reply_to: Unused by spectrum-ts today; retained for the base
                signature.
            metadata: Adapter-specific extras, retained for the base signature.

        Returns:
            :class:`SendResult`; failures are returned, never raised.
        """

        return await self._sidecar_send(chat_id, self.format_message(content))

    # -- Outbound media (parity with the BlueBubbles iMessage channel) -----

    #

    # Photon ships outbound attachments via spectrum-ts' `attachment()` /

    # `voice()` content builders. The sidecar's `/send-attachment` endpoint

    # wraps `space.send(attachment(path, {...}))`. These overrides mirror

    # BlueBubbles: URL-based helpers cache to a local path first, file-based

    # helpers pass the path straight through.

    async def send_image(

        self,

        chat_id: str,

        image_url: str,

        caption: Optional[str] = None,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

    ) -> SendResult:

        """Send an image from a URL, caching it to a local path first.

        The sidecar's ``attachment()`` builder reads from a filesystem path,
        so URL-based sends must download first. When the download fails, fall
        back to the base implementation, which sends the URL as plain text
        rather than dropping the message silently.

        Args:
            chat_id: Spectrum space id to post to.
            image_url: Public or vendor-signed image URL.
            caption: Optional caption text.
            reply_to: Unused by spectrum-ts today; retained for the base
                signature.
            metadata: Adapter-specific extras, retained for the base signature.

        Returns:
            :class:`SendResult` for the attachment, or for the text fallback.
        """

        try:

            from encre.gateway.platforms.base import cache_image_from_url

            local_path = await cache_image_from_url(image_url)

        except Exception:

            # Couldn't fetch the URL -- fall back to sending it as text.

            return await super().send_image(chat_id, image_url, caption, reply_to)

        return await self._sidecar_send_attachment(

            chat_id, local_path, caption=caption,

        )

    async def send_image_file(

        self,

        chat_id: str,

        image_path: str,

        caption: Optional[str] = None,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

        **kwargs,

    ) -> SendResult:

        """Send an image already on local disk.

        Unlike :meth:`send_image` there is no download step, so the path is
        handed straight to :meth:`_sidecar_send_attachment`, which still
        re-validates it with
        :meth:`BasePlatformAdapter.validate_media_delivery_path` before it
        reaches the Node sidecar.

        Args:
            chat_id: Spectrum space id to post to.
            image_path: Local image file path.
            caption: Optional caption text.
            reply_to: Unused by spectrum-ts today; retained for the base
                signature.
            metadata: Adapter-specific extras, retained for the base signature.
            **kwargs: Absorbed so callers may pass adapter-specific extras.

        Returns:
            :class:`SendResult` from the sidecar's ``/send-attachment`` call.
        """

        return await self._sidecar_send_attachment(

            chat_id, image_path, caption=caption,

        )

    async def send_voice(

        self,

        chat_id: str,

        audio_path: str,

        caption: Optional[str] = None,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

        **kwargs,

    ) -> SendResult:

        """Send an audio file as a voice note.

        Passes ``kind="voice"`` so the sidecar uses spectrum-ts' ``voice()``
        content builder; platforms without native voice notes degrade it to an
        ordinary audio attachment server-side.

        Args:
            chat_id: Spectrum space id to post to.
            audio_path: Local audio file path.
            caption: Optional caption text.
            reply_to: Unused by spectrum-ts today; retained for the base
                signature.
            metadata: Adapter-specific extras, retained for the base signature.
            **kwargs: Absorbed so callers may pass adapter-specific extras.

        Returns:
            :class:`SendResult` from the sidecar's ``/send-attachment`` call.
        """

        return await self._sidecar_send_attachment(

            chat_id, audio_path, caption=caption, kind="voice",

        )

    async def send_video(

        self,

        chat_id: str,

        video_path: str,

        caption: Optional[str] = None,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

        **kwargs,

    ) -> SendResult:

        """Send a video from a local path.

        Videos ride the same ``attachment()`` builder as images and documents;
        iMessage delivers them as playable video attachments, so no distinct
        sidecar endpoint is needed.

        Args:
            chat_id: Spectrum space id to post to.
            video_path: Local video file path.
            caption: Optional caption text.
            reply_to: Unused by spectrum-ts today; retained for the base
                signature.
            metadata: Adapter-specific extras, retained for the base signature.
            **kwargs: Absorbed so callers may pass adapter-specific extras.

        Returns:
            :class:`SendResult` from the sidecar's ``/send-attachment`` call.
        """

        return await self._sidecar_send_attachment(

            chat_id, video_path, caption=caption,

        )

    async def send_document(

        self,

        chat_id: str,

        file_path: str,

        caption: Optional[str] = None,

        file_name: Optional[str] = None,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

        **kwargs,

    ) -> SendResult:

        """Send a generic document from a local path.

        The only media override that forwards ``name``: spectrum-ts derives
        the display filename from the path, so passing ``file_name`` is how a
        caller's chosen name survives instead of a temp-file basename.

        Args:
            chat_id: Spectrum space id to post to.
            file_path: Local file path.
            caption: Optional caption text.
            file_name: Display filename override; omitted when ``None`` so the
                sidecar infers from the path.
            reply_to: Unused by spectrum-ts today; retained for the base
                signature.
            metadata: Adapter-specific extras, retained for the base signature.
            **kwargs: Absorbed so callers may pass adapter-specific extras.

        Returns:
            :class:`SendResult` from the sidecar's ``/send-attachment`` call.
        """

        return await self._sidecar_send_attachment(

            chat_id, file_path, name=file_name, caption=caption,

        )

    async def send_animation(

        self,

        chat_id: str,

        animation_url: str,

        caption: Optional[str] = None,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

    ) -> SendResult:

        """Send a GIF animation by delegating to the image path.

        iMessage has no separate animation container -- a GIF arrives as an
        ordinary image attachment and plays inline -- so there is nothing for a
        distinct code path to do. Delegation also inherits the URL-download and
        text-fallback behaviour of :meth:`send_image`.

        Args:
            chat_id: Spectrum space id to post to.
            animation_url: URL of the GIF animation.
            caption: Optional caption text.
            reply_to: Unused by spectrum-ts today; retained for the base
                signature.
            metadata: Adapter-specific extras, retained for the base signature.

        Returns:
            :class:`SendResult` from :meth:`send_image`.
        """

        # iMessage renders GIFs inline as ordinary image attachments.

        return await self.send_image(

            chat_id, animation_url, caption, reply_to, metadata,

        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:

        """Show the typing indicator, throttled per chat.

        The cooldown gate runs before the HTTP call so a burst of stream
        deltas from one reply collapses into a single ``/typing`` POST per
        chat. iMessage is a personal channel, so suppressing rapid repeats
        reduces upstream gRPC pressure during Photon overflow events.

        Args:
            chat_id: Spectrum space id to signal.
            metadata: Unused; retained for the base signature.
        """

        now = time.time()

        if now - self._typing_last_sent.get(chat_id, 0.0) < _TYPING_COOLDOWN_SECONDS:

            return

        self._typing_last_sent[chat_id] = now

        try:

            await self._sidecar_call(

                "/typing", {"spaceId": chat_id, "state": "start"}

            )

        except Exception as e:

            logger.debug("[photon] send_typing failed: %s", e)

    async def stop_typing(self, chat_id: str) -> None:

        """Clear the typing indicator for a chat.

        Drops the cooldown entry unconditionally, so a caller that stops and
        immediately restarts typing is not blocked by a timestamp set by an
        earlier indicator.

        Args:
            chat_id: Spectrum space id to signal.
        """

        self._typing_last_sent.pop(chat_id, None)

        try:

            await self._sidecar_call(

                "/typing", {"spaceId": chat_id, "state": "stop"}

            )

        except Exception as e:

            logger.debug("[photon] stop_typing failed: %s", e)

    # -- Reactions (tapbacks) -----------------------------------------------

    #

    # Same lifecycle-hook pattern as Telegram/Discord: "thinking" while processing,

    # swapped for "fail"/"success" on completion. Opt-in via PHOTON_REACTIONS -- iMessage

    # is a personal-texting channel, and a tapback on every text is noisy.

    _SENT_IDS_MAX = 1000

    _LAST_INBOUND_CHATS_MAX = 200

    def _record_sent_message(self, message_id: Optional[str]) -> None:

        """Remember a message id we sent, for inbound reaction routing.

        The sidecar echoes the created ``messageId`` back on every send;
        storing it is what lets :meth:`_dispatch_inbound` tell a tapback
        addressed to one of our own messages from a tapback on a human-to-human
        message.

        Args:
            message_id: The id returned by the sidecar, or ``None`` when the
                send failed and nothing was created.

        Note:
            Re-inserting an existing id refreshes its insertion order, and the
            dict is capped at ``_SENT_IDS_MAX`` by evicting the oldest entries,
            so a chatty chat cannot grow it without limit.
        """

        if not message_id:

            return

        sent = self._sent_message_ids

        if message_id in sent:

            del sent[message_id]  # refresh insertion order

        sent[message_id] = time.time()

        if len(sent) > self._SENT_IDS_MAX:

            for old in list(sent.keys())[: len(sent) - self._SENT_IDS_MAX]:

                del sent[old]

    # A DM space is addressable two ways -- the chat GUID (`any;-;+1555...`)

    # that inbound events carry, and the bare E.164 phone that home-channel

    # config typically uses. The sidecar's resolveSpace treats them as the

    # same space; normalize to the bare phone so the last-inbound tracker

    # does too (mirrors phoneTargetFromSpaceId in sidecar/index.mjs).

    _DM_CHAT_GUID_RE = re.compile(r"^any;-;(\+\d{6,})$")

    @classmethod

    def _normalize_chat_key(cls, chat_id: str) -> str:

        """Fold a DM chat GUID to the bare E.164 phone number.

        iMessage DM spaces are addressable two ways: the chat GUID
        (``any;-;+1555...``) that inbound events carry, and the bare phone that
        home-channel config typically uses. The sidecar treats them as the same
        space, so the last-inbound tracker has to too or it would never match
        the id a caller passes in.

        Args:
            chat_id: A chat GUID or a bare phone number.

        Returns:
            The extracted E.164 for a DM GUID, or ``chat_id`` unchanged for
            group ids and anything else that does not match.
        """

        match = cls._DM_CHAT_GUID_RE.match(chat_id)

        return match.group(1) if match else chat_id

    def _record_last_inbound(

        self, chat_id: Optional[str], message_id: Optional[str]

    ) -> None:

        """Track the most recent inbound message id per chat.

        Lets the agent-facing ``add_reaction`` default to "the message that
        triggered me" without the model threading message ids through tool
        calls. Keys are normalised by :meth:`_normalize_chat_key` so a GUID
        from the inbound event and a bare phone from a caller collapse onto one
        entry.

        Args:
            chat_id: The chat the message arrived in; ``None`` skips the write.
            message_id: The inbound message id to remember; ``None`` skips the
                write.

        Note:
            Recorded before the mention gate in :meth:`_dispatch_inbound`, so a
            reaction to a non-wake-word group message is still valid. Bounded
            at ``_LAST_INBOUND_CHATS_MAX`` by eviction of the oldest chats.
        """

        if not chat_id or not message_id:

            return

        key = self._normalize_chat_key(chat_id)

        last = self._last_inbound_by_chat

        if key in last:

            del last[key]  # refresh insertion order

        last[key] = message_id

        if len(last) > self._LAST_INBOUND_CHATS_MAX:

            for old in list(last.keys())[

                : len(last) - self._LAST_INBOUND_CHATS_MAX

            ]:

                del last[old]

    def _reactions_enabled(self) -> bool:

        """Report whether the automatic per-message tapback hooks are opted in.

        Gates only :meth:`on_processing_start` and
        :meth:`on_processing_complete`. The agent-facing ``add_reaction`` and
        ``remove_reaction`` are deliberate intents and ignore this switch, so
        ``PHOTON_REACTIONS=false`` mutes automatic noise without disabling
        explicit requests.

        Returns:
            True when ``PHOTON_REACTIONS`` is a truthy string; defaults to
            False, since a tapback on every message is noisy in a
            personal-texting channel.
        """

        return os.getenv("PHOTON_REACTIONS", "false").strip().lower() in {

            "true", "1", "yes", "on",

        }

    async def _add_reaction(

        self, chat_id: str, message_id: str, emoji: str

    ) -> bool:

        """Tapback ``emoji`` onto a message. Soft-fails (False), never raises."""

        try:

            await self._sidecar_call(

                "/react",

                {"spaceId": chat_id, "messageId": message_id, "emoji": emoji},

            )

            return True

        except Exception as e:

            logger.debug("[photon] add_reaction failed: %s", e)

            return False

    async def _remove_reaction(self, chat_id: str, message_id: str) -> bool:

        """Retract our tapback from a message. Soft-fails (False), never raises.



        The sidecar tracks one reaction handle per target message; after a

        sidecar restart the handle is gone and removal is best-effort (the

        stale tapback self-heals when the next reaction replaces it).

        """

        try:

            await self._sidecar_call(

                "/unreact", {"spaceId": chat_id, "messageId": message_id},

            )

            return True

        except Exception as e:

            logger.debug("[photon] remove_reaction failed: %s", e)

            return False

    # -- Agent-facing reactions (send_message action="react") ---------------

    #

    # Unlike the lifecycle hooks below, these are deliberate agent intents,

    # so they are NOT gated by PHOTON_REACTIONS (that env var exists to mute

    # the automatic per-message tapback noise, not explicit requests).

    async def add_reaction(

        self,

        chat_id: str,

        emoji: str,

        message_id: Optional[str] = None,

    ) -> Dict[str, Any]:

        """Tapback ``emoji`` onto a message in ``chat_id``.



        Without ``message_id``, targets the chat's most recent inbound

        message (typically the one the agent is responding to). iMessage

        maps the system tapback emoji set to native tapbacks; anything else uses Apple's

        custom-emoji reaction.

        """

        target = message_id or self._last_inbound_by_chat.get(

            self._normalize_chat_key(chat_id)

        )

        if not target:

            return {

                "success": False,

                "error": "no message to react to 鈥?pass message_id (no "

                "inbound message seen in this chat since the gateway started)",

            }

        ok = await self._add_reaction(chat_id, target, emoji)

        if not ok:

            return {

                "success": False,

                "error": "reaction failed (see gateway debug log)",

            }

        return {"success": True, "message_id": target}

    async def remove_reaction(

        self, chat_id: str, message_id: Optional[str] = None

    ) -> Dict[str, Any]:

        """Retract our tapback from a message (best-effort)."""

        target = message_id or self._last_inbound_by_chat.get(

            self._normalize_chat_key(chat_id)

        )

        if not target:

            return {

                "success": False,

                "error": "no message to unreact 鈥?pass message_id",

            }

        ok = await self._remove_reaction(chat_id, target)

        if not ok:

            return {

                "success": False,

                "error": "unreact failed (see gateway debug log)",

            }

        return {"success": True, "message_id": target}

    async def on_processing_start(self, event: MessageEvent) -> None:

        """Tapback "thinking" on the triggering message while the agent works."""

        if not self._reactions_enabled():

            return

        chat_id = getattr(event.source, "chat_id", None)

        message_id = getattr(event, "message_id", None)

        if chat_id and message_id:

            await self._add_reaction(chat_id, message_id, "\U0001f440")

    async def on_processing_complete(

        self, event: MessageEvent, outcome: ProcessingOutcome

    ) -> None:

        """Swap the progress tapback for a fail/success result tapback.



        Remove-then-add rather than a bare replace: deterministic whether the

        platform replaces a sender's previous tapback or stacks them, and it

        keeps the sidecar's reaction-handle slot coherent.

        """

        if not self._reactions_enabled():

            return

        chat_id = getattr(event.source, "chat_id", None)

        message_id = getattr(event, "message_id", None)

        if not chat_id or not message_id:

            return

        await self._remove_reaction(chat_id, message_id)

        if outcome == ProcessingOutcome.SUCCESS:

            await self._add_reaction(chat_id, message_id, "\U0001f44d")

        elif outcome == ProcessingOutcome.FAILURE:

            await self._add_reaction(chat_id, message_id, "\U0001f44e")

        # CANCELLED: leave the message unreacted.

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:

        """Return whatever we know about a Spectrum space id.



        Photon's ``space.id`` is opaque; the inbound event also carries the

        DM/group type, but here we only have the id, so infer conservatively.

        """

        return {"name": chat_id, "type": "dm", "id": chat_id}

    def format_message(self, content: str) -> str:

        """Shape agent output into the outbound grammar Photon accepts.

        With markdown enabled the text is forwarded verbatim so the sidecar can
        hand it to spectrum-ts' ``markdown()`` builder and iMessage can render
        it natively; fences and emphasis survive untouched. With
        ``PHOTON_MARKDOWN=false`` the same text is flattened through
        :func:`encre.gateway.platforms.helpers.strip_markdown` so a user can
        drop back to plain iMessage bubbles without changing code.

        Args:
            content: Agent output text, before platform shaping.

        Returns:
            The text ready for :meth:`_sidecar_send`. Formatting never raises;
            callers can treat the result as always string-safe.

        Note:
            :func:`_standalone_send` re-derives this decision itself, applying
            its own length cap inline, rather than calling here because it has
            no adapter instance to borrow.
        """
        # Markdown is passed through verbatim -- -- the sidecar sends it with the

        # markdown() builder and iMessage renders it. The strip path remains

        # as the PHOTON_MARKDOWN=false kill-switch.

        if _markdown_enabled():

            return content

        return strip_markdown(content)

    @staticmethod

    def _is_retryable_error(error: Optional[str]) -> bool:

        """Decide whether a send failure is worth retrying.

        Combines the generic patterns from
        :meth:`BasePlatformAdapter._is_retryable_error` with
        ``_PHOTON_RETRYABLE_PATTERNS``, which name the Envoy and spectrum-ts
        upstream-overload signatures this adapter sees that the shared list does
        not cover. Substring matching, case-insensitive, over the whole error.

        Args:
            error: Error string from a failed :class:`SendResult`, or ``None``.

        Returns:
            True when the failure looks like transient upstream pressure rather
            than a permanent configuration or delivery problem.
        """

        if BasePlatformAdapter._is_retryable_error(error):

            return True

        if not error:

            return False

        lowered = error.lower()

        return any(pat in lowered for pat in _PHOTON_RETRYABLE_PATTERNS)

    async def _send_with_retry(

        self,

        chat_id: str,

        content: str,

        reply_to: Optional[str] = None,

        metadata: Any = None,

        max_retries: int = 1,

        base_delay: float = 2.0,

    ) -> SendResult:

        """Retry sends without the generic Markdown banner.



        Photon replies are markdown (rendered by iMessage) or stripped plain

        text under ``PHOTON_MARKDOWN=false`` -- either way the gateway's

        generic banner never applies.

        """

        text = self.format_message(content)

        result = await self.send(

            chat_id=chat_id,

            content=text,

            reply_to=reply_to,

            metadata=metadata,

        )

        if result.success:

            return result

        error_str = result.error or ""

        is_network = result.retryable or self._is_retryable_error(error_str)

        if not is_network and self._is_timeout_error(error_str):

            return result

        if is_network:

            for attempt in range(1, max_retries + 1):

                delay = base_delay * (2 ** (attempt - 1))

                logger.warning(

                    "[photon] Send failed (attempt %d/%d, retrying in %.1fs): %s",

                    attempt, max_retries, delay, error_str,

                )

                await asyncio.sleep(delay)

                result = await self.send(

                    chat_id=chat_id,

                    content=text,

                    reply_to=reply_to,

                    metadata=metadata,

                )

                if result.success:

                    return result

                error_str = result.error or ""

                if not (result.retryable or self._is_retryable_error(error_str)):

                    break

            else:

                logger.error(

                    "[photon] Failed to deliver response after %d retries: %s",

                    max_retries, error_str,

                )

                return result

        logger.warning(

            "[photon] Send failed: %s - retrying plain-text message",

            error_str,

        )

        fallback_result = await self.send(

            chat_id=chat_id,

            content=text[: self.MAX_MESSAGE_LENGTH],

            reply_to=reply_to,

            metadata=metadata,

        )

        if not fallback_result.success:

            logger.error("[photon] Plain-text retry also failed: %s", fallback_result.error)

        return fallback_result

    async def _sidecar_send(self, space_id: str, text: str) -> SendResult:

        """Post one text message to the sidecar's ``/send`` endpoint.

        Applies the outbound length cap before the request so truncation is
        visible in this adapter's log rather than surfacing as an opaque
        upstream reject, forwards ``format: markdown`` only when markdown is on,
        and records the created ``messageId`` so a later tapback on this
        message can be routed to us by :meth:`_dispatch_inbound`.

        Args:
            space_id: Spectrum space id to post into.
            text: Message text, already shaped by :meth:`format_message`.

        Returns:
            :class:`encre.gateway.platforms.base.SendResult` with the created
            message id, or ``success=False`` carrying the transport error.
            Failures are returned rather than raised so :meth:`_send_with_retry`
            can classify and retry them.

        Note:
            The ``format`` key is omitted rather than set to ``"plain"`` when
            markdown is disabled, so a sidecar built before that key existed
            still accepts the body during a half-upgraded restart.
        """

        if len(text) > self.MAX_MESSAGE_LENGTH:

            logger.warning(

                "[photon] truncating outbound from %d to %d chars",

                len(text), self.MAX_MESSAGE_LENGTH,

            )

            text = text[: self.MAX_MESSAGE_LENGTH]

        body: Dict[str, Any] = {"spaceId": space_id, "text": text}

        # Omit the key when disabled so an older sidecar (pre-`format`)

        # keeps accepting the body during a half-upgraded restart.

        if _markdown_enabled():

            body["format"] = "markdown"

        try:

            data = await self._sidecar_call("/send", body)

        except Exception as e:

            return SendResult(success=False, error=str(e))

        self._record_sent_message(data.get("messageId"))

        return SendResult(success=True, message_id=data.get("messageId"))

    async def _sidecar_send_attachment(

        self,

        space_id: str,

        path: str,

        *,

        name: Optional[str] = None,

        mime_type: Optional[str] = None,

        caption: Optional[str] = None,

        kind: str = "attachment",

    ) -> SendResult:

        """POST a local file to the sidecar's ``/send-attachment`` endpoint.



        ``kind`` is ``"voice"`` for audio sent as a voice note (downgrades

        to a plain audio attachment on platforms without voice notes),

        otherwise ``"attachment"``. spectrum-ts infers ``name`` and

        ``mimeType`` from the file extension; we only pass overrides when

        Encre supplied them.

        """

        # Defense-in-depth: re-validate the path before handing it to the

        # Node sidecar. The gateway already filters MEDIA paths, but

        # send_*_file / cron callers may pass arbitrary strings.

        safe_path = self.validate_media_delivery_path(str(path))

        if not safe_path:

            return SendResult(

                success=False, error=f"unsafe or missing attachment path: {path}"

            )

        if not mime_type:

            import mimetypes

            guessed, _ = mimetypes.guess_type(safe_path)

            mime_type = guessed or None

        body: Dict[str, Any] = {

            "spaceId": space_id,

            "path": safe_path,

            "kind": "voice" if kind == "voice" else "attachment",

        }

        if name:

            body["name"] = name

        if mime_type:

            body["mimeType"] = mime_type

        if caption:

            body["caption"] = caption

        try:

            data = await self._sidecar_call("/send-attachment", body)

        except Exception as e:

            return SendResult(success=False, error=str(e))

        self._record_sent_message(data.get("messageId"))

        return SendResult(success=True, message_id=data.get("messageId"))

    async def _sidecar_call(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:

        """POST one JSON body to a sidecar control endpoint and decode the reply.

        Shared transport for every outbound control call: text send, attachment
        send, typing indicator, and reaction add/remove all funnel through here,
        so the bearer token, the loopback address, and the 30s timeout are
        defined once.

        Args:
            path: Sidecar endpoint path, e.g. ``"/send"`` or ``"/typing"``.
            body: JSON-serialisable request body.

        Returns:
            The decoded response body. Guaranteed to carry a truthy ``ok`` key,
            so callers can read endpoint-specific fields such as ``messageId``
            without re-checking success.

        Raises:
            RuntimeError: The adapter is not connected, the endpoint answered a
                non-200 status, or the body reported ``ok: false`` with an
                error string.

        Note:
            A fresh :class:`httpx.AsyncClient` is opened per call rather than
            reusing ``_http_client``: this method may be reached from a worker
            thread on a different event loop than the one the persistent client
            was created on, and httpx binds a client to its creating loop. The
            inbound streaming loop keeps using ``_http_client`` directly since
            it always runs on the gateway's loop.
        """

        # Guard: adapter not yet connected (no sidecar address known).

        if self._http_client is None:

            raise RuntimeError("Photon adapter not connected")

        # Use a fresh client per call so this method is safe when invoked from

        # a worker thread that owns a different event loop than the one the

        # persistent _http_client was created on (e.g. via _run_async in

        # send_message_tool).  The inbound streaming loop continues to use

        # _http_client directly -- it always runs on the gateway's loop.

        url = f"http://{self._sidecar_bind}:{self._sidecar_port}{path}"

        headers = {"X-Encre-Sidecar-Token": self._sidecar_token}

        async with httpx.AsyncClient(timeout=30.0) as client:

            resp = await client.post(url, json=body, headers=headers)

        if resp.status_code != 200:

            raise RuntimeError(

                f"Photon sidecar {path} returned {resp.status_code}: {resp.text[:200]}"

            )

        data = resp.json() or {}

        if not data.get("ok"):

            raise RuntimeError(

                f"Photon sidecar {path} reported error: {data.get('error')}"

            )

        return data


# ---------------------------------------------------------------------------

# Helpers


def _attachment_message_type(mime: str) -> MessageType:
    """Map an inbound attachment MIME type to the closest :class:`MessageType`.

    Only the MIME major class is inspected, so ``image/heic`` and ``image/jpeg``
    collapse into one bucket. Empty, unknown, ``application/*`` and any other
    prefix land on ``MessageType.DOCUMENT``, which is the safest bucket for the
    gateway's media routing since a document is never rendered inline.

    Args:
        mime: Attachment MIME type from the sidecar event (may be empty).

    Returns:
        ``MessageType.PHOTO``, ``VIDEO``, ``AUDIO``, or ``DOCUMENT``.
    """

    mime = (mime or "").lower()

    if mime.startswith("image/"):

        return MessageType.PHOTO

    if mime.startswith("video/"):

        return MessageType.VIDEO

    if mime.startswith("audio/"):

        return MessageType.AUDIO

    if mime.startswith("application/"):

        return MessageType.DOCUMENT

    return MessageType.DOCUMENT


# MIME -> file-extension maps for caching inbound attachment bytes. These mirror

# the BlueBubbles iMessage channel so both adapters name cached media the same.

_IMAGE_EXT_BY_MIME = {

    "image/jpeg": ".jpg",

    "image/png": ".png",

    "image/gif": ".gif",

    "image/webp": ".webp",

    "image/heic": ".jpg",

    "image/heif": ".jpg",

    "image/tiff": ".jpg",

}

_AUDIO_EXT_BY_MIME = {

    "audio/mp3": ".mp3",

    "audio/mpeg": ".mp3",

    "audio/ogg": ".ogg",

    "audio/wav": ".wav",

    "audio/x-caf": ".mp3",

    "audio/mp4": ".m4a",

    "audio/aac": ".m4a",

}


def _cache_inbound_attachment(

    content: Dict[str, Any],

    name: str,

    mime: str,

    *,

    force_audio: bool = False,

) -> Optional[str]:

    """Decode a base64-inlined inbound attachment and cache it locally.



    The sidecar inlines the attachment bytes as ``content["data"]`` (base64).

    We decode them and route to the shared media cache by MIME type, returning

    the cached absolute path so the caller can populate ``media_urls`` (which

    the gateway then hands to the model). Returns ``None`` when there are no

    bytes (over the sidecar's inline cap or a failed read) or when caching

    fails, so the caller can fall back to a text marker.

    """

    data_b64 = content.get("data")

    if not data_b64:

        return None

    try:

        raw = base64.b64decode(data_b64)

    except (ValueError, TypeError) as exc:

        logger.warning("[photon] failed to decode inbound attachment bytes: %s", exc)

        return None

    from encre.gateway.platforms.base import (

        cache_audio_from_bytes,

        cache_document_from_bytes,

        cache_image_from_bytes,

    )

    mime = (mime or "").lower()

    # Prefer the real extension from the filename; fall back to the MIME map.

    suffix = Path(name).suffix if name else ""

    try:

        if mime.startswith("image/"):

            ext = suffix or _IMAGE_EXT_BY_MIME.get(mime, ".jpg")

            try:

                return cache_image_from_bytes(raw, ext)

            except ValueError:

                # Bytes don't look like a supported image (e.g. HEIC magic) --
                # still deliver them as a document rather than dropping them.

                return cache_document_from_bytes(raw, name)

        if force_audio or mime.startswith("audio/"):

            ext = suffix or _AUDIO_EXT_BY_MIME.get(

                mime, ".m4a" if force_audio else ".mp3"

            )

            return cache_audio_from_bytes(raw, ext)

        # Video, application/*, and everything else -> document cache.

        return cache_document_from_bytes(raw, name)

    except Exception as exc:

        logger.warning("[photon] failed to cache inbound attachment %s: %s", name, exc)

        return None


# ---------------------------------------------------------------------------

# Standalone (out-of-process) send for cron deliveries when the gateway

# is not co-resident.  Reuses a live sidecar already listening on the

# configured port (cron processes cannot spawn the sidecar themselves).


async def _standalone_send(

    pconfig: PlatformConfig,

    chat_id: str,

    message: str,

    *,

    thread_id: Optional[str] = None,  # noqa: ARG001 -- Spectrum has no threads yet

    media_files: Optional[list] = None,

    force_document: bool = False,  # noqa: ARG001 -- iMessage auto-detects file kind

) -> Dict[str, Any]:

    """Deliver a message from a process with no live gateway connection.

    This is the cron and one-shot sender path: the caller has a
    :class:`PlatformConfig` but no :class:`PhotonAdapter`, and Photon's
    sidecar cannot be spawned here because it needs the credentials and
    subprocess supervision that only :meth:`PhotonAdapter.connect` sets up. The
    function therefore borrows a sidecar that an already-running gateway left
    listening on the loopback port, authenticating with the token that gateway
    published in the environment.

    Text is sent before media so it leads the thread, each file goes out as its
    own ``/send-attachment`` call so one bad attachment cannot sink the text,
    and every path is re-validated with
    :meth:`BasePlatformAdapter.validate_media_delivery_path` before it is
    handed to the Node process.

    Args:
        pconfig: Platform config for this platform; only ``extra["sidecar_port"]``
            is consulted, falling back to ``PHOTON_SIDECAR_PORT`` then the
            compiled default.
        chat_id: Spectrum space id to post into.
        message: Text body; truncated to ``_MAX_MESSAGE_LENGTH``. An empty
            string skips the text step and sends media only.
        thread_id: Unused; Spectrum has no reply threads.
        media_files: ``(path, is_voice)`` pairs, as produced by
            :meth:`BasePlatformAdapter.filter_media_delivery_paths`.
        force_document: Unused; iMessage auto-detects the file kind.

    Returns:
        ``{"success": True, "message_id": ...}`` on delivery, or an
        ``{"error": ...}`` dict describing the failure. Callers must inspect
        ``success``; this function never raises, since the cron runner
        aggregates results across platforms.

    Raises:
        Never; all failures are folded into the returned error dict.
    """

    if not HTTPX_AVAILABLE:

        return {"error": "httpx not installed"}

    port = _coerce_port(

        (pconfig.extra or {}).get("sidecar_port") or os.getenv("PHOTON_SIDECAR_PORT"),

        _DEFAULT_SIDECAR_PORT,

    )

    token = os.getenv("PHOTON_SIDECAR_TOKEN")

    if not token:

        return {

            "error": (

                "Photon standalone send requires a running sidecar with "

                "PHOTON_SIDECAR_TOKEN set in the environment. Cron processes "

                "cannot spawn the sidecar themselves."

            )

        }

    base = f"http://{_DEFAULT_SIDECAR_BIND}:{port}"

    headers = {"X-Encre-Sidecar-Token": token}

    last_message_id: Optional[str] = None

    try:

        async with httpx.AsyncClient(timeout=30.0) as client:

            # 1. Text body first (if any), so it leads the conversation.

            if message:

                send_body: Dict[str, Any] = {

                    "spaceId": chat_id,

                    "text": message[:_MAX_MESSAGE_LENGTH],

                }

                if _markdown_enabled():

                    send_body["format"] = "markdown"

                resp = await client.post(

                    f"{base}/send", json=send_body, headers=headers,

                )

                if resp.status_code != 200:

                    return {"error": f"sidecar returned {resp.status_code}: {resp.text[:200]}"}

                data = resp.json() or {}

                if not data.get("ok"):

                    return {"error": data.get("error") or "sidecar reported failure"}

                last_message_id = data.get("messageId")

            # 2. Each attachment as a separate /send-attachment call.

            #    media_files is List[Tuple[path, is_voice]] (see

            #    BasePlatformAdapter.filter_media_delivery_paths).

            import mimetypes

            for media_path, is_voice in media_files or []:

                safe_path = BasePlatformAdapter.validate_media_delivery_path(str(media_path))

                if not safe_path:

                    logger.warning("[photon] standalone send skipping unsafe path")

                    continue

                guessed, _ = mimetypes.guess_type(safe_path)

                att_body: Dict[str, Any] = {

                    "spaceId": chat_id,

                    "path": safe_path,

                    "kind": "voice" if is_voice else "attachment",

                }

                if guessed:

                    att_body["mimeType"] = guessed

                resp = await client.post(

                    f"{base}/send-attachment", json=att_body, headers=headers,

                )

                if resp.status_code != 200:

                    return {"error": f"sidecar returned {resp.status_code}: {resp.text[:200]}"}

                data = resp.json() or {}

                if not data.get("ok"):

                    return {"error": data.get("error") or "sidecar reported failure"}

                last_message_id = data.get("messageId") or last_message_id

        return {"success": True, "message_id": last_message_id}

    except Exception as e:

        return {"error": f"Photon standalone send failed: {e}"}

# ---------------------------------------------------------------------------

# Plugin entry point    # Register CLI subcommands --`encre photon ...`

    ctx.register_cli_command(

        name="photon",

        help="Set up and manage the Photon iMessage integration",

        setup_fn=_cli.register_cli,

        handler_fn=_cli.dispatch,

    )


def _build_adapter(config):
    """Adapter factory for the ``photon`` platform registry entry.

    Called by :class:`encre.gateway.run.GatewayRunner` when it resolves
    ``Platform.PHOTON`` to a live adapter, so it must not perform I/O or
    block; sidecar startup is deferred to
    :meth:`PhotonAdapter.connect`.

    Args:
        config: The :class:`PlatformConfig` assembled from ``config.yaml``
            plus environment overrides.

    Returns:
        A new, not-yet-connected :class:`PhotonAdapter`.
    """

    return PhotonAdapter(config)

