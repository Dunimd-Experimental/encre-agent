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

"""Markdown-to-Signal inline styling translator.

Encre's Signal adapter (:class:`encre.gateway.platforms.signal.SignalAdapter`) is
one of the IM platform adapters driven by :class:`encre.gateway.run.GatewayRunner`.
Agent output reaches that adapter's ``send()`` path via
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`, so this module sits
on the outbound edge of Encre's Signal surface.

Signal clients do not render Markdown. Inline styling is carried out of band as a
``bodyRanges`` annotation, which signal-cli exposes as the ``textStyle`` /
``textStyles`` request parameters. Keeping the Markdown-to-``bodyRanges`` lowering
in one module means every outbound Signal message, whichever Encre call path
produced it, emits the same ``bodyRanges`` shape and therefore renders identically
in the recipient's client.

Data flow: agent Markdown text -> :func:`markdown_to_signal` ->
``(plain_text, style_strings)`` -> signal-cli ``send`` RPC payload.

Public API:
    * :func:`markdown_to_signal` -- the only public entry point.
"""

import re


def markdown_to_signal(text: str) -> tuple[str, list[str]]:
    """Lower Markdown-flavoured text into Signal's inline styling model.

    Signal has no Markdown renderer, so visible Markdown syntax must be removed
    from the body and re-expressed as a list of style ranges that signal-cli
    accepts as ``textStyles``. Each entry uses the textual form
    ``start:length:STYLE``, and positions are measured in UTF-16 code units, the
    unit the Signal wire protocol uses for string offsets. Plain ``len`` counts
    code points, so any text with astral-plane characters (emoji, CJK
    ideographs) would yield silently wrong ranges if measured that way; the UTF-16
    helper below is used instead.

    Conversion order is fixed and load-bearing:
        1. Whitespace runs and edge padding are normalised so every offset later
           reported for the body is relative to the body origin.
        2. Markdown bullet markers become real Unicode bullets, with fenced code
           blocks left byte-for-byte intact.
        3. Fenced code blocks collapse to their inner content and are recorded as
           MONOSPACE ranges.
        4. Heading markers are dropped and the surviving heading text is recorded
           as BOLD.
        5. Inline emphasis spans are collected with collision detection, so no
           character range can be claimed by two patterns.
        6. Markdown delimiter characters are deleted and every surviving
           pre-removal range is rebased onto the shortened text.
        7. Code-point ranges are converted to UTF-16 code-unit ranges and rendered
           as ``start:length:STYLE`` strings.

    Supported styles: ``BOLD``, ``ITALIC``, ``STRIKETHROUGH``, ``MONOSPACE``.

    Args:
        text: Markdown-flavoured source string produced by the agent.

    Returns:
        Tuple of ``(plain_text, style_strings)`` where ``plain_text`` is the body
        with Markdown syntax removed and ``style_strings`` is the list of
        ``start:length:STYLE`` entries signal-cli applies to it.

    Note:
        The returned ``style_strings`` are consumed by
        :meth:`encre.gateway.platforms.signal.SignalAdapter.send`, which puts a
        single entry under ``textStyle`` and multiple entries under ``textStyles``.
    """

    def _utf16_len(s: str) -> int:
        """Return the length of *s* measured in UTF-16 code units.

        ``bodyRanges`` offsets are UTF-16 code units, so the string is encoded as
        UTF-16 little-endian and the byte length is halved rather than counting
        Unicode code points directly. This is what keeps astral-plane characters,
        which occupy two UTF-16 units each, at the correct offsets.

        Args:
            s: The string whose UTF-16 length should be computed.

        Returns:
            The number of UTF-16 code units occupied by *s*.
        """
        return len(s.encode("utf-16-le")) // 2

    def _normalize_bullet_markers(source: str) -> str:
        """Replace Markdown bullet markers with the Unicode bullet character.

        Signal has no list renderer, so ``- item`` and ``* item`` would otherwise
        arrive as literal Markdown markers and read as noise. They are rewritten
        to a real bullet (U+2022) so the list stays legible in any client. Fenced
        code blocks are preserved byte-for-byte: a list-looking line inside a fence
        is code, not prose, and re-bulleting it would corrupt the sample.

        Args:
            source: The text whose bullet markers should be normalised.

        Returns:
            A copy of *source* with top-level bullet markers replaced, leaving
            code fences untouched.
        """
        # Splitting on the fence keeps code blocks as odd-indexed parts, so the
        # re-bullet pass can skip them rather than corrupting code samples.
        parts = re.split(r"(```.*?```)", source, flags=re.DOTALL)
        for idx, part in enumerate(parts):
            # Odd indices are the fenced blocks themselves; leave them alone.
            if idx % 2 == 1:
                continue
            # Up to three leading spaces/tabs are tolerated and echoed back via
            # the capture group so existing indentation is preserved.
            parts[idx] = re.sub(r"(?m)^([ \t]{0,3})[-*+]\s+", r"\1鈥?", part)
        return "".join(parts)

    # Three or more blank lines collapse so blank-line runs do not inflate the
    # body and drag every later style offset along with them.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip the edges so offset 0 is the true body origin, which is what
    # signal-cli expects from the caller.
    text = text.strip()
    # Bullets are normalised early so later passes measure final character widths
    # and the code-fence protection is already in place.
    text = _normalize_bullet_markers(text)

    # Ranges detected before delimiter stripping are held here as
    # (start, length, style) tuples in code-point coordinates of the current *text*.
    styles: list[tuple[int, int, str]] = []

    # --- Fenced code blocks become a single MONOSPACE range. ---
    code_block = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
    while match := code_block.search(text):
        # Only the inner content survives; the fence markers are discarded.
        inner = match.group(1).rstrip("\n")
        start = match.start()
        text = text[: match.start()] + inner + text[match.end() :]
        styles.append((start, len(inner), "MONOSPACE"))

    # --- Headings: drop the '#' markers, keep the text styled as BOLD. ---
    heading = re.compile(r"^#{1,6}\s+", re.MULTILINE)
    new_text = ""
    last_end = 0
    for match in heading.finditer(text):
        # Text preceding this heading is carried over verbatim.
        new_text += text[last_end : match.start()]
        last_end = match.end()
        # The heading body runs from the marker to the end of its line.
        eol = text.find("\n", match.end())
        if eol == -1:
            eol = len(text)
        heading_text = text[match.end() : eol]
        start = len(new_text)
        new_text += heading_text
        styles.append((start, len(heading_text), "BOLD"))
        last_end = eol
    new_text += text[last_end:]
    text = new_text

    # Inline emphasis patterns, ordered so the specific, well-delimited forms
    # (bold, strikethrough, inline code) are tried before the weaker single-* and
    # single-_ italic forms that would otherwise swallow them.
    patterns = [
        (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), "BOLD"),
        (re.compile(r"__(.+?)__", re.DOTALL), "BOLD"),
        (re.compile(r"~~(.+?)~~", re.DOTALL), "STRIKETHROUGH"),
        (re.compile(r"`(.+?)`"), "MONOSPACE"),
        (re.compile(r"(?<!\*)\*(?!\*| )(.+?)(?<!\*)\*(?!\*)"), "ITALIC"),
        (re.compile(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)"), "ITALIC"),
    ]

    # Each accepted match is stored as (outer_start, outer_end, group1_start,
    # group1_end, style). ``occupied`` records claimed outer spans so a character
    # range cannot be styled by two patterns at once.
    all_matches: list[tuple[int, int, int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for pattern, style in patterns:
        for match in pattern.finditer(text):
            ms, me = match.start(), match.end()
            # Reject the match if its outer span overlaps an already claimed one.
            if not any(ms < oe and me > os for os, oe in occupied):
                all_matches.append((ms, me, match.start(1), match.end(1), style))
                occupied.append((ms, me))
    # Sort so the deletion and rebuild passes walk the text left to right.
    all_matches.sort()

    # Character spans to delete: the Markdown delimiters immediately outside each
    # captured group, expressed as (position, length) in pre-stripping text.
    removals: list[tuple[int, int]] = []
    for ms, me, g1s, g1e, _ in all_matches:
        if g1s > ms:
            removals.append((ms, g1s - ms))
        if me > g1e:
            removals.append((g1e, me - g1e))
    removals.sort()

    def _adjust(pos: int) -> int:
        """Rebase a pre-stripping offset onto the delimiter-stripped text.

        Deleting Markdown delimiters shifts every later offset left. This returns
        where *pos* lands once all earlier deletions have been applied.

        Args:
            pos: The original offset within the pre-stripping text.

        Returns:
            The equivalent offset within the delimiter-stripped text.
        """
        shift = 0
        for remove_pos, remove_len in removals:
            # Only deletions strictly before *pos* contribute to its shift, and
            # removals are sorted, so anything after is unreachable.
            if remove_pos < pos:
                shift += min(remove_len, pos - remove_pos)
            else:
                break
        return pos - shift

    # The code-block and heading ranges were captured before delimiters were
    # deleted, so rebase them onto the shortened text to keep them accurate.
    adjusted_prior: list[tuple[int, int, str]] = []
    for start, length, style in styles:
        new_start = _adjust(start)
        new_end = _adjust(start + length)
        if new_end > new_start:
            adjusted_prior.append((new_start, new_end - new_start, style))

    # Rebuild the body by concatenating plain segments with each captured inner
    # group, recording the inline range at its post-stripping position.
    result = ""
    last_end = 0
    inline_styles: list[tuple[int, int, str]] = []
    for ms, me, g1s, g1e, style in all_matches:
        result += text[last_end:ms]
        pos = len(result)
        inner = text[g1s:g1e]
        result += inner
        inline_styles.append((pos, len(inner), style))
        last_end = me
    result += text[last_end:]
    text = result

    # Merge the rebased pre-strip ranges with the inline ranges, then render each
    # as a UTF-16 ``start:length:STYLE`` string for signal-cli.
    styles = adjusted_prior + inline_styles

    style_strings: list[str] = []
    for cp_start, cp_len, style_type in sorted(styles):
        # Defensive bound check: any range that slipped past the final arithmetic
        # is dropped rather than sent to signal-cli, which would reject the payload.
        if cp_start < 0 or cp_start + cp_len > len(text):
            continue
        # Convert code-point offsets to UTF-16 code-unit offsets for the wire.
        u16_start = _utf16_len(text[:cp_start])
        u16_len = _utf16_len(text[cp_start : cp_start + cp_len])
        style_strings.append(f"{u16_start}:{u16_len}:{style_type}")

    return text, style_strings
