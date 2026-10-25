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
WeCom callback-mode message cryptography, BizMsgCrypt-compatible.

This module implements the exact wire format of Tencent's official
``WXBizMsgCrypt`` helper so that a self-built WeCom application can verify,
encrypt and decrypt the callback envelopes WeCom exchanges with it. It is the
crypto satellite of the callback adapter:
:mod:`encre.gateway.platforms.wecom.callback_adapter` calls
:meth:`WXBizMsgCrypt.decrypt` on every inbound HTTP callback body and
:meth:`WXBizMsgCrypt.encrypt` when it needs to wrap an outbound payload. The
WebSocket adapter (:class:`encre.gateway.platforms.wecom.adapter.WeComAdapter`)
does not use this module at all.

Envelope format, per message:

* 16 random bytes, chosen fresh per message so identical plaintexts never
  produce identical ciphertexts under CBC's fixed IV.
* 4 bytes, the plaintext length as a network-byte-order unsigned int. The
  explicit length is required because the trailing receiver id is itself
  variable length, so the split point cannot be inferred otherwise.
* The plaintext (an XML document) in UTF-8.
* The ``receive_id`` (corp id) as a trailing UTF-8 string, which lets the
  receiver assert that the message was addressed to it.

The whole blob is PKCS#7 padded to a **32-byte** block size -- not AES's native
16-byte block -- matching Tencent's reference implementation, then encrypted
with AES-256-CBC using the key derived from ``EncodingAESKey`` (base64 with
one omitted ``=`` padding character; the first 16 decoded bytes double as the
fixed CBC initialisation vector). The result is base64 encoded.

Authentication is a separate MAC: the base64 ciphertext, the timestamp, the
nonce and the shared callback token are sorted lexicographically,
concatenated, and SHA-1 hashed. The digest is compared with the
``msg_signature`` query parameter before any decryption work happens, so an
unauthenticated attacker cannot use the service as an AES oracle.

Public API:

* :class:`WXBizMsgCrypt` -- the codec: :meth:`WXBizMsgCrypt.verify_url`,
  :meth:`WXBizMsgCrypt.decrypt`, :meth:`WXBizMsgCrypt.encrypt`.
* :class:`WeComCryptoError` and subclasses -- the uniform exception family.
* :class:`PKCS7Encoder` -- 32-byte-block padding helper, exposed because it
  encodes a vendor quirk rather than plain crypto.
