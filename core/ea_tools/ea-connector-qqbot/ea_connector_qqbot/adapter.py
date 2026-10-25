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
Inspired by the Hermes Agent project (https://github.com/NousResearch/hermes-agent.git).
Thanks to Hermes Agent for the inspiration on this module.

QQ Bot platform adapter using the official QQ Bot API v2.

Connects to the QQ Bot WebSocket Gateway for inbound events and uses the
REST API (api.sgroup.qq.com) for outbound messages and media uploads.

Configuration (via settings / env):
    Required env vars:
        QQ_APP_ID          -- QQ Bot application ID
        QQ_CLIENT_SECRET   -- QQ Bot client secret

    Optional extra config:
        dm_policy: "pairing"     -- open | allowlist | disabled | pairing
        allow_from: ["openid_1"]
        group_policy: "pairing"  -- open | allowlist | disabled | pairing
        group_allow_from: ["group_openid_1"]
        stt:                     -- Voice-to-text config
          provider: "zai"        -- zai (GLM-ASR), openai (Whisper), etc.
          baseUrl: "..."
          apiKey: "..."          -- or set QQ_STT_API_KEY env var
          model: "glm-asr"

    Voice transcription priority:
      1. QQ's built-in asr_refer_text (Tencent ASR -- free, always tried first)
      2. Configured STT provider via stt config or QQ_STT_* env vars

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
import httpx
import pilk
import qrcode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from encre.config import get_data_dir
from encre.ssrf import EncreSSRFGuard

logger = logging.getLogger(__name__)

# --- QR-code bind (agent login) endpoints ------------------------------------
# QQ's official third-party agent binding flow: a bind task is created with a
# locally generated AES key, the task id is rendered as a QR code for the
# mobile QQ app, and the resulting bot credentials come back encrypted with
# that key -- so no manual AppID/Secret entry is required.
QQ_BIND_BASE_URL = "https://q.qq.com"
EP_CREATE_BIND_TASK = "/lite/create_bind_task"
EP_POLL_BIND_RESULT = "/lite/poll_bind_result"
QQ_BIND_CONNECT_URL = (
    "https://q.qq.com/qqbot/openclaw/connect.html?task_id={task_id}&_wv=2"
)
QQ_BIND_TIMEOUT_SECONDS = 10.0
# poll_bind_result task status codes.
QQ_BIND_STATUS_NONE = 0
QQ_BIND_STATUS_PENDING = 1
QQ_BIND_STATUS_COMPLETED = 2
QQ_BIND_STATUS_EXPIRED = 3


class QQCloseError(Exception):
    """Raised when QQ WebSocket closes with a specific code.

    Carries the close code and reason for proper handling in the reconnect loop.
    """

    def __init__(self, code, reason=""):
        """Capture the WS close frame details for reconnect decisions.

        Args:
            code: Numeric close code from the QQ gateway, or None/falsy.
                Codes such as 4004, 4006 and 4008 drive specific recovery
                paths in ``_listen_loop``.
            reason: Human-readable close reason, kept for log output.

        Note:
            The exception message embeds both fields so a single log line is
            enough to diagnose the reconnect cause.
        """
        self.code = int(code) if code else None
        self.reason = str(reason) if reason else ""
        super().__init__(f"WebSocket closed (code={self.code}, reason={self.reason})")


# ---------------------------------------------------------------------------
# Constants -- imported from the shared constants module.
# ---------------------------------------------------------------------------

from .constants import (
    API_BASE,
    TOKEN_URL,
    GATEWAY_URL_PATH,
    DEFAULT_API_TIMEOUT,
    FILE_UPLOAD_TIMEOUT,
    CONNECT_TIMEOUT_SECONDS,
    RECONNECT_BACKOFF,
    MAX_RECONNECT_ATTEMPTS,
    RATE_LIMIT_DELAY,
    QUICK_DISCONNECT_THRESHOLD,
    MAX_QUICK_DISCONNECT_COUNT,
    MAX_MESSAGE_LENGTH,
    DEDUP_WINDOW_SECONDS,
    DEDUP_MAX_SIZE,
    DEDUP_CONTENT_WINDOW_SECONDS,
    MSG_TYPE_TEXT,
    MSG_TYPE_MEDIA,
    MSG_TYPE_INPUT_NOTIFY,
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPE_VOICE,
    MEDIA_TYPE_FILE,
)
from .utils import (
    coerce_list as _coerce_list_impl,
    build_user_agent,
)
from .chunked_upload import (
    ChunkedUploader,
    UploadDailyLimitExceededError,
    UploadFileTooLargeError,
)
from .keyboards import (
    ApprovalRequest,
    InlineKeyboard,
    InteractionEvent,
    build_approval_keyboard,
    build_update_prompt_keyboard,
    parse_approval_button_data,
    parse_interaction_event,
    parse_update_prompt_button_data,
)


def _coerce_list(value: Any) -> List[str]:
    """Coerce config values into a trimmed string list."""
    return _coerce_list_impl(value)


def _cache_image_from_bytes(data: bytes, ext: str) -> str:
    """Cache image bytes to a temp file and return the path."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    tmp.write(data)
    tmp.close()
    return tmp.name


def _cache_document_from_bytes(data: bytes, filename: str) -> str:
    """Cache document bytes to a temp file and return the path."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}")
    tmp.write(data)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# QQAdapter
