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

import json
import logging
import re
import shlex
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from encre.autosafety import EncreAutoSafetyClassifier
from encre.config import EncreConfig
from encre.sandbox.container import EncreContainerSandbox
from encre.sandbox.types import (
    EnvConfig,
    FileProtectionConfig,
    NetworkConfig,
    NetworkPolicy,
    ResourceConfig,
    SandboxConfig,
    SandboxMode,
    SandboxResult,
    SeccompConfig,
    SeccompProfile,
)
from encre.ssrf import EncreSSRFGuard
from encre.utils.types import (
    PermissionAllow,
    PermissionAsk,
    PermissionDecision,
    PermissionDeny,
)

logger = logging.getLogger(__name__)


class DangerLevel(Enum):
    SAFE = auto()        # Read-only, no side effects
    LOW = auto()         # Writes to project directory only
    MEDIUM = auto()      # Writes outside project, network access
    HIGH = auto()        # System modification, privilege escalation
    CRITICAL = auto()    # Data destruction, reverse shells, kernel access


@dataclass
class BashAnalysis:
    """Result of static analysis on a bash command."""
    command: str
    danger_level: DangerLevel = DangerLevel.SAFE
    injection_detected: bool = False
    injection_details: list[str] = field(default_factory=list)
    contains_substitution: bool = False
    contains_redirect: bool = False
    contains_pipe: bool = False
    contains_chained_command: bool = False
    contains_network_access: bool = False
    contains_file_write: bool = False
    contains_system_modification: bool = False
    contains_privilege_escalation: bool = False
    contains_encoded_content: bool = False
    contains_unicode_homoglyph: bool = False
    write_targets: list[str] = field(default_factory=list)
    network_targets: list[str] = field(default_factory=list)
    subcommands: list[str] = field(default_factory=list)
    shlex_error_type: str = ""  # "", "unbalanced_quote", "injection_detected"
    #: Blacklist verdicts from the Rust engine: ``{severity, category, pattern}``.
    #: Populated by :func:`_dangerous_matches`; the source of the danger level.
    danger_hits: list[dict[str, str]] = field(default_factory=list)


#: Engine ``category`` -> the ``BashAnalysis`` flags it implies. The blacklist
#: itself lives in ``dangerous_commands.txt``; this map only translates a
#: verdict into the shape callers of this module already read.
_DANGER_CATEGORY_FLAGS: dict[str, tuple[str, ...]] = {
    "fs": ("contains_file_write",),
    "priv-esc": ("contains_privilege_escalation", "contains_system_modification"),
    "shell": ("contains_network_access",),
    "remote-exec": ("contains_network_access",),
    "exfil": ("contains_network_access",),
    "network": ("contains_network_access", "contains_system_modification"),
    "obfuscation": ("contains_encoded_content",),
    "system": ("contains_system_modification",),
    "package": ("contains_system_modification",),
    "persistence": ("contains_system_modification",),
    "power": ("contains_system_modification",),
    "resource": (),
    "disclosure": (),
}

#: Readable name for a verdict, so a blocked command explains itself to the
#: model and the user instead of surfacing a raw regex.
_DANGER_CATEGORY_LABEL: dict[str, str] = {
    "fs": "filesystem destruction",
    "priv-esc": "privilege escalation",
    "shell": "reverse shell / interactive shell",
    "remote-exec": "remote code piped into an interpreter",
    "exfil": "data exfiltration",
    "network": "network or firewall change",
    "obfuscation": "encoded or obfuscated execution",
    "system": "system service or kernel change",
    "package": "destructive package operation",
    "persistence": "persistence or boot modification",
    "power": "power state change",
    "resource": "resource exhaustion",
    "disclosure": "sensitive file read",
}