"""

import base64
import hashlib
import os
import secrets
import socket
import struct
import time
from typing import Optional
from xml.etree import ElementTree as ET

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeComCryptoError(Exception):
    """Base class for every failure this module can raise.

    All signature, decryption and encryption failures inherit from this
    exception so that a caller (typically the callback adapter's request loop)
    can catch one type and decide uniformly whether a callback body was merely
    meant for a different app -- ``continue`` -- or was malformed -- ``break``.
    """


class SignatureError(WeComCryptoError):
    """The supplied ``msg_signature`` does not match the locally computed SHA-1.

    Raised when the envelope is not addressed to this app's token/timestamp/
    nonce combination, which is the normal outcome when iterating over several
    configured apps before finding the one that owns the callback.
    """


class DecryptError(WeComCryptoError):
    """Base64, AES, PKCS#7 padding or receiver-id validation failed."""


class EncryptError(WeComCryptoError):
    """Blob assembly or AES encryption failed."""


class PKCS7Encoder:
    """PKCS#7 padding at WeCom's 32-byte block size.

    WeCom pads to 32 bytes instead of AES's native 16, mirroring Tencent's
    reference implementation. The constant is a class attribute so the block
    size stays in one place and both directions of padding agree by
    construction.

    Attributes:
        block_size: Padding block size in bytes. Fixed at 32 to match WeCom.
    """

    block_size = 32

    @classmethod
    def encode(cls, text: bytes) -> bytes:
        """Pad ``text`` up to a multiple of the block size.

        Unlike plain PKCS#7, a payload already aligned to the block size still
        gets a full extra block of padding rather than none, so the padding
        byte is always in ``[1, block_size]`` and can never be mistaken for
        plaintext.

        Args:
            text: The raw bytes to pad.

        Returns:
            bytes: ``text`` with padding bytes appended.
        """
        amount_to_pad = cls.block_size - (len(text) % cls.block_size)
        if amount_to_pad == 0:
            amount_to_pad = cls.block_size
        pad = bytes([amount_to_pad]) * amount_to_pad
        return text + pad

    @classmethod
    def decode(cls, decrypted: bytes) -> bytes:
        """Validate and strip PKCS#7 padding.

        Args:
            decrypted: Decrypted bytes still carrying padding.

        Returns:
            bytes: The unpadded plaintext.

        Raises:
            DecryptError: The payload is empty, the final byte is outside
                ``[1, block_size]``, or the trailing run does not consist
                entirely of that byte value.
        """
        if not decrypted:
            raise DecryptError("empty decrypted payload")
        pad = decrypted[-1]
        if pad < 1 or pad > cls.block_size:
            raise DecryptError("invalid PKCS7 padding")
        if decrypted[-pad:] != bytes([pad]) * pad:
            raise DecryptError("malformed PKCS7 padding")
        return decrypted[:-pad]


def _sha1_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """Compute WeCom's envelope MAC.

    The four inputs are sorted **lexicographically** -- not in the documented
    argument order -- concatenated without separators, UTF-8 encoded, and
    SHA-1 hashed. Sorting rather than fixed ordering is what makes the check
    symmetric between sender and receiver.

    Args:
        token: The app's callback token.
        timestamp: The request timestamp as a string.
        nonce: The request nonce as a string.
        encrypt: The base64-encoded ciphertext being authenticated.

    Returns:
        str: The 40-character lowercase SHA-1 hex digest.
    """
    parts = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


class WXBizMsgCrypt:
    """Stateless-per-message codec bound to one app's credentials.

    One instance is constructed per configured self-built app by
    :mod:`encre.gateway.platforms.wecom.callback_adapter` and reused across
    callbacks. The instance holds only the derived AES key/IV and the expected
    receiver id; no per-message state accumulates, so the object is safe to
    share across concurrent request handlers.

    The verification order is deliberately strict: signature first, then
    base64, then AES, then padding, then receiver id. Decrypting before
    verifying the signature would let a remote party probe the key.

    Invariants:
        * :attr:`key` is exactly 32 bytes and :attr:`iv` is ``key[:16]``.
        * :attr:`receive_id` is non-empty and is compared against the receiver
          id recovered from every decrypted blob.
    """

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        """Bind credentials and derive the AES key and IV.

        Args:
            token: The callback token shared with WeCom, used in the SHA-1
                signature over every envelope.
            encoding_aes_key: The 43-character ``EncodingAESKey``. WeCom hands
                this out as base64 with its single trailing ``=`` removed, so
                the padding is restored here before decoding.
            receive_id: The corp id that must appear inside every decrypted
                blob; mismatched callbacks are rejected.

        Raises:
            ValueError: Any argument is missing, or ``encoding_aes_key`` is
                not exactly 43 characters.
        """
        if not token:
            raise ValueError("token is required")
        if not encoding_aes_key:
            raise ValueError("encoding_aes_key is required")
        if len(encoding_aes_key) != 43:
            raise ValueError("encoding_aes_key must be 43 chars")
        if not receive_id:
            raise ValueError("receive_id is required")

        self.token = token
        self.receive_id = receive_id
        # The key is stored base64 with one ``=`` deliberately stripped;
        # re-padding lets base64 decode the full 32-byte AES-256 key, whose
        # first 16 bytes also serve as the fixed CBC initialisation vector.
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.key[:16]

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """Decrypt WeCom's URL-verification handshake and return the echo text.

        When WeCom configures a callback URL it performs a GET carrying an
        encrypted ``echostr``; returning that string verbatim proves ownership
        of the URL. This is the decrypt half of :meth:`decrypt` with the
        plaintext decoded to UTF-8 text.

        Args:
            msg_signature: Signature supplied in the handshake query string.
            timestamp: Handshake timestamp.
            nonce: Handshake nonce.
            echostr: The encrypted echo string to decrypt.

        Returns:
            str: The plaintext echo string to send back to WeCom.

        Raises:
            SignatureError: The signature does not match this app's token.
            DecryptError: Decryption, padding or receiver-id validation failed.
        """
        plain = self.decrypt(msg_signature, timestamp, nonce, echostr)
        return plain.decode("utf-8")

    def decrypt(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bytes:
        """Verify the envelope and decrypt its XML payload.

        The signature is checked first. The base64 ciphertext is then
        AES-256-CBC decrypted with the fixed key/IV, PKCS#7 unpadded, and
        split into the 16-byte random prefix, the 4-byte big-endian content
        length, the XML content, and the trailing receiver id. The receiver id
        is validated before anything is returned.

        Args:
            msg_signature: The signature to verify.
            timestamp: The envelope timestamp.
            nonce: The envelope nonce.
            encrypt: The base64-encoded ciphertext.

        Returns:
            bytes: The decrypted XML content, unpadded.

        Raises:
            SignatureError: The signature does not match.
            DecryptError: Base64, AES, padding or receiver-id failure.
        """
        expected = _sha1_signature(self.token, timestamp, nonce, encrypt)
        if expected != msg_signature:
            raise SignatureError("signature mismatch")
        try:
            cipher_text = base64.b64decode(encrypt)
        except Exception as exc:
            raise DecryptError(f"invalid base64 payload: {exc}") from exc
        try:
            cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
            decryptor = cipher.decryptor()
            padded = decryptor.update(cipher_text) + decryptor.finalize()
            plain = PKCS7Encoder.decode(padded)
            # 16 random bytes precede the length-prefixed payload.
            content = plain[16:]
            xml_length = socket.ntohl(struct.unpack("I", content[:4])[0])
            xml_content = content[4:4 + xml_length]
            receive_id = content[4 + xml_length:].decode("utf-8")
        except WeComCryptoError:
            raise
        except Exception as exc:
            raise DecryptError(f"decrypt failed: {exc}") from exc

        if receive_id != self.receive_id:
            raise DecryptError("receive_id mismatch")
        return xml_content

    def encrypt(self, plaintext: str, nonce: Optional[str] = None, timestamp: Optional[str] = None) -> str:
        """Encrypt ``plaintext`` and wrap it in a signed XML envelope.

        Builds the ``<xml>`` document WeCom expects in proactive replies, with
        the ciphertext plus the ``MsgSignature``/``TimeStamp``/``Nonce``
        triple that lets the receiver re-verify the MAC.

        Args:
            plaintext: The XML string to encrypt.
            nonce: Optional nonce; a random one is generated when omitted.
            timestamp: Optional timestamp string; the current wall-clock
                seconds are used when omitted.

        Returns:
            str: The full XML envelope containing ``Encrypt``,
            ``MsgSignature``, ``TimeStamp`` and ``Nonce`` children.
        """
        nonce = nonce or self._random_nonce()
        timestamp = timestamp or str(int(time.time()))
        encrypt = self._encrypt_bytes(plaintext.encode("utf-8"))
        signature = _sha1_signature(self.token, timestamp, nonce, encrypt)
        root = ET.Element("xml")
        ET.SubElement(root, "Encrypt").text = encrypt
        ET.SubElement(root, "MsgSignature").text = signature
        ET.SubElement(root, "TimeStamp").text = timestamp
        ET.SubElement(root, "Nonce").text = nonce
        return ET.tostring(root, encoding="unicode")

    def _encrypt_bytes(self, raw: bytes) -> str:
        """Assemble and encrypt one WeCom message blob.

        The blob layout is ``random_prefix(16) || length(4, network order) ||
        raw || receive_id``. The explicit length is what allows the receiver to
        separate the variable-length ``raw`` from the variable-length receiver
        id after unpadding.

        Args:
            raw: The plaintext bytes to encrypt.

        Returns:
            str: The base64-encoded ciphertext.

        Raises:
            EncryptError: Blob assembly, padding, AES encryption or base64
                encoding failed.
        """
        try:
            random_prefix = os.urandom(16)
            msg_len = struct.pack("I", socket.htonl(len(raw)))
            payload = random_prefix + msg_len + raw + self.receive_id.encode("utf-8")
            padded = PKCS7Encoder.encode(payload)
            cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
            encryptor = cipher.encryptor()
            encrypted = encryptor.update(padded) + encryptor.finalize()
            return base64.b64encode(encrypted).decode("utf-8")
        except Exception as exc:
            raise EncryptError(f"encrypt failed: {exc}") from exc

    @staticmethod
    def _random_nonce(length: int = 10) -> str:
        """Generate a URL-safe alphanumeric nonce.

        Restricted to the ASCII alphanumeric set so the value can travel safely
        as an XML text node and in a query string without escaping.

        Args:
            length: Desired nonce length in characters.

        Returns:
            str: The randomly generated nonce.
        """
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return "".join(secrets.choice(alphabet) for _ in range(length))
