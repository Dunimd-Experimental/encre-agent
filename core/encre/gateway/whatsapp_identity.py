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

"""Canonicalisation of WhatsApp sender identity across JID and LID forms.

WhatsApp's bridge can surface one human under two different JID shapes inside
a single conversation:

- LID form: ``999999999999999@lid``
- Phone form: ``15551234567@s.whatsapp.net``

The bridge may flip between them for the same contact, so any two pieces of
code that compare WhatsApp identifiers have to collapse the aliases first or
they will disagree. Two call sites need this:

- the authorisation path, which matches an inbound sender against an
  allow-list configured by the operator;
- the session-key path in :mod:`encre.gateway.session`, which derives the
  agent session key from a chat id or a group participant id.

If each kept its own resolution logic the two would drift and the same person
would end up authorised under one shape and unauthorised under another. This
module is the single source of truth, so both paths import from here.

Layer position: this sits at the bottom of the gateway stack. It has no
adapter dependency, opens only the bridge's own mapping files, and returns
plain strings, so it is safe to call from authorisation checks, session-key
construction, and gateway hooks alike.

Public helpers:

- :func:`normalize_whatsapp_identifier` -- strip JID/LID/device/plus syntax
  down to the bare numeric identifier.
- :func:`to_whatsapp_jid` -- build a bridge-safe outbound JID from a bare
  phone number, passing an already-qualified JID through unchanged.
- :func:`canonical_whatsapp_identifier` -- walk the bridge's
  ``lid-mapping-*.json`` files and return one stable canonical identity
  across phone/LID variants.
- :func:`expand_whatsapp_aliases` -- return the full alias set for an
  identifier, for authorisation code that must match any known form of a
  sender.

Plugins that need per-sender behaviour on WhatsApp (role-based routing,
per-contact authorisation, policy gating in a gateway hook) should use
:func:`canonical_whatsapp_identifier` so their bookkeeping lines up with
session keys.
"""

import json
import logging
import re
from typing import Set

logger = logging.getLogger(__name__)

# Identifier character whitelist used as a path-safety gate in
# :func:`expand_whatsapp_aliases`. A real WhatsApp JID only ever contains
# digits, ``@``, ``.``, ``+``, and ``-`` once the device suffix has been
# stripped by :func:`normalize_whatsapp_identifier`, which is why the
# explicit ASCII range is used here instead of ``\w`` -- in Unicode mode
# ``\w`` would also match full-width digits, defeating the traversal
# protection. ``:`` is excluded too: a colon can never appear in a
# ``lid-mapping-`` filename anyway.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9@.+\-]+$")

from encre.config import get_data_dir
# Aliased so callers that expect ``encre_dir`` do not need to know the
# underlying accessor lives in :mod:`encre.paths` and is merely
# re-exported by :mod:`encre.config`.
get_encre_dir = get_data_dir


def normalize_whatsapp_identifier(value: str) -> str:
    """Strip WhatsApp JID/LID syntax down to its stable numeric identifier.

    Accepts any of the identifier shapes the WhatsApp bridge may emit:
    ``"60123456789@s.whatsapp.net"``, ``"60123456789:47@s.whatsapp.net"``,
    ``"60123456789@lid"``, or a bare ``"+601****6789"`` / ``"60123456789"``.
    Returns just the numeric identifier (``"60123456789"``) suitable for
    equality comparisons.

    The removal order is significant: the leading ``+`` is dropped first so
    a stripped string can never be misread as starting with punctuation, then
    the ``:device`` suffix and the ``@domain`` part are cut off by splitting
    on the first occurrence of each separator. Splitting rather than
    deleting the substrings keeps multi-separator input well formed.

    Useful for plugins that want to match sender IDs against user-supplied
    config (phone numbers in ``config.yaml``) without worrying about which
    variant the bridge happens to deliver.

    Note:
        This is the inverse direction of :func:`to_whatsapp_jid`: this
        function destroys the JID suffix to make identifiers comparable,
        that one rebuilds the suffix to make a target sendable.

    Args:
        value: The raw identifier string as emitted by the bridge. ``None``
            is tolerated and treated as empty.

    Returns:
        The bare numeric identifier, or an empty string when ``value`` is
        empty/whitespace or ``None``.
    """
    return (
        str(value or "")
        .strip()
        .replace("+", "", 1)
        .split(":", 1)[0]
        .split("@", 1)[0]
    )


