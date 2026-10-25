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

"""AES-256-GCM helpers for the QQ Bot scan-to-configure credential exchange.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module implements the client half of the secret-exchange protocol that
:mod:`ea_connector_qqbot.onboard` drives. The QQ portal must never
receive a bot's ``client_secret`` in the clear. Instead the local process
generates a random 256-bit AES key, hands the key to ``create_bind_task``,
and the portal encrypts the ``client_secret`` with that key, returning the
ciphertext as ``bot_encrypt_secret`` from ``poll_bind_result``. Because only
the CLI process holds the key, the secret never crosses the network or lands
on disk in plaintext.

Data flow through the two functions::

    generate_bind_key()  ->  POST create_bind_task({"key": key})   outbound
    poll_bind_result()   ->  bot_encrypt_secret (base64 ciphertext) inbound
    decrypt_secret()     ->  client_secret (plaintext)             local

Ciphertext layout, after base64-decoding ``bot_encrypt_secret``::

    IV (12 bytes) | ciphertext (N bytes) | AuthTag (16 bytes)

The leading 12 bytes are the GCM nonce. Everything after them is the
ciphertext with the 16-byte authentication tag appended in-band, which is
the exact layout
:class:`cryptography.hazmat.primitives.ciphers.aead.AESGCM.decrypt` expects
in its second argument -- passing the tag separately would abort the
authentication check.

Public API:
    :func:`generate_bind_key`
    :func:`decrypt_secret`

Dependencies:
    :mod:`cryptography` is a dependency of the qqbot platform extra. It is
    imported inside :func:`decrypt_secret` rather than at module level so that
    a missing install only breaks the onboard path and never the adapter import
    graph. :func:`generate_bind_key` needs no third-party dependency.

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

import base64
import os


def generate_bind_key() -> str:
    """Generate a fresh 256-bit AES key and return it base64-encoded.

    The returned key is passed to ``create_bind_task`` so the portal can
    encrypt the bot's ``client_secret`` before returning it. The same value
    is later fed back into :func:`decrypt_secret`, so the caller must retain
    it for the duration of the registration session.

    The key comes from :func:`os.urandom`, which draws from the operating
    system CSPRNG. 32 raw bytes expand to 44 base64 characters. The key is
    held in process memory only and is never persisted.

    Returns:
        A 44-character base64 string representing 32 AES-256 key bytes.

    Example::

        >>> key = generate_bind_key()
        >>> len(key)
        44
    """
    return base64.b64encode(os.urandom(32)).decode()


def decrypt_secret(encrypted_base64: str, key_base64: str) -> str:
    """Decrypt an AES-256-GCM ciphertext into the bot's client_secret.

    Consumes the ``bot_encrypt_secret`` field of a ``poll_bind_result``
    response and the bind key produced by :func:`generate_bind_key`. GCM is
    an authenticated cipher mode: the 16-byte trailing tag is verified as
    part of the decrypt, so a wrong key or tampered ciphertext fails loudly
    instead of yielding garbage.

    Ciphertext layout (after base64-decoding)::

        IV (12 bytes) | ciphertext (N bytes) | AuthTag (16 bytes)

    Args:
        encrypted_base64: The ``bot_encrypt_secret`` value from the
            ``poll_bind_result`` response, already base64-encoded.
        key_base64: The base64 AES-256 key from :func:`generate_bind_key`.
            Must be the same key that was sent to ``create_bind_task``.

    Returns:
        The decrypted ``client_secret`` decoded as UTF-8.

    Raises:
        cryptography.exceptions.InvalidTag: The authentication tag does not
            verify, which means the key is wrong or the payload was altered.
        ValueError: The base64 input is malformed and cannot be decoded.

    Note:
        ``None`` is passed as the additional authenticated data because the
        portal signs only the ciphertext, not an AAD context.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    # Both inputs are base64 on the wire; convert to raw key/ciphertext bytes.
    key = base64.b64decode(key_base64)
    raw = base64.b64decode(encrypted_base64)

    # Split the nonce prefix from the body. AESGCM.decrypt wants the body as
    # ciphertext-with-tag-concatenated, so no further slicing is performed.
    iv = raw[:12]
    ciphertext_with_tag = raw[12:]  # AESGCM expects ciphertext + tag concatenated

    # Build the cipher instance and perform authenticated decryption.
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext_with_tag, None)
    return plaintext.decode("utf-8")
