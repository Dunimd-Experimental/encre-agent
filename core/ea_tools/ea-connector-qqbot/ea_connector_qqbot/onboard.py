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

"""Synchronous scan-to-configure (QR code onboard) flow for QQ Bot.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module lets a first-time Encre operator obtain QQ Bot credentials
without ever touching the QQ Open Platform web console. A human scans a
QR code with the QQ mobile app; the portal binds their existing bot to the
session and hands the ``client_secret`` back encrypted. The encrypted
secret is decryptable only by the CLI process, because the AES-256 key is
generated locally and sent up front (see
:mod:`ea_connector_qqbot.crypto`).

Unlike the rest of the gateway layer, this module is deliberately
synchronous. It runs from an interactive terminal during setup, not inside
the asyncio gateway loop, so it uses blocking :mod:`httpx` client calls and
``print`` for operator-facing progress.

Flow executed by :func:`qr_register`::

    1. generate_bind_key()                         -> random AES-256 key
    2. POST /lite/create_bind_task {key}           -> task_id
    3. render QR code for the task URL             -> operator scans with QQ
    4. loop POST /lite/poll_bind_result {task_id}  -> status, appid, secret
    5. decrypt_secret(bot_encrypt_secret, key)     -> client_secret
    6. return {"app_id", "client_secret", "user_openid"}

Endpoints are resolved against :const:`PORTAL_HOST` (the ``q.qq.com``
portal), not against the REST API base. The QR URL is built from
:const:`QR_URL_TEMPLATE` and tagged ``source=encre`` so the portal can tell
Encre-initiated scans apart from other bot clients.

Public API:
    :func:`qr_register`
    :func:`build_connect_url`
    :class:`BindStatus`

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

import logging
import time
from enum import IntEnum
from typing import Optional, Tuple
from urllib.parse import quote

from .constants import (
    ONBOARD_API_TIMEOUT,
    ONBOARD_CREATE_PATH,
    ONBOARD_POLL_INTERVAL,
    ONBOARD_POLL_PATH,
    PORTAL_HOST,
    QR_URL_TEMPLATE,
)
from .crypto import decrypt_secret, generate_bind_key
from .utils import get_api_headers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BindStatus -- result codes from poll_bind_result
# ---------------------------------------------------------------------------

class BindStatus(IntEnum):
    """Result codes for the bind task reported by ``poll_bind_result``.

    Mirrors the ``status`` field of the portal's ``poll_bind_result``
    payload, which is an integer. It is an :class:`IntEnum` so the raw API
    integer can be coerced with :class:`BindStatus` directly and compared
    with ``==`` against named members.

    Lifecycle as observed while polling:

    * ``NONE`` -- the task exists but nobody has scanned yet, or the portal
      has no result to report. Keep polling.
    * ``PENDING`` -- a device scanned the code and is waiting for the user
      to confirm on the phone. Keep polling.
    * ``COMPLETED`` -- the user confirmed; ``bot_appid`` and
      ``bot_encrypt_secret`` are populated and ready to decrypt.
    * ``EXPIRED`` -- the task's validity window elapsed with no confirmed
      scan. A new task must be created before continuing.

    Attributes:
        NONE: No bind result yet; continue polling.
        PENDING: Scanned but not confirmed; continue polling.
        COMPLETED: Confirmed; credentials are available.
        EXPIRED: Task expired; start a fresh bind task.
    """

    NONE = 0
    PENDING = 1
    COMPLETED = 2
    EXPIRED = 3


# ---------------------------------------------------------------------------
# QR rendering -- terminal display helper
# ---------------------------------------------------------------------------

import qrcode as _qrcode_mod  # optional -- QR rendering falls back to URL if missing


def _render_qr(url: str) -> bool:
    """Attempt to print a scannable QR code for *url* to stdout.

    Renders an ASCII QR code via the optional ``qrcode`` distribution. The
    method is best-effort: any missing dependency, encoding failure, or
    renderer error returns ``False`` so the caller can degrade to printing
    the plain URL, which the operator can still open manually in QQ.

    Medium error correction (``ERROR_CORRECT_M``) is chosen because terminal
    rendering is lossy -- some characters are lost to line-wrapping and
    font substitution -- so the code needs headroom to stay scannable.

    Args:
        url: The target URL to encode; produced by :func:`build_connect_url`.

    Returns:
        ``True`` when the QR code was printed, ``False`` when the caller
        should fall back to the plain URL.
    """
    if _qrcode_mod is None:
        return False
    try:
        qr = _qrcode_mod.QRCode(
            error_correction=_qrcode_mod.constants.ERROR_CORRECT_M,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Synchronous HTTP helpers
# ---------------------------------------------------------------------------

def _create_bind_task(timeout: float = ONBOARD_API_TIMEOUT) -> Tuple[str, str]:
    """Create a bind task and return ``(task_id, aes_key_base64)``.

    Generates a fresh AES-256 key locally, posts it to the portal's
    ``create_bind_task`` endpoint, and returns the assigned ``task_id``
    together with the key. The key must be retained by the caller because it
    is the only material that can later decrypt
    ``bot_encrypt_secret``.

    Sequence:

    1. :func:`generate_bind_key` derives the key.
    2. ``POST https://<PORTAL_HOST>/lite/create_bind_task`` with body
       ``{"key": key}``.
    3. Verify ``retcode == 0`` and extract ``data.task_id``.

    Args:
        timeout: HTTP timeout in seconds for the POST.

    Returns:
        A tuple of ``(task_id, aes_key_base64)``.

    Raises:
        RuntimeError: The portal returned a non-zero ``retcode``, or the
            response body has no ``data.task_id``. The message carries the
            portal's ``msg`` field when one was supplied.
    """
    import httpx

    url = f"https://{PORTAL_HOST}{ONBOARD_CREATE_PATH}"
    key = generate_bind_key()

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(url, json={"key": key}, headers=get_api_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg", "create_bind_task failed"))

    task_id = data.get("data", {}).get("task_id")
    if not task_id:
        raise RuntimeError("create_bind_task: missing task_id in response")

    logger.debug("create_bind_task ok: task_id=%s", task_id)
    return task_id, key


def _poll_bind_result(
    task_id: str,
    timeout: float = ONBOARD_API_TIMEOUT,
) -> Tuple[BindStatus, str, str, str]:
    """Issue one ``poll_bind_result`` request and return the parsed outcome.

    This performs a single poll and returns immediately; callers loop until
    the status transitions out of :attr:`BindStatus.NONE` and
    :attr:`BindStatus.PENDING`. Splitting the single poll out from the loop
    keeps retry and timeout policy in :func:`qr_register` and makes this
    helper independently testable.

    Args:
        task_id: The task ID returned by :func:`_create_bind_task`.
        timeout: HTTP timeout in seconds for the POST.

    Returns:
        A 4-tuple of ``(status, bot_appid, bot_encrypt_secret, user_openid)``:

        * ``status`` -- a :class:`BindStatus` value.
        * ``bot_appid`` -- the bot application ID, as a string.
        * ``bot_encrypt_secret`` -- base64 AES-256-GCM ciphertext holding the
          ``client_secret``; empty until the task is complete.
        * ``user_openid`` -- OpenID of whoever scanned the code, or empty.

    Raises:
        RuntimeError: The portal returned a non-zero ``retcode``.
    """
    import httpx

    url = f"https://{PORTAL_HOST}{ONBOARD_POLL_PATH}"

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(url, json={"task_id": task_id}, headers=get_api_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg", "poll_bind_result failed"))

    d = data.get("data", {})
    return (
        BindStatus(d.get("status", 0)),
        str(d.get("bot_appid", "")),
        d.get("bot_encrypt_secret", ""),
        d.get("user_openid", ""),
    )


def build_connect_url(task_id: str) -> str:
    """Build the QR code target URL for *task_id*.

    URL-encodes the task ID and substitutes it into
    :const:`QR_URL_TEMPLATE`. The resulting page is meant to be opened by
    the QQ mobile app after a camera scan, where the operator confirms the
    bot binding.

    Encoding matters: the portal keys the bind task by this exact string, so
    any character the task ID contributes (such as ``&`` or ``?``) must be
    percent-encoded rather than inserted raw, or the query string would be
    parsed ambiguously.

    Args:
        task_id: Task ID returned by :func:`_create_bind_task`.

    Returns:
        The fully-formed QR target URL.
    """
    return QR_URL_TEMPLATE.format(task_id=quote(task_id))


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

# Maximum number of times to refresh the QR code before giving up.
_MAX_REFRESHES = 3


def qr_register(timeout_seconds: int = 600) -> Optional[dict]:
    """Run the complete QQ Bot scan-to-configure flow and return credentials.

    This is the single public entry point for first-run provisioning. It
    drives the full lifecycle synchronously: create a bind task, show the
    operator something scannable, poll until a human confirms on their phone,
    decrypt the returned secret, and hand back a credential dict.

    Loop structure::

        for refresh_count in range(_MAX_REFRESHES + 1):
            create bind task            -> (task_id, aes_key)
            render QR / print URL
            while monotonic() < deadline:
                poll bind result
                  COMPLETED -> decrypt, return credentials
                  EXPIRED   -> refresh the QR, unless this was the last try
            deadline elapsed -> return None

    The outer bound is ``_MAX_REFRESHES + 1`` attempts, so the operator gets
    one initial code plus a bounded number of refreshes. Expiry of a code
    triggers a brand-new bind task rather than re-polling the dead one,
    because the portal invalidates the task server-side.

    Args:
        timeout_seconds: Overall wall-clock budget for the whole operation,
            covering every refresh attempt combined. Defaults to 600
            seconds (10 minutes), which is comfortably longer than a portal
            task's own validity window.

    Returns:
        On success a dict with keys ``"app_id"``, ``"client_secret"``, and
        ``"user_openid"``. Returns ``None`` if task creation fails, the
        deadline is reached, or the code expires on the final refresh.

    Example::

        >>> result = qr_register(timeout_seconds=300)
        >>> if result:
        ...     print(f"App ID: {result['app_id']}")
        ...     print(f"Client Secret: {result['client_secret']}")
    """
    deadline = time.monotonic() + timeout_seconds

    for refresh_count in range(_MAX_REFRESHES + 1):
        # Create bind task
        try:
            task_id, aes_key = _create_bind_task()
        except Exception as exc:
            logger.warning("[QQBot onboard] Failed to create bind task: %s", exc)
            return None

        url = build_connect_url(task_id)

        # Display QR code + URL
        print()
        if _render_qr(url):
            print(f"  Scan the QR code above, or open this URL directly:\n  {url}")
        else:
            print(f"  Open this URL in QQ on your phone:\n  {url}")
            print("  Tip: pip install qrcode  to display a scannable QR code here")
        print()

        # Poll loop
        while time.monotonic() < deadline:
            try:
                status, app_id, encrypted_secret, user_openid = _poll_bind_result(task_id)
            except Exception:
                time.sleep(ONBOARD_POLL_INTERVAL)
                continue

            if status == BindStatus.COMPLETED:
                client_secret = decrypt_secret(encrypted_secret, aes_key)
                print()
                print(f"  QR scan complete! (App ID: {app_id})")
                if user_openid:
                    print(f"  Scanner's OpenID: {user_openid}")
                return {
                    "app_id": app_id,
                    "client_secret": client_secret,
                    "user_openid": user_openid,
                }

            if status == BindStatus.EXPIRED:
                if refresh_count >= _MAX_REFRESHES:
                    logger.warning("[QQBot onboard] QR code expired %d times 鈥?giving up", _MAX_REFRESHES)
                    return None
                print(f"\n  QR code expired, refreshing... ({refresh_count + 1}/{_MAX_REFRESHES})")
                break  # next for-loop iteration creates a new task

            time.sleep(ONBOARD_POLL_INTERVAL)
        else:
            # deadline reached without completing
            logger.warning("[QQBot onboard] Poll timed out after %ds", timeout_seconds)
            return None

    return None
