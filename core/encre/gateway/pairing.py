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

"""DM pairing flow for gateway user authorization.

Pairing is the zero-trust onboarding path for the gateway.  Instead of an
operator hand-editing an allowlist, an already-authorized user mints a
short-lived code and a new user redeems it by DMing the bot ``/pair <code>``,
which binds their ``(platform, user_id)`` into the authorized set.

Flow::

    Operator: /pair            ->  PairingStore.create_code()  ->  "ABC123"
    New user:  /pair ABC123     ->  PairingStore.redeem(...)    ->  "Paired!"

The store is one of five inputs to :class:`encre.gateway.authz.AuthorizationChecker`
-- the pairing layer.  It is consulted on every inbound message, so
:meth:`PairingStore.is_paired` must stay a lock-protected dict lookup.

State lives in ``<data_dir>/gateway_pairing.json`` as two maps: pending codes
and redeemed user bindings.  The file goes through :mod:`encre.secure_io`, so
pairing state is at rest under the same encryption as every other Encre data
file.  Every mutation persists immediately, which is what makes
:meth:`PairingStore.close` a no-op and what makes a crash lose at most the last
unflushed mutation.
"""

import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from encre.config import get_data_dir
from encre.secure_io import read_json, write_json

logger = logging.getLogger("encre.gateway.pairing")

# Pairing code lifetime (seconds).  Default 10-minute window.
PAIRING_CODE_TTL = 600.0
# Length of the human-friendly pairing code (no ambiguous chars).
PAIRING_CODE_LEN = 6
# Alphabet without look-alikes (no 0/O, 1/I/L).  Codes are typed into a chat by
# hand, so ambiguous glyphs cost redemptions, not just aesthetics.
_PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class PairingCode:
    """A pending (un-redeemed) pairing code.

    Attributes:
        code: The secret string the redeemer types.  Stored as minted; lookups
            are normalized with ``strip().upper()`` so case never matters.
        created_at: Unix timestamp of mint, for debug and expiry display.
        expires_at: Unix timestamp after which redemption is refused.
        redeemed: Whether one user has already used it.  A code is single-use;
            the flag keeps the minted entry inspectable after use instead of
            deleting it immediately.
    """

    code: str
    created_at: float
    expires_at: float
    redeemed: bool = False


@dataclass
class PairedUser:
    """A user binding created by redeeming a pairing code.

    Attributes:
        platform: Platform id the binding applies to.  Pairing is
            platform-scoped, so redeeming a code on telegram does not authorize
            the same human on discord.
        user_id: The platform user id, in the form the platform adapter reports
            in :attr:`encre.gateway.session.SessionSource.user_id`.
        paired_at: Unix timestamp of the successful redemption.
    """

    platform: str
    user_id: str
    paired_at: float


def _binding_key(platform: str, user_id: str) -> str:
    """Build the composite dict key for a ``(platform, user_id)`` binding.

    The separator is ``:``.  A platform user id that itself contains a colon
    could in principle collide with a different ``(platform, user_id)`` pair;
    the platform ids are all lowercase alphabetic, so the prefix stays
    unambiguous in practice.  Do not change the separator without considering
    that persisted files already use it.
    """
    return f"{platform}:{user_id}"