#: ``severity`` strings used by ``dangerous_commands.txt`` -> DangerLevel, plus
#: the ordering the worst-of-N comparison walks.
_DANGER_SEVERITY_LEVEL: dict[str, DangerLevel] = {
    "critical": DangerLevel.CRITICAL,
    "high": DangerLevel.HIGH,
    "medium": DangerLevel.MEDIUM,
    "low": DangerLevel.LOW,
    "safe": DangerLevel.SAFE,
}
_DANGER_SEVERITY_ORDER: dict[str, int] = {
    "safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}


def _dangerous_matches(command: str) -> list[dict[str, str]]:
    """Every blacklist rule the command hits, as decided by the Rust engine.

    ``all_sections`` widens the pattern set past the host's own shell families:
    a review happens before the interpreter is settled, so a Linux shape seen on
    a Windows host must still be classified. Enforcement is unaffected -- it
    keeps going through ``permission_check_mode``, which stays host-scoped.
    """
    import json

    from encre import native as _native
    _ensure_danger_table()
    try:
        return list(_native.permission_dangerous_matches(
            "bash", json.dumps({"command": command}, ensure_ascii=False), True,
        ))
    except Exception as exc:  # pragma: no cover - native is mandatory
        logger.warning("dangerous-match unavailable: %s", exc)
        return []


#: The shipped blacklist is loaded once per process. ``analyze_bash_command`` is
#: a module function that callers reach without constructing an
#: :class:`EncreSafetyEngine`, so it cannot assume the engine already holds the
#: table -- an empty table would silently classify everything as safe.
_DANGER_TABLE_LOADED = False


def _ensure_danger_table() -> None:
    """Push ``dangerous_commands.txt`` into the engine if nothing has yet."""
    global _DANGER_TABLE_LOADED
    if _DANGER_TABLE_LOADED:
        return
    _DANGER_TABLE_LOADED = True
    from pathlib import Path

    from encre import native as _native
    path = Path(__file__).parent / "dangerous_commands.txt"
    if path.exists():
        try:
            _native.permission_set_dangerous_patterns(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - native is mandatory
            logger.warning("dangerous-command table unavailable: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# Immune paths -- always-deny for destructive ops even in bypass mode
# ═══════════════════════════════════════════════════════════════════════════

# Immune-path enforcement lives entirely in the Rust permission engine
# (``IMMUNE_PATH_FRAGMENTS`` / ``WRITE_TOOLS`` in permission.rs): the table,
# the target-path extraction and the matching are all native, and the floor
# is evaluated inside the engine's decision pipeline.  Python performs no
# permission logic of its own -- the compiled extension is a mandatory
# runtime component and every helper below is a direct native call.


def matches_immune_path(raw_path: str) -> str | None:
    """Return the matched immune fragment, or ``None`` if the path is clean.

    Direct native call into the Rust permission engine -- no Python fallback;
    the compiled extension is a mandatory runtime component.
    """
    from encre import native as _native
    hit = _native.permission_check_immune_path(raw_path or "")
    return str(hit) if hit else None


# ═══════════════════════════════════════════════════════════════════════════
# Injection detection patterns -- 30+ categories
# ═══════════════════════════════════════════════════════════════════════════

# Command substitution (primary injection vector)
_RE_COMMAND_SUBSTITUTION_DOLLAR = re.compile(r'\$\(.+\)', re.DOTALL)
_RE_COMMAND_SUBSTITUTION_BACKTICK = re.compile(r'`[^`]+`')
_RE_PROCESS_SUBSTITUTION = re.compile(r'[<(]\([^)]+\)')


# Fork bombs
_RE_FORK_BOMB = re.compile(r':\(\)\s*\{[^}]*:\|[^}]*:[^}]*\}', re.IGNORECASE)


# Variable expansion trickery (PATH manipulation, etc.)
_RE_PATH_VAR_MANIP = re.compile(r'\$\{(?:PATH|HOME|SHELL|IFS)[:#%/]', re.IGNORECASE)
_RE_IFS_MANIP = re.compile(r'\bIFS\s*=', re.IGNORECASE)


# Brace expansion (bash) -- {a,b} expands to multiple tokens, letting an
# attacker construct filenames/patterns that evade single-token regex
# checks (e.g. /etc/{passwd,shadow} splits into two disclosure targets).
# Benign uses exist (mkdir {a,b,c}) so this sets the expansion flag.
_RE_BRACE_EXPANSION = re.compile(r'\{[^{}]*,[^{}]*\}')

# ${IFS} / $IFS reference -- the Internal Field Separator used as a
# whitespace separator substitute to break up command tokens and evade
# space-anchored pattern matching (e.g. cat${IFS}/etc/passwd).  The
# negative lookahead avoids matching unrelated vars like $IFSHOME.
_RE_IFS_REFERENCE = re.compile(r'\$\{?IFS\}?(?![A-Za-z0-9_])', re.IGNORECASE)


# Backslash command-name fragmentation -- escapes inside a command word
# to evade token-anchored regex (c\at -> cat).  shlex de-escapes these at
# tokenization time, so a raw regex on the command string misses them; this
# pattern catches letter-backslash-letter mid-token.  Checked against the
# quote-stripped command to avoid flagging escapes inside string literals.
_RE_BACKSLASH_FRAGMENT = re.compile(r'[A-Za-z]\\[A-Za-z]')


def _strip_quoted_spans(command: str) -> str:
    """Return *command* with single/double-quoted spans replaced by spaces.

    Used to run evasion-detection regexes (brace expansion, ``${IFS}``,
    backslash fragmentation) only on the parts of the command that bash
    actually parses as code, so legitimate escapes inside string literals
    (``echo -e "foo\\nbar"``) don't trigger false positives.  Quote
    characters themselves are also blanked.  Approximate: it does not
    handle backslash-escaped quotes inside strings, but that's fine for
    a best-effort evasion screen.
    """
    out: list[str] = []
    quote: str = ""
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
            out.append(" ")
        elif ch in ("'", '"'):
            quote = ch
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def analyze_bash_command(command: str) -> BashAnalysis:
    """Perform multi-layer static analysis of a shell command.

    Layer 1: Tokenization via shlex (catches syntax-level tricks)
    Layer 2: Unicode homoglyph detection
    Layer 3: Regex pattern matching (30+ patterns)
    Layer 4: Subcommand extraction and per-subcommand checking
    Layer 5: Danger level classification
    """
    analysis = BashAnalysis(command=command)

    # ── Layer 1: Tokenization ──
    try:
        tokens = shlex.split(command)
        analysis.subcommands = []
        for tok in tokens:
            if (not tok.startswith("-") and "/" not in tok[:
                2]) or tok.startswith("--"):
                analysis.subcommands.append(tok)
    except ValueError as ve:
        # Classify the shlex failure so callers can distinguish a benign
        # unbalanced quote from a more suspicious malformed token.  shlex
        # does not parse braces, so "unbalanced_brace" surfaces from the
        # brace-expansion check below, not here.
        msg = str(ve).lower()
        if "quot" in msg or "no escaped character" in msg or "unterminated" in msg:
            analysis.shlex_error_type = "unbalanced_quote"
            analysis.injection_details.append(
                f"shlex: unbalanced_quote: {ve}"
            )
        else:
            analysis.shlex_error_type = "injection_detected"
            analysis.injection_details.append(
                f"shlex: injection_detected: {ve}"
            )
        analysis.injection_detected = True

    # ── Layer 2: Unicode homoglyph detection ──
    for i, ch in enumerate(command):
        cat = unicodedata.category(ch)
        # Cf = format characters (zero-width spaces, BOM, etc.)
        # Cc = control characters (except common whitespace)
        if cat == "Cf":
            analysis.contains_unicode_homoglyph = True
            analysis.injection_detected = True
            analysis.injection_details.append(
                f"Unicode format character U+{ord(ch):04X} at position {i}"
            )
        elif cat == "Cc" and ch not in ("\n", "\r", "\t"):
            analysis.contains_unicode_homoglyph = True
            analysis.injection_detected = True
            analysis.injection_details.append(
                f"Control character U+{ord(ch):04X} at position {i}"
            )

    # CR differential: a lone carriage return (0x0D) not part of a CRLF
    # line ending causes the displayed/logged command to differ from what
    # bash executes -- the terminal moves the cursor to column 0, hiding
    # earlier content from a human reviewer (CRLF smuggling / log hiding).
    # Layer 2 above deliberately excludes ``\r`` to avoid false positives on
    # Windows CRLF in multi-line scripts, so a lone CR needs this dedicated
    # check.  ``\r\n`` (proper line ending) is NOT flagged.
    lone_cr = [
        i for i, ch in enumerate(command)
        if ch == "\r" and (i + 1 >= len(command) or command[i + 1] != "\n")
    ]
    if lone_cr:
        analysis.injection_detected = True
        analysis.injection_details.append(
            f"lone carriage return (CR) at position(s) {lone_cr[:3]}"
            " -- log/display differential"
        )

    # ── Layer 3: Pattern matching ──

    # Command substitution
    if _RE_COMMAND_SUBSTITUTION_DOLLAR.search(command):
        analysis.contains_substitution = True
    if _RE_COMMAND_SUBSTITUTION_BACKTICK.search(command):
        analysis.contains_substitution = True
    if _RE_PROCESS_SUBSTITUTION.search(command):
        analysis.contains_substitution = True

    # Pipes
    if "|" in command and not _RE_FORK_BOMB.search(command):
        analysis.contains_pipe = True

    # Chained commands
    if re.search(r'[;&](?:\s*\n?\s*)(?:rm|dd|mkfs|chmod|chown|kill|reboot|shutdown)', command, re.IGNORECASE):
        analysis.contains_chained_command = True

    # ── Blacklist: answered by the Rust engine, never re-implemented here.
    #
    # ``dangerous_commands.txt`` is the single definition of what counts as a
    # dangerous command; it now carries ``severity`` and ``category`` per block,
    # so the flags and the danger level below are derived from engine verdicts
    # rather than from a second, drifting copy of the patterns in Python.
    # ``all_sections`` is used because an inspection happens before the shell is
    # settled: a Linux shape reviewed on a Windows host must still be recognised.
    # Enforcement (``permission_check_mode``) stays host-scoped and is unaffected.
    for hit in _dangerous_matches(command):
        severity = str(hit.get("severity") or "high")
        category = str(hit.get("category") or "")
        pattern = str(hit.get("pattern") or "")
        analysis.danger_hits.append({
            "severity": severity,
            "category": category,
            "pattern": pattern,
        })
        for flag in _DANGER_CATEGORY_FLAGS.get(category, ()):
            setattr(analysis, flag, True)
        if severity in ("critical", "high"):
            analysis.injection_detected = True
        analysis.injection_details.append(
            f"{_DANGER_CATEGORY_LABEL.get(category, category or 'dangerous command')} "
            f"[{severity}]"
        )

    # Variable manipulation tricks
    if _RE_PATH_VAR_MANIP.search(command):
        analysis.injection_details.append("PATH variable manipulation")
        analysis.injection_detected = True
    if _RE_IFS_MANIP.search(command):
        analysis.injection_details.append("IFS manipulation attempt")
        analysis.injection_detected = True

    # Evasion techniques -- run against the quote-stripped command so escapes
    # inside string literals (echo -e "foo\nbar") don't false-positive.
    unquoted = _strip_quoted_spans(command)
    if _RE_IFS_REFERENCE.search(unquoted):
        analysis.injection_details.append(
            "${IFS}/$IFS reference -- whitespace-substitute token evasion"
        )
        analysis.injection_detected = True
    if _RE_BACKSLASH_FRAGMENT.search(unquoted):
        analysis.injection_details.append(
            "backslash command-name fragmentation (e.g. c\\at) -- evades token regex"
        )
        analysis.injection_detected = True
    # Brace expansion is expansion (not injection): benign uses like
    # ``mkdir {a,b,c}`` are common, so flag for review without blocking.
    if _RE_BRACE_EXPANSION.search(unquoted):
        analysis.contains_substitution = True
        analysis.injection_details.append(
            "brace expansion {a,b} -- may evade single-token pattern checks"
        )

    # Network detection (broad, lower priority)
    if re.search(r'\b(curl|wget|nc|ncat|socat)\s', command, re.IGNORECASE):
        analysis.contains_network_access = True

    # ── Layer 4: Danger level classification ──
    # The engine's worst verdict leads; the structural layers can only escalate.
    if analysis.danger_hits:
        worst = max(
            (str(h.get("severity") or "high") for h in analysis.danger_hits),
            key=lambda s: _DANGER_SEVERITY_ORDER.get(s, 1),
        )
        analysis.danger_level = _DANGER_SEVERITY_LEVEL.get(worst, DangerLevel.HIGH)
        if not analysis.injection_detected:
            analysis.injection_detected = True
    elif analysis.injection_details:
        # No blacklist hit: the structural signals still escalate, exactly as
        # before, so evasion shapes cannot be downgraded by this refactor.
        if analysis.contains_privilege_escalation or analysis.contains_system_modification:
            analysis.danger_level = DangerLevel.HIGH
        elif analysis.contains_file_write or analysis.contains_network_access:
            analysis.danger_level = DangerLevel.MEDIUM
        else:
            analysis.danger_level = DangerLevel.LOW
    elif analysis.contains_network_access and analysis.contains_pipe:
        analysis.danger_level = DangerLevel.MEDIUM
    elif analysis.contains_network_access:
        analysis.danger_level = DangerLevel.LOW
    elif analysis.contains_file_write:
        analysis.danger_level = DangerLevel.MEDIUM
    else:
        analysis.danger_level = DangerLevel.SAFE

    return analysis


class EncreSafetyEngine:
    """Centralised safety gate for tool execution.

    Coordinates three independent protection layers:

    1. **Bash static analysis** (:func:`analyze_bash_command`) -- a 5-layer
       regex + tokenisation scanner that classifies shell commands by danger
       level (SAFE / LOW / MEDIUM / HIGH / CRITICAL) and flags injection
       vectors (command substitution, unicode homoglyphs, reverse shells,
       fork bombs, privilege escalation, data exfiltration, etc.).
    2. **Immune-path hard deny** -- writes/deletes targeting system-sensitive
       paths (``.git/``, ``~/.ssh/``, shell init files) are always blocked,
       even when the user has granted global bypass permission.  Read access
       to these paths is untouched.
    3. **Rust native permission engine** -- the single authoritative source
       for tool-authorization decisions.  Python delegates to the native
       extension for the actual allow/deny/ask verdict, using an in-memory
       policy table and dangerous-command pattern list.

    When sandbox mode is enabled the engine also spins up a container
    (:class:`~encre.sandbox.container.EncreContainerSandbox`) for tools
    that require OS isolation, and applies a :class:`~encre.ssrf.EncreSSRFGuard`
    to outbound URLs to prevent server-side request forgery.

    Attributes
    ----------
    config : EncreConfig
        Agent configuration; provides permission policies, sandbox settings,
        and sensitive-pattern lists.
    sandbox : EncreContainerSandbox | None
        Container sandbox instance when sandboxing is enabled, else ``None``.
    _ssrf_guard : EncreSSRFGuard
        URL-validator that blocks requests to internal/metadata endpoints.
    """
    def __init__(
        self,
        config: EncreConfig,
        sandbox_enabled: bool | None = None,
        sandbox_config: SandboxConfig | None = None,
        workspace: str = "",
        auto_classifier: EncreAutoSafetyClassifier | None = None,
    ) -> None:
        self.config = config

        # Push the blacklist into the Rust engine so it is defined in exactly
        # one place, with its section tags intact.  The engine is a mandatory
        # runtime component, so this is a direct native call.
        #
        # ``dangerous_command_patterns`` is the documented way for an operator
        # to extend the list (docs/SECURITY.md); those extras are appended as
        # [universal] rules so they apply under every shell, and the whole
        # table still reaches the engine in a single push.
        patterns_path = Path(__file__).parent / "dangerous_commands.txt"
        table_text = patterns_path.read_text(encoding="utf-8") if patterns_path.exists() else ""
        extra = [
            str(pattern).strip()
            for pattern in (getattr(config, "dangerous_command_patterns", None) or [])
            if str(pattern).strip()
        ]
        if extra:
            table_text = f"{table_text}\n[universal]\n" + "\n".join(extra)
        if table_text.strip():
            from encre import native as _native
            _native.permission_set_dangerous_patterns(table_text)
        # The engine push is authoritative (it folds the operator extras in), so
        # the lazy default load in _ensure_danger_table must not run after it.
        global _DANGER_TABLE_LOADED
        _DANGER_TABLE_LOADED = True

        # Honor the config-level sandbox switch when the caller does not
        # pass an explicit override.  Note the sandbox instance itself is
        # inert until ``mode`` is CONTAINER/HYBRID (see
        # ``require_container_sandbox``), so a plain default config keeps
        # host execution.
        self.sandbox_enabled = (
            bool(sandbox_enabled) if sandbox_enabled is not None
            else bool(getattr(config, "sandbox_enabled", False))
        )
        if self.sandbox_enabled and workspace:
            # Build a comprehensive sandbox config from the EncreConfig
            sc = sandbox_config or SandboxConfig()

            # Inherit network policy from config if applicable
            if sc.network.policy == NetworkPolicy.NONE and config.sandbox_enabled:
                # Strict default: no network, read-only, capped resources
                sc.network = NetworkConfig(policy=NetworkPolicy.NONE)
                sc.file_protection = FileProtectionConfig(
                    read_only_root=True,
                    workspace_mode="rw",
                    symlink_protection=True,
                    mount_protection=True,
                    no_suid=True,
                )
                sc.resource = ResourceConfig(
                    memory_limit="512m",
                    cpu_limit=1.0,
                    pids_limit=64,
                    no_new_privileges=True,
                )
                sc.seccomp = SeccompConfig(profile=SeccompProfile.UNPRIVILEGED)
                sc.env = EnvConfig(inherit_env=False, env_vars={})
                sc.disable_sudo = True
                sc.disable_network_tooling = True
                sc.disable_interactive = True

            self.sandbox = EncreContainerSandbox(workspace, sc)
        else:
            self.sandbox = None
        self._ssrf_guard = EncreSSRFGuard()
        self._auto_classifier = auto_classifier
        # A classifier injected by the caller is authoritative and never
        # replaced.  For the auto permission mode one is built lazily from the
        # session's active model instead (see ``_get_auto_classifier``).
        self._auto_classifier_explicit = auto_classifier is not None
        self._auto_classifier_key: tuple[str, str, str, str] | None = None

    def native_sandbox_provides_fs_isolation(self) -> bool:
        """Return True when the Rust native sandbox applies real filesystem isolation.

        The Rust ``sandbox_execute`` applies OS-native isolation that varies by
        platform (see ``native/crates/encre-core/src/sandbox.rs``):

          * Linux   -> Landlock LSM (kernel 5.13+): r/w workspace only, no network.
          * macOS   -> sandbox_init(3): r/w workspace only, no network.
          * Windows -> Job Object: process/memory limits only, NO filesystem
            restriction -- ``rm -rf`` outside the workspace still succeeds.

        Only on Linux/macOS does the native path substitute for a container's
        filesystem isolation.  Windows still needs Docker for FS confinement.
        """
        import sys as _sys
        return _sys.platform in ("linux", "darwin")

    def require_container_sandbox(self, tool_name: str) -> bool:
        """Check if a tool call should be routed through the container sandbox.

        Only ``bash`` commands are sandboxed (file operations use path
        isolation instead).  The sandbox must be enabled, available, and
        configured with a mode that supports container isolation.

        Unified sandbox awareness: the Rust ``sandbox_execute`` already applies
        OS-native filesystem isolation to every bash command (Landlock on
        Linux, sandbox_init on macOS).  When that native isolation is available
        AND the configured network policy is ``NONE`` (the native path's
        no-network guarantee), a Docker container layer on top would be
        redundant -- it adds startup overhead without strengthening FS or
        network isolation.  We therefore do NOT force a container in that
        case, even when the mode is CONTAINER/HYBRID.  A non-NONE network
        policy (the user wants network access the native path cannot provide)
        or a non-native platform (Windows Job Object has no FS isolation)
        still routes to Docker.
        """
        if tool_name not in ("bash",):
            return False
        if not self.sandbox_enabled or self.sandbox is None:
            return False

        mode = getattr(self.sandbox.config, "mode", None)
        network = getattr(self.sandbox.config, "network", None)
        net_policy = getattr(network, "policy", None)

        if (
            mode in (SandboxMode.CONTAINER, SandboxMode.HYBRID)
            and self.native_sandbox_provides_fs_isolation()
            and net_policy == NetworkPolicy.NONE
        ):
            # Native path already provides FS isolation + no network -- skip Docker.
            return False

        # Check the sandbox mode: CONTAINER or HYBRID trigger container isolation
        if mode is not None:
            return mode in (SandboxMode.CONTAINER, SandboxMode.HYBRID)
        # Legacy/default: sandbox IS container mode
        return True

    def execute_in_sandbox(self, command: str, timeout: int | None = None) -> SandboxResult:
        if self.sandbox is None:
            raise RuntimeError("Sandbox not available")
        return self.sandbox.execute(command, timeout)

    def analyze_bash(self, command: str) -> BashAnalysis:
        """Expose bash static analysis for pre-execution review."""
        return analyze_bash_command(command)

    def is_bash_safe(self, command: str) -> tuple[bool, str]:
        """Quick check: is this bash command safe to run without sandbox?"""
        analysis = analyze_bash_command(command)
        if analysis.danger_level in (DangerLevel.CRITICAL, DangerLevel.HIGH):
            return False, "; ".join(analysis.injection_details)
        if analysis.injection_detected and analysis.danger_level == DangerLevel.MEDIUM:
            return False, "; ".join(analysis.injection_details)
        if analysis.contains_unicode_homoglyph:
            return False, "Unicode homoglyph/zero-width characters detected"
        return True, ""

    def _permission_mode(self) -> str:
        """Return the active permission mode, defaulting to ``default``."""
        return str(getattr(self.config, "permission_mode", "") or "default")

    def _dangerous_rule(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Return the dangerous-command rule this call hits, or ``""``.

        Direct native call -- the blacklist is a hard floor that no permission
        mode can silence, so it is consulted before the AI classifier and
        before the policy table.
        """
        from encre import native as _native
        payload = json.dumps(tool_input or {}, ensure_ascii=False)
        return str(_native.permission_check_dangerous(tool_name, payload) or "")

    async def check_tool_permission(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecision:
        """Unified permission entry point for every non-read-only tool call.

        Dispatches on the active mode so that every call site agrees on the
        same semantics:

        * ``custom``  -- the user-managed per-tool / per-capability policy
          table decides (resolved inside the Rust engine).
        * ``default`` -- manual approval; a blacklisted subcommand comes back
          as ``Ask`` so the frontend can prompt.
        * ``auto``    -- handed to :meth:`check_yolo_permission`; the AI
          classifier decides and the user is never prompted.
        * ``bypass``  -- full access; never prompts.  Only the dangerous-
          command blacklist and the immune-path floor still refuse a call.

        The policy table is deliberately *not* applied outside ``custom``: the
        settings panel describes those rules as custom-mode-only, and the Rust
        engine mirrors that by ignoring them for the preset modes.
        """
        if self._permission_mode() == "auto":
            return await self.check_yolo_permission(tool_name, tool_input)

        decision = await self._engine_check(tool_name, tool_input)

        if self._permission_mode() == "bypass" and decision.behavior == "ask":
            # Full access never prompts.  A blacklisted subcommand is refused
            # instead; any other ``Ask`` can only come from a preset policy
            # that must stay inert in this mode, so it degrades to allow.
            rule = self._dangerous_rule(tool_name, tool_input)
            if rule:
                return PermissionDeny(
                    reason=f"Blocked by dangerous-command rule: {rule}"
                )
            return PermissionAllow(reason=getattr(decision, "reason", ""))

        return decision

    async def _engine_check(self, tool_name: str, tool_input: dict[str, Any]) -> PermissionDecision:
        # The Rust permission engine is the single source of truth for
        # authorization.  Direct native call -- the mode travels with the
        # call so the policy table is consulted exactly when this session is
        # in custom mode, never based on a process-wide slot another session
        # may have overwritten.  No Python fallback exists: the compiled
        # extension is a mandatory runtime component.  The decision dict
        # returned by Rust is mapped onto the PermissionAllow / PermissionDeny
        # / PermissionAsk dataclasses so the agent loop and the WS protocol
        # keep working unchanged.
        from encre import native as _native
        payload = json.dumps(tool_input or {}, ensure_ascii=False)
        decision = _native.permission_check_mode(
            tool_name, payload, self._permission_mode()
        )
        behavior = decision.get("behavior")
        reason = str(decision.get("reason") or "")
        rule = str(decision.get("rule") or "")

        # NOTE: immune-path enforcement (.git/.claude/.ssh/shell-init) lives
        # inside the Rust decision pipeline as an always-on hard floor (step
        # 0b), so a Deny for a protected path arrives here directly.  Python
        # no longer post-filters; the engine is the single source of truth.

        if behavior == "deny":
            return PermissionDeny(reason=reason)
        if behavior == "ask":
            return PermissionAsk(reason=reason, rule=rule)
        return PermissionAllow()

    async def check_readonly_permission(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> PermissionDecision:
        """Permission check for tools that declare themselves read-only.

        Read-only tools never trigger a user prompt, but an explicit deny
        policy, an immune-path target, or a dangerous command smuggled into a
        ``command`` argument must still be enforced.  Engine asks degrade to
        allow here (a read-only operation is not worth prompting for); only a
        hard deny survives.
        """
        decision = await self._engine_check(tool_name, tool_input)
        if isinstance(decision, PermissionDeny):
            return decision
        return PermissionAllow()

    def check_immune_path(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Return an immune-path fragment the tool call would mutate, or None.

        Direct native call -- extraction and matching live in the Rust
        permission engine.  Pure path matching: the caller (the executor gate
        or ``check_tool_permission``) decides whether an immune hit warrants
        a hard deny based on whether the operation is destructive.
        """
        from encre import native as _native
        payload = json.dumps(tool_input or {}, ensure_ascii=False)
        hit = _native.permission_check_immune(tool_name, payload)
        return str(hit) if hit else None

    def record_permission_decision(self, tool_name: str, allowed: bool) -> None:
        """Persist the user's answer to a previous ``Ask`` decision.

        Direct native call -- the decision table lives in the Rust engine.
        """
        from encre import native as _native
        _native.permission_record_decision(tool_name, "allow" if allowed else "deny")

    def set_policies(self, tools: dict[str, str], capabilities: dict[str, str]) -> None:
        """Replace the user-managed policy table inside the Rust engine.

        ``tools`` keys are tool names (``"bash"``, ``"file_write"``, ...);
        ``capabilities`` keys are capability names (``"network"``,
        ``"file"``, ``"docker"``, ...).  Values are one of
        ``"allow" | "deny" | "ask" | "default"``.
        """
        from encre import native as _native
        payload = json.dumps(
            {"tools": tools, "capabilities": capabilities},
            ensure_ascii=False,
        )
        _native.permission_set_policies(payload)

    def get_policies(self) -> dict[str, dict[str, str]]:
        """Return the current policy table as ``{"tools": {...}, "capabilities": {...}}``."""
        from encre import native as _native
        raw = _native.permission_get_policies()
        parsed = json.loads(raw) if raw else {}
        tools = parsed.get("tools", {}) if isinstance(parsed, dict) else {}
        capabilities = parsed.get("capabilities", {}) if isinstance(parsed, dict) else {}
        return {"tools": tools, "capabilities": capabilities}

    def _sync_policies_to_native(self) -> None:
        """Push the current ``EncreConfig.permission_settings`` map into Rust.

        We split the flat ``permission_settings`` dict into ``tools`` and
        ``capabilities`` by checking the well-known capability names.
        Everything else is treated as a per-tool override.

        **Important**: we skip pushing when ``permission_settings`` is empty
        to avoid overwriting the Rust global state — which may have been
        set by the frontend via ``set_permission_policies`` — with an empty
        policy table during ``EncreSafetyEngine`` initialization.  Only when
        the config actually carries permission settings (e.g. loaded from a
        persisted config file) do we synchronise them into Rust.
        """
        if not self.config.permission_settings:
            return  # Nothing to sync — preserve any existing Rust state.

        capability_keys = {
            "network", "file", "bash_io", "docker", "browser",
            "workflow", "git", "deploy", "desktop", "database", "misc", "mcp",
        }
        tools: dict[str, str] = {}
        capabilities: dict[str, str] = {}
        for key, value in self.config.permission_settings.items():
            if key in capability_keys:
                capabilities[key] = value
            else:
                tools[key] = value
        self.set_policies(tools, capabilities)

    def _get_auto_classifier(self) -> EncreAutoSafetyClassifier | None:
        """Return the classifier that reviews tool calls in auto mode.

        The classifier is built lazily from the session's active model so it
        follows the model the user selected (switching models rebuilds it) and
        is reused between calls so the static pre-filter and the verdict cache
        stay warm.  Returns ``None`` when no model is configured, in which case
        auto mode falls back to the static engine.
        """
        if self._auto_classifier_explicit:
            return self._auto_classifier
        # Use classifier_model if configured; otherwise fall back to the
        # session's active model so the classifier follows model switches.
        classifier_model = getattr(self.config, "classifier_model", "") or ""
        if classifier_model:
            key = (
                classifier_model,
                str(self.config.backend_type or ""),
                str(self.config.api_key or ""),
                str(self.config.base_url or ""),
            )
        else:
            try:
                active = self.config.get_active_model()
            except Exception:
                active = None
            model_id = str(getattr(active, "model_id", "") or "")
            if active is None or (not model_id and not self.config.model):
                return None
            key = (
                model_id or str(self.config.model or ""),
                str(getattr(active, "backend_type", "") or self.config.backend_type or ""),
                str(getattr(active, "api_key", "") or self.config.api_key or ""),
                str(getattr(active, "base_url", "") or self.config.base_url or ""),
            )
        if self._auto_classifier is None or self._auto_classifier_key != key:
            self._auto_classifier = EncreAutoSafetyClassifier(
                backend_type=key[1],
                api_key=key[2],
                base_url=key[3],
                model=key[0],
            )
            self._auto_classifier_key = key
        return self._auto_classifier

    async def check_yolo_permission(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        conversation_context: str = "",
        tool: Any = None,
    ) -> PermissionDecision:
        """Auto approval: the AI classifier decides, the user is never asked.

        The classifier reviews the tool call (plus the recent conversation
        when it is available) and grants it unless it judges the call high
        risk or malicious, in which case the call is denied.  The dangerous-
        command blacklist is a hard floor the classifier cannot override, and
        here it refuses the call outright instead of prompting, because auto
        approval exists precisely to keep the user out of the loop.

        Two hard floors are enforced before the classifier runs and can never
        be overridden by it, both resolved inside the Rust permission engine:
        the dangerous-command blacklist and the immune-path floor
        (.git/.claude/.ssh/shell-init writes are refused in every mode,
        including auto).
        """
        rule = self._dangerous_rule(tool_name, tool_input)
        if rule:
            return PermissionDeny(
                reason=f"Blocked by dangerous-command rule: {rule}"
            )

        immune = self.check_immune_path(tool_name, tool_input)
        if immune:
            return PermissionDeny(
                reason=(
                    f"Refused: target path '{immune}' is immune "
                    f"(system-sensitive: .git/.claude/.ssh/shell-init). "
                    f"Cannot be modified by the agent, even in auto mode."
                )
            )

        classifier = self._get_auto_classifier()
        if classifier is None:
            # No model to review with -- fall back to the static engine and
            # downgrade any residual prompt, since auto approval never asks.
            decision = await self._engine_check(tool_name, tool_input)
            if decision.behavior == "ask":
                return PermissionAllow(reason=getattr(decision, "reason", ""))
            return decision

        from encre.autosafety import AutoDecision

        if conversation_context:
            result = await classifier.classify_with_context(
                tool_name, tool_input, conversation_context, tool=tool,
            )
        else:
            result = await classifier.classify(tool_name, tool_input, tool=tool)

        if result.decision in (AutoDecision.HIGH_RISK, AutoDecision.BLOCK):
            return PermissionDeny(
                reason=result.reasoning or f"Auto classifier rejected {tool_name}."
            )
        if result.decision == AutoDecision.ASK_USER and result.confidence <= 0.0:
            # Sentinel from the classifier's exception fallback: the AI
            # reviewer was unreachable and static analysis could not clear
            # the call.  Fall back to the native engine and prefer Deny for
            # anything dangerous-looking; auto approval never asks the user.
            decision = await self._engine_check(tool_name, tool_input)
            if self._is_dangerous(tool_name, tool_input):
                return PermissionDeny(
                    reason=(
                        "Auto classifier unavailable and the call matches "
                        "dangerous patterns; refusing (fail-closed)."
                    )
                )
            if decision.behavior == "deny":
                return decision
            if decision.behavior == "allow":
                return PermissionAllow(reason=getattr(decision, "reason", ""))
            return PermissionDeny(
                reason=(
                    "Auto classifier unavailable and the native engine "
                    "requires approval for this call; refusing (fail-closed)."
                )
            )
        # SAFE / LOW_RISK / ASK_USER: the AI has reviewed the call and grants
        # it.  Auto approval never falls back to prompting the user.
        return PermissionAllow(reason=result.reasoning)

    def _validate_url_safe(self, url: str) -> bool:
        if not url:
            return False
        if url.startswith("http://") or url.startswith("https://"):
            return self._ssrf_guard.validate_url(url)
        return False

    def _is_dangerous(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if tool_name == "bash":
            command = tool_input.get("command", "")
            if not command:
                return False
            safe, _ = self.is_bash_safe(command)
            return not safe
        # Non-bash tools carry no shell payload here; the native engine's
        # dangerous-pattern table (which covers any tool with a command/cmd
        # argument) is the single source of truth for command screening.
        return False

    def validate_tool_output(self, _tool_name: str, output: str) -> str:
        if len(output) > self.config.tool_result_max_chars:
            output = output[: self.config.tool_result_max_chars] + "\n... (truncated)"
        return output
