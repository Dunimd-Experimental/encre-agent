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

"""Encre cryptographic layer -- provides encrypt/decrypt for all sensitive data.

Architecture
============

All encryption uses **AES-256-GCM** (32‑byte key, 12‑byte random nonce,
16‑byte authentication tag).

Master key
    On first use a 256‑bit master key is generated from ``os.urandom(32)`` and
    stored in ``<data_dir>/keys/keyfile`` (mode ``0o600``).  The on-disk
    representation is the master key itself wrapped with a *machine-binding*
    key derived via HKDF-SHA256 (RFC 5869: Extract + Expand) from a
    multi-source host fingerprint (Windows MachineGuid / /etc/machine-id /
    DMI product UUID + hostname + username).

    This means even if ``keyfile`` is exfiltrated it cannot be unwrapped on
    any other machine.

    With the above::

        encrypt(plain_bytes)  ->  base64(nonce || ciphertext || tag)
        decrypt(b64_ciphertext) -> plain_bytes

Key material never lives in source or config files: the vendor application
secret is injected at build time (GitHub Actions) into the native module, with
an ``ENCRE_EA_SECRET`` runtime fallback for development.
"""


import base64
import contextlib
import getpass
import hashlib
import hmac
import os
import pathlib
import platform
import secrets
import shutil
import stat
import sys
import typing as _t



from encre.logging_config import get_logger
logger = get_logger(__name__)

