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
Transport-agnostic WhatsApp behavior shared by the Baileys bridge adapter
and the official WhatsApp Cloud API adapter.

The mixin provides:
- Allow-list / DM / group gating
- Mention detection (explicit @-mentions + configurable regex patterns)
- Quoted-reply-to-bot detection
- Broadcast / Channel / Newsletter filtering
- WhatsApp-flavored markdown conversion
- Outgoing chunk length budgeting

It is the *behavior layer*. Transport-specific concerns (subprocess management,
HTTP webhooks, Graph API calls, media upload protocols) live in each adapter.

Mixin contract -- the adapter must set these on ``self`` before any of the
mixin's methods are called (typically in ``__init__``):

    self.config        # encre.gateway.config.PlatformConfig
    self.name          # str -- adapter name (used in log lines)
    self._dm_policy             # str: "open" | "allowlist" | "disabled"
    self._allow_from            # set[str]
    self._group_policy          # str: "open" | "allowlist" | "disabled"
    self._group_allow_from      # set[str]
    self._mention_patterns      # list[re.Pattern]
    self._reply_prefix          # Optional[str]

Class attributes ``MAX_MESSAGE_LENGTH`` and ``DEFAULT_REPLY_PREFIX`` are
defined on the mixin and may be overridden per-adapter if needed.
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