class PairingStore:
    """Persisted pairing state: pending codes plus authorized user bindings.

    The store is the source of truth for "which users paired themselves via a
    code".  It is one input to the authorization check (the pairing layer, see
    :mod:`encre.gateway.authz`); allow-all / allowlist / global flags are
    evaluated separately.

    Lifecycle: constructed (loads state) -> :meth:`create_code` /
    :meth:`redeem` / :meth:`unpair` mutate and persist ->
    :meth:`is_paired` is polled on every inbound message ->
    :meth:`close` at shutdown (no-op).

    Concurrency: one :class:`threading.Lock` guards all state.  A ``Lock``
    rather than an ``asyncio.Lock`` is deliberate -- the inbound path that
    calls :meth:`is_paired` is synchronous platform callback code that can run
    on any worker thread, and the adapters may share this store.

    Invariant: :meth:`_save` must only be called while ``self._lock`` is held,
    so that a persisted snapshot never mixes state from two interleaved
    mutations.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        """Open the store at ``path`` and load any persisted state into memory.

        The path is resolved first and stored as a :class:`pathlib.Path` so the
        lock, the two tables, and the snapshot writer all share one location
        without re-parsing a string on every :meth:`_save`. Loading is done in
        the constructor rather than lazily on first read, because
        :meth:`is_paired` sits on the synchronous inbound message path and must
        never trigger disk IO mid-request.

        Invariant: after construction the in-memory :attr:`_codes` and
        :attr:`_paired` tables are the only authoritative view of pairing
        state, and every later mutation pairs it with a :meth:`_save` call under
        :attr:`_lock`. A missing or unreadable file yields an empty store
        rather than a raise, so a broken pairing snapshot can never block
        gateway startup.

        Args:
            path: File to persist state under.  Defaults to
                ``<data_dir>/gateway_pairing.json``.
        """
        if path is None:
            path = Path(get_data_dir()) / "gateway_pairing.json"
        self._path = Path(path)
        self._lock = threading.Lock()
        self._codes: dict[str, PairingCode] = {}
        self._paired: dict[str, PairedUser] = {}
        self._load()

    def _load(self) -> None:
        """Load codes and bindings from disk into memory.

        A missing file means a first run and is not an error.  A file that
        exists but fails to decode (corrupt, wrong schema, a key renamed in a
        different Encre version) degrades to an empty store rather than raising,
        because refusing to start the gateway because of pairing state is the
        worse failure.  Callers get no signal either way, so nothing else can
        tell a cold start from a corrupted one.

        Args: none.
        """
        with self._lock:
            if not self._path.exists():
                return
            try:
                data = read_json(self._path, default={})
                self._codes = {
                    str(k): PairingCode(**v) for k, v in (data.get("codes") or {}).items()
                }
                self._paired = {
                    str(k): PairedUser(**v) for k, v in (data.get("paired") or {}).items()
                }
            except Exception as e:
                logger.warning("[pairing] failed to load %s: %s", self._path, e)
                self._codes = {}
                self._paired = {}

    def _save(self) -> None:
        """Persist both maps to disk.

        Caller holds self._lock.

        Failures are logged and swallowed.  Persistence is best-effort
        durability here: the in-memory maps remain authoritative for this
        process, so a transient write failure costs at most the unflushed
        mutations rather than refusing the operation that triggered them.

        Args: none.
        """
        try:
            data = {
                "codes": {k: asdict(v) for k, v in self._codes.items()},
                "paired": {k: asdict(v) for k, v in self._paired.items()},
            }
            write_json(self._path, data)
        except Exception as e:
            logger.warning("[pairing] failed to save %s: %s", self._path, e)

    def create_code(self, ttl: float = PAIRING_CODE_TTL) -> str:
        """Mint a fresh pairing code valid for ``ttl`` seconds.

        Returns the human-friendly code string.  Existing pending codes are
        left in place (an operator may mint several for different users).

        Args:
            ttl: Lifetime in seconds.  Defaults to :data:`PAIRING_CODE_TTL`.

        Returns:
            The code string, exactly as stored.

        Note:
            Randomness is drawn outside the lock, which keeps the critical
            section to a single dict insert plus one file write.  With a 32-char
            alphabet and a 6-char code a collision is rare; if one does occur
            the new code overwrites the older pending code, because it is the
            index being stored.
        """
        code = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LEN))
        now = time.time()
        with self._lock:
            self._codes[code] = PairingCode(code, now, now + ttl)
            self._save()
        logger.info("[pairing] minted code (ttl=%.0fs)", ttl)
        return code

    def redeem(self, code: str, platform: str, user_id: str) -> bool:
        """Redeem a pending code, binding ``(platform, user_id)``.

        Returns True on success, False if the code is unknown / expired /
        already redeemed.  A successful redeem is idempotent for the binding:
        re-redeeming an already-paired user's code is a no-op success only if
        the code is still pending.

        Args:
            code: The code as typed by the redeemer.  Whitespace is stripped and
                the string uppercased, matching the minted form.
            platform: Platform id of the redeemer's chat.
            user_id: Platform user id of the redeemer.

        Returns:
            Whether the redemption bound the user.

        Note:
            The expiry check deletes the code entry before returning, so an
            expired code cannot be redeemed by a second clock-read later.
            Redeeming again for an already-bound user overwrites
            :attr:`PairedUser.paired_at` rather than rejecting, which keeps
            re-pairing a device painless.
        """
        if not code or not platform or not user_id:
            return False
        code = code.strip().upper()
        with self._lock:
            entry = self._codes.get(code)
            if entry is None:
                return False
            if entry.redeemed:
                return False
            if time.time() > entry.expires_at:
                self._codes.pop(code, None)
                self._save()
                return False
            entry.redeemed = True
            key = _binding_key(platform, user_id)
            self._paired[key] = PairedUser(platform, user_id, time.time())
            self._save()
            logger.info("[pairing] redeemed code for %s", key)
            return True

    def is_paired(self, platform: str, user_id: str) -> bool:
        """True if ``(platform, user_id)`` was bound by a prior redeem.

        This is the hot path: :class:`encre.gateway.authz.AuthorizationChecker`
        calls it for every inbound platform message, on every platform.  It is
        a single lock acquisition plus a dict membership test and allocates
        nothing, which is the whole reason the store keeps bindings in a
        ``dict`` instead of a list.

        Args:
            platform: Platform id.
            user_id: Platform user id.

        Returns:
            Whether the pair is authorized through pairing.  Empty inputs are
            always unauthorized, so a malformed message cannot match an empty
            binding.
        """
        if not platform or not user_id:
            return False
        with self._lock:
            return _binding_key(platform, user_id) in self._paired

    def unpair(self, platform: str, user_id: str) -> bool:
        """Remove a user binding.  Returns True if a binding was removed.

        Args:
            platform: Platform id.
            user_id: Platform user id.

        Returns:
            ``True`` if a binding existed and was removed.  A missing binding
            does not persist, because nothing changed.
        """
        if not platform or not user_id:
            return False
        with self._lock:
            existed = self._paired.pop(_binding_key(platform, user_id), None) is not None
            if existed:
                self._save()
            return existed

    def list_paired(self) -> list[PairedUser]:
        """Return all authorized user bindings (for status/debug).

        Returns:
            A copy of the binding values, so the caller can iterate or serialise
            without holding the lock.
        """
        with self._lock:
            return list(self._paired.values())

    def list_pending_codes(self) -> list[PairingCode]:
        """Return all pending (un-redeemed, un-expired) codes.

        Filtering happens here rather than in the caller so that every view of
        the store agrees on what "pending" means.  Expired and redeemed entries
        are kept in the in-memory dict on purpose -- they stay inspectable for
        an operator reviewing why a code failed -- so this method is the only
        place the liveness predicate is applied.

        Returns:
            Live codes, in insertion order.
        """
        now = time.time()
        with self._lock:
            return [c for c in self._codes.values() if not c.redeemed and c.expires_at > now]

    def close(self) -> None:
        """No-op (state is persisted on every mutation).

        Present so that the gateway's adapter/runner shutdown sequence can call
        ``close()`` uniformly on every resource without special-casing the
        store.  There is no open handle to release.
        """
