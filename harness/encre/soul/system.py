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
EncreSoulSystem -- Agent identity, personality, and user relationship files.

Manages three markdown files that define the agent's core identity and
its relationship with the user:

- SOUL.md      -- Agent's core personality, values, behavior principles
- IDENTITY.md  -- Agent's background story, capabilities, self-description
- USER.md      -- Agent's knowledge about the user (preferences, history)

These files are stored in the data directory alongside memory/ and are
encrypted on disk using AES-256-GCM (via encre.crypto). They are loaded
at startup and injected into the agent's system prompt.

The :class:`EncreSoulSystem` class lazily loads the files on first access
via :meth:`build_prompt` or :mthod:`get_frontend_data`, falling back to
built-in defaults when the files do not yet exist.  Updates are immediately
persisted to disk so that knowledge survives across agent sessions.
"""

import os
import time
from dataclasses import dataclass
from typing import Any

from encre.logging_config import get_logger

logger = get_logger("encre.soul")

_SOUL_FILENAME = "SOUL.md"
_IDENTITY_FILENAME = "IDENTITY.md"
_USER_FILENAME = "USER.md"


def _read_file(filepath: str) -> str:
    """Read a file with decryption fallback. Returns empty string on failure.

    Checks for plaintext markers (``---`` frontmatter or ``#`` heading)
    before attempting decryption.  This heuristic avoids logging a spurious
    decrypt-failure warning on every normal plaintext file.  Encrypted
    content (written by :func:`_write_file`) does not start with these
    markers, so it correctly enters the decrypt path.

    All exceptions are caught and result in an empty string return, which
    the caller treats as "file not found or unreadable" and substitutes
    a default value.
    """
    if not os.path.isfile(filepath):
        return ""
    try:
        with open(filepath, encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return ""
        # Plaintext detection: YAML frontmatter or markdown headings indicate
        # a plaintext file that does not need decryption.
        if raw.startswith("---") or raw.startswith("#"):
            return raw
        from encre.crypto import decrypt
        return decrypt(raw)
    except Exception:
        # Encryption failures, file permissions errors, etc. are non-fatal;
        # the caller will substitute a default value.
        return ""


def _write_file(filepath: str, content: str) -> None:
    """Write a file with encryption. Creates parent directories.

    Always encrypts content before writing.  Encryption failures are
    NOT caught — this is a fail-closed design: if encryption is
    unavailable, the write must fail rather than silently storing
    plaintext identity data on disk.

    Parent directories are created as needed because the soul directory
    may not exist on a fresh install.
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    from encre.crypto import encrypt
    # Fail-closed: encryption failure must never fall back to plaintext.
    encrypted = encrypt(content)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(encrypted)


@dataclass
class SoulFiles:
    """Container for the three soul file contents.

    A simple dataclass holding the plaintext contents of the three
    markdown files that define the agent's identity.  Used as the
    return type of :meth:`EncreSoulSystem.load` and as an internal
    cache after files are read from disk.

    Attributes:
        soul: Contents of SOUL.md (agent's personality and values).
        identity: Contents of IDENTITY.md (agent's self-description).
        user: Contents of USER.md (agent's knowledge about the user).
    """

    soul: str = ""
    identity: str = ""
    user: str = ""


class EncreSoulSystem:
    """Manages the agent's identity files (SOUL.md, IDENTITY.md, USER.md).

    These files define:
    - SOUL.md: The agent's core personality, values, and behavioral principles.
      This is the agent's "heart" -- what it stands for, how it approaches problems.
    - IDENTITY.md: The agent's self-description, capabilities, background story.
      This is the agent's "resume" -- what it can do, its expertise.
    - USER.md: The agent's evolving knowledge about the user -- preferences,
      communication style, past context, learned habits.

    All files are encrypted at rest using AES-256-GCM.

    The system lazily loads files on first access (via :meth:`build_prompt`
    or :meth:`get_frontend_data`).  If a file is missing or unreadable,
    a built-in default is substituted so the agent always has an identity.
    Updates via :meth:`update_soul`, :meth:`update_identity`, and
    :meth:`update_user` immediately persist to disk.
    """

    def __init__(self, soul_dir: str | None = None) -> None:
        """Initialise the soul system, optionally at a custom directory.

        When *soul_dir* is not provided, defaults to the standard data
        directory (``<data_dir>/soul``).  Files are not loaded until
        :meth:`build_prompt` or :meth:`load` is called, which enables
        lazy initialisation and avoids unnecessary I/O on startup.

        Args:
            soul_dir: Optional directory path for soul files.  When
                ``None``, uses ``<data_dir>/soul`` from the config module.
        """
        if soul_dir is None:
            from encre.config import get_data_dir
            soul_dir = str(get_data_dir() / "soul")
        self._soul_dir = soul_dir
        self._files = SoulFiles()
        self._loaded = False

    def ensure_defaults(self) -> None:
        """Create default soul files if they don't exist.

        Idempotent: safe to call multiple times.  Only creates files that
        are missing, never overwrites existing ones.  This is called during
        first-run setup to give the agent a baseline identity before the
        user has had a chance to customise anything.
        """
        os.makedirs(self._soul_dir, exist_ok=True)

        soul_path = os.path.join(self._soul_dir, _SOUL_FILENAME)
        if not os.path.isfile(soul_path):
            default_soul = self._default_soul()
            _write_file(soul_path, default_soul)
            logger.info("Created default SOUL.md")

        identity_path = os.path.join(self._soul_dir, _IDENTITY_FILENAME)
        if not os.path.isfile(identity_path):
            default_identity = self._default_identity()
            _write_file(identity_path, default_identity)
            logger.info("Created default IDENTITY.md")

        user_path = os.path.join(self._soul_dir, _USER_FILENAME)
        if not os.path.isfile(user_path):
            default_user = self._default_user()
            _write_file(user_path, default_user)
            logger.info("Created default USER.md")

    def load(self) -> SoulFiles:
        """Load all three soul files from disk.

        Reads SOUL.md, IDENTITY.md, and USER.md from the soul directory.
        If any file is missing or unreadable, a built-in default is
        substituted so the agent always has a complete identity.  This
        fallback ensures the system prompt is never empty, even on a
        fresh install where files haven't been created yet.

        Returns:
            A :class:`SoulFiles` container with the loaded (or defaulted)
            contents of all three files.
        """
        soul_path = os.path.join(self._soul_dir, _SOUL_FILENAME)
        identity_path = os.path.join(self._soul_dir, _IDENTITY_FILENAME)
        user_path = os.path.join(self._soul_dir, _USER_FILENAME)

        self._files = SoulFiles(
            soul=_read_file(soul_path),
            identity=_read_file(identity_path),
            user=_read_file(user_path),
        )
        self._loaded = True

        # Substitute defaults for any file that was missing or unreadable.
        if not self._files.soul:
            self._files.soul = self._default_soul()
        if not self._files.identity:
            self._files.identity = self._default_identity()
        if not self._files.user:
            self._files.user = self._default_user()

        return self._files

    def get_soul_dir(self) -> str:
        """Return the directory where the soul files are stored.

        Exposed for debugging and for frontend file management features
        that need to know the soul directory path.
        """
        return self._soul_dir

    def build_prompt(self) -> str:
        """Build the soul section to inject into the agent system prompt.

        Concatenates the three soul files into a single markdown-formatted
        string with section headings.  The headings are chosen to match
        the style of the surrounding system prompt so the soul content
        integrates naturally with other prompt sections.

        Lazily loads files on first call.  Empty files (after default
        substitution) are omitted to keep the prompt concise.
        """
        if not self._loaded:
            self.load()

        parts: list[str] = []
        if self._files.soul:
            parts.append("## Agent Identity (Soul)")
            parts.append(self._files.soul)
            parts.append("")

        if self._files.identity:
            parts.append("## Agent Capabilities")
            parts.append(self._files.identity)
            parts.append("")

        if self._files.user:
            parts.append("## About the User")
            parts.append(self._files.user)
            parts.append("")

        return "\n".join(parts)

    def update_soul(self, content: str) -> None:
        """Replace SOUL.md content and persist to disk.

        Updates both the in-memory cache and the encrypted file on disk.
        The file is written atomically (write-then-close) to avoid
        corruption from partial writes during a crash.
        """
        self._files.soul = content
        path = os.path.join(self._soul_dir, _SOUL_FILENAME)
        _write_file(path, content)

    def update_identity(self, content: str) -> None:
        """Replace IDENTITY.md content and persist to disk.

        Same semantics as :meth:`update_soul`.  Typically called after
        the agent's capabilities change or when the user customises the
        agent's self-description.
        """
        self._files.identity = content
        path = os.path.join(self._soul_dir, _IDENTITY_FILENAME)
        _write_file(path, content)

    def update_user(self, content: str) -> None:
        """Replace USER.md content. This is updated automatically over time.

        The USER.md file accumulates knowledge about the user across
        sessions.  This method replaces the entire content, which is
        appropriate for full rewrites (e.g. after the agent has synthesised
        new user knowledge from a conversation).
        """
        self._files.user = content
        path = os.path.join(self._soul_dir, _USER_FILENAME)
        _write_file(path, content)

    def append_user_note(self, note: str) -> None:
        """Append a timestamped note to USER.md (for learned preferences over time).

        Adds a new entry to the USER.md file in a structured format
        with a timestamp.  This is the primary mechanism by which the
        agent's knowledge about the user grows incrementally without
        overwriting previously learned preferences.

        Lazily loads the file if it hasn't been loaded yet, so this
        method can be called at any point during a session.

        Args:
            note: The note text to append (e.g. a discovered preference).
        """
        if not self._loaded:
            self.load()
        existing = self._files.user
        # Timestamp in ISO format for human readability and lexicographic sorting.
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        new_entry = f"\n\n_Learned {timestamp}:_\n{note}"
        updated = existing + new_entry
        self.update_user(updated)

    def get_frontend_data(self) -> dict[str, Any]:
        """Return data for frontend display.

        Provides the plaintext contents of all three soul files plus
        the directory path, so the desktop UI can render an editable
        view of the agent's identity.  This data is sent to the
        frontend via the web server API.

        Lazily loads files on first call.

        Returns:
            Dict with keys: ``soul``, ``identity``, ``user``, and
            ``soul_dir``.
        """
        if not self._loaded:
            self.load()
        return {
            "soul": self._files.soul,
            "identity": self._files.identity,
            "user": self._files.user,
            "soul_dir": self._soul_dir,
        }

    @staticmethod
    def _default_soul() -> str:
        """Default contents for SOUL.md when no file exists yet.

        Defines the agent's baseline personality and values.  This is
        used as a fallback when the file is missing or unreadable,
        ensuring the agent always has an identity to work with.
        """
        return """I am Encre, an autonomous AI agent built for thoughtful, thorough work.

My core principles:
- I take initiative and solve problems proactively
- I am honest about my limitations and uncertainties
- I prioritize the user's goals above my own curiosity
- I learn from every interaction to serve better over time
- I protect user privacy and handle all data with care

I approach every task with curiosity, precision, and a commitment to delivering real results -- not just plausible answers."""

    @staticmethod
    def _default_identity() -> str:
        """Default contents for IDENTITY.md when no file exists yet.

        Describes the agent's general-purpose capabilities.  This is a
        broad overview that works well for first-run experiences; users
        can customise it later to reflect their specific workflow.
        """
        return """I am a general-purpose AI agent with expertise across software engineering, data analysis, research, and creative problem-solving.  # noqa: E501

My capabilities include:
- Full-stack software development (Python, TypeScript, Rust, Go, and more)
- Code analysis, review, refactoring, and debugging
- System architecture and design
- Data analysis and visualization
- Web research and information synthesis
- File and project management
- Git operations
- Running shell commands and scripts
- Multi-agent orchestration and delegation

I work best when given clear goals and the autonomy to figure out the best path to reach them."""

    @staticmethod
    def _default_user() -> str:
        """Default contents for USER.md when no file exists yet.

        Placeholder content explaining that this file will be
        automatically populated as the agent learns about the user
        through conversations.  The timestamp format used by
        :meth:`append_user_note` is shown so users understand the
        structure they'll see as it fills in.
        """
        return """I am learning about the user's preferences over time.

This file will be automatically updated as I observe:
- Communication style preferences (formal vs casual, detail level)
- Programming languages and frameworks the user works with
- Common task patterns and workflows
- Corrections and feedback the user provides
- Tools and technologies the user prefers

_Empty -- will be populated through interactions._"""