# A target that is "just a phone number": optional leading ``+`` then digits
# and the usual human separators (spaces, dots, dashes, parens). Anything that
# already carries an ``@`` is a fully-qualified JID and must pass through
# untouched (group ``@g.us``, LID ``@lid``, ``status@broadcast`` etc.). The
# separator class is deliberately permissive so an operator can paste
# ``"555 123-4567"`` straight out of a contact list.
_BARE_PHONE_RE = re.compile(r"^\+?[\d\s().\-]+$")


def to_whatsapp_jid(value: str) -> str:
    """Normalize an *outbound* WhatsApp target to a bridge-safe JID.

    Baileys' ``jidDecode`` crashes on a bare phone number -- it expects a
    fully-qualified JID such as ``50766715226@s.whatsapp.net``. This helper is
    the inverse of :func:`normalize_whatsapp_identifier`: instead of stripping a
    JID down to its numeric core for comparison, it *builds* the JID a send must
    use.

    Behaviour:

    - ``"+50766715226"`` / ``"50766715226"`` -> ``"50766715226@s.whatsapp.net"``
    - ``"50766715226@s.whatsapp.net"`` -> unchanged
    - ``"group-id@g.us"`` / ``"130631430344750@lid"`` -> unchanged
    - ``"user:device@s.whatsapp.net"`` style colon-before-``@`` -> ``@`` form
    - anything that is not a recognizable bare phone -> returned unchanged so
      the bridge can surface a meaningful error rather than us mangling it.

    Returns ``""`` for an empty/whitespace input.

    Invariant: this function never raises for odd input. A broken outbound
    target should produce a bridge error at send time, not an exception inside
    the caller's delivery path.

    Args:
        value: The outbound target, either a bare phone number or a JID.

    Returns:
        A bridge-safe JID string, or ``""`` when input is empty.
    """
    if not value:
        return ""

    normalized = str(value).strip()
    # Drop a device suffix before the domain: ``user:device@domain`` is a
    # legacy Baileys shape whose ``:device`` part is not addressable --
    # collapse it to ``user@domain``. (Mirrors
    # normalize_whatsapp_identifier, which splits the bare id on ``:`` for the
    # same reason.)
    if ":" in normalized and "@" in normalized:
        prefix, _, domain = normalized.partition("@")
        normalized = f"{prefix.split(':', 1)[0]}@{domain}"

    # Already a fully-qualified JID -- leave it alone.
    if "@" in normalized:
        return normalized

    # Only after the ``@`` check do we treat the input as a bare phone, so a
    # group id like ``130631430344750@g.us`` can never be re-derived into a
    # wrong phone-form JID.
    if _BARE_PHONE_RE.fullmatch(normalized):
        digits = re.sub(r"\D+", "", normalized)
        if digits:
            return f"{digits}@s.whatsapp.net"

    return normalized