class WhatsAppBehaviorMixin:
    """Shared behavior for all WhatsApp adapters (Baileys + Cloud API).

    See module docstring for the attribute contract the host adapter must
    satisfy. This mixin owns no state of its own -- every value it touches
    is either a class attribute or set by the adapter's ``__init__``.
    """

    # WhatsApp message limits -- practical UX limit, not protocol max.
    # WhatsApp allows ~65K but long messages are unreadable on mobile.
    MAX_MESSAGE_LENGTH: int = 4096
    supports_code_blocks = True  # WhatsApp renders fenced code blocks (monospace)

    DEFAULT_REPLY_PREFIX: str = "鈿?*Encre Agent*\n鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€\n"

    _OUTBOUND_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u2060\u2063\ufeff]")
    _OUTBOUND_ODD_SPACE_RE = re.compile(r"[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]")

    @classmethod
    def _sanitize_outbound_text(cls, content: str) -> str:
        """Remove invisible formatting chars that leak badly in WhatsApp.

        Some provider/gateway formatting paths can emit unicode like WORD
        JOINER (U+2060) plus NARROW NO-BREAK SPACE (U+202F). WhatsApp may
            render those as mojibake-looking prefixes in front of the text instead of
        invisible spacing. Keep normal text and emoji joiners intact, but
        strip known zero-width format chars and normalize odd unicode spaces.
        """
        if not content:
            return content
        content = cls._OUTBOUND_INVISIBLE_CHARS_RE.sub("", content)
        return cls._OUTBOUND_ODD_SPACE_RE.sub(" ", content)

    @property
    def enforces_own_access_policy(self) -> bool:
        """WhatsApp gates DM/group access at intake via dm_policy/group_policy."""
        return True

    # ------------------------------------------------------------------ config
    def _effective_reply_prefix(self) -> str:
        """Return the prefix to add to outgoing replies in self-chat mode.

        Subclasses that don't have a self-chat concept (the Cloud API
        adapter) can override this to always return ``""`` or apply a
        different policy.
        """
        whatsapp_mode = os.getenv("WHATSAPP_MODE", "self-chat")
        if whatsapp_mode != "self-chat":
            return ""
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        env_prefix = os.getenv("WHATSAPP_REPLY_PREFIX")
        if env_prefix is not None:
            return env_prefix.replace("\\n", "\n")
        return self.DEFAULT_REPLY_PREFIX

    def _outgoing_chunk_limit(self) -> int:
        """Reserve room for the reply prefix so the final message fits."""
        prefix_len = len(self._effective_reply_prefix())
        # Keep enough space for truncate_message's pagination indicator and
        # code-fence repair even if a user configures a very long prefix.
        return max(1024, self.MAX_MESSAGE_LENGTH - prefix_len)

    def _whatsapp_require_mention(self) -> bool:
        """Report whether group messages must mention the bot to be processed.

        The ``require_mention`` config extra takes precedence over the
        ``WHATSAPP_REQUIRE_MENTION`` environment variable: the env var is only
        consulted when the config key is absent, not when it is falsy, so an
        explicit ``false`` in config cannot be overridden by an exported env var.

        Returns:
            True when group intake requires an explicit trigger -- an @-mention,
            a quoted reply to the bot, a leading slash command, or a configured
            mention pattern -- before :meth:`_should_process_message` lets the
            message through.

        Note:
            String values count as on when they are ``true``, ``1``, ``yes``, or
            ``on`` case-insensitively; any other string counts as off.
        """
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("WHATSAPP_REQUIRE_MENTION", "false").lower() in {
            "true",
            "1",
            "yes",
            "on",
        }

    def _whatsapp_free_response_chats(self) -> set[str]:
        """Return the chat IDs exempted from the mention requirement.

        A group listed here is processed on every message no matter what
        :meth:`_whatsapp_require_mention` says, which is how an operator creates
        a private "always answer me" channel without weakening the public groups.

        Returns:
            Set of stripped chat identifiers. Empty set when neither the config
            extra nor ``WHATSAPP_FREE_RESPONSE_CHATS`` supplies a value.

        Note:
            Membership is a plain string comparison against the inbound
            ``chatId``, so entries must already be in the JID form the bridge
            delivers -- no LID/phone resolution is applied here.
        """
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("WHATSAPP_FREE_RESPONSE_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    @staticmethod
    def _coerce_allow_list(raw) -> set[str]:
        """Parse allow_from / group_allow_from from config or env var."""
        if raw is None:
            return set()
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    # ------------------------------------------------------------------ JID helpers
    @staticmethod
    def _normalize_whatsapp_id(value: Optional[str]) -> str:
        """Return a canonical WhatsApp identifier for comparison and storage.

        WhatsApp reaches this codebase in at least two historical shapes: the
        Baileys legacy ``phone:+6281234567890`` form and the plain
        ``6281234567890@s.whatsapp.net`` JID. The colon separator is only a
        display artifact of the legacy sender field, so the first ``:`` is
        rewritten to ``@`` before any allowlist or bot-ID comparison, otherwise
        the same contact is seen as two different principals.

        Returns:
            The stripped identifier with at most the first colon converted, or
            an empty string when ``value`` is falsy. Empty input yields ``""``
            so callers can rely on truthiness directly.
        """
        if not value:
            return ""
        normalized = str(value).strip()
        if ":" in normalized and "@" in normalized:
            normalized = normalized.replace(":", "@", 1)
        return normalized

    @staticmethod
    def _is_broadcast_chat(chat_id: str) -> bool:
        """True for WhatsApp pseudo-chats that aren't real conversations.

        Covers Status updates (Stories) and Channel/Newsletter broadcasts.
        These show up as inbound messages on Baileys but the agent should
        never reply -- answering a Story update spams the contact's status
        feed, and Channel posts aren't addressable in the first place.
        """
        if not chat_id:
            return False
        cid = chat_id.strip().lower()
        if cid == "status@broadcast":
            return True
        # @broadcast suffix covers status@broadcast plus any future
        # broadcast-list variants. @newsletter is the Channel JID suffix.
        if cid.endswith("@broadcast") or cid.endswith("@newsletter"):
            return True
        return False

    # ------------------------------------------------------------------ gating
    def _open_dm_opted_in(self) -> bool:
        """Whether the operator opted the instance into answering any DM.

        The ``dm_policy: open`` mode does not connect on its own; it still needs
        an explicit allow-all switch so a misconfiguration cannot silently expose
        a private phone number to strangers. The gateway-wide
        ``GATEWAY_ALLOW_ALL_USERS`` flag is honored first because it is the single
        knob operators reach for on a personal single-user gateway, while
        ``WHATSAPP_ALLOW_ALL_USERS`` gives the same effect scoped to WhatsApp.

        Returns:
            True when either flag is set to ``true``, ``1``, or ``yes``
            case-insensitively; False otherwise, including when both are unset.
        """
        if os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}:
            return True
        return os.getenv("WHATSAPP_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}

    @staticmethod
    def _matches_whatsapp_allowlist(candidate: str, allow_from) -> bool:
        """Match a WhatsApp identifier against an allowlist across phone/LID forms.

        WhatsApp delivers inbound senders in LID form (``<id>@lid``) while
        operators usually configure allowlists with phone numbers, and vice
        versa. A raw set-membership check therefore never matches a known
        contact. Resolve both the candidate and each allowlist entry through
        the bridge's ``lid-mapping-*.json`` files (the shared
        ``encre.gateway.whatsapp_identity`` helper that the gateway authz and
        session-key paths already use) so either configured form resolves to
        the inbound form.
        """
        if not allow_from:
            return False
        # Fast path: exact match against the raw configured value (e.g. a full
        # ``@g.us`` group JID or an entry that already matches verbatim).
        if candidate in allow_from:
            return True

        from encre.gateway.whatsapp_identity import (
            expand_whatsapp_aliases,
            normalize_whatsapp_identifier,
        )

        candidate_aliases = expand_whatsapp_aliases(candidate)
        if not candidate_aliases:
            return False
        for entry in allow_from:
            if entry == "*":
                return True
            if normalize_whatsapp_identifier(entry) in candidate_aliases:
                return True
            # Entry may itself be an unmapped form; expand it too so a phone
            # allowlist entry resolves when the inbound sender arrived as a LID.
            if expand_whatsapp_aliases(entry) & candidate_aliases:
                return True
        return False

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Strict DM authorization -- pairing does not imply access."""
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(sender_id, self._allow_from)
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Whether a DM may reach the gateway intake (pairing handshake path)."""
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._matches_whatsapp_allowlist(principal, self._allow_from)
        if self._dm_policy == "pairing":
            return True
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_group_allowed(self, chat_id: str) -> bool:
        """Check whether a group chat should be processed."""
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return self._matches_whatsapp_allowlist(chat_id, self._group_allow_from)
        if self._group_policy == "pairing":
            return False
        if self._group_policy == "open":
            return True
        return False

    def _compile_mention_patterns(self):
        """Compile the regex patterns that count as an implicit @-mention.

        ``@bot`` is not the only reliable way to reach the agent on WhatsApp:
        some clients do not offer mention autocomplete for bots and forwarded
        messages drop mention metadata entirely. Operators therefore configure
        free-form patterns such as ``(?i)\\bping\\b``. Patterns are compiled once
        at construction time rather than per message, and an invalid entry is
        dropped with a warning instead of raising, so one bad pattern cannot take
        the adapter offline.

        Returns:
            List of compiled case-insensitive patterns. Empty when nothing is
            configured, which disables the pattern trigger.

        Note:
            ``mention_patterns`` in config wins over ``WHATSAPP_MENTION_PATTERNS``.
            The env var is accepted as JSON, else as newline-separated then
            comma-separated text, because a shell export cannot hold a JSON
            array portably.
        """
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("WHATSAPP_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    patterns = [
                        part.strip() for part in raw.splitlines() if part.strip()
                    ]
                    if not patterns:
                        patterns = [
                            part.strip() for part in raw.split(",") if part.strip()
                        ]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning(
                "[%s] whatsapp mention_patterns must be a list or string; got %s",
                self.name,
                type(patterns).__name__,
            )
            return []

        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "[%s] Invalid WhatsApp mention pattern %r: %s",
                    self.name,
                    pattern,
                    exc,
                )
        if compiled:
            logger.info(
                "[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled)
            )
        return compiled

    def _bot_ids_from_message(self, data: Dict[str, Any]) -> set[str]:
        """Collect this gateway's own bot identifiers from a message event.

        A multi-instance deployment runs several accounts behind one gateway, so
        "mention the bot" cannot hard-code a single JID. The bridge ships the
        relevant identifiers in ``botIds`` on every event, and each one is run
        through :meth:`_normalize_whatsapp_id` so the legacy colon form and the
        ``@s.whatsapp.net`` form collapse onto one comparison key.

        Returns:
            Set of normalized bot identifiers, possibly empty when the bridge
            supplied none or every entry normalized to an empty string.
        """
        bot_ids = set()
        for candidate in data.get("botIds") or []:
            normalized = self._normalize_whatsapp_id(candidate)
            if normalized:
                bot_ids.add(normalized)
        return bot_ids

    def _message_is_reply_to_bot(self, data: Dict[str, Any]) -> bool:
        """Whether the message quotes one of this gateway's own bot messages.

        Quoting is the strongest attention signal that needs no mention
        autocomplete, which is why it bypasses ``require_mention`` in
        :meth:`_should_process_message`. Only the quoted message's participant is
        checked; the quoted body itself is irrelevant to this decision.

        Returns:
            True when ``quotedParticipant`` normalizes to a non-empty value that
            also appears in :meth:`_bot_ids_from_message`. False for messages
            that quote nothing, since there is then no participant to compare.
        """
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        if not quoted_participant:
            return False
        return quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: Dict[str, Any]) -> bool:
        """Whether the message addresses one of this gateway's bot accounts.

        Two signals are combined because WhatsApp clients disagree about how they
        report mentions. Structured ``mentionedIds`` is authoritative when
        present, but many clients -- and forwarded messages -- omit it, so the
        body text is scanned for the bot's bare JID with or without a leading
        ``@`` as a fallback. The bare-id scan is what makes ``+6281234567890`` in
        free text count the same as ``@+6281234567890``.

        Returns:
            True on a structured mention match or a body-text hit. False when no
            bot identifier is known at all, which also short-circuits the scan.
        """
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned_ids = {
            nid
            for candidate in (data.get("mentionedIds") or [])
            if (nid := self._normalize_whatsapp_id(candidate))
        }
        if mentioned_ids & bot_ids:
            return True

        body = str(data.get("body") or "")
        lower_body = body.lower()
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0].lower()
            if bare_id and (f"@{bare_id}" in lower_body or bare_id in lower_body):
                return True
        return False

    def _message_matches_mention_patterns(self, data: Dict[str, Any]) -> bool:
        """Whether any compiled mention pattern matches the message body.

        This is the escape hatch for operators who cannot rely on WhatsApp's
        mention system: a configured pattern such as ``\\bhey encre\\b`` lets a
        group talk to the agent with plain text. Patterns are searched rather
        than fully matched, so a hit anywhere in the body counts.

        Returns:
            True when at least one pattern matches. False immediately when no
            patterns were compiled, so the unconfigured case costs nothing.
        """
        if not self._mention_patterns:
            return False
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns)

    def _clean_bot_mention_text(self, text: str, data: Dict[str, Any]) -> str:
        """Strip this bot's own @-mentions from inbound text before prompting.

        WhatsApp keeps the mention target inside the message body, so an inbound
        ``@encre-bot please summarize`` reaches the model as two separate
        principals and it may answer the mention instead of the request. Only
        mentions of this gateway's own bot IDs are removed; mentions of other
        people stay, because they may name the user the reply should address.

        Returns:
            Text with each own-bot mention, plus any directly trailing comma,
            colon, hyphen, and whitespace, removed. The original text is returned
            unchanged when nothing matched or the result would be empty, so a
            message consisting solely of a mention is never lost.
        """
        if not text:
            return text
        bot_ids = self._bot_ids_from_message(data)
        cleaned = text
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(
                    rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned
                )
        return cleaned.strip() or text

    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        """Single decision point for whether an inbound message is acted on.

        Concentrating the policy here keeps the transport adapters thin: each can
        hand the raw bridge event straight to the loop and rely on one shared
        implementation of the WhatsApp-specific gates. The ordering matters --
        broadcast chats are dropped first because they would otherwise be
        answered as ordinary DMs, and a DM that clears its policy is accepted
        unconditionally because the DM allowlist already did the work a group
        mention check would repeat.

        Returns:
            True when the message should enter the agent loop. False for Status
            and Channel traffic, for chats blocked by the DM or group policy, and
            for group messages carrying no qualifying trigger.

        Note:
            Group messages pass when the chat is listed in
            :meth:`_whatsapp_free_response_chats`, when mentions are not required,
            when the body starts with ``/``, or when it quotes, mentions, or
            pattern-matches one of this gateway's bot accounts.
        """
        chat_id_raw = str(data.get("chatId") or "")
        # WhatsApp uses pseudo-chats for Status updates (Stories) and
        # Channel/Newsletter broadcasts. These are not real conversations
        # and the agent should never reply to them -- even in self-chat mode
        # where the bridge may surface them as "fromMe" events.
        if self._is_broadcast_chat(chat_id_raw):
            return False
        is_group = data.get("isGroup", False)
        if is_group:
            chat_id = chat_id_raw
            if not self._is_group_allowed(chat_id):
                return False
        else:
            sender_id = str(data.get("senderId") or data.get("from") or "")
            if not self._is_dm_intake_allowed(sender_id):
                return False
            # DMs that pass the policy gate are always processed
            return True
        # Group messages: check mention / free-response settings
        chat_id = str(data.get("chatId") or "")
        if chat_id in self._whatsapp_free_response_chats():
            return True
        if not self._whatsapp_require_mention():
            return True
        body = str(data.get("body") or "").strip()
        if body.startswith("/"):
            return True
        if self._message_is_reply_to_bot(data):
            return True
        if self._message_mentions_bot(data):
            return True
        return self._message_matches_mention_patterns(data)

    # ------------------------------------------------------------------ formatting
    def format_message(self, content: str) -> str:
        """Convert standard markdown to WhatsApp-compatible formatting.

        WhatsApp supports: *bold*, _italic_, ~strikethrough~, ```code```,
        and monospaced `inline`. Standard markdown uses different syntax
        for bold/italic/strikethrough, so we convert here.

        Code blocks (``` fenced) and inline code (`) are protected from
        conversion via placeholder substitution.
        """
        if not content:
            return content

        content = self._sanitize_outbound_text(content)

        # --- 1. Protect fenced code blocks from formatting changes ---
        _FENCE_PH = "\x00FENCE"
        fences: list[str] = []

        def _save_fence(m: re.Match) -> str:
            """Stash a fenced code block so formatting conversion skips it.

            Fenced blocks are protected because their contents are literal: a
            ``*emphasis*`` inside one must survive to WhatsApp as backticks, not
            as italic. Each match is indexed into ``fences`` and replaced by a
            NUL-delimited placeholder that no plausible message text can collide
            with, then restored verbatim in the final step of ``format_message``.

            Returns:
                Placeholder for this match: ``_FENCE_PH``, the zero-based index
                of the block in ``fences``, and a trailing NUL byte.
            """
            fences.append(m.group(0))
            return f"{_FENCE_PH}{len(fences) - 1}\x00"

        result = re.sub(r"```[\s\S]*?```", _save_fence, content)

        # --- 2. Protect inline code ---
        _CODE_PH = "\x00CODE"
        codes: list[str] = []

        def _save_code(m: re.Match) -> str:
            """Stash an inline code span so formatting conversion skips it.

            Inline code follows the same placeholder scheme as fenced blocks,
            using its own prefix and index list so the two can be restored in
            either order. The ``[^`\n]+`` match stops at the next backtick or
            line break, which is deliberate: a multi-line span belongs to a fenced
            block and is already handled by :func:`_save_fence`.

            Returns:
                Placeholder for this match: ``_CODE_PH``, the zero-based index of
                the span in ``codes``, and a trailing NUL byte.
            """
            codes.append(m.group(0))
            return f"{_CODE_PH}{len(codes) - 1}\x00"

        result = re.sub(r"`[^`\n]+`", _save_code, result)

        # --- 3. Convert markdown formatting to WhatsApp syntax ---
        # Italic: standard Markdown *text* -> WhatsApp _text_.  Do this before
        # bold conversion so **bold** does not become italic by accident.  The
        # lookarounds avoid list bullets and bold delimiters.
        result = re.sub(
            r"(?<!\*)\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?!\*)",
            r"_\1_",
            result,
        )
        # Bold: **text** or __text__ ->*text*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        result = re.sub(r"__(.+?)__", r"*\1*", result)
        # Strikethrough: ~~text~~ ->~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        # _text_ is already WhatsApp italic -- leave as-is

        # --- 4. Convert markdown headers to bold text ---
        # # Header ->*Header*. Strip any *...* wrapping already produced
        # by step 3 (e.g. "# **Title**" ->"*Title*", not "**Title**",
        # which WhatsApp renders with literal asterisks).
        def _header_to_bold(m: re.Match) -> str:
            """Turn a markdown header into WhatsApp bold, unwrapping stray stars.

            Header conversion runs after the italic step, so ``# **Title**``
            arrives here already rewritten as ``# *Title*``. Without the unwrap
            loop the result would be ``**Title**``, which WhatsApp renders with
            the asterisks still visible. The loop peels every symmetric pair of
            surrounding stars, so deeply nested input collapses to one bold span.

            Returns:
                The header text wrapped in exactly one pair of ``*`` delimiters.
            """
            inner = m.group(1).strip()
            while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
                inner = inner[1:-1].strip()
            return f"*{inner}*"

        result = re.sub(
            r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE
        )

        # --- 5. Convert markdown links: [text](url) -> text (url) ---
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

        # --- 6. Restore protected sections ---
        for i, fence in enumerate(fences):
            result = result.replace(f"{_FENCE_PH}{i}\x00", fence)
        for i, code in enumerate(codes):
            result = result.replace(f"{_CODE_PH}{i}\x00", code)

        return result