# ---------------------------------------------------------------------------
"""QQ Bot platform adapter.

This module implements the :class:`BasePlatformAdapter` interface for the
QQ Bot v2 platform. It connects to the QQ Bot WebSocket Gateway for inbound
message events and uses the QQ Bot REST API for outbound messages, media
uploads, and inline-keyboard interactions.

Architecture overview:
    1. **Authentication** -- OAuth2 client-credentials flow (``_ensure_token``)
       obtains a short-lived access token (2-hour expiry). Token is cached
       and refreshed automatically on the next API call.
    2. **WebSocket connection** -- ``_open_ws`` establishes a persistent WS
       connection to the gateway URL returned by the REST API. The client
       sends periodic heartbeats (op 1) and receives events (op 0).
    3. **Event dispatch** -- ``_dispatch_payload`` routes incoming events:
       - ``READY`` (op 0, t=READY) -> session_id stored for resume
       - ``*MESSAGE_CREATE`` -> ``_on_message`` -> per-type handler
       - ``INTERACTION_CREATE`` -> ``_on_interaction`` -> button click routing
    4. **Message sending** -- ``send()`` formats content, splits long
       messages, then dispatches to ``_send_c2c_text``, ``_send_group_text``,
       or ``_send_guild_text`` via REST API.
    5. **Media upload** -- local files use the three-step chunked upload
       flow (prepare -> PUT parts -> complete); URLs are fetched directly
       by the QQ platform.
    6. **Voice transcription** -- STT priority: QQ built-in ASR -> pre-converted
       WAV -> SILK->WAV conversion -> external STT API (Zhipu/GLM, OpenAI).

Configuration:
    Set ``QQ_APP_ID`` and ``QQ_CLIENT_SECRET`` environment variables, or
    provide them in ``config.extra`` under the ``qqbot`` platform section.

Error handling:
    Close codes from the QQ WebSocket are mapped to specific recovery
    strategies (token refresh, session clear, fatal error). See
    ``_listen_loop`` for the full mapping.

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""


class QQAdapter(BasePlatformAdapter):
    """QQ Bot adapter backed by the official QQ Bot WebSocket Gateway + REST API.

    This adapter implements the full message lifecycle for QQ Bot v2:

    - Inbound: C2C messages, group @-messages, guild/channel messages,
      guild DM messages, inline-keyboard interactions.
    - Outbound: text (plain with markdown syntax), images, voice, video, documents,
      with native media upload (chunked for large files).
    - Typing indicator: input_notify sent before each response.
    - Approval flows: 3-button keyboard (allow-once / allow-always / deny).
    - Update prompts: Yes/No keyboard for Encre update confirmations.

    Lifecycle:
        __init__ -> connect() -> _listen_loop() / _heartbeat_loop()
        _on_message() -> handle_message() -> send() -> disconnect()

    Thread safety:
        All async methods run within the asyncio event loop. The token
        refresh uses ``asyncio.Lock`` to prevent concurrent token requests
        (singleflight pattern).

    Attributes:
        SUPPORTS_MESSAGE_EDITING: Always False -- QQ Bot API does not support
            editing sent messages.
        MAX_MESSAGE_LENGTH: Maximum characters per message chunk (from constants).
        _TYPING_INPUT_SECONDS: Duration of typing indicator (60s).
        _TYPING_DEBOUNCE_SECONDS: Minimum interval between typing indicators (50s).
    """

    # QQ Bot API does not support editing sent messages.
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    _TYPING_INPUT_SECONDS = 60  # input_notify duration reported to QQ
    _TYPING_DEBOUNCE_SECONDS = 50  # refresh before it expires

    @property
    def _log_tag(self) -> str:
        """Log prefix including app_id for multi-instance disambiguation."""
        app_id = getattr(self, "_app_id", None)
        if app_id:
            return f"QQBot:{app_id}"
        return "QQBot"

    def _fail_pending(self, reason: str) -> None:
        """Fail all pending response futures."""
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending_responses.clear()

    def _mark_transport_disconnected(self) -> None:
        """Mark QQ WS down without stopping the reconnect loop.

        BasePlatformAdapter uses _running for both process lifecycle and
        connection status. QQBot needs to keep the listener task alive across
        transient transport drops so it can continue reconnect attempts after a
        short-lived gateway or network failure.
        """
        if self.has_fatal_error:
            return
        self._write_runtime_status_safe(
            "disconnected",
            platform_state="disconnected",
            error_code=None,
            error_message=None,
        )

    @property
    def is_connected(self) -> bool:
        """Return True only when the QQ WebSocket transport is usable."""
        return bool(self._running and self._ws and not self._ws.closed)

    def __init__(self, config: PlatformConfig):
        """Initialize the QQ Bot adapter with platform configuration.

        Extracts credentials, ACL policies, and initializes all connection
        state variables. Does NOT connect -- that happens in ``connect()``.

        Args:
            config: Platform configuration containing app_id, client_secret,
                dm_policy, group_policy, STT settings, etc.

        State initialized here:
            - Credentials (app_id, client_secret) from config or env vars.
            - Auth/ACL policies (dm_policy, group_policy, allowlists).
            - Connection objects (ws, session, http_client) set to None.
            - Token cache (access_token, token_expires_at, token_lock).
            - Message dedup window and chat type mapping.
            - Interaction callback (defaults to _default_interaction_dispatch).
        """
        super().__init__(config, Platform.QQ)

        # Extract application credentials from config.extra or environment.
        extra = config.extra or {}
        self._app_id = str(extra.get("app_id") or os.getenv("QQ_APP_ID", "")).strip()
        self._client_secret = str(
            extra.get("client_secret") or os.getenv("QQ_CLIENT_SECRET", "")
        ).strip()

        # --- Auth/ACL policies ---
        # DM access control: "disabled" | "allowlist" | "pairing" | "open"
        self._dm_policy = str(extra.get("dm_policy", "pairing")).strip().lower()
        self._allow_from = _coerce_list(
            extra.get("allow_from") or extra.get("allowFrom")
        )
        # Group access control: same policy options as DM.
        self._group_policy = str(extra.get("group_policy", "pairing")).strip().lower()
        self._group_allow_from = _coerce_list(
            extra.get("group_allow_from") or extra.get("groupAllowFrom")
        )

        # --- Connection state ---
        # aiohttp session for WebSocket; httpx async client for REST API.
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        # Background tasks for event listening and heartbeat sending.
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        # Heartbeat interval received from server via Hello event (default 30s).
        self._heartbeat_interval: float = 30.0  # seconds, updated by Hello
        # Session state for resume after reconnect (set on READY event).
        self._session_id: Optional[str] = None
        # Latest sequence number from gateway events (for resume).
        self._last_seq: Optional[int] = None
        # Maps chat_id -> "c2c"|"group"|"guild"|"dm" based on last inbound msg.
        self._chat_type_map: Dict[str, str] = {}  # chat_id -> "c2c"|"group"|"guild"|"dm"

        # --- Request/response correlation ---
        # Pending HTTP requests waiting for a matching WS event response.
        self._pending_responses: Dict[str, asyncio.Future] = {}
        # Message dedup window: msg_id -> timestamp (pruned on each check).
        self._seen_messages: Dict[str, float] = {}
        # Content-based dedup (short window, separate from msg_id dedup).
        self._seen_content: Dict[str, float] = {}
        self._content_dedup_lock: asyncio.Lock = asyncio.Lock()

        # --- Typing indicator state ---
        # Last inbound message ID per chat -- used by send_typing to reference.
        self._last_msg_id: Dict[str, str] = {}
        # Typing debounce: chat_id -> last send_typing timestamp.
        self._typing_sent_at: Dict[str, float] = {}

        # --- Token cache ---
        # OAuth2 access token and its expiry time (Unix timestamp).
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        # Lock to prevent concurrent token refresh requests (singleflight).
        self._token_lock = asyncio.Lock()
        # Lock for message dedup (prevents concurrent tasks from all passing the check).
        self._dedup_lock = asyncio.Lock()

        # --- Upload cache ---
        # Content hash -> {file_info, file_uuid, expires_at} for deduplicating
        # uploads of the same file within its expiry window.
        self._upload_cache: Dict[str, Dict[str, Any]] = {}

        # --- QR bind state ---
        # task_id -> AES key generated at bind-task creation; the poll step
        # needs it to decrypt the bot secret returned by the server.
        self._qr_keys: Dict[str, str] = {}

        # --- Inline-keyboard interaction routing ---
        # Callback invoked for every INTERACTION_CREATE event after ACK.
        # Callers register via set_interaction_callback().
        self._interaction_callback: Optional[
            Callable[[InteractionEvent], Awaitable[None]]
        ] = None

        # Default interaction dispatcher: routes approval-button clicks to
        # tools.approval.resolve_gateway_approval() and update-prompt clicks
        # to <data_dir>/.update_response. Set here so the cross-adapter gateway
        # contract (send_exec_approval / send_update_prompt) works out of the
        # box; callers can override with set_interaction_callback(None) or
        # register a custom handler.
        self._interaction_callback = self._default_interaction_dispatch

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Adapter identifier reported to the gateway registry.

        ``GatewayRunner`` uses this string to look up credentials and
        config.extra sections, so it must match the platform key in
        config.yaml exactly.
        """
        return "QQBot"

    @property
    def enforces_own_access_policy(self) -> bool:
        """QQBot gates DM/group access at intake via dm_policy/group_policy."""
        return True

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Authenticate, obtain gateway URL, and open the WebSocket.

        Four-step connection sequence:
            1. Validate dependencies (aiohttp, httpx) and credentials.
            2. Acquire OAuth2 access token via ``_ensure_token()``.
            3. Fetch WebSocket gateway URL via ``_get_gateway_url()``.
            4. Open WS connection via ``_open_ws()`` and start background tasks.

        Args:
            is_reconnect: False on cold boot; True when the reconnect watcher
                re-establishes this platform after an outage. QQBot has no
                server-side update queue so this flag is accepted for interface
                conformance only.

        Returns:
            True if connected successfully, False otherwise (fatal error set).
        """
        # Step 0: Validate credentials before connecting.
        # aiohttp and httpx are imported at module level -- ImportError on startup if missing.
        if not self._app_id or not self._client_secret:
            message = "QQ startup failed: QQ_APP_ID and QQ_CLIENT_SECRET are required"
            self._set_fatal_error("qq_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self._log_tag, message)
            return False

        try:
            # Initialize HTTP client for REST API calls (token fetch, gateway URL).
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
            )

            # Step 1: Obtain OAuth2 access token (cached, refreshed if expired).
            await self._ensure_token()

            # Step 2: Fetch WebSocket gateway URL from REST API.
            gateway_url = await self._get_gateway_url()
            logger.info("[%s] Gateway URL: %s", self._log_tag, gateway_url)

            # Step 3: Open persistent WebSocket connection.
            await self._open_ws(gateway_url)

            # Step 4: Start background tasks for event listening and heartbeat.
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._mark_connected()
            logger.info("[%s] Connected", self._log_tag)
            return True
        except Exception as exc:
            message = f"QQ startup failed: {exc}"
            self._set_fatal_error("qq_connect_error", message, retryable=True)
            logger.error("[%s] %s", self._log_tag, message, exc_info=True)
            await self._cleanup()
            return False

    async def disconnect(self) -> None:
        """Close all connections and stop listeners."""
        self._running = False
        self._mark_disconnected()

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        await self._cleanup()
        logger.info("[%s] Disconnected", self._log_tag)

    async def _cleanup(self) -> None:
        """Close WebSocket, HTTP session, and client."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        # Fail pending
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(RuntimeError("Disconnected"))
        self._pending_responses.clear()

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed (with singleflight).

        Uses double-checked locking: checks cache before acquiring the lock,
        then re-checks after acquiring to avoid race conditions during
        concurrent refresh attempts. Token is refreshed 60 seconds before
        actual expiry to prevent edge-case failures.

        Returns:
            Valid access token string.

        Raises:
            RuntimeError: If the OAuth2 token endpoint returns an error.
        """
        # Fast path: return cached token if still valid (with 60s safety margin).
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        # Acquire lock to prevent concurrent token refreshes (singleflight).
        async with self._token_lock:
            # Double-check after acquiring lock -- another coroutine may have refreshed.
            if self._access_token and time.time() < self._token_expires_at - 60:
                return self._access_token

            # POST to QQ OAuth2 token endpoint with app credentials.
            try:
                resp = await self._http_client.post(
                    TOKEN_URL,
                    json={"appId": self._app_id, "clientSecret": self._client_secret},
                    timeout=DEFAULT_API_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                raise RuntimeError(f"Failed to get QQ Bot access token: {exc}") from exc

            token = data.get("access_token")
            if not token:
                raise RuntimeError(
                    f"QQ Bot token response missing access_token: {data}"
                )

            # Cache the token with its expiry time.
            expires_in = int(data.get("expires_in", 7200))
            self._access_token = token
            self._token_expires_at = time.time() + expires_in
            logger.info(
                "[%s] Access token refreshed, expires in %ds", self._log_tag, expires_in
            )
            return self._access_token

    async def _get_gateway_url(self) -> str:
        """Fetch the WebSocket gateway URL from the REST API.

        Requires a valid access token (obtained via ``_ensure_token()``).
        The gateway URL is long-lived but may change across sessions.

        Returns:
            WebSocket gateway URL string.

        Raises:
            RuntimeError: If the API call fails or response is missing url.
        """
        token = await self._ensure_token()
        try:
            resp = await self._http_client.get(
                f"{API_BASE}{GATEWAY_URL_PATH}",
                headers={
                    "Authorization": f"QQBot {token}",
                    "User-Agent": build_user_agent(),
                },
                timeout=DEFAULT_API_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Failed to get QQ Bot gateway URL: {exc}") from exc

        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ Bot gateway response missing url: {data}")
        return url

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def _open_ws(self, gateway_url: str) -> None:
        """Open a WebSocket connection to the QQ Bot gateway.

        Cleans up any existing WS/session resources before opening a new
        connection. Respects WSS_PROXY / HTTPS_PROXY environment variables
        for environments behind a corporate proxy.

        Args:
            gateway_url: WebSocket URL from the REST API.

        Raises:
            aiohttp.ClientConnectorError: If the connection cannot be established.
        """
        # Close existing WebSocket and session resources first.
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

        # Honor WSL proxy env for QQ WebSocket.
        self._session = aiohttp.ClientSession(trust_env=True)
        ws_proxy = (
            os.getenv("WSS_PROXY")
            or os.getenv("wss_proxy")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("ALL_PROXY")
            or os.getenv("all_proxy")
        )
        self._ws = await self._session.ws_connect(
            gateway_url,
            headers={
                "User-Agent": build_user_agent(),
            },
            timeout=CONNECT_TIMEOUT_SECONDS,
            proxy=ws_proxy,
        )
        logger.info("[%s] WebSocket connected to %s", self._log_tag, gateway_url)

    async def _listen_loop(self) -> None:
        """Read WebSocket events and reconnect on errors.

        This is the main event loop -- runs until ``_running`` is False or a
        fatal error occurs. Handles all QQ Bot close codes according to the
        platform specification:

        Fatal codes (stop reconnecting):
            4001: Invalid opcode
            4002: Invalid payload
            4010: Invalid shard configuration
            4011: Sharding required but not configured
            4012: Invalid API version
            4013: Invalid intent specified
            4014: Intent not authorized for this bot
            4914: Bot offline / sandbox-only mode
            4915: Bot has been banned

        Recoverable codes:
            4004: Invalid token -> clear cached token, refresh, reconnect
            4006/4007/4900-4913: Session invalid -> clear session, re-identify
            4008: Rate limited -> back off 60s, then reconnect
            Other exceptions -> exponential backoff reconnect

        Quick disconnect detection:
            If the connection drops within QUICK_DISCONNECT_THRESHOLD seconds
            more than MAX_QUICK_DISCONNECT_COUNT times in a row, it likely
            indicates a misconfiguration (wrong credentials, missing permissions).
        """
        backoff_idx = 0
        connect_time = 0.0
        quick_disconnect_count = 0

        while self._running:
            try:
                connect_time = time.monotonic()
                await self._read_events()
                # Reset backoff on successful read cycle completion.
                backoff_idx = 0
                quick_disconnect_count = 0
            except asyncio.CancelledError:
                return
            except QQCloseError as exc:
                if not self._running:
                    return

                code = exc.code
                logger.warning(
                    "[%s] WebSocket closed: code=%s reason=%s",
                    self._log_tag,
                    code,
                    exc.reason,
                )

                # Quick disconnect detection (permission issues, misconfiguration).
                duration = time.monotonic() - connect_time
                if duration < QUICK_DISCONNECT_THRESHOLD and connect_time > 0:
                    quick_disconnect_count += 1
                    logger.info(
                        "[%s] Quick disconnect (%.1fs), count: %d",
                        self._log_tag,
                        duration,
                        quick_disconnect_count,
                    )
                    if quick_disconnect_count >= MAX_QUICK_DISCONNECT_COUNT:
                        logger.error(
                            "[%s] Too many quick disconnects. "
                            "Check: 1) AppID/Secret correct 2) Bot permissions on QQ Open Platform",
                            self._log_tag,
                        )
                        self._set_fatal_error(
                            "qq_quick_disconnect",
                            "Too many quick disconnects 鈥?check bot permissions",
                            retryable=True,
                        )
                        return
                else:
                    quick_disconnect_count = 0

                self._mark_transport_disconnected()
                self._fail_pending("Connection closed")

                # Stop reconnecting for fatal codes (unrecoverable errors).
                if code in {
                        4001,  # Invalid opcode
                        4002,  # Invalid payload
                        4010,  # Invalid shard
                        4011,  # Sharding required
                        4012,  # Invalid API version
                        4013,  # Invalid intent
                        4014,  # Intent not authorized
                        4914,  # Offline/sandbox-only
                        4915,  # Banned
                }:
                    fatal_descriptions = {
                        4001: "invalid opcode",
                        4002: "invalid payload",
                        4010: "invalid shard",
                        4011: "sharding required",
                        4012: "invalid API version",
                        4013: "invalid intent",
                        4014: "intent not authorized",
                        4914: "offline/sandbox-only",
                        4915: "banned",
                    }
                    desc = fatal_descriptions.get(code, f"fatal error (code={code})")
                    logger.error(
                        "[%s] Bot is %s. Check QQ Open Platform.", self._log_tag, desc
                    )
                    self._set_fatal_error(
                        f"qq_{desc}", f"Bot is {desc}", retryable=False
                    )
                    return

                # Rate limited -- wait and attempt reconnect with backoff.
                if code == 4008:
                    logger.info(
                        "[%s] Rate limited (4008), waiting %ds",
                        self._log_tag,
                        RATE_LIMIT_DELAY,
                    )
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        self._mark_disconnected()
                        return
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                    if await self._reconnect(backoff_idx):
                        backoff_idx = 0
                        quick_disconnect_count = 0
                    else:
                        backoff_idx += 1
                    continue

                # Token invalid -> clear cached token so _ensure_token() refreshes.
                if code == 4004:
                    logger.info(
                        "[%s] Invalid token (4004), will refresh and reconnect",
                        self._log_tag,
                    )
                    self._access_token = None
                    self._token_expires_at = 0.0

                # Session invalid -> clear session, will re-identify on next Hello.
                # Note: 4009 (connection timeout) is NOT included here -- it is
                # resumable per the QQ protocol and should preserve session state.
                if code in {
                        4006,
                        4007,
                        4900,
                        4901,
                        4902,
                        4903,
                        4904,
                        4905,
                        4906,
                        4907,
                        4908,
                        4909,
                        4910,
                        4911,
                        4912,
                        4913,
                }:
                    logger.info(
                        "[%s] Session error (%d), clearing session for re-identify",
                        self._log_tag,
                        code,
                    )
                    self._session_id = None
                    self._last_seq = None

                if await self._reconnect(backoff_idx):
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        logger.error("[%s] Max reconnect attempts reached (QQCloseError)", self._log_tag)
                        self._mark_disconnected()
                        return

            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self._log_tag, exc)
                self._mark_transport_disconnected()
                self._fail_pending("Connection interrupted")

                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("[%s] Max reconnect attempts reached", self._log_tag)
                    self._mark_disconnected()
                    return

                if await self._reconnect(backoff_idx):
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1

    async def _reconnect(self, backoff_idx: int) -> bool:
        """Attempt to reconnect the WebSocket. Returns True on success."""
        delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
        logger.info(
            "[%s] Reconnecting in %ds (attempt %d)...",
            self._log_tag,
            delay,
            backoff_idx + 1,
        )
        await asyncio.sleep(delay)

        self._heartbeat_interval = 30.0  # reset until Hello
        try:
            await self._ensure_token()
            gateway_url = await self._get_gateway_url()
            await self._open_ws(gateway_url)
            self._mark_connected()
            logger.info("[%s] Reconnected", self._log_tag)
            return True
        except Exception as exc:
            logger.warning("[%s] Reconnect failed: %s", self._log_tag, exc)
            return False

    async def _read_events(self) -> None:
        """Read WebSocket frames until connection closes.

        Processes incoming frames by type:
            TEXT -> parse JSON -> dispatch via ``_dispatch_payload``
            PING -> aiohttp auto-replies with PONG (no action needed)
            CLOSE -> raise ``QQCloseError`` with code/reason for reconnect
            CLOSED/ERROR -> raise ``RuntimeError`` to trigger reconnect

        Raises:
            RuntimeError: If the WebSocket is not connected or already closed.
                This prevents a 100% CPU spin loop when the WS is closed but
                non-None (the while-condition would be false on entry).
            QQCloseError: On CLOSE frame -- handled by ``_listen_loop``.
        """
        if not self._ws:
            raise RuntimeError("WebSocket not connected")
        if self._ws.closed:
            # A closed-but-non-None ws makes the while-condition false on entry,
            # so this would return normally -- which _listen_loop treats as a
            # clean read and immediately retries with backoff reset to 0,
            # producing a 100% CPU spin. Raise so the reconnect/backoff path runs.
            raise RuntimeError("WebSocket closed")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    self._dispatch_payload(payload)
            elif msg.type in {aiohttp.WSMsgType.PING,}:
                # aiohttp auto-replies with PONG
                pass
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                raise QQCloseError(msg.data, msg.extra)
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed")

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats (QQ Gateway expects op 1 heartbeat with latest seq).

        The interval is set from the Hello (op 10) event's heartbeat_interval.
        QQ's default is ~41s; we send at 80% of the interval to stay safe.
        Heartbeats include the last known sequence number (``_last_seq``) or
        null if no messages have been received yet.

        This loop runs until ``_running`` is False or the task is cancelled.
        Connection errors are silently caught -- the reconnect loop handles recovery.
        """
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    # d should be the latest sequence number received, or null
                    await self._ws.send_json({"op": 1, "d": self._last_seq})
                except Exception as exc:
                    logger.debug("[%s] Heartbeat failed: %s", self._log_tag, exc)
        except asyncio.CancelledError:
            pass

    async def _send_identify(self) -> None:
        """Send op 2 Identify to authenticate the WebSocket connection.

        After receiving op 10 Hello, the client must send op 2 Identify with
        the bot token and intents. On success the server replies with a
        READY dispatch event.

        Reference: https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/reference.html
        """
        token = await self._ensure_token()
        identify_payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": (1 << 25)
                           | (1 << 30)
                           | (1 << 12)
                           | (1 << 26),  # C2C_GROUP_AT_MESSAGES + PUBLIC_GUILD_MESSAGES + DIRECT_MESSAGE + INTERACTION
                "shard": [0, 1],
                "properties": {
                    "$os": "macOS",
                "$browser": "QQBotAdapter",
                "$device": "QQBotAdapter",
                },
            },
        }
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(identify_payload)
                logger.info("[%s] Identify sent", self._log_tag)
            else:
                logger.warning(
                    "[%s] Cannot send Identify: WebSocket not connected", self._log_tag
                )
        except Exception as exc:
            logger.error("[%s] Failed to send Identify: %s", self._log_tag, exc)

    async def _send_resume(self) -> None:
        """Send op 6 Resume to re-authenticate after a reconnection.

        Reference: https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/reference.html
        """
        token = await self._ensure_token()
        resume_payload = {
            "op": 6,
            "d": {
                "token": f"QQBot {token}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        }
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(resume_payload)
                logger.info(
                    "[%s] Resume sent (session_id=%s, seq=%s)",
                    self._log_tag,
                    self._session_id,
                    self._last_seq,
                )
            else:
                logger.warning(
                    "[%s] Cannot send Resume: WebSocket not connected", self._log_tag
                )
        except Exception as exc:
            logger.error("[%s] Failed to send Resume: %s", self._log_tag, exc)
            # If resume fails, clear session and fall back to identify on next Hello
            self._session_id = None
            self._last_seq = None

    @staticmethod
    def _create_task(coro):
        """Schedule a coroutine, silently skipping if no event loop is running.

        This avoids ``RuntimeError: no running event loop`` when tests call
        ``_dispatch_payload`` synchronously outside of ``asyncio.run()``.
        """
        try:
            loop = asyncio.get_running_loop()
            return loop.create_task(coro)
        except RuntimeError:
            return None

    def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Route inbound WebSocket payloads (dispatch synchronously, spawn async handlers).

        QQ Bot WebSocket uses an op/code-based protocol similar to Discord:

        Operations:
            0 = Dispatch (inbound event) -- ``t`` field indicates event type.
            1 = Heartbeat (outbound) -- sent by ``_heartbeat_loop``.
            2 = Identify (outbound) -- authenticate new connection (``_send_identify``).
            6 = Resume (outbound) -- re-authenticate after reconnect (``_send_resume``).
            7 = Server Reconnect (outbound) -- close WS, reconnect with resume.
            9 = Invalid Session (outbound) -- session is invalid (True=resumable).
            10 = Hello (inbound) -- server heartbeat interval; triggers Identify/Resume.
            11 = Heartbeat ACK (inbound) -- server confirms heartbeat received.

        Sequence numbers (``s`` field):
            Monotonically increasing per gateway session. Used for resume after
            reconnect -- the client sends the last known seq in the Resume payload.

        Args:
            payload: Parsed JSON dict from a TEXT WebSocket frame.
        """
        op = payload.get("op")
        t = payload.get("t")
        s = payload.get("s")
        d = payload.get("d")
        # Track sequence number -- used for resume after reconnect.
        if isinstance(s, int) and (self._last_seq is None or s > self._last_seq):
            self._last_seq = s

        # op 10 = Hello (heartbeat interval) -- must reply with Identify/Resume.
        if op == 10:
            d_data = d if isinstance(d, dict) else {}
            interval_ms = d_data.get("heartbeat_interval", 30000)
            # Send heartbeats at 80% of the server interval to stay safe.
            self._heartbeat_interval = interval_ms / 1000.0 * 0.8
            logger.debug(
                "[%s] Hello received, heartbeat_interval=%dms (sending every %.1fs)",
                self._log_tag,
                interval_ms,
                self._heartbeat_interval,
            )
            # Authenticate: send Resume if we have a session, else Identify.
            # Use _create_task which is safe when no event loop is running (tests).
            if self._session_id and self._last_seq is not None:
                self._create_task(self._send_resume())
            else:
                self._create_task(self._send_identify())
            return

        # op 0 = Dispatch -- route by event type (``t`` field).
        if op == 0 and t:
            if t == "READY":
                self._handle_ready(d)
            elif t == "RESUMED":
                logger.info("[%s] Session resumed", self._log_tag)
            elif t in {
                    "C2C_MESSAGE_CREATE",
                    "GROUP_AT_MESSAGE_CREATE",
                    "DIRECT_MESSAGE_CREATE",
                    "GUILD_MESSAGE_CREATE",
                    "GUILD_AT_MESSAGE_CREATE",
            }:
                # Message events are handled asynchronously to avoid blocking the WS read loop.
                asyncio.create_task(self._on_message(t, d))
            elif t == "INTERACTION_CREATE":
                # Interaction events (button clicks) are handled asynchronously.
                self._create_task(self._on_interaction(d))
            else:
                logger.debug("[%s] Unhandled dispatch: %s", self._log_tag, t)
            return

        # op 11 = Heartbeat ACK -- server confirms our heartbeat was received.
        if op == 11:
            return

        # op 7 = Server Reconnect -- server asks client to reconnect (e.g.
        # load-balancing, maintenance). Close the WS so _read_events raises
        # and the outer loop triggers a reconnect with Resume.
        if op == 7:
            logger.info("[%s] Server requested reconnect (op 7)", self._log_tag)
            if self._ws and not self._ws.closed:
                self._create_task(self._ws.close())
            return

        # op 9 = Invalid Session -- d=True means session is resumable,
        # d=False means we must re-identify from scratch.
        if op == 9:
            resumable = bool(d) if d is not None else False
            if not resumable:
                logger.info(
                    "[%s] Invalid session (op 9, not resumable), clearing session",
                    self._log_tag,
                )
                self._session_id = None
                self._last_seq = None
            else:
                logger.info("[%s] Invalid session (op 9, resumable)", self._log_tag)
            if self._ws and not self._ws.closed:
                self._create_task(self._ws.close())
            return

        logger.debug("[%s] Unknown op: %s", self._log_tag, op)

    def _handle_ready(self, d: Any) -> None:
        """Handle the READY event -- store session_id for resume."""
        if isinstance(d, dict):
            self._session_id = d.get("session_id")
            logger.info("[%s] Ready, session_id=%s", self._log_tag, self._session_id)

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        """Decode a raw QQ gateway frame into a dict, tolerating bad frames.

        Frames arriving from the WS are already JSON text, but a truncated
        or garbage frame must not take down the listener loop -- returning
        None lets the caller drop the frame silently after a warning.

        Args:
            raw: Frame payload as received from the socket.

        Returns:
            Parsed dict, or None when the payload is not valid JSON.
        """
        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("[QQBot] Failed to parse JSON: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _next_msg_seq(msg_id: str) -> int:
        """Generate a message sequence number in 0..65535 range."""
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    # ------------------------------------------------------------------
    # Inbound message handling
    # ------------------------------------------------------------------

    async def handle_message(self, event: MessageEvent) -> None:
        """Cache the last message ID per chat, then delegate to base.

        The cached ``_last_msg_id`` is used by:
            - ``send_typing()`` to reference the originating message.
            - ``send_exec_approval()`` for reply-to context.
            - ``send_update_prompt()`` for reply-to context.

        Args:
            event: The processed message event from a handler method.
        """
        if event.message_id and event.source.chat_id:
            self._last_msg_id[event.source.chat_id] = event.message_id
        await super().handle_message(event)

    async def _on_message(self, event_type: str, d: Any) -> None:
        """Process an inbound QQ Bot message event.

        Extracts common fields (msg_id, content, author, timestamp), checks
        for duplicates, then dispatches to the appropriate per-type handler
        based on the event type string.

        Args:
            event_type: One of "C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE",
                "DIRECT_MESSAGE_CREATE", "GUILD_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE".
            d: Raw event data dict from the WebSocket dispatch payload.
        """
        if not isinstance(d, dict):
            return

        msg_id = str(d.get("id", ""))
        content = str(d.get("content", "")).strip()
        author = d.get("author") if isinstance(d.get("author"), dict) else {}
        author_id = str(author.get("id", "")) if author else ""
        timestamp = str(d.get("timestamp", ""))

        # Primary dedup by msg_id.
        if msg_id and await self._is_duplicate(msg_id):
            logger.debug("[%s] Duplicate msg_id: %s", self._log_tag, msg_id)
            return

        # Secondary dedup by content fingerprint (QQ Bot may dispatch the same
        # message under multiple event types with different IDs).
        # Uses a short window (5s) to avoid filtering legitimate repeats.
        if author_id and content:
            fingerprint = f"{author_id}:{content}:{timestamp[:16]}"
            if await self._is_content_duplicate(fingerprint):
                logger.debug(
                    "[%s] Duplicate content from %s: %s", self._log_tag, author_id, content[:50]
                )
                return

        # Route by event type to the appropriate handler.
        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c_message(d, msg_id, content, author, timestamp)
        elif event_type in {"GROUP_AT_MESSAGE_CREATE",}:
            await self._handle_group_message(d, msg_id, content, author, timestamp)
        elif event_type in {"GUILD_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE"}:
            await self._handle_guild_message(d, msg_id, content, author, timestamp)
        elif event_type == "DIRECT_MESSAGE_CREATE":
            await self._handle_dm_message(d, msg_id, content, author, timestamp)

    # ------------------------------------------------------------------
    # Inline-keyboard interactions (INTERACTION_CREATE)
    # ------------------------------------------------------------------

    def set_interaction_callback(
        self,
        callback: Optional[Callable[[InteractionEvent], Awaitable[None]]],
    ) -> None:
        """Register (or clear) the interaction callback.

        Invoked once per ``INTERACTION_CREATE`` event *after* the adapter has
        ACKed the interaction. The callback is responsible for routing the
        button click to the right subsystem (approval resolver, update-prompt
        resolver, etc.) based on the ``button_data`` payload.
        """
        self._interaction_callback = callback

    async def _on_interaction(self, d: Any) -> None:
        """Handle an ``INTERACTION_CREATE`` event.

        Responsibilities:

        1. Parse the raw payload into an :class:`InteractionEvent`.
        2. ACK the interaction (``PUT /interactions/{id}``) so the client
           stops showing a loading indicator on the button.
        3. Dispatch to the registered interaction callback, if any.
        """
        if not isinstance(d, dict):
            return
        try:
            event = parse_interaction_event(d)
        except Exception as exc:
            logger.warning(
                "[%s] Failed to parse INTERACTION_CREATE: %s", self._log_tag, exc
            )
            return

        if not event.id:
            logger.warning(
                "[%s] INTERACTION_CREATE missing id, skipping ACK", self._log_tag
            )
            return

        # ACK the interaction promptly -- per the QQ docs the client will show
        # an error icon on the button if we don't respond quickly.
        try:
            await self._acknowledge_interaction(event.id)
        except Exception as exc:
            logger.warning(
                "[%s] Failed to ACK interaction %s: %s",
                self._log_tag, event.id, exc,
            )

        logger.info(
            "[%s] Interaction: scene=%s button_data=%r operator=%s",
            self._log_tag, event.scene, event.button_data, event.operator_openid,
        )

        callback = self._interaction_callback
        if callback is None:
            logger.debug(
                "[%s] No interaction callback registered; dropping button "
                "click %r",
                self._log_tag, event.button_data,
            )
            return
        try:
            await callback(event)
        except Exception as exc:
            logger.error(
                "[%s] Interaction callback raised: %s",
                self._log_tag, exc, exc_info=True,
            )

    async def _acknowledge_interaction(
            self,
            interaction_id: str,
            code: int = 0,
    ) -> None:
        """ACK a button interaction via ``PUT /interactions/{id}``.

        :param interaction_id: The ``id`` field from the
            ``INTERACTION_CREATE`` event.
        :param code: Response code (``0`` = success).
        """
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized 鈥?not connected?")
        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": build_user_agent(),
        }
        resp = await self._http_client.put(
            f"{API_BASE}/interactions/{interaction_id}",
            headers=headers,
            json={"code": code},
            timeout=DEFAULT_API_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Interaction ACK failed [{resp.status_code}]: "
                f"{resp.text[:200]}"
            )

    # Mapping from QQ keyboard button decisions -> the ``choice`` vocabulary
    # accepted by ``tools.approval.resolve_gateway_approval``. QQ's 3-button
    # layout (mobile-space constraint) collapses "session" and "always" into
    # a single "always" button; users wanting session-only approval can fall
    # back to the ``/approve session`` text command.
    _APPROVAL_BUTTON_TO_CHOICE = {
        "allow-once": "once",
        "allow-always": "always",
        "deny": "deny",
    }

    @staticmethod
    def _parse_gateway_session_key(session_key: str) -> Optional[Dict[str, str]]:
        """Parse ``agent:main:<platform>:<chat_type>:<chat_id>[:<user_id>]``."""
        parts = str(session_key or "").split(":")
        if len(parts) < 5 or parts[0] != "agent" or parts[1] != "main":
            return None
        parsed = {
            "platform": parts[2],
            "chat_type": parts[3],
            "chat_id": parts[4],
        }
        if len(parts) > 5:
            parsed["user_id"] = parts[5]
        return parsed

    def _is_authorized_interaction_for_session(
            self,
            event: InteractionEvent,
            session_key: str,
    ) -> bool:
        """Authorize approval/update interactions against session + operator."""
        parsed = self._parse_gateway_session_key(session_key)
        operator = str(event.operator_openid or "").strip()
        if not parsed or parsed.get("platform") != "qqbot" or not operator:
            return False

        chat_type = parsed.get("chat_type", "")
        chat_id = parsed.get("chat_id", "")
        if chat_type == "c2c":
            return bool(chat_id) and operator == chat_id

        if chat_type in {"group", "guild"}:
            event_chat = str(event.group_openid or event.guild_id or "").strip()
            if not event_chat or event_chat != chat_id:
                return False
            session_user = str(parsed.get("user_id", "")).strip()
            return bool(session_user) and operator == session_user

        return False

    async def _default_interaction_dispatch(
            self,
            event: InteractionEvent,
    ) -> None:
        """Route ``INTERACTION_CREATE`` button clicks to the right subsystem.

        - ``approve:<session_key>:<decision>`` ->
          :func:`tools.approval.resolve_gateway_approval`
          (unblocks the agent thread waiting on a dangerous-command approval).
        - ``update_prompt:<answer>`` ->
          writes the answer to ``<data_dir>/.update_response`` for the
          detached Encre update process to consume.
        - Anything else is logged at DEBUG and ignored.

        Installed as the adapter's default interaction callback in
        ``__init__``. Callers can replace via
        :meth:`set_interaction_callback` to route clicks elsewhere (or pass
        ``None`` to drop them entirely).
        """
        button_data = event.button_data
        if not button_data:
            return

        approval = parse_approval_button_data(button_data)
        if approval is not None:
            session_key, decision = approval
            choice = self._APPROVAL_BUTTON_TO_CHOICE.get(decision)
            if choice is None:
                logger.warning(
                    "[%s] Unknown approval decision %r (session=%s)",
                    self._log_tag, decision, session_key,
                )
                return
            if not self._is_authorized_interaction_for_session(event, session_key):
                logger.warning(
                    "[%s] Rejected unauthorized approval click for session %s "
                    "(operator=%s)",
                    self._log_tag, session_key, event.operator_openid,
                )
                return
            try:
                # Approval resolution via Encre's permission system
                # TODO: Implement gateway approval routing for inline keyboards
                logger.info(
                    "[%s] Button approval for session %s (choice=%s, operator=%s)",
                    self._log_tag, session_key, choice,
                    event.operator_openid,
                )
            except Exception as exc:
                logger.error(
                    "[%s] resolve_gateway_approval failed for session %s: %s",
                    self._log_tag, session_key, exc,
                )
            return

        update_answer = parse_update_prompt_button_data(button_data)
        if update_answer is not None:
            update_session_key = f"agent:main:qqbot:{event.scene}:{event.group_openid or event.guild_id or event.user_openid}"
            if not self._is_authorized_interaction_for_session(event, update_session_key):
                logger.warning(
                    "[%s] Rejected unauthorized update prompt click (operator=%s)",
                    self._log_tag, event.operator_openid,
                )
                return
            self._write_update_response(update_answer, event.operator_openid)
            return

        logger.debug(
            "[%s] Unrecognised button_data %r from interaction %s",
            self._log_tag, button_data, event.id,
        )

    @staticmethod
    def _write_update_response(answer: str, operator: str = "") -> None:
        """Atomically write the update-prompt answer to ``.update_response``.

        The detached Encre update watcher polls this file for a ``y``/``n``
        response to its interactive prompts (stash-restore, config migration).
        Writes via ``tmp + rename`` so a partial write can't fool the reader.
        """
        try:
            home = get_data_dir()
            response_path = home / ".update_response"
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(answer)
            tmp.replace(response_path)
            logger.info(
                "QQ update prompt answered %r by %s",
                answer, operator or "(unknown)",
            )
        except Exception as exc:
            logger.error("Failed to write update response: %s", exc)

    async def _handle_c2c_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """Handle a C2C (private) message event."""
        user_openid = str(author.get("user_openid", ""))
        if not user_openid:
            return
        if not self._is_dm_intake_allowed(user_openid):
            return

        text = content
        attachments_raw = d.get("attachments")
        logger.info(
            "[%s] C2C message: id=%s content=%r attachments=%s",
            self._log_tag,
            msg_id,
            content[:50] if content else "",
            (
                f"{len(attachments_raw) if isinstance(attachments_raw, list) else 0} items"
                if attachments_raw
                else "None"
            ),
        )
        if attachments_raw and isinstance(attachments_raw, list):
            for _i, _att in enumerate(attachments_raw):
                if isinstance(_att, dict):
                    logger.info(
                        "[%s] attachment[%d]: content_type=%s url=%s filename=%s",
                        self._log_tag,
                        _i,
                        _att.get("content_type", ""),
                        str(_att.get("url", ""))[:80],
                        _att.get("filename", ""),
                    )

        # Process all attachments uniformly (images, voice, files)
        att_result = await self._process_attachments(attachments_raw)
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        # Append voice transcripts to the text body
        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        # Append non-media attachment info
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        logger.info(
            "[%s] After processing: images=%d, voice=%d",
            self._log_tag,
            len(image_urls),
            len(voice_transcripts),
        )

        # Merge any quoted-message context (message_type=103 -> msg_elements[0]).
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[user_openid] = "c2c"
        event = MessageEvent(
            source=self.build_source(
                chat_id=user_openid,
                user_id=user_openid,
                chat_type="dm",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    async def _handle_group_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """Handle a group @-message event."""
        group_openid = str(d.get("group_openid", ""))
        if not group_openid:
            return
        if not self._is_group_allowed(
                group_openid, str(author.get("member_openid", ""))
        ):
            return

        # Strip the @bot mention prefix from content
        text = self._strip_at_mention(content)
        att_result = await self._process_attachments(d.get("attachments"))
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        # Append voice transcripts
        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        # Merge any quoted-message context (message_type=103 -> msg_elements[0]).
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[group_openid] = "group"
        event = MessageEvent(
            source=self.build_source(
                chat_id=group_openid,
                user_id=str(author.get("member_openid", "")),
                chat_type="group",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    async def _handle_guild_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """Handle a guild/channel message event."""
        channel_id = str(d.get("channel_id", ""))
        if not channel_id:
            return

        # Apply group_policy ACL -- guild channels are group-like contexts.
        # Without this check any member of any guild the bot is in could
        # bypass the configured allowlist.
        guild_id = str(d.get("guild_id", ""))
        author_id = str(author.get("id", ""))
        if not self._is_group_allowed(guild_id or channel_id, author_id):
            logger.debug(
                "[%s] Guild message blocked by ACL: channel=%s user=%s",
                self._log_tag, channel_id, author_id,
            )
            return

        member = d.get("member") if isinstance(d.get("member"), dict) else {}
        nick = str(member.get("nick", "")) or str(author.get("username", ""))

        text = content
        att_result = await self._process_attachments(d.get("attachments"))
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        # Merge any quoted-message context (message_type=103 -> msg_elements[0]).
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[channel_id] = "guild"
        event = MessageEvent(
            source=self.build_source(
                chat_id=channel_id,
                user_id=str(author.get("id", "")),
                user_name=nick or None,
                chat_type="group",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    async def _handle_dm_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """Handle a guild DM message event."""
        guild_id = str(d.get("guild_id", ""))
        if not guild_id:
            return

        # Apply dm_policy ACL -- guild DMs were previously unauthenticated.
        # Without this check any member of any guild the bot is in could
        # bypass the configured allowlist via direct messages.
        author_id = str(author.get("id", ""))
        if not self._is_dm_intake_allowed(author_id):
            logger.debug(
                "[%s] Guild DM blocked by ACL: guild=%s user=%s",
                self._log_tag, guild_id, author_id,
            )
            return

        text = content
        att_result = await self._process_attachments(d.get("attachments"))
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        # Merge any quoted-message context (message_type=103 -> msg_elements[0]).
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[guild_id] = "dm"
        event = MessageEvent(
            source=self.build_source(
                chat_id=guild_id,
                user_id=str(author.get("id", "")),
                chat_type="dm",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Quoted-message handling
    # ------------------------------------------------------------------

    async def _process_quoted_context(
            self,
            d: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Process the quoted message a user is replying to.

        When a user replies while quoting another message, the platform sets
        ``message_type = 103`` and pushes the referenced message's content and
        attachments inside ``msg_elements[0]``. The old adapter ignored
        ``msg_elements`` entirely, so:

        - Quoted text was surfaced only when the user typed something of
          their own -- bare quote-replies showed nothing.
        - Quoted attachments (images, voice, files) were never downloaded
          or described.
        - Quoted voice messages specifically produced no transcript, so the
          LLM had no way to see what the user was referring to.

        This method parses ``msg_elements`` and runs the quoted attachments
        through the same :meth:`_process_attachments` pipeline as the main
        message body, so quoted voice messages get STT transcripts and
        quoted images are cached identically.

        :param d: Raw inbound message dict (from the WS dispatch payload).
        :returns: Dict with keys:

            - ``quote_block``: string to prepend to the user's text body
              (empty when there's nothing quoted).
            - ``image_urls``: list of cached quoted-image paths.
            - ``image_media_types``: parallel list of image MIME types.
        """
        empty = {
            "quote_block": "",
            "image_urls": [],
            "image_media_types": [],
        }
        # Short-circuit: only message_type 103 indicates a quote.
        try:
            if int(d.get("message_type", 0) or 0) != 103:
                return empty
        except (TypeError, ValueError):
            return empty

        elements = d.get("msg_elements")
        if not isinstance(elements, list) or not elements:
            return empty

        # msg_elements[0] carries the referenced message. Additional elements
        # (if any) are very rare in practice; we concatenate their text and
        # union their attachments for completeness.
        quoted_text_parts: List[str] = []
        all_attachments: List[Dict[str, Any]] = []
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            etext = str(elem.get("content", "")).strip()
            if etext:
                quoted_text_parts.append(etext)
            eatts = elem.get("attachments")
            if isinstance(eatts, list):
                for a in eatts:
                    if isinstance(a, dict):
                        all_attachments.append(a)

        att_result = await self._process_attachments(all_attachments)
        quoted_voice = att_result.get("voice_transcripts") or []
        quoted_info = att_result.get("attachment_info") or ""
        quoted_images = att_result.get("image_urls") or []
        quoted_image_types = att_result.get("image_media_types") or []

        lines: List[str] = []
        if quoted_text_parts:
            lines.append(" ".join(quoted_text_parts))
        for t in quoted_voice:
            lines.append(t)
        if quoted_info:
            lines.append(quoted_info)

        if not lines and not quoted_images:
            return empty

        if lines:
            quote_block = "[Quoted message]:\n" + "\n".join(lines)
        else:
            # Images-only quote: give the LLM at least a marker so it knows
            # context was referenced.
            quote_block = "[Quoted message]: (image)"

        return {
            "quote_block": quote_block,
            "image_urls": quoted_images,
            "image_media_types": quoted_image_types,
        }

    @staticmethod
    def _merge_quote_into(text: str, quote_block: str) -> str:
        """Prepend ``quote_block`` to *text*, separated by a blank line."""
        if not quote_block:
            return text
        if text.strip():
            return f"{quote_block}\n\n{text}".strip()
        return quote_block

    # ------------------------------------------------------------------
    # Attachment processing
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_message_type(media_urls: list, media_types: list):
        """Determine MessageType from attachment content types."""
        if not media_urls:
            return MessageType.TEXT
        if not media_types:
            return MessageType.PHOTO
        first_type = media_types[0].lower() if media_types else ""
        if "audio" in first_type or "voice" in first_type or "silk" in first_type:
            return MessageType.VOICE
        if "video" in first_type:
            return MessageType.VIDEO
        if "image" in first_type or "photo" in first_type:
            return MessageType.PHOTO
        logger.debug(
            "Unknown media content_type '%s', defaulting to TEXT",
            first_type,
        )
        return MessageType.TEXT

    async def _process_attachments(
            self,
            attachments: Any,
    ) -> Dict[str, Any]:
        """Process inbound attachments (all message types).

        Handles images, voice, and
        other files uniformly.

        Returns a dict with:
        - image_urls: list[str]  -- cached local image paths
        - image_media_types: list[str] -- MIME types of cached images
        - voice_transcripts: list[str] -- STT transcripts for voice messages
        - attachment_info: str -- text description of non-image, non-voice attachments
        """
        if not isinstance(attachments, list):
            return {
                "image_urls": [],
                "image_media_types": [],
                "voice_transcripts": [],
                "attachment_info": "",
            }

        image_urls: List[str] = []
        image_media_types: List[str] = []
        voice_transcripts: List[str] = []
        other_attachments: List[str] = []

        for att in attachments:
            if not isinstance(att, dict):
                continue

            ct = str(att.get("content_type", "")).strip().lower()
            url_raw = str(att.get("url", "")).strip()
            filename = str(att.get("filename", ""))
            if url_raw.startswith("//"):
                url = f"https:{url_raw}"
            elif url_raw:
                url = url_raw
            else:
                url = ""
                continue

            logger.debug(
                "[%s] Processing attachment: content_type=%s, url=%s, filename=%s",
                self._log_tag,
                ct,
                url[:80],
                filename,
            )

            if self._is_voice_content_type(ct, filename):
                # Voice: use QQ's asr_refer_text first, then voice_wav_url, then STT.
                asr_refer = (
                    str(att.get("asr_refer_text", "")).strip()
                    if isinstance(att.get("asr_refer_text"), str)
                    else ""
                )
                voice_wav_url = (
                    str(att.get("voice_wav_url", "")).strip()
                    if isinstance(att.get("voice_wav_url"), str)
                    else ""
                )

                transcript = await self._stt_voice_attachment(
                    url,
                    ct,
                    filename,
                    asr_refer_text=asr_refer or None,
                    voice_wav_url=voice_wav_url or None,
                )
                if transcript:
                    voice_transcripts.append(f"[Voice] {transcript}")
                    logger.debug("[%s] Voice transcript: %s", self._log_tag, transcript)
                else:
                    logger.warning("[%s] Voice STT failed for %s", self._log_tag, url[:60])
                    voice_transcripts.append("[Voice] [STT failed]")
            elif ct.startswith("image/"):
                # Image: download and cache locally.
                try:
                    cached_path = await self._download_and_cache(url, ct, filename)
                    if cached_path and os.path.isfile(cached_path):
                        image_urls.append(cached_path)
                        image_media_types.append(ct or "image/jpeg")
                    elif cached_path:
                        logger.warning(
                            "[%s] Cached image path does not exist: %s",
                            self._log_tag,
                            cached_path,
                        )
                except Exception as exc:
                    logger.debug("[%s] Failed to cache image: %s", self._log_tag, exc)
            else:
                # Other attachments (video, file, etc.): download and record with path.
                try:
                    cached_path = await self._download_and_cache(url, ct, filename)
                    if cached_path:
                        name = filename or ct
                        if ct.startswith("video/"):
                            other_attachments.append(f"[video: {name} ({cached_path})]")
                        else:
                            other_attachments.append(f"[file: {name} ({cached_path})]")
                except Exception as exc:
                    logger.debug("[%s] Failed to cache attachment: %s", self._log_tag, exc)

        attachment_info = "\n".join(other_attachments) if other_attachments else ""
        return {
            "image_urls": image_urls,
            "image_media_types": image_media_types,
            "voice_transcripts": voice_transcripts,
            "attachment_info": attachment_info,
        }

    async def _download_and_cache(
            self, url: str, content_type: str, original_name: str = "",
    ) -> Optional[str]:
        """Download a URL and cache it locally.

        :param original_name: Preferred filename from attachment metadata.
            Falls back to the URL path basename if empty.
        """
        if not EncreSSRFGuard().validate_url(url):
            raise ValueError(f"Blocked unsafe URL: {url[:80]}")

        if not self._http_client:
            return None

        try:
            resp = await self._http_client.get(
                url,
                timeout=30.0,
                headers=self._qq_media_headers(),
            )
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:
            logger.debug(
                "[%s] Download failed for %s: %s", self._log_tag, url[:80], exc
            )
            return None

        if content_type.startswith("image/"):
            ext = mimetypes.guess_extension(content_type) or ".jpg"
            return _cache_image_from_bytes(data, ext)
        elif content_type == "voice" or content_type.startswith("audio/"):
            # QQ voice messages are typically .amr or .silk format.
            # Convert to .wav using ffmpeg so STT engines can process it.
            return await self._convert_audio_to_wav(data, url)
        else:
            filename = (
                original_name
                or Path(urlparse(url).path).name
                or "qq_attachment"
            )
            return _cache_document_from_bytes(data, filename)

    @staticmethod
    def _is_voice_content_type(content_type: str, filename: str) -> bool:
        """Check if an attachment is a voice/audio message."""
        ct = content_type.strip().lower()
        fn = filename.strip().lower()
        if ct == "voice" or ct.startswith("audio/"):
            return True
        _VOICE_EXTENSIONS = (
            ".silk",
            ".amr",
            ".mp3",
            ".wav",
            ".ogg",
            ".m4a",
            ".aac",
            ".speex",
            ".flac",
        )
        if any(fn.endswith(ext) for ext in _VOICE_EXTENSIONS):
            return True
        return False

    def _qq_media_headers(self) -> Dict[str, str]:
        """Return Authorization headers for QQ multimedia CDN downloads.

        QQ's multimedia URLs (multimedia.nt.qq.com.cn) require the bot's
        access token in an Authorization header, otherwise the download
        returns a non-200 status.
        """
        if self._access_token:
            return {"Authorization": f"QQBot {self._access_token}"}
        return {}

    async def _stt_voice_attachment(
            self,
            url: str,
            content_type: str,
            filename: str,
            *,
            asr_refer_text: Optional[str] = None,
            voice_wav_url: Optional[str] = None,
    ) -> Optional[str]:
        """Download a voice attachment, convert to wav, and transcribe.

        Priority:
        1. QQ's built-in ``asr_refer_text`` (Tencent's own ASR -- free, no API call).
        2. Self-hosted STT on ``voice_wav_url`` (pre-converted WAV from QQ, avoids SILK decoding).
        3. Self-hosted STT on the original attachment URL (requires SILK->WAV conversion).

        Returns the transcript text, or None on failure.
        """
        # 1. Use QQ's built-in ASR text if available
        if asr_refer_text:
            logger.debug(
                "[%s] STT: using QQ asr_refer_text: %r", self._log_tag, asr_refer_text[:100]
            )
            return asr_refer_text

        # Determine which URL to download (prefer voice_wav_url -- already WAV)
        download_url = url
        is_pre_wav = False
        if voice_wav_url:
            if voice_wav_url.startswith("//"):
                voice_wav_url = f"https:{voice_wav_url}"
            download_url = voice_wav_url
            is_pre_wav = True
            logger.debug("[%s] STT: using voice_wav_url (pre-converted WAV)", self._log_tag)

        if not EncreSSRFGuard().validate_url(download_url):
            logger.warning("[QQ] STT blocked unsafe URL: %s", download_url[:80])
            return None

        try:
            # 2. Download audio (QQ CDN requires Authorization header)
            if not self._http_client:
                logger.warning("[%s] STT: no HTTP client", self._log_tag)
                return None

            download_headers = self._qq_media_headers()
            logger.debug(
                "[%s] STT: downloading voice from %s (pre_wav=%s, headers=%s)",
                self._log_tag,
                download_url[:80],
                is_pre_wav,
                bool(download_headers),
            )
            resp = await self._http_client.get(
                download_url,
                timeout=30.0,
                headers=download_headers,
                follow_redirects=True,
            )
            resp.raise_for_status()
            audio_data = resp.content
            logger.debug(
                "[%s] STT: downloaded %d bytes, content_type=%s",
                self._log_tag,
                len(audio_data),
                resp.headers.get("content-type", "unknown"),
            )

            if len(audio_data) < 10:
                logger.warning(
                    "[%s] STT: downloaded data too small (%d bytes), skipping",
                    self._log_tag,
                    len(audio_data),
                )
                return None

            # 3. Convert to wav (skip if we already have a pre-converted WAV)
            if is_pre_wav:
                import tempfile

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(audio_data)
                    wav_path = tmp.name
                logger.debug(
                    "[%s] STT: using pre-converted WAV directly (%d bytes)",
                    self._log_tag,
                    len(audio_data),
                )
            else:
                logger.debug(
                    "[%s] STT: converting to wav, filename=%r", self._log_tag, filename
                )
                wav_path = await self._convert_audio_to_wav_file(audio_data, filename)
                if not wav_path or not Path(wav_path).exists():
                    logger.warning(
                        "[%s] STT: ffmpeg conversion produced no output", self._log_tag
                    )
                    return None

            # 4. Call STT API
            logger.debug("[%s] STT: calling ASR on %s", self._log_tag, wav_path)
            transcript = await self._call_stt(wav_path)

            # 5. Cleanup temp file
            try:
                os.unlink(wav_path)
            except OSError:
                pass

            if transcript:
                logger.debug("[%s] STT success: %r", self._log_tag, transcript[:100])
            else:
                logger.warning("[%s] STT: ASR returned empty transcript", self._log_tag)
            return transcript
        except (httpx.HTTPStatusError, httpx.TransportError, IOError) as exc:
            logger.warning(
                "[%s] STT failed for voice attachment: %s: %s",
                self._log_tag,
                type(exc).__name__,
                exc,
            )
            return None

    async def _convert_audio_to_wav_file(
            self, audio_data: bytes, filename: str
    ) -> Optional[str]:
        """Convert audio bytes to a temp .wav file using pilk (SILK) or ffmpeg.

        QQ voice messages are typically SILK format which ffmpeg cannot decode.
        Strategy: always try pilk first, fall back to ffmpeg if pilk fails.

        Returns the wav file path, or None on failure.
        """
        import tempfile

        ext = (
            Path(filename).suffix.lower()
            if Path(filename).suffix
            else self._guess_ext_from_data(audio_data)
        )
        logger.info(
            "[%s] STT: audio_data size=%d, ext=%r, first_20_bytes=%r",
            self._log_tag,
            len(audio_data),
            ext,
            audio_data[:20],
        )

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_src:
            tmp_src.write(audio_data)
            src_path = tmp_src.name

        wav_path = src_path.rsplit(".", 1)[0] + ".wav"

        # Try pilk first (handles SILK and many other formats)
        result = await self._convert_silk_to_wav(src_path, wav_path)

        # If pilk failed, try ffmpeg
        if not result:
            result = await self._convert_ffmpeg_to_wav(src_path, wav_path)

        # If ffmpeg also failed, try writing raw PCM as WAV (last resort)
        if not result:
            result = await self._convert_raw_to_wav(audio_data, wav_path)

        # Cleanup source file
        try:
            os.unlink(src_path)
        except OSError:
            pass

        return result

    @staticmethod
    def _guess_ext_from_data(data: bytes) -> str:
        """Guess file extension from magic bytes."""
        if data[:9] == b"#!SILK_V3" or data[:6] == b"#!SILK":
            return ".silk"
        if data[:2] == b"\x02!":
            return ".silk"
        if data[:4] == b"RIFF":
            return ".wav"
        if data[:4] == b"fLaC":
            return ".flac"
        if data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
            return ".mp3"
        if data[:4] == b"\x30\x26\xb2\x75" or data[:4] == b"\x4f\x67\x67\x53":
            return ".ogg"
        if data[:4] == b"\x00\x00\x00\x20" or data[:4] == b"\x00\x00\x00\x1c":
            return ".amr"
        # Default to .amr for unknown (QQ's most common voice format)
        return ".amr"

    @staticmethod
    def _looks_like_silk(data: bytes) -> bool:
        """Check if bytes look like a SILK audio file."""
        return data[:6] == b"#!SILK" or data[:2] == b"\x02!" or data[:9] == b"#!SILK_V3"

    async def _convert_silk_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        """Convert audio file to WAV using the pilk library.

        Tries the file as-is first, then as .silk if the extension differs.
        pilk can handle SILK files with various headers (or no header).
        """
        # Try converting the file as-is
        try:
            pilk.silk_to_wav(src_path, wav_path, rate=16000)
            if Path(wav_path).exists() and Path(wav_path).stat().st_size > 44:
                logger.debug(
                    "[%s] pilk converted %s to wav (%d bytes)",
                    self._log_tag,
                    Path(src_path).name,
                    Path(wav_path).stat().st_size,
                )
                return wav_path
        except Exception as exc:
            logger.debug("[%s] pilk direct conversion failed: %s", self._log_tag, exc)

        # Try renaming to .silk and converting (pilk checks the extension)
        silk_path = src_path.rsplit(".", 1)[0] + ".silk"
        try:
            import shutil

            shutil.copy2(src_path, silk_path)
            pilk.silk_to_wav(silk_path, wav_path, rate=16000)
            if Path(wav_path).exists() and Path(wav_path).stat().st_size > 44:
                logger.debug(
                    "[%s] pilk converted %s (as .silk) to wav (%d bytes)",
                    self._log_tag,
                    Path(src_path).name,
                    Path(wav_path).stat().st_size,
                )
                return wav_path
        except Exception as exc:
            logger.debug("[%s] pilk .silk conversion failed: %s", self._log_tag, exc)
        finally:
            try:
                os.unlink(silk_path)
            except OSError:
                pass

        return None

    async def _convert_raw_to_wav(self, audio_data: bytes, wav_path: str) -> Optional[str]:
        """Last resort: try writing audio data as raw PCM 16-bit mono 16kHz WAV.

        This will produce garbage if the data isn't raw PCM, but at least
        the ASR engine won't crash -- it'll just return empty.
        """
        try:
            import wave

            with wave.open(wav_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_data)
            return wav_path
        except Exception as exc:
            logger.debug("[%s] raw PCM fallback failed: %s", self._log_tag, exc)
            return None

    async def _convert_ffmpeg_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        """Convert audio file to WAV using ffmpeg."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode != 0:
                stderr = await proc.stderr.read() if proc.stderr else b""
                logger.warning(
                    "[%s] ffmpeg failed for %s: %s",
                    self._log_tag,
                    Path(src_path).name,
                    stderr[:200].decode(errors="replace"),
                )
                return None
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            logger.warning("[%s] ffmpeg conversion error: %s", self._log_tag, exc)
            return None

        if not Path(wav_path).exists() or Path(wav_path).stat().st_size <= 44:
            logger.warning(
                "[%s] ffmpeg produced no/small output for %s",
                self._log_tag,
                Path(src_path).name,
            )
            return None
        logger.debug(
            "[%s] ffmpeg converted %s to wav (%d bytes)",
            self._log_tag,
            Path(src_path).name,
            Path(wav_path).stat().st_size,
        )
        return wav_path

    def _resolve_stt_config(self) -> Optional[Dict[str, str]]:
        """Resolve STT backend configuration from config/environment.

        Priority order for STT configuration lookup:
            1. Plugin-specific: ``channels.qqbot.stt`` in config.yaml -> ``self.config.extra["stt"]``
            2. QQ-specific env vars: ``QQ_STT_API_KEY`` / ``QQ_STT_BASE_URL`` / ``QQ_STT_MODEL``
            3. Return None if nothing is configured (STT will be skipped, QQ built-in ASR still works).

        Returns:
            Dict with keys "base_url", "api_key", "model" if configured, else None.
        """
        extra = self.config.extra or {}

        # 1. Plugin-specific STT config: ``stt`` in config.extra
        stt_cfg = extra.get("stt")
        if isinstance(stt_cfg, dict) and stt_cfg.get("enabled") is not False:
            base_url = stt_cfg.get("baseUrl") or stt_cfg.get("base_url", "")
            api_key = stt_cfg.get("apiKey") or stt_cfg.get("api_key", "")
            model = stt_cfg.get("model", "")
            if base_url and api_key:
                return {
                    "base_url": base_url.rstrip("/"),
                    "api_key": api_key,
                    "model": model or "whisper-1",
                }
            # Provider-only config: just model name, use default provider
            if api_key:
                provider = stt_cfg.get("provider", "zai")
                # Map provider name to base URL
                _PROVIDER_BASE_URLS = {
                    "zai": "https://open.bigmodel.cn/api/coding/paas/v4",
                    "openai": "https://api.openai.com/v1",
                    "glm": "https://open.bigmodel.cn/api/coding/paas/v4",
                }
                base_url = _PROVIDER_BASE_URLS.get(provider, "")
                if base_url:
                    return {
                        "base_url": base_url,
                        "api_key": api_key,
                        "model": model
                                 or ("glm-asr" if provider in {"zai", "glm"} else "whisper-1"),
                    }

        # 2. QQ-specific env vars (set by Encre gateway setup)
        qq_stt_key = os.getenv("QQ_STT_API_KEY", "")
        if qq_stt_key:
            base_url = os.getenv(
                "QQ_STT_BASE_URL",
                "https://open.bigmodel.cn/api/coding/paas/v4",
            )
            model = os.getenv("QQ_STT_MODEL", "glm-asr")
            return {
                "base_url": base_url.rstrip("/"),
                "api_key": qq_stt_key,
                "model": model,
            }

        return None

    async def _call_stt(self, wav_path: str) -> Optional[str]:
        """Call an OpenAI-compatible STT API to transcribe a wav file.

        Uses the provider configured in ``channels.qqbot.stt`` config,
        falling back to QQ's built-in ``asr_refer_text`` if not configured.
        Returns None if STT is not configured or the call fails.
        """
        stt_cfg = self._resolve_stt_config()
        if not stt_cfg:
            logger.warning(
                "[%s] STT not configured (no stt config or QQ_STT_API_KEY)",
                self._log_tag,
            )
            return None

        base_url = stt_cfg["base_url"]
        api_key = stt_cfg["api_key"]
        model = stt_cfg["model"]

        try:
            with open(wav_path, "rb") as f:
                resp = await self._http_client.post(
                    f"{base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (Path(wav_path).name, f, "audio/wav")},
                    data={"model": model},
                    timeout=30.0,
                )
            resp.raise_for_status()
            result = resp.json()
            # Zhipu/GLM format: {"choices": [{"message": {"content": "transcript text"}}]}
            choices = result.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content.strip():
                    return content.strip()
            # OpenAI/Whisper format: {"text": "transcript text"}
            text = result.get("text", "")
            if text.strip():
                return text.strip()
            return None
        except (httpx.HTTPStatusError, IOError) as exc:
            logger.warning(
                "[%s] STT API call failed (model=%s, base=%s): %s",
                self._log_tag,
                model,
                base_url[:50],
                exc,
            )
            return None

    async def _convert_audio_to_wav(
            self, audio_data: bytes, source_url: str
    ) -> Optional[str]:
        """Convert audio bytes to .wav using pilk (SILK) or ffmpeg, caching the result."""
        import tempfile

        # Determine source format from magic bytes or URL
        ext = (
            Path(urlparse(source_url).path).suffix.lower()
            if urlparse(source_url).path
            else ""
        )
        if not ext or ext not in {
                ".silk",
                ".amr",
                ".mp3",
                ".wav",
                ".ogg",
                ".m4a",
                ".aac",
                ".flac",
        }:
            ext = self._guess_ext_from_data(audio_data)

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_src:
            tmp_src.write(audio_data)
            src_path = tmp_src.name

        wav_path = src_path.rsplit(".", 1)[0] + ".wav"
        try:
            is_silk = ext == ".silk" or self._looks_like_silk(audio_data)
            if is_silk:
                result = await self._convert_silk_to_wav(src_path, wav_path)
            else:
                result = await self._convert_ffmpeg_to_wav(src_path, wav_path)

            if not result:
                logger.warning(
                    "[%s] audio conversion failed for %s (format=%s)",
                    self._log_tag,
                    source_url[:60],
                    ext,
                )
                return _cache_document_from_bytes(audio_data, f"qq_voice{ext}")
        except Exception:
            return _cache_document_from_bytes(audio_data, f"qq_voice{ext}")
        finally:
            try:
                os.unlink(src_path)
            except OSError:
                pass

        # Verify output and cache
        try:
            wav_data = Path(wav_path).read_bytes()
            os.unlink(wav_path)
            return _cache_document_from_bytes(wav_data, "qq_voice.wav")
        except Exception as exc:
            logger.debug("[%s] Failed to read converted wav: %s", self._log_tag, exc)
            return None

    # ------------------------------------------------------------------
    # Outbound messaging -- REST API
    # ------------------------------------------------------------------

    async def _api_request(
            self,
            method: str,
            path: str,
            body: Optional[Dict[str, Any]] = None,
            timeout: float = DEFAULT_API_TIMEOUT,
    ) -> Dict[str, Any]:
        """Make an authenticated REST API request to QQ Bot API.

        Automatically includes the access token in the Authorization header
        and retries timeout errors. All paths are relative to API_BASE.

        Args:
            method: HTTP method ("GET", "POST", "PUT", "DELETE").
            path: API endpoint path (e.g., "/v2/users/{id}/messages").
            body: Optional JSON request body.
            timeout: Request timeout in seconds.

        Returns:
            Parsed JSON response dict.

        Raises:
            RuntimeError: If the API returns a non-2xx status or times out.
        """
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized 鈥?not connected?")

        # Ensure we have a valid token before making the request.
        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": build_user_agent(),
        }

        try:
            resp = await self._http_client.request(
                method,
                f"{API_BASE}{path}",
                headers=headers,
                json=body,
                timeout=timeout,
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"QQ Bot API error [{resp.status_code}] {path}: "
                    f"{data.get('message', data)}"
                )
            return data
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"QQ Bot API timeout [{path}]: {exc}") from exc

    async def _upload_media(
            self,
            target_type: str,
            target_id: str,
            file_type: int,
            url: Optional[str] = None,
            file_data: Optional[str] = None,
            srv_send_msg: bool = False,
            file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload media via the QQ Bot REST API and return file_info.

        Supports two upload modes:
            - URL-based: platform fetches from ``url`` (requires accessible HTTP endpoint).
            - Data-based: inline base64 data in ``file_data`` (capped at ~10 MB).

        Retries transient failures (non-4xx/5xx errors) up to 3 times with
        exponential backoff. Permanent errors (4xx, timeout) are raised immediately.

        Args:
            target_type: "c2c" for private chat, "group" for group chat.
            target_id: User openid or group_openid.
            file_type: Media type constant (MEDIA_TYPE_IMAGE, VIDEO, VOICE, FILE).
            url: HTTP(S) URL of the media file.
            file_data: Base64-encoded media data (alternative to url).
            srv_send_msg: Whether to send as a server message.
            file_name: Display name for file uploads (MEDIA_TYPE_FILE only).

        Returns:
            API response dict containing ``file_info`` token.

        Raises:
            RuntimeError: On permanent API errors or all retries exhausted.
        """
        path = (
            f"/v2/users/{target_id}/files"
            if target_type == "c2c"
            else f"/v2/groups/{target_id}/files"
        )

        body: Dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": srv_send_msg,
        }
        if url:
            body["url"] = url
        elif file_data:
            body["file_data"] = file_data
        if file_type == MEDIA_TYPE_FILE and file_name:
            body["file_name"] = file_name

        # Retry transient upload failures (up to 3 attempts).
        for attempt in range(3):
            try:
                return await self._api_request(
                    "POST", path, body, timeout=FILE_UPLOAD_TIMEOUT
                )
            except RuntimeError as exc:
                err_msg = str(exc)
                # Permanent errors -- don't retry (4xx, auth, invalid, timeout).
                if any(
                        kw in err_msg
                        for kw in ("400", "401", "Invalid", "timeout", "Timeout")
                ):
                    raise
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                else:
                    raise

    # Maximum time (seconds) to wait for reconnection before giving up on send.
    _RECONNECT_WAIT_SECONDS = 15.0
    # How often (seconds) to poll is_connected while waiting.
    _RECONNECT_POLL_INTERVAL = 0.5

    async def _wait_for_reconnection(self) -> bool:
        """Wait for the WebSocket listener to reconnect.

        The listener loop (_listen_loop) auto-reconnects on disconnect, but
        there is a race window where send() is called right after a disconnect
        and before the reconnect completes.  This method polls is_connected
        for up to _RECONNECT_WAIT_SECONDS (15s) with _RECONNECT_POLL_INTERVAL (0.5s) steps.

        Returns:
            True if reconnected within the timeout, False otherwise.
        """
        logger.info("[%s] Not connected 鈥?waiting for reconnection (up to %.0fs)",
                    self._log_tag, self._RECONNECT_WAIT_SECONDS)
        waited = 0.0
        while waited < self._RECONNECT_WAIT_SECONDS:
            await asyncio.sleep(self._RECONNECT_POLL_INTERVAL)
            waited += self._RECONNECT_POLL_INTERVAL
            if self.is_connected:
                logger.info("[%s] Reconnected after %.1fs", self._log_tag, waited)
                return True
        logger.warning("[%s] Still not connected after %.0fs", self._log_tag, self._RECONNECT_WAIT_SECONDS)
        return False

    async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text or markdown message to a QQ user or group.

        Processing pipeline:
            1. Check connection status -- wait up to 15s for reconnection.
            2. Format content via ``format_message()`` (plain text with markdown syntax).
            3. Truncate to ``MAX_MESSAGE_LENGTH`` and split into chunks.
            4. Send each chunk sequentially via ``_send_chunk()`` with retry.
            5. Only the first chunk uses ``reply_to``; subsequent chunks are standalone.

        Args:
            chat_id: Target chat ID (openid for C2C, group_openid for groups).
            content: Message text content (may contain markdown formatting).
            reply_to: Message ID to reply to (only applied to first chunk).
            metadata: Unused by QQ adapter (kept for interface compatibility).

        Returns:
            SendResult with success status and assigned message ID.
        """
        del metadata

        # Wait for reconnection if WebSocket is down (up to 15 seconds).
        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(success=False, error="Not connected", retryable=True)

        # Skip empty messages -- treat as success (no-op).
        if not content or not content.strip():
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        last_result = SendResult(success=False, error="No chunks")
        for chunk in chunks:
            last_result = await self._send_chunk(chat_id, chunk, reply_to)
            if not last_result.success:
                return last_result
            # Only reply_to the first chunk
            reply_to = None
        return last_result

    async def _send_chunk(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a single chunk with retry + exponential backoff.

        Guesses the chat type from ``_chat_type_map``, then dispatches to the
        appropriate send method (C2C / group / guild). Retries transient errors
        up to 3 times with exponential backoff (1s, 2s, 4s). Permanent errors
        (invalid, forbidden, not found) are not retried.

        Args:
            chat_id: Target chat ID.
            content: Message text (single chunk, already truncated).
            reply_to: Message ID to reply to.

        Returns:
            SendResult with success status and assigned message ID.
        """
        last_exc: Optional[Exception] = None
        chat_type = self._guess_chat_type(chat_id)

        for attempt in range(3):
            try:
                if chat_type == "c2c":
                    return await self._send_c2c_text(chat_id, content, reply_to)
                elif chat_type == "group":
                    return await self._send_group_text(chat_id, content, reply_to)
                elif chat_type == "guild":
                    return await self._send_guild_text(chat_id, content, reply_to)
                else:
                    return SendResult(
                        success=False, error=f"Unknown chat type for {chat_id}"
                    )
            except Exception as exc:
                last_exc = exc
                err = str(exc).lower()
                # Permanent errors -- don't retry (invalid, forbidden, not found, bad request).
                if any(
                        k in err
                        for k in ("invalid", "forbidden", "not found", "bad request")
                ):
                    break
                # Transient -- back off and retry with exponential delay.
                if attempt < 2:
                    delay = 1.0 * (2 ** attempt)
                    logger.warning(
                        "[%s] send retry %d/3 after %.1fs: %s",
                        self._log_tag,
                        attempt + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        error_msg = str(last_exc) if last_exc else "Unknown error"
        logger.error("[%s] Send failed: %s", self._log_tag, error_msg)
        retryable = not any(
            k in error_msg.lower() for k in ("invalid", "forbidden", "not found")
        )
        return SendResult(success=False, error=error_msg, retryable=retryable)

    async def _send_c2c_text(
            self,
            openid: str,
            content: str,
            reply_to: Optional[str] = None,
            keyboard: Optional[InlineKeyboard] = None,
    ) -> SendResult:
        """Send text to a C2C user via REST API.

        :param keyboard: Optional inline keyboard attached to the message.
        """
        self._next_msg_seq(reply_to or openid)
        body = self._build_text_body(content, reply_to)
        if reply_to:
            body["msg_id"] = reply_to
        if keyboard is not None:
            body["keyboard"] = keyboard.to_dict()

        data = await self._api_request("POST", f"/v2/users/{openid}/messages", body)
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw=data)

    async def _send_group_text(
            self,
            group_openid: str,
            content: str,
            reply_to: Optional[str] = None,
            keyboard: Optional[InlineKeyboard] = None,
    ) -> SendResult:
        """Send text to a group via REST API.

        :param keyboard: Optional inline keyboard attached to the message.
        """
        self._next_msg_seq(reply_to or group_openid)
        body = self._build_text_body(content, reply_to)
        if reply_to:
            body["msg_id"] = reply_to
        if keyboard is not None:
            body["keyboard"] = keyboard.to_dict()

        data = await self._api_request(
            "POST", f"/v2/groups/{group_openid}/messages", body
        )
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw=data)

    async def _send_guild_text(
            self, channel_id: str, content: str, reply_to: Optional[str] = None
    ) -> SendResult:
        """Send text to a guild channel via REST API."""
        body: Dict[str, Any] = {"content": content[: self.MAX_MESSAGE_LENGTH]}
        if reply_to:
            body["msg_id"] = reply_to

        data = await self._api_request("POST", f"/channels/{channel_id}/messages", body)
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw=data)

    # ------------------------------------------------------------------
    # Inline-keyboard outbound helpers (approval / update-prompt flows)
    # ------------------------------------------------------------------

    async def send_with_keyboard(
            self,
            chat_id: str,
            content: str,
            keyboard: InlineKeyboard,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a single text message with an inline keyboard attached.

        Unlike :meth:`send`, this does NOT split long content into chunks --
        a keyboard message has exactly one interactive surface, and splitting
        would orphan the buttons from the first chunk. Callers should keep
        approval/update-prompt bodies short.

        Guild (channel) chats don't support inline keyboards; returns a
        non-retryable failure for those.
        """
        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(
                    success=False, error="Not connected", retryable=True
                )

        chat_type = self._guess_chat_type(chat_id)
        formatted = self.format_message(content)
        truncated = formatted[: self.MAX_MESSAGE_LENGTH]
        try:
            if chat_type == "c2c":
                return await self._send_c2c_text(
                    chat_id, truncated, reply_to, keyboard=keyboard,
                )
            if chat_type == "group":
                return await self._send_group_text(
                    chat_id, truncated, reply_to, keyboard=keyboard,
                )
            return SendResult(
                success=False,
                error=(
                    f"Inline keyboards not supported for chat_type "
                    f"{chat_type!r}"
                ),
                retryable=False,
            )
        except Exception as exc:
            logger.error(
                "[%s] send_with_keyboard failed: %s", self._log_tag, exc
            )
            return SendResult(success=False, error=str(exc))

    async def send_approval_request(
            self,
            chat_id: str,
            req: ApprovalRequest,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send a 3-button approval request (``allow-once / allow-always / deny``).

        The rendered text comes from :func:`build_approval_text`; callers can
        override by passing a custom :class:`ApprovalRequest`.

        Users click the button -> ``INTERACTION_CREATE`` fires -> the adapter's
        registered :meth:`set_interaction_callback` handler decodes
        ``button_data`` via :func:`parse_approval_button_data`.
        """
        from .keyboards import build_approval_text
        return await self.send_with_keyboard(
            chat_id,
            build_approval_text(req),
            build_approval_keyboard(
                req.session_key,
                allow_permanent=getattr(req, "allow_permanent", True),
            ),
            reply_to=reply_to,
        )

    # ------------------------------------------------------------------
    # Cross-adapter gateway contract -- send_exec_approval + send_update_prompt
    # ------------------------------------------------------------------
    #
    # These mirror the signatures that gateway/run.py detects on the adapter
    # class (e.g. type(adapter).send_exec_approval, type(adapter).send_update_prompt)
    # for button-based approval / update-confirm UX. Discord, Telegram, Slack,
    # Matrix, and Feishu already implement the same contract.

    async def send_exec_approval(
            self,
            chat_id: str,
            command: str,
            session_key: str,
            description: str = "dangerous command",
            metadata: Optional[Dict[str, Any]] = None,
        allow_permanent: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """Send a button-based exec-approval prompt for a dangerous command.

        Called by ``gateway/run.py``'s ``_approval_notify_sync`` when the
        agent is blocked waiting for approval. Button clicks resolve via
        :func:`tools.approval.resolve_gateway_approval` -- dispatched by the
        adapter's interaction callback (:meth:`_default_interaction_dispatch`).
        """
        del metadata  # QQ doesn't have thread_id / DM targeting overrides.
        if smart_denied:
            description += " Owner override applies to this one operation only."

        # Use the reply-to message for passive-message context when we have one.
        # QQ requires a msg_id on outbound messages to a user we've never
        # seen; the last inbound msg_id is the natural choice.
        msg_id = self._last_msg_id.get(chat_id)

        req = ApprovalRequest(
            session_key=session_key,
            title="Execute this command?",
            description=description,
            command_preview=command,
            timeout_sec=self._APPROVAL_TIMEOUT_SECONDS,
            allow_permanent=allow_permanent and not smart_denied,
        )
        return await self.send_approval_request(
            chat_id, req, reply_to=msg_id,
        )

    _APPROVAL_TIMEOUT_SECONDS = 300  # matches gateway's default gateway_timeout

    async def send_update_prompt(
            self,
            chat_id: str,
            prompt: str,
            default: str = "",
            session_key: str = "",
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Yes/No update-confirmation prompt with inline buttons.

        Matches the cross-adapter contract used by
        ``gateway/run.py``'s Encre update watcher. Button
        clicks surface as ``INTERACTION_CREATE`` with
        ``button_data = 'update_prompt:y'`` or ``'update_prompt:n'``;
        the adapter's interaction callback writes the answer to
        ``<data_dir>/.update_response`` so the detached update process
        can read it.
        """
        del session_key, metadata  # present for contract parity only.

        default_hint = f" (default: {default})" if default else ""
        content = f"鈿?**Update Needs Your Input**\n\n{prompt}{default_hint}"
        msg_id = self._last_msg_id.get(chat_id)
        return await self.send_with_keyboard(
            chat_id,
            content,
            build_update_prompt_keyboard(),
            reply_to=msg_id,
        )

    def _build_text_body(
            self, content: str, reply_to: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build the message body for C2C/group text sending.

        Always sends as plain text (MSG_TYPE_TEXT) to avoid platform
        rendering issues -- content carries markdown formatting in the
        plain-text body for downstream display (e.g. watch rendering).
        """
        msg_seq = self._next_msg_seq(reply_to or "default")

        body: Dict[str, Any] = {
            "content": content[: self.MAX_MESSAGE_LENGTH],
            "msg_type": MSG_TYPE_TEXT,
            "msg_seq": msg_seq,
        }

        if reply_to:
            body["message_reference"] = {"message_id": reply_to}

        return body

    # ------------------------------------------------------------------
    # Native media sending
    # ------------------------------------------------------------------

    async def send_image(
            self,
            chat_id: str,
            image_url: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively via QQ Bot API upload.

        Attempts native media upload first; if that fails and the source is
        a URL, falls back to sending the URL as plain text.

        Args:
            chat_id: Target chat ID.
            image_url: Local file path or HTTP(S) URL of the image.
            caption: Optional caption text.
            reply_to: Message ID to reply to.
            metadata: Unused (kept for interface compatibility).

        Returns:
            SendResult -- success if native upload works, or fallback text sent.
        """
        del metadata

        result = await self._send_media(
            chat_id, image_url, MEDIA_TYPE_IMAGE, "image", caption, reply_to
        )
        # If native upload succeeded or source is not a URL, return result directly.
        if result.success or not self._is_url(image_url):
            return result

        # Fallback to text URL when native upload fails.
        logger.warning(
            "[%s] Image send failed, falling back to text: %s",
            self._log_tag,
            result.error,
        )
        fallback = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=fallback, reply_to=reply_to)

    async def send_image_file(
            self,
            chat_id: str,
            image_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a local image file natively via QQ Bot API.

        Args:
            chat_id: Target chat ID.
            image_path: Local filesystem path to the image file.
            caption: Optional caption text.
            reply_to: Message ID to reply to.
            **kwargs: Unused (kept for interface compatibility).

        Returns:
            SendResult with success status and assigned message ID.
        """
        del kwargs
        return await self._send_media(
            chat_id, image_path, MEDIA_TYPE_IMAGE, "image", caption, reply_to
        )

    async def send_voice(
            self,
            chat_id: str,
            audio_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a voice message natively via QQ Bot API.

        Args:
            chat_id: Target chat ID.
            audio_path: Local filesystem path to the audio file (.silk, .amr, etc.).
            caption: Optional caption text.
            reply_to: Message ID to reply to.
            **kwargs: Unused (kept for interface compatibility).

        Returns:
            SendResult with success status and assigned message ID.
        """
        del kwargs
        return await self._send_media(
            chat_id, audio_path, MEDIA_TYPE_VOICE, "voice", caption, reply_to
        )

    async def send_video(
            self,
            chat_id: str,
            video_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a video natively via QQ Bot API.

        Args:
            chat_id: Target chat ID.
            video_path: Local filesystem path to the video file.
            caption: Optional caption text.
            reply_to: Message ID to reply to.
            **kwargs: Unused (kept for interface compatibility).

        Returns:
            SendResult with success status and assigned message ID.
        """
        del kwargs
        return await self._send_media(
            chat_id, video_path, MEDIA_TYPE_VIDEO, "video", caption, reply_to
        )

    async def send_document(
            self,
            chat_id: str,
            file_path: str,
            caption: Optional[str] = None,
            file_name: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """Send a file/document natively via QQ Bot API.

        Args:
            chat_id: Target chat ID.
            file_path: Local filesystem path to the file.
            caption: Optional caption text.
            file_name: Optional display name for the file.
            reply_to: Message ID to reply to.
            **kwargs: Unused (kept for interface compatibility).

        Returns:
            SendResult with success status and assigned message ID.
        """
        del kwargs
        return await self._send_media(
            chat_id,
            file_path,
            MEDIA_TYPE_FILE,
            "file",
            caption,
            reply_to,
            file_name=file_name,
        )

    async def _send_media(
            self,
            chat_id: str,
            media_source: str,
            file_type: int,
            kind: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            file_name: Optional[str] = None,
    ) -> SendResult:
        """Upload media and send as a native message.

        Upload strategy depends on the source type:

        - **HTTP(S) URLs** -> single ``POST /v2/{users|groups}/{id}/files``
          with ``url=...``. The QQ platform fetches the URL directly; fastest
          path when the source is already hosted.
        - **Local files** -> three-step chunked upload (prepare / PUT parts /
          complete). Handles files up to the platform's ~100 MB per-file
          limit without the ~10 MB inline-base64 cap of the old adapter.

        Args:
            chat_id: Target chat ID (openid or group_openid).
            media_source: Local file path or HTTP(S) URL.
            file_type: Media type constant (MEDIA_TYPE_IMAGE, VIDEO, VOICE, FILE).
            kind: Human-readable media kind label ("image", "video", etc.).
            caption: Optional text caption accompanying the media.
            reply_to: Message ID to reply to.
            file_name: Optional display name for documents.

        Returns:
            SendResult with success status, message ID, and raw API response.

        Special exceptions:
            UploadDailyLimitExceededError -- daily quota hit (non-retryable).
            UploadFileTooLargeError -- file exceeds platform per-file limit.
        """
        # Wait for reconnection if WebSocket is down.
        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(success=False, error="Not connected", retryable=True)

        # Guild channels don't support native media upload via this path.
        chat_type = self._guess_chat_type(chat_id)
        if chat_type == "guild":
            return SendResult(
                success=False,
                error="Guild media send not supported via this path",
            )

        try:
            if self._is_url(media_source):
                # URL upload -- let the platform fetch it directly.
                resolved_name = (
                    file_name
                    or Path(urlparse(media_source).path).name
                    or "media"
                )
                upload = await self._upload_media(
                    chat_type,
                    chat_id,
                    file_type,
                    url=media_source,
                    srv_send_msg=False,
                    file_name=resolved_name if file_type == MEDIA_TYPE_FILE else None,
                )
            else:
                # Local file -- chunked upload (prepare / PUT parts / complete).
                resolved_name, upload = await self._upload_local_file(
                    chat_type,
                    chat_id,
                    media_source,
                    file_type,
                    file_name,
                )

            file_info = upload.get("file_info") or (
                upload.get("data", {}) or {}
            ).get("file_info")
            if not file_info:
                return SendResult(
                    success=False,
                    error=f"Upload returned no file_info: {upload}",
                )

            # Build and send the RichMedia message with the uploaded file.
            msg_seq = self._next_msg_seq(chat_id)
            body: Dict[str, Any] = {
                "msg_type": MSG_TYPE_MEDIA,
                "media": {"file_info": file_info},
                "msg_seq": msg_seq,
            }
            if caption:
                body["content"] = caption[: self.MAX_MESSAGE_LENGTH]
            if reply_to:
                body["msg_id"] = reply_to

            send_data = await self._api_request(
                "POST",
                (
                    f"/v2/users/{chat_id}/messages"
                    if chat_type == "c2c"
                    else f"/v2/groups/{chat_id}/messages"
                ),
                body,
            )
            return SendResult(
                success=True,
                message_id=str(send_data.get("id", uuid.uuid4().hex[:12])),
                raw_response=send_data,
            )
        except UploadDailyLimitExceededError as exc:
            # Non-retryable: daily quota hit. Give the caller actionable text
            # so the model can compose a helpful reply.
            logger.warning(
                "[%s] Daily upload limit exceeded for %s (%s)",
                self._log_tag, exc.file_name, exc.file_size_human,
            )
            return SendResult(
                success=False,
                error=(
                    f"QQ daily upload limit exceeded for {exc.file_name!r} "
                    f"({exc.file_size_human}). Retry tomorrow."
                ),
                retryable=False,
            )
        except UploadFileTooLargeError as exc:
            logger.warning(
                "[%s] File too large: %s (%s, platform limit %s)",
                self._log_tag, exc.file_name, exc.file_size_human, exc.limit_human,
            )
            return SendResult(
                success=False,
                error=(
                    f"{exc.file_name!r} ({exc.file_size_human}) exceeds the "
                    f"QQ per-file upload limit ({exc.limit_human})."
                ),
                retryable=False,
            )
        except Exception as exc:
            logger.error("[%s] Media send failed: %s", self._log_tag, exc)
            return SendResult(success=False, error=str(exc))

    async def _upload_local_file(
            self,
            chat_type: str,
            chat_id: str,
            media_source: str,
            file_type: int,
            file_name: Optional[str],
    ) -> Tuple[str, Dict[str, Any]]:
        """Chunked-upload a local file and return ``(resolved_name, complete_response)``.

        The returned ``complete_response`` contains the ``file_info`` token
        that goes into the subsequent RichMedia message body.

        :raises UploadDailyLimitExceededError: On biz_code 40093002.
        :raises UploadFileTooLargeError: When the file exceeds the platform limit.
        :raises FileNotFoundError: If the path does not exist.
        :raises ValueError: If the path looks like a placeholder (``<path>``).
        :raises RuntimeError: If the HTTP client is not initialized.
        """
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized 鈥?not connected?")

        local_path = Path(media_source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()

        if not local_path.exists() or not local_path.is_file():
            if media_source.startswith("<") or len(media_source) < 3:
                raise ValueError(
                    f"Invalid media source (looks like a placeholder): {media_source!r}"
                )
            raise FileNotFoundError(f"Media file not found: {local_path}")

        resolved_name = file_name or local_path.name
        uploader = ChunkedUploader(
            api_request=self._api_request,
            http_put=self._http_client.put,
            log_tag=self._log_tag,
        )
        complete = await uploader.upload(
            chat_type=chat_type,
            target_id=chat_id,
            file_path=str(local_path),
            file_type=file_type,
            file_name=resolved_name,
        )
        return resolved_name, complete

    async def _load_media(
            self, source: str, file_name: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """Load media from URL or local path. Returns (base64_or_url, content_type, filename)."""
        source = str(source).strip()
        if not source:
            raise ValueError("Media source is required")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            # For URLs, pass through directly to the upload API
            content_type = mimetypes.guess_type(source)[0] or "application/octet-stream"
            resolved_name = file_name or Path(parsed.path).name or "media"
            return source, content_type, resolved_name

        # Local file -- encode as raw base64 for QQ Bot API file_data field.
        # The QQ API expects plain base64, NOT a data URI.
        local_path = Path(source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()

        if not local_path.exists() or not local_path.is_file():
            # Guard against placeholder paths like "<path>" that the LLM
            # sometimes emits instead of real file paths.
            if source.startswith("<") or len(source) < 3:
                raise ValueError(
                    f"Invalid media source (looks like a placeholder): {source!r}"
                )
            raise FileNotFoundError(f"Media file not found: {local_path}")

        raw = local_path.read_bytes()
        resolved_name = file_name or local_path.name
        content_type = (
                mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        )
        b64 = base64.b64encode(raw).decode("ascii")
        return b64, content_type, resolved_name

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send an input notify to a C2C user (only supported for C2C).

        Debounced to one request per ~50s (the API sets a 60s indicator).
        The QQ API requires the originating message ID -- retrieved from
        ``_last_msg_id`` which is populated by ``_on_message``.
        """
        if not self.is_connected:
            return

        chat_type = self._guess_chat_type(chat_id)
        if chat_type != "c2c":
            return

        msg_id = self._last_msg_id.get(chat_id)
        if not msg_id:
            return

        # Debounce -- skip if we sent recently
        now = time.time()
        last_sent = self._typing_sent_at.get(chat_id, 0.0)
        if now - last_sent < self._TYPING_DEBOUNCE_SECONDS:
            return

        try:
            msg_seq = self._next_msg_seq(chat_id)
            body = {
                "msg_type": MSG_TYPE_INPUT_NOTIFY,
                "msg_id": msg_id,
                "input_notify": {
                    "input_type": 1,
                    "input_second": self._TYPING_INPUT_SECONDS,
                },
                "msg_seq": msg_seq,
            }
            await self._api_request("POST", f"/v2/users/{chat_id}/messages", body)
            self._typing_sent_at[chat_id] = now
        except Exception as exc:
            logger.debug("[%s] send_typing failed: %s", self._log_tag, exc)

    # ------------------------------------------------------------------
    # Format
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        """Format message for QQ.

        Always returns content as-is with markdown formatting preserved
        in the text body. The message is sent as plain text (MSG_TYPE_TEXT),
        not QQ's rich markdown (MSG_TYPE_MARKDOWN), to avoid platform
        rendering issues on watches or when QQ Bot markdown is broken.
        """
        return content

    # ------------------------------------------------------------------
    # Chat info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return chat info based on chat type heuristics."""
        chat_type = self._guess_chat_type(chat_id)
        return {
            "name": chat_id,
            "type": "group" if chat_type in {"group", "guild"} else "dm",
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_url(source: str) -> bool:
        """Return True when ``source`` is an http(s) URL rather than a path.

        Outbound media handlers branch on this to decide between uploading a
        local file and letting the QQ platform fetch the resource itself.
        Accepts arbitrary types because callers pass config values that may
        be None or numbers.

        Args:
            source: File path, URL, or any value coercible to str.

        Returns:
            True only for schemes "http" and "https".
        """
        return urlparse(str(source)).scheme in {"http", "https"}

    def _guess_chat_type(self, chat_id: str) -> str:
        """Determine chat type from stored inbound metadata, fallback to 'c2c'."""
        if chat_id in self._chat_type_map:
            return self._chat_type_map[chat_id]
        return "c2c"

    @staticmethod
    def _strip_at_mention(content: str) -> str:
        """Strip the @bot mention prefix from group message content."""
        # QQ group @-messages may have the bot's QQ/ID as prefix
        import re

        stripped = re.sub(r"^@\S+\s*", "", content.strip())
        return stripped

    def _open_dm_opted_in(self) -> bool:
        """Whether an open-policy DM is actually permitted to talk to the bot.

        ``dm_policy == "open"`` is a permissive default, not an unconditional
        allow: QQ Bot servers require users to opt in before the bot may
        message them. These two env vars override that requirement for local
        development, where opting in through the QQ client is impractical.

        Returns:
            True when either GATEWAY_ALLOW_ALL_USERS or QQ_ALLOW_ALL_USERS is
            set to a truthy value ("true", "1", "yes"), else False.
        """
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}:
            return True
        return os.getenv("QQ_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}

    def _is_dm_allowed(self, user_id: str) -> bool:
        """Whether the adapter may reply to a DM from ``user_id``.

        Governs the outbound half of the policy: after a message is accepted
        for intake, send() still consults this before pushing a response back,
        so a policy change mid-session takes effect immediately.

        Args:
            user_id: QQ openid of the DM counterpart.

        Returns:
            True when the configured dm_policy permits replying. Pairing mode
            is intentionally excluded -- a paired user must have been
            confirmed first, which is enforced on the intake side.
        """
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._entry_matches(self._allow_from, user_id)
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, user_id: str) -> bool:
        """Whether an inbound DM from ``user_id`` should be processed at all.

        This is the intake gate, consulted before a message is even parsed.
        It deliberately permits pairing mode, since a first DM from an
        unpaired user is exactly what starts the pairing flow -- rejecting it
        here would make pairing unreachable.

        Args:
            user_id: QQ openid of the sender, possibly empty.

        Returns:
            False for blank ids and disabled policy; True in pairing mode;
            allowlist and open modes defer to their respective checks.
        """
        principal = str(user_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._entry_matches(self._allow_from, principal)
        if self._dm_policy == "pairing":
            return True
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_group_allowed(self, group_id: str, user_id: str) -> bool:
        """Whether a message from ``group_id`` is permitted under group_policy.

        Groups are authorized by group id, not by the individual member who
        spoke, so ``user_id`` is accepted for signature symmetry with the DM
        helpers but not consulted.

        Args:
            group_id: QQ group open_id of the originating group.
            user_id: openid of the member who sent the message.

        Returns:
            False for disabled policy and pairing mode -- group pairing is not
            a supported flow, so pairing groups must be added to the
            allowlist manually.
        """
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return self._entry_matches(self._group_allow_from, group_id)
        if self._group_policy == "pairing":
            return False
        if self._group_policy == "open":
            return True
        return False

    @staticmethod
    def _entry_matches(entries: List[str], target: str) -> bool:
        """Check an allowlist entry match, tolerating wildcard and case noise.

        Config files frequently carry stray whitespace and mixed case from
        copy/paste, so both sides are normalized before comparison. A single
        "*" entry grants access to everyone, which is the documented way to
        opt out of filtering without switching the whole policy mode.

        Args:
            entries: Allowlist values from config (allow_from /
                group_allow_from).
            target: The id to test against the list.

        Returns:
            True on a normalized equality match or a "*" wildcard entry.
        """
        normalized_target = str(target).strip().lower()
        for entry in entries:
            normalized = str(entry).strip().lower()
            if normalized == "*" or normalized == normalized_target:
                return True
        return False

    def _parse_qq_timestamp(self, raw: str) -> datetime:
        """Parse QQ API timestamp (ISO 8601 string or integer ms).

        The QQ API changed from integer milliseconds to ISO 8601 strings.
        This handles both formats gracefully.
        """
        if not raw:
            return datetime.now(tz=timezone.utc)
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            pass
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        except (ValueError, TypeError):
            pass
        return datetime.now(tz=timezone.utc)

    async def _is_duplicate(self, msg_id: str) -> bool:
        """Record and report a msg_id seen within the dedup window.

        QQ can redeliver the same message after a reconnect or resume, and
        duplicate deliveries would otherwise produce doubled replies. The
        dict is pruned whenever it grows past DEDUP_MAX_SIZE rather than on a
        timer, so memory stays bounded even when pruning is never triggered.

        Args:
            msg_id: QQ message id from the event payload.

        Returns:
            True when this id was already recorded (caller should drop the
            message), False the first time it is seen.
        """
        async with self._dedup_lock:
            now = time.time()
            if len(self._seen_messages) > DEDUP_MAX_SIZE:
                cutoff = now - DEDUP_WINDOW_SECONDS
                self._seen_messages = {
                    key: ts for key, ts in self._seen_messages.items() if ts > cutoff
                }
            if msg_id in self._seen_messages:
                return True
            self._seen_messages[msg_id] = now
            return False

    async def _is_content_duplicate(self, fingerprint: str) -> bool:
        """Content-based dedup with a short (5s) window.

        QQ Bot may dispatch the same message under multiple event types (C2C,
        DIRECT, GUILD) with different ``msg_id`` values.  This method catches
        those by fingerprinting ``author_id + content + timestamp``.
        """
        async with self._content_dedup_lock:
            now = time.time()
            cutoff = now - DEDUP_CONTENT_WINDOW_SECONDS
            # Prune stale entries
            self._seen_content = {k: v for k, v in self._seen_content.items() if v > cutoff}
            if fingerprint in self._seen_content:
                return True
            self._seen_content[fingerprint] = now
            return False

    # ------------------------------------------------------------------
    # QR-code bind (agent login)
    # ------------------------------------------------------------------

    @staticmethod
    def _decrypt_bind_secret(encrypted: str, key_b64: str) -> str:
        """Decrypt the bind task secret with the locally generated AES key.

        The server encrypts the bot secret with AES-256-GCM using the key the
        client generated at task creation. The wire format is
        ``iv(12) || ciphertext || auth_tag(16)``, all base64-encoded.

        Args:
            encrypted: Base64 payload returned as ``bot_encrypt_secret``.
            key_b64: Base64 32-byte key generated by :meth:`get_qrcode_url`.

        Returns:
            The plaintext bot secret.

        Raises:
            RuntimeError: When the ``cryptography`` package is unavailable.
            ValueError: When the key or payload has an unexpected size.
        """
        key = base64.b64decode(key_b64)
        data = base64.b64decode(encrypted)
        if len(key) != 32:
            raise ValueError("QQ bind key must be 32 bytes")
        if len(data) < 12 + 16:
            raise ValueError("QQ bind secret payload too short")
        iv, ciphertext, tag = data[:12], data[12:-16], data[-16:]
        return AESGCM(key).decrypt(iv, ciphertext + tag, None).decode("utf-8")

    async def get_qrcode_url(self) -> tuple[str, str]:
        """Create a bind task and render its connect URL as a QR image.

        Mirrors ``WeixinAdapter.get_qrcode_url``: the connect URL is not an
        image, so it is rendered into an SVG and embedded as a base64 data URI
        for display in a plain ``<img>`` tag. Without the optional ``qrcode``
        package the raw URL is returned and only a warning logged.

        Returns:
            A ``(display_url, task_id)`` pair; ``task_id`` is the opaque token
            later passed to :meth:`poll_qrcode_status`.

        Raises:
            RuntimeError: When the bind task cannot be created.
        """
        key = base64.b64encode(os.urandom(32)).decode("ascii")
        async with httpx.AsyncClient(
            timeout=QQ_BIND_TIMEOUT_SECONDS, follow_redirects=True,
        ) as client:
            resp = await client.post(
                f"{QQ_BIND_BASE_URL}{EP_CREATE_BIND_TASK}", json={"key": key},
            )
            resp.raise_for_status()
            payload = resp.json()
        if payload.get("retcode") != 0 or not payload.get("data", {}).get("task_id"):
            raise RuntimeError(
                f"create_bind_task failed: {payload.get('msg') or payload}"
            )
        task_id = str(payload["data"]["task_id"])
        # The poll step needs the key to decrypt the secret; keep it indexed
        # by task id (the same adapter instance serves both calls).
        self._qr_keys[task_id] = key
        url = QQ_BIND_CONNECT_URL.format(task_id=task_id)
        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(url)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        svg_data = img.to_string()
        if isinstance(svg_data, str):
            svg_data = svg_data.encode("utf-8")
        url = f"data:image/svg+xml;base64,{base64.b64encode(svg_data).decode('ascii')}"
        return url, task_id

    async def poll_qrcode_status(self, qrcode_token: str) -> dict | None:
        """Poll the bind task state and return credentials once completed.

        Mirrors ``WeixinAdapter.poll_qrcode_status``: a falsy result means
        "not finished yet" and the caller keeps polling until its deadline.

        Args:
            qrcode_token: Task id returned by :meth:`get_qrcode_url`.

        Returns:
            ``{"app_id", "app_secret", "user_openid"}`` when the bind task is
            COMPLETED, ``{"expired": True}`` when the task expired
            server-side, otherwise ``None`` (pending).
        """
        key = self._qr_keys.get(qrcode_token, "")
        if not key:
            return None
        async with httpx.AsyncClient(
            timeout=QQ_BIND_TIMEOUT_SECONDS, follow_redirects=True,
        ) as client:
            resp = await client.post(
                f"{QQ_BIND_BASE_URL}{EP_POLL_BIND_RESULT}",
                json={"task_id": qrcode_token},
            )
            resp.raise_for_status()
            payload = resp.json()
        if payload.get("retcode") != 0:
            logger.warning(
                "[%s] poll_bind_result failed: %s",
                self._log_tag,
                payload.get("msg"),
            )
            return None
        data = payload.get("data") or {}
        status = int(data.get("status") or QQ_BIND_STATUS_NONE)
        if status == QQ_BIND_STATUS_EXPIRED:
            self._qr_keys.pop(qrcode_token, None)
            return {"expired": True}
        if status != QQ_BIND_STATUS_COMPLETED:
            return None
        app_id = str(data.get("bot_appid") or "")
        if not app_id:
            return None
        app_secret = self._decrypt_bind_secret(
            str(data.get("bot_encrypt_secret") or ""), key,
        )
        self._qr_keys.pop(qrcode_token, None)
        return {
            "app_id": app_id,
            "app_secret": app_secret,
            "user_openid": str(data.get("user_openid") or ""),
        }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def check_qq_requirements() -> bool:
    """Check if QQ runtime dependencies are available.

    aiohttp and httpx are imported at module level, so ImportError on startup
    if missing. This function is kept for interface compatibility with the
    platform_registry.check_fn contract.
    """
    return True


def _validate_qq_config(config: PlatformConfig) -> bool:
    """Validate QQ Bot configuration."""
    extra = config.extra or {}
    app_id = str(extra.get("app_id") or os.getenv("QQ_APP_ID", "")).strip()
    client_secret = str(
        extra.get("client_secret") or os.getenv("QQ_CLIENT_SECRET", "")
    ).strip()
    return bool(app_id and client_secret)


# Register the QQBot platform adapter (always -- aiohttp/httpx are required deps).
# Registration moved to plugin.py: this adapter ships as the ea-connector-qqbot
# plugin and registers through the S8 gateway-adapter surface at activation,
# so the bridge (register_plugin_adapter) owns the PlatformEntry lifecycle.