__all__ = [
    "DecryptionError",
    "decrypt",
    "decrypt_bytes",
    "encrypt",
    "encrypt_bytes",
    "ensure_keyfile",
    "keyfile_path",
    "migrate_keyfile",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The keyfile lives **inside the data tree** (``<data_dir>/keys/keyfile``).
#: It used to sit in a second root (``~/.encre``), which quietly broke the
#: promise that "backing up the data directory" is enough to restore: the
#: ciphertexts travelled but the key did not.  New keys are written to the
#: data tree; the legacy path is still *read* so existing installs keep
#: working, and :func:`migrate_keyfile` moves it over.
_LEGACY_KEYFILE_PATH = pathlib.Path("~/.encre/keyfile").expanduser()
_KEYFILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0o600 -- owner read/write only

_KEY_LENGTH = 32       # AES-256
_NONCE_LENGTH = 12     # GCM standard
_TAG_LENGTH = 16       # GCM 128-bit auth tag
_GCM_IV_LENGTH = 12

# HKDF constants for deriving the machine-binding wrapping key (scheme v3).
# v3 mixes a vendor-supplied application secret (build-time injected via
# GitHub Actions into the native module, or provided at runtime through the
# ``ENCRE_EA_SECRET`` environment variable) into the IKM, so the wrapping key
# depends on BOTH the machine fingerprint AND the distribution secret.
_HKDF_SALT = b"encre-crypto-hkdf-v3"
_HKDF_SALT_SHA256 = hashlib.sha256(_HKDF_SALT).digest()[:32]

_WRAP_CTX_AAD = b"encre-keywrap-v1"

#: Keyfile wire format: ``b"\x03" + nonce(12) || ciphertext(32) || tag(16)``.
#: The version byte identifies the derivation scheme; unknown versions are
#: refused (fail-closed) instead of silently regenerating a key.
_KEYFILE_VERSION = 3
_KEYFILE_HEADER = bytes([_KEYFILE_VERSION])

#: Public development fallback used ONLY when neither the native module nor
#: the ``ENCRE_EA_SECRET`` environment variable provides the vendor secret.
#: Release builds never fall through here.
_DEV_APP_SECRET = b"encre-dev-app-secret-public-dev-fallback"

#: Environment variable carrying the vendor application secret.
_APP_SECRET_ENV = "ENCRE_EA_SECRET"

# Cached master key (module lifetime) -- avoid reading keyfile on every call
_master_key_cache: bytes | None = None


# ---------------------------------------------------------------------------
# Machine-binding identity
# ---------------------------------------------------------------------------

def _read_machine_id() -> bytes:
    """Return a stable machine-identifier blob for key-wrapping.

    Mixes **every** fingerprint source available on this host into a single
    IKM (framed ``source=value`` lines), so the wrapping key does not depend
    on a single mutable or missing attribute:

    * Windows: ``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``
    * Linux:   ``/etc/machine-id`` and ``/sys/class/dmi/id/product_uuid``
    * hostname and current username (round out hosts without hardware ids)

    Raises:
        RuntimeError: If no fingerprint source yields a value -- key
            derivation must fail closed rather than silently bind to a
            constant.
    """
    parts: list[bytes] = []

    def _add(source: str, value: bytes | None) -> None:
        if value:
            stripped = value.strip()
            if stripped:
                parts.append(source.encode("utf-8") + b"=" + stripped)

    # Windows: registry machine GUID (stable across reboots and renames)
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                _add("win-machineguid", str(guid).encode("utf-8"))
        except OSError:
            logger.debug("exception suppressed", exc_info=True)

    for path, source in (
        ("/etc/machine-id", "machine-id"),
        ("/sys/class/dmi/id/product_uuid", "dmi-product-uuid"),
    ):
        try:
            mid = pathlib.Path(path).read_text(encoding="utf-8")
            if mid and mid.strip() != "uninitialized":
                _add(source, mid.encode("utf-8"))
        except (OSError, UnicodeDecodeError):
            logger.debug("exception suppressed", exc_info=True)

    _add("hostname", platform.node().encode("utf-8"))
    try:
        _add("username", getpass.getuser().encode("utf-8"))
    except Exception:
        logger.debug("exception suppressed", exc_info=True)

    if not parts:
        raise RuntimeError(
            "No machine fingerprint source available; cannot derive the "
            "machine-binding key (fail-closed)."
        )
    return b"\n".join(parts)


# ---------------------------------------------------------------------------
# HKDF-SHA256 (RFC 5869) -- implemented with hmac/hashlib, no third party
# ---------------------------------------------------------------------------

def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 Extract step: ``PRK = HMAC-Hash(salt, IKM)``."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 Expand step: derive ``length`` output bytes from the PRK."""
    if length <= 0:
        raise ValueError("HKDF-Expand length must be positive")
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


_HKDF_INFO = b"encre-machine-binding-wrapping-key-v3"


def _read_app_secret() -> bytes:
    """Return the vendor application secret (scheme v3 IKM component).

    Resolution order:

    1. Native build-time injection -- GitHub Actions compiles the secret into
       the native module (``ea_build_secret``), so packaged releases carry it
       without any file or environment dependency.
    2. The ``ENCRE_EA_SECRET`` runtime environment variable.
    3. Windows registry fallback (Machine, then User scope) -- covers processes
       started before the variable was set system-wide.
    4. Public development fallback (dev machines only).
    """
    try:
        from encre import _native  # type: ignore[attr-defined]

        getter = getattr(_native, "ea_build_secret", None)
        if getter is not None:
            injected = getter()
            if injected:
                return injected.encode("utf-8")
    except Exception:
        logger.debug("exception suppressed", exc_info=True)

    value = os.environ.get(_APP_SECRET_ENV)
    if value and value.strip():
        return value.strip().encode("utf-8")

    if sys.platform == "win32":
        try:
            import winreg

            hives = (
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                (winreg.HKEY_CURRENT_USER, "Environment"),
            )
            for hive, path in hives:
                try:
                    with winreg.OpenKey(hive, path) as key:
                        reg_value, _ = winreg.QueryValueEx(key, _APP_SECRET_ENV)
                        if reg_value and str(reg_value).strip():
                            return str(reg_value).strip().encode("utf-8")
                except OSError:
                    continue
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

    return _DEV_APP_SECRET


def _derive_wrapping_key() -> bytes:
    """Derive the AES-256 key used to wrap/unwrap the master-key file.

    Scheme v3: the machine fingerprint and the vendor application secret are
    both folded into the HKDF input key material with length-prefix framing
    (unambiguous concatenation), then run through the standard RFC 5869
    Extract + Expand steps.
    """
    machine = _read_machine_id()
    secret = _read_app_secret()
    ikm = (
        len(machine).to_bytes(4, "big") + machine
        + len(secret).to_bytes(4, "big") + secret
    )
    prk = _hkdf_extract(_HKDF_SALT_SHA256, ikm)
    return _hkdf_expand(prk, _HKDF_INFO, 32)


# ---------------------------------------------------------------------------
# Master‑key management (AES‑GCM wrap/unwrap with machine‑binding key)
# ---------------------------------------------------------------------------

def _generate_master_key() -> bytes:
    """Return a fresh 256‑bit master key."""
    return secrets.token_bytes(_KEY_LENGTH)


def _wrap_master_key(master_key: bytes) -> bytes:
    """Wrap *master_key* with the machine‑binding key via AES‑256‑GCM.

    Returns
    -------
    bytes
        ``nonce (12) || ciphertext (32) || tag (16)`` -- total 60 bytes.
        The ciphertext is the encrypted master key.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    wrapping_key = _derive_wrapping_key()
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    aesgcm = AESGCM(wrapping_key)
    ciphertext = aesgcm.encrypt(nonce, master_key, _WRAP_CTX_AAD)
    # ciphertext already includes the 16‑byte tag appended
    return nonce + ciphertext


def _unwrap_master_key(data: bytes) -> bytes:
    """Unwrap ``nonce || ciphertext || tag`` back to the master key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    wrapping_key = _derive_wrapping_key()
    nonce = data[:_NONCE_LENGTH]
    rest = data[_NONCE_LENGTH:]  # ciphertext + tag
    aesgcm = AESGCM(wrapping_key)
    return aesgcm.decrypt(nonce, rest, _WRAP_CTX_AAD)


# ---------------------------------------------------------------------------
# Keyfile persistence
# ---------------------------------------------------------------------------

def keyfile_path() -> pathlib.Path:
    """Return the canonical keyfile location: ``<data_dir>/keys/keyfile``.

    Falls back to the legacy ``~/.encre/keyfile`` only if the data root
    cannot be resolved (e.g. a partially installed interpreter).
    """
    try:
        from encre.paths import get_data_dir

        return get_data_dir("keys", ensure=False) / "keyfile"
    except Exception:  # pragma: no cover - defensive
        return _LEGACY_KEYFILE_PATH


def _candidate_keyfiles() -> list[pathlib.Path]:
    """Keyfile locations to try, in priority order (new, then legacy)."""
    new = keyfile_path()
    return [new] if new == _LEGACY_KEYFILE_PATH else [new, _LEGACY_KEYFILE_PATH]


def migrate_keyfile() -> pathlib.Path:
    """Move a legacy ``~/.encre/keyfile`` into the data tree.  Idempotent.

    The master key itself is untouched -- only the file moves -- so every
    existing ciphertext stays decryptable.  Returns the canonical path.
    """
    target = keyfile_path()
    if target.exists():
        return target
    legacy = _LEGACY_KEYFILE_PATH
    if legacy.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            target.parent.chmod(stat.S_IRWXU)
        shutil.move(str(legacy), str(target))
        with contextlib.suppress(OSError):
            os.chmod(target, _KEYFILE_MODE)
    return target


def _create_keyfile() -> bytes:
    """Generate a new master key, wrap it, write the keyfile and return the key."""
    path = keyfile_path()
    keyfile_dir = path.parent
    keyfile_dir.mkdir(parents=True, exist_ok=True)
    # Only the owner of this directory should have access
    with contextlib.suppress(OSError):
        keyfile_dir.chmod(stat.S_IRWXU)

    master_key = _generate_master_key()
    wrapped = _KEYFILE_HEADER + _wrap_master_key(master_key)

    # Atomic write via temp file + rename
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(wrapped)
    with contextlib.suppress(OSError):
        os.chmod(tmp, _KEYFILE_MODE)
    os.replace(tmp, path)

    return master_key


def _load_keyfile() -> bytes | None:
    """Read the wrapped master key from disk and unwrap it.

    Wire format: ``version byte || nonce || ciphertext || tag``.  A keyfile
    with an unknown or missing version byte is refused (fail-closed) rather
    than silently replaced -- the caller decides whether to regenerate.

    Tries the canonical location first, then the legacy ``~/.encre`` one so
    an install that has not been migrated yet still starts.

    Returns None if no keyfile exists or it cannot be decrypted.
    """
    for path in _candidate_keyfiles():
        if not path.exists():
            continue
        try:
            raw = path.read_bytes()
            if not raw.startswith(_KEYFILE_HEADER):
                continue
            body = raw[len(_KEYFILE_HEADER):]
            if len(body) < _NONCE_LENGTH + _KEY_LENGTH + _TAG_LENGTH:
                continue
            return _unwrap_master_key(body)
        except Exception:
            continue
    return None


def ensure_keyfile() -> bytes:
    """Return the active master key, creating the keyfile if missing.

    This is the primary entry‑point -- call once at startup.
    """
    global _master_key_cache
    if _master_key_cache is not None:
        return _master_key_cache

    key = _load_keyfile()
    if key is None:
        key = _create_keyfile()

    _master_key_cache = key
    return key


# ---------------------------------------------------------------------------
# AES-256-GCM encrypt / decrypt (user-facing API)
# ---------------------------------------------------------------------------

if _t.TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_aesgcm() -> AESGCM:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(ensure_keyfile())


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF‑8 string with AES‑256‑GCM.

    Returns a base64‑encoded ciphertext (nonce || ct || tag).
    """
    return encrypt_bytes(plaintext.encode("utf-8"))


def decrypt(ciphertext: str) -> str:
    """Decrypt a base64 ciphertext back to the original UTF‑8 string."""
    return decrypt_bytes(ciphertext).decode("utf-8")


def encrypt_bytes(plaintext: str | bytes) -> str:
    """Encrypt bytes (or a string treated as UTF‑8) -> base64 string."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    aesgcm = _get_aesgcm()
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    ct = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ct).decode("ascii")