def expand_whatsapp_aliases(identifier: str) -> Set[str]:
    """Resolve WhatsApp phone/LID aliases via bridge session mapping files.

    Returns the set of all identifiers transitively reachable through the
    bridge's ``whatsapp/session/lid-mapping-*.json`` files under the Encre
    data dir, starting from ``identifier``. The result always includes the
    normalized input itself, so callers can safely ``in`` check against the
    return value without a separate fallback branch.

    Walk algorithm: a plain BFS. Each dequeued identifier is added to the
    resolved set, then both its forward mapping (``lid-mapping-<id>.json``)
    and its reverse mapping (``lid-mapping-<id>_reverse.json``) are read.
    The ``_reverse`` variant exists because a bridge connection learns the
    alias from the opposite direction -- it sees an LID first and records
    the phone under ``_reverse`` -- so both files are needed for the graph
    to be complete. Reachable-but-not-yet-enqueued aliases are appended to
    the queue, and the ``current in resolved`` guard is what terminates the
    walk, since a mapping cycle must not loop forever.

    File-read failures are swallowed at debug level. The mapping files are a
    best-effort accelerator, not authoritative state: a partially written
    file during a bridge restart must degrade to "single identity" rather
    than fail authorisation.

    Args:
        identifier: The starting WhatsApp identifier (any variant).

    Returns:
        The full set of reachable alias identifiers, including the
        normalized input. Empty set when ``identifier`` normalizes to
        empty.
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return set()

    # Note both fragments are joined, so the resolved directory is
    # ``<data_dir>/platforms/whatsapp/session/whatsapp/session``. That is the
    # layout the bridge itself writes to; changing it here would silently
    # disconnect this resolver from the real mapping files.
    session_dir = get_encre_dir("platforms/whatsapp/session", "whatsapp/session")
    resolved: Set[str] = set()
    queue = [normalized]

    while queue:
        current = queue.pop(0)
        if not current or current in resolved:
            continue
        # Defense-in-depth: reject identifiers that could sneak path
        # separators / traversal segments into the ``lid-mapping-{current}``
        # filename below. The hardcoded ``lid-mapping-`` prefix already
        # prevents escape via pathlib's component split (an attacker can't
        # create ``lid-mapping-..`` as a real directory in session_dir), but
        # this keeps the identifier space to the characters WhatsApp JIDs
        # actually use and avoids depending on that filesystem-layout
        # invariant.
        if not _SAFE_IDENTIFIER_RE.match(current):
            continue

        resolved.add(current)
        for suffix in ("", "_reverse"):
            mapping_path = session_dir / f"lid-mapping-{current}{suffix}.json"
            if not mapping_path.exists():
                continue
            try:
                mapped = normalize_whatsapp_identifier(
                    json.loads(mapping_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("whatsapp_identity: failed to read %s: %s", mapping_path, exc)
                continue
            if mapped and mapped not in resolved:
                queue.append(mapped)

    return resolved


def canonical_whatsapp_identifier(identifier: str) -> str:
    """Return a stable WhatsApp sender identity across phone-JID/LID variants.

    WhatsApp may surface the same person under either a phone-format JID
    (``60123456789@s.whatsapp.net``) or a LID (``1234567890@lid``). This
    applies to a DM ``chat_id`` *and* to the ``participant_id`` of a member
    inside a group chat -- both represent a user identity, and the bridge may
    flip between the two for the same human.

    This helper reads the bridge's ``whatsapp/session/lid-mapping-*.json``
    files, walks the mapping transitively, and picks the shortest
    (numeric-preferred) alias as the canonical identity.
    :func:`encre.gateway.session.build_session_key` uses this for both
    WhatsApp DM chat_ids and WhatsApp group participant_ids, so callers get
    the same session-key identity.

    Plugins that need per-sender behaviour (role-based routing,
    authorisation, per-contact policy) should use this so their
    bookkeeping stays consistent even when the bridge reshuffles aliases.

    The ordering key is ``(len, value)``, not length alone: ties must be
    broken by value so the function is deterministic across processes and
    restarts. ``min`` returns the same alias every time for the same alias
    set, which is the whole point of calling it canonical.

    Returns an empty string if ``identifier`` normalizes to empty. If no
    mapping files exist yet (fresh bridge install), returns the
    normalized input unchanged.

    Note:
        This function performs filesystem reads, one per reachable alias, so
        it is safe to call at session-key construction time but should not be
        run on every message of a long conversation. Callers normally resolve
        once per inbound message and reuse the result.

    Args:
        identifier: The raw WhatsApp identifier (phone JID, LID, or bare
            number) to canonicalise.

    Returns:
        The stable canonical identifier, or ``""`` when input is empty.
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return ""

    # expand_whatsapp_aliases always includes `normalized` itself in the
    # returned set, so the min() below degrades gracefully to `normalized`
    # when no lid-mapping files are present.
    aliases = expand_whatsapp_aliases(normalized)
    return min(aliases, key=lambda candidate: (len(candidate), candidate))