# ---------------------------------------------------------------------------
# Shared bridge directory resolution for CLI and adapter
# ---------------------------------------------------------------------------

def resolve_whatsapp_bridge_dir() -> Path:
    """Resolve the WhatsApp bridge directory, mirroring to data dir if needed.

    When the install tree is read-only (e.g., Docker), this function
    mirrors the bridge source to a writable data directory location and returns
    that path. This ensures npm install works in Docker environments.

    Returns the resolved bridge directory path.
    """
    import shutil
    from pathlib import Path

    # Default location in install tree (may be read-only)
    from encre.config import get_data_dir
    get_encre_home = get_data_dir
    install_bridge = Path(__file__).resolve().parents[2] / "scripts" / "whatsapp-bridge"

    # Try data dir location first
    encre_home = get_encre_home()
    encre_home_bridge = encre_home / "scripts" / "whatsapp-bridge"

    # Check if install dir is writable
    try:
        test_file = install_bridge / ".write_test"
        test_file.touch()
        test_file.unlink()
        install_writable = True
    except (OSError, PermissionError):
        install_writable = False

    if install_writable:
        return install_bridge

    # Install dir is read-only, mirror to data dir if needed
    if encre_home_bridge.exists():
        return encre_home_bridge

    # Mirror the bridge source to data dir
    try:
        encre_home_bridge.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            install_bridge,
            encre_home_bridge,
            dirs_exist_ok=False,
        )
        return encre_home_bridge
    except Exception:
        return install_bridge