class DecryptionError(ValueError):
    """Raised when ciphertext cannot be authenticated or decrypted.

    Typical causes: the blob was produced under a different machine-binding
    key (copied from another host, or written before the key-derivation
    scheme changed) or the data is corrupted.  Callers should surface this
    as an actionable error, not a crash.
    """


def decrypt_bytes(ciphertext: str) -> bytes:
    """Decrypt a base64 ciphertext -> raw bytes."""
    try:
        raw = base64.b64decode(ciphertext)
        nonce = raw[:_NONCE_LENGTH]
        ct = raw[_NONCE_LENGTH:]
        aesgcm = _get_aesgcm()
        return aesgcm.decrypt(nonce, ct, None)
    except DecryptionError:
        raise
    except Exception as exc:
        raise DecryptionError(
            "Decryption failed: the data was not encrypted with this "
            "machine's key (wrong machine, reinstalled OS/user, or an older "
            "key-derivation scheme) or the ciphertext is corrupted."
        ) from exc


# ---------------------------------------------------------------------------
# Convenience: encrypt/decrypt raw bytes -> raw bytes (for binary data)
# ---------------------------------------------------------------------------

def encrypt_raw(plain_bytes: bytes) -> bytes:
    """Encrypt raw bytes -> nonce || ciphertext || tag."""
    aesgcm = _get_aesgcm()
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    return nonce + aesgcm.encrypt(nonce, plain_bytes, None)


def decrypt_raw(packed: bytes) -> bytes:
    """Decrypt nonce || ciphertext || tag -> raw bytes."""
    try:
        nonce = packed[:_NONCE_LENGTH]
        ct = packed[_NONCE_LENGTH:]
        aesgcm = _get_aesgcm()
        return aesgcm.decrypt(nonce, ct, None)
    except DecryptionError:
        raise
    except Exception as exc:
        raise DecryptionError(
            "Decryption failed: the data was not encrypted with this "
            "machine's key (wrong machine, reinstalled OS/user, or an older "
            "key-derivation scheme) or the ciphertext is corrupted."
        ) from exc
