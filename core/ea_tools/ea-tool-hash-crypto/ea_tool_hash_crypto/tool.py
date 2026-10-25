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

"""Hashing and lightweight cryptography tool.

Computes checksums (md5/sha1/sha256) and HMACs, and performs base64 / symmetric
cipher (AES) encode-decode helpers without external key management.
"""


import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool


async def _hash_crypto_execute(**kwargs: Any) -> str:
    """Hash crypto execute.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    action = kwargs.get("action", "")
    algorithm = kwargs.get("algorithm", "sha256")
    data = kwargs.get("data", "")
    file_path = kwargs.get("file_path", "")
    key = kwargs.get("key", "")
    mode = kwargs.get("mode", "CBC")
    encoding = kwargs.get("encoding", "hex")

    loop = asyncio.get_event_loop()

    if action == "hash":
        if not data and not file_path:
            return "Missing required field: data or file_path"

        def _hash() -> str:
            """Hash."""
            try:
                h = hashlib.new(algorithm)
                if file_path:
                    if not os.path.exists(file_path):
                        return f"File not found: {file_path}"
                    with open(file_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
                    result = h.hexdigest() if encoding == "hex" else base64.b64encode(h.digest()).decode()
                    return json.dumps({
                        "algorithm": algorithm,
                        "hash": result,
                        "encoding": encoding,
                        "file": file_path,
                        "size": os.path.getsize(file_path),
                    }, ensure_ascii=False, indent=2)
                else:
                    h.update(data.encode("utf-8"))
                    result = h.hexdigest() if encoding == "hex" else base64.b64encode(h.digest()).decode()
                    return json.dumps({
                        "algorithm": algorithm,
                        "hash": result,
                        "encoding": encoding,
                    }, ensure_ascii=False, indent=2)
            except ValueError as e:
                return f"Unsupported algorithm: {algorithm}. {e}"
            except OSError as e:
                return f"File error: {e}"

        return await loop.run_in_executor(None, _hash)

    elif action == "hmac":
        if not data:
            return "Missing required field: data"
        if not key:
            return "Missing required field: key"

        def _hmac() -> str:
            """Hmac."""
            try:
                import hmac as hmac_mod
                h = hmac_mod.new(key.encode("utf-8"), data.encode("utf-8"), algorithm)
                result = h.hexdigest() if encoding == "hex" else base64.b64encode(h.digest()).decode()
                return json.dumps({
                    "algorithm": f"HMAC-{algorithm.upper()}",
                    "hash": result,
                    "encoding": encoding,
                }, ensure_ascii=False, indent=2)
            except ValueError as e:
                return f"HMAC error: {e}"

        return await loop.run_in_executor(None, _hmac)

    elif action == "checksum":
        if not file_path:
            return "Missing required field: file_path"

        def _checksum() -> str:
            """Checksum."""
            try:
                if not os.path.exists(file_path):
                    return f"File not found: {file_path}"
                results = {}
                algs = algorithm.split(",") if "," in algorithm else [algorithm]
                for alg in algs:
                    alg = alg.strip()
                    h = hashlib.new(alg)
                    with open(file_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
                    results[alg] = h.hexdigest()
                return json.dumps({
                    "file": file_path,
                    "size": os.path.getsize(file_path),
                    "checksums": results,
                }, ensure_ascii=False, indent=2)
            except ValueError as e:
                return f"Unsupported algorithm: {e}"
            except OSError as e:
                return f"File error: {e}"

        return await loop.run_in_executor(None, _checksum)

    elif action == "encrypt":
        if not data:
            return "Missing required field: data"
        if not key:
            return "Missing required field: key"

        def _encrypt() -> str:
            """Encrypt."""
            try:
                from cryptography.hazmat.backends import default_backend
                from cryptography.hazmat.primitives import padding
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

                key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
                iv = os.urandom(16)
                data_bytes = data.encode("utf-8")

                padder = padding.PKCS7(128).padder()
                padded_data = padder.update(data_bytes) + padder.finalize()

                if mode.upper() == "CBC":
                    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
                elif mode.upper() == "ECB":
                    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=default_backend())
                elif mode.upper() == "CTR":
                    nonce = os.urandom(8)
                    cipher = Cipher(algorithms.AES(key_bytes), modes.CTR(nonce), backend=default_backend())
                    iv = nonce
                    padded_data = data_bytes
                elif mode.upper() == "GCM":
                    cipher = Cipher(algorithms.AES(key_bytes), modes.GCM(iv), backend=default_backend())
                    padded_data = data_bytes
                else:
                    return f"Unsupported mode: {mode}. Supported: CBC, ECB, CTR, GCM"

                encryptor = cipher.encryptor()
                ct = encryptor.update(padded_data) + encryptor.finalize()

                tag = getattr(encryptor, "tag", b"")
                result = {
                    "ciphertext": base64.b64encode(ct).decode(),
                    "iv": base64.b64encode(iv).decode(),
                    "algorithm": f"AES-{mode}",
                    "encoding": "base64",
                }
                if tag:
                    result["tag"] = base64.b64encode(tag).decode()
                return json.dumps(result, ensure_ascii=False, indent=2)
            except ImportError:
                return "cryptography library required. Install: pip install cryptography"
            except Exception as e:
                return f"Encryption failed: {e}"

        return await loop.run_in_executor(None, _encrypt)

    elif action == "decrypt":
        if not data:
            return "Missing required field: data (base64 ciphertext)"
        if not key:
            return "Missing required field: key"

        def _decrypt() -> str:
            """Decrypt."""
            try:
                from cryptography.hazmat.backends import default_backend
                from cryptography.hazmat.primitives import padding
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

                key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
                ct = base64.b64decode(data)
                iv_raw = kwargs.get("iv", "")
                if not iv_raw:
                    return "Missing required field: iv (base64)"
                iv = base64.b64decode(iv_raw)

                if mode.upper() == "CBC":
                    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
                elif mode.upper() == "ECB":
                    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=default_backend())
                elif mode.upper() == "CTR":
                    cipher = Cipher(algorithms.AES(key_bytes), modes.CTR(iv), backend=default_backend())
                elif mode.upper() == "GCM":
                    tag_raw = kwargs.get("tag", "")
                    if not tag_raw:
                        return "Missing required field: tag for GCM mode"
                    tag = base64.b64decode(tag_raw)
                    cipher = Cipher(algorithms.AES(key_bytes), modes.GCM(iv, tag), backend=default_backend())
                else:
                    return f"Unsupported mode: {mode}"

                decryptor = cipher.decryptor()
                padded_data = decryptor.update(ct) + decryptor.finalize()

                if mode.upper() in ("CBC", "ECB"):
                    unpadder = padding.PKCS7(128).unpadder()
                    plaintext = unpadder.update(padded_data) + unpadder.finalize()
                else:
                    plaintext = padded_data

                return json.dumps({
                    "plaintext": plaintext.decode("utf-8"),
                    "algorithm": f"AES-{mode}",
                }, ensure_ascii=False, indent=2)
            except ImportError:
                return "cryptography library required. Install: pip install cryptography"
            except Exception as e:
                return f"Decryption failed: {e}"

        return await loop.run_in_executor(None, _decrypt)

    elif action == "base64_encode":
        if not data:
            return "Missing required field: data"
        if file_path:
            try:
                raw = Path(file_path).read_bytes()
                return base64.b64encode(raw).decode()
            except OSError as e:
                return f"File error: {e}"
        return base64.b64encode(data.encode("utf-8")).decode()

    elif action == "base64_decode":
        if not data:
            return "Missing required field: data"
        if file_path:
            try:
                decoded = base64.b64decode(data)
                Path(file_path).write_bytes(decoded)
                return f"Decoded to {file_path} ({len(decoded)} bytes)"
            except OSError as e:
                return f"File error: {e}"
            except Exception as e:
                return f"Base64 decode failed: {e}"
        try:
            return base64.b64decode(data).decode("utf-8")
        except UnicodeDecodeError:
            return f"Binary data decoded ({len(base64.b64decode(data))} bytes). Use file_path to write to file."
        except Exception as e:
            return f"Base64 decode failed: {e}"

    return f"Unknown action: {action}. Supported: hash, hmac, checksum, encrypt, decrypt, base64_encode, base64_decode"


hash_crypto_tool = build_tool(
    name="hash_crypto",
    description=(
        "Compute hashes (MD5/SHA1/SHA256/SHA512), HMACs, and file checksums, "
        "perform AES symmetric encrypt/decrypt (CBC/ECB/CTR/GCM), and "
        "base64 encode/decode. Use this instead of shelling out to openssl "
        "or python one-liners -- it returns structured JSON and streams large "
        "files in chunks for hashing.\n\n"
        "WHEN to use: verify file integrity (checksums), hash a string, sign a "
        "payload with HMAC, encrypt/decrypt a secret with a passphrase, or do "
        "quick base64 conversions.\n"
        "WHEN NOT to use: for password storage use a slow KDF (argon2/bcrypt) "
        "rather than plain sha256; for TLS/SSL or public-key cryptography use a "
        "dedicated library; for production key management use a vault.\n"
        "TIPS: for checksum, pass a comma-separated 'algorithm' (e.g. "
        "'sha256,sha1') to compute several checksums in one pass; the AES key "
        "is derived as sha256(passphrase), so the same passphrase reproduces "
        "the same key; GCM mode is recommended for authenticated encryption "
        "(it returns a tag you must keep for decrypt).\n"
        "PITFALLS: avoid MD5/SHA1 for security-sensitive purposes -- prefer "
        "SHA256 or stronger; AES ECB mode is insecure for most data -- prefer "
        "CBC or GCM; decrypt requires the exact iv (and tag for GCM) returned "
        "by the matching encrypt call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["hash", "hmac", "checksum", "encrypt", "decrypt", "base64_encode", "base64_decode"],
                "description": "Action (required). 'hash' hashes text or a file; 'hmac' computes an HMAC; 'checksum' computes file checksums; 'encrypt'/'decrypt' run AES; 'base64_encode'/'base64_decode' handle base64.",
            },
            "algorithm": {
                "type": "string",
                "description": "Hash algorithm (optional, default 'sha256'): md5, sha1, sha256, sha512. For 'checksum', comma-separated to compute several at once.",
            },
            "data": {"type": "string", "description": "Text payload for hash/hmac/encrypt/decrypt/base64 actions (required when file_path is not given). For decrypt, this is the base64 ciphertext produced by the matching encrypt call."},
            "file_path": {"type": "string", "description": "Absolute path to a file (optional). Used by hash/checksum (streamed in 64KB chunks, suitable for large files) and base64_encode (reads bytes) / base64_decode (writes decoded bytes to the path)."},
            "key": {"type": "string", "description": "Secret passphrase for HMAC and AES actions (required for hmac/encrypt/decrypt). The AES key is derived as sha256(key) internally, so any string works but longer/random values are stronger."},
            "mode": {
                "type": "string",
                "enum": ["CBC", "ECB", "CTR", "GCM"],
                "description": "AES cipher mode (optional, default 'CBC'). GCM is recommended for authenticated encryption.",
            },
            "encoding": {
                "type": "string",
                "enum": ["hex", "base64"],
                "description": "Output encoding for hash/hmac results (optional, default 'hex').",
            },
            "iv": {"type": "string", "description": "Base64 initialization vector returned by encrypt (required for decrypt)."},
            "tag": {"type": "string", "description": "Base64 auth tag returned by GCM encrypt (required for GCM decrypt)."},
        },
        "required": ["action"],
    },
    execute=_hash_crypto_execute,
    intents=["general", "coding", "system"],
    category="system",
    semantic_type="general",
    cost_level="low",
    retryability="auto",
    is_concurrency_safe=lambda _: True,
    is_readonly=lambda _: True,
)
