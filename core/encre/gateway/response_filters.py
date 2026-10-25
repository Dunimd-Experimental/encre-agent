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

"""Response filters for the gateway delivery pipeline.

The gateway runs the agent on inbound IM messages and normally delivers
whatever text the agent produced. One exception: the agent sometimes
deliberately declines to answer -- a reaction, a message it is ignoring, a
question that is not addressed to it. The model cannot signal that with a
structured field, so it narrates it in prose: ``(silent)``, ``no response``,
a muted-speaker emoji. If that prose were delivered, the user would receive a
literally empty-looking message from the bot.

This module owns the pattern matching for that case. It is pure string
logic: no adapters, no sessions, no I/O, so it is safe to call from the
hot path of
:meth:`encre.gateway.run.GatewayRunner` message handling and from stream
consumers without worrying about blocking.

Public API:
    :func:`is_intentional_silence_response` -- whole-response test.
    :func:`is_partial_silence_marker` -- containment test.
    :func:`strip_silence_markers` -- in-place marker removal.
"""

import re

# Matches a response that is *nothing but* a silence narration, optionally
# wrapped in markdown emphasis or code ticks. Two alternatives:
#   1. the words "silent", "silence", "no response", or "no reply" in
#      parentheses (or bare), with an optional trailing period;
#   2. a run of the muted-speaker U+1F507 emoji or ellipsis characters, which
#      is how some models express the same intent pictographically.
# Anchored at both ends so a real answer that merely mentions "(silent)" is
# never misclassified as silence.
_SILENCE_RE = re.compile(
    r"^[\s*_~`]*\(?\s*(silent|silence|no\s+response|no\s+reply)\s*\.?\)?[\s*_~`]*$"
    r"|^[\s*_~`]*[\U0001F507\.\u2026]+[\s*_~`]*$",
    re.IGNORECASE,
)

# Partial silence marker that may appear mid-stream (strip before delivery).
_PARTIAL_SILENCE_RE = re.compile(
    r"\(silent\)|\(silence\)|\(no response\)",
    re.IGNORECASE,
)


def is_intentional_silence_response(content: str | None) -> bool:
    """Return ``True`` when the response is only a silence narration token.

    This is the delivery gate used by
    :meth:`encre.gateway.run.GatewayRunner` after an agent run finalises: when
    it returns ``True`` the response is suppressed rather than posted to the
    platform, and the suppression is logged for an operator to verify the
    agent behaved as intended.

    Two guards make the test conservative, because a false positive silently
    swallows a real reply while a false negative only produces an
    unflattering-looking empty message:

    - ``None`` and empty input are not silence. An empty string is the
      natural result of a filtered or aborted run and must not be confused
      with a deliberate decision.
    - anything over 64 characters is not silence. A narration token fits in
      a few characters; anything longer is real content that happens to
      contain the words.

    Args:
        content: The final assembled response text, possibly ``None``.

    Returns:
        ``True`` only when the entire stripped content matches
        :data:`_SILENCE_RE`.
    """
    if not content:
        return False
    stripped = content.strip()
    if not stripped or len(stripped) > 64:
        return False
    return bool(_SILENCE_RE.match(stripped))


def is_partial_silence_marker(text: str) -> bool:
    """Return ``True`` when *text* contains an inline silence marker.

    Unlike :func:`is_intentional_silence_response` this is a containment
    test, so a marker embedded in otherwise real content counts. Callers use
    it to decide whether to scrub a chunk before delivery rather than to
    drop the whole response.

    Args:
        text: The text to inspect, e.g. an accumulated stream buffer.

    Returns:
        ``True`` if :data:`_PARTIAL_SILENCE_RE` matches anywhere in *text*.
    """
    return bool(_PARTIAL_SILENCE_RE.search(text))


def strip_silence_markers(text: str) -> str:
    """Remove inline silence markers from *text* and trim the result.

    Used to scrub a marker out of content that is otherwise worth
    delivering. The trailing ``strip()`` is deliberate: removing a marker
    can leave a dangling space at the boundary where it was, which would
    otherwise show up as odd spacing in the posted message.

    Args:
        text: The text to clean.

    Returns:
        A new string with every marker occurrence removed and leading and
        trailing whitespace trimmed. Never ``None``, and never empty when
        *text* had non-marker content.
    """
    return _PARTIAL_SILENCE_RE.sub("", text).strip()
