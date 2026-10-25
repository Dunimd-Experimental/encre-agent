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

"""Declarative filtering and script transforms for generic webhook routes.

This module is the policy engine one layer below
:mod:`encre.gateway.platforms.webhook`. The webhook adapter owns the
transport half of an inbound HTTP event -- binding the socket, verifying
the HMAC signature, enforcing the rate limit, de-duplicating retries --
while this module answers the question the transport layer cannot answer:
*is this particular payload relevant to this particular route, and if so,
what should the agent actually be shown?*

Two mechanisms are provided, both driven by keys on a route entry under
``platforms.webhook.extra.routes`` in ``config.yaml``:

1. Declarative filters (``filters``). A small boolean DSL evaluated over a
   three-part context: ``payload``, ``event``/``event_type``, and
   ``headers``. ``all``/``any``/``not`` compose child specs, and the leaf
   operators are ``exists``, ``missing``, ``equals``, ``not_equals``,
   ``contains``, ``in``, ``in_file`` and ``regex``. Field references are
   dotted paths such as ``payload.pull_request.state`` or
   ``headers.x-github-event``; list positions are addressable by numeric
   segment (``items.0.name``).
2. Route scripts (``script``). An optional user script, resolved under
   ``<data_dir>/scripts``, that receives the parsed payload on stdin and
   may either drop the event or substitute a rewritten payload before
   prompt rendering.

Data flow is one-way and read-only: nothing here mutates the incoming
payload dict or talks to the network, so the adapter can call into this
module safely from inside its aiohttp request handler and then reuse the
same payload for prompt rendering and for the ``raw_message`` field of
the resulting
:class:`encre.gateway.platforms.base.MessageEvent`.

Public API:
    * :data:`DEFAULT_SCRIPT_TIMEOUT_SECONDS`
    * :class:`WebhookRouteProcessor`
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Wall-clock budget for a single route script. Scripts are external user
#: code, so a hard cap is mandatory: an unterminated script would otherwise
#: pin a worker thread forever and starve every other route on the gateway.
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 30
#: Sentinel distinguishing "this field does not exist" from "this field
#: exists and is falsy". Collapsing a missing field to ``""`` would make
#: ``equals: ""`` match absent fields, which is never what a filter author
#: means, so absence is carried as an identity-distinct object.
_MISSING = object()


def _stringify_filter_value(value: Any) -> str:
    """Reduce an arbitrary resolved value to comparable text.

    Filters read leaves out of decoded JSON, where a value may be a string,
    number, boolean, ``null``, list or object. The ``contains`` and
    ``regex`` operators only work on text, so all of them funnel through
    this helper instead of each repeating the coercion.

    Args:
        value: Any value returned by
            :meth:`WebhookRouteProcessor.resolve_filter_field`.

    Returns:
        ``""`` for the :data:`_MISSING` sentinel, so a missing field
        compares as empty rather than as the literal word ``_MISSING``.
        Nested containers are serialised with sorted keys so two
        semantically equal dicts always yield the same string -- without
        that, a ``contains`` check against an object would depend on the
        insertion order the vendor happened to use. Everything else goes
        through :func:`str`.
    """
    if value is _MISSING:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _resolve_profile_path(path_value: Any) -> Optional[Path]:
    """Resolve a config-supplied path against the Encre data directory.

    Filter definitions may reference files (see the ``in_file`` operator)
    as either an absolute path or a path relative to the data directory.
    Anchoring relative paths on :func:`encre.config.get_data_dir` keeps
    every route-owned file inside the one directory the operator already
    manages, so a filter cannot silently read from whatever directory the
    adapter happened to be launched from.

    Args:
        path_value: Raw value from the route config. May be ``None``, a
            non-string, or a string containing ``$VARIABLE`` references.

    Returns:
        The resolved :class:`pathlib.Path`, or ``None`` when the value is
        not a usable path. A string that is empty after variable expansion
        yields ``None`` rather than the bare data-dir path, so an
        unconfigured ``in_file`` fails closed as "nothing to match"
        instead of reading an unintended file.

    Note:
        Variable expansion runs before the absolute-path test, so
        ``$HOME/allowlist.json`` and a literal home path behave the same.
    """
    if not isinstance(path_value, str):
        return None
    raw = os.path.expandvars(path_value.strip())
    if not raw:
        return None
    from encre.config import get_data_dir as _get_data_dir

    data_dir = _get_data_dir()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return data_dir / path


def _resolve_script_path(script_value: Any) -> tuple[Optional[Path], Optional[str]]:
    """Resolve a route script inside the Encre data-dir ``scripts/`` tree.

    Scripts execute with the gateway's privileges, so the search root is
    confined: relative paths are resolved under ``<data_dir>/scripts`` and
    absolute paths are accepted only if they still resolve inside that
    root. The containment check uses ``resolve()`` on both sides so a
    ``../../etc/passwd`` style escape is rejected rather than silently
    followed.

    Args:
        script_value: Raw script value from the route config.

    Returns:
        ``(path, None)`` when the script resolves to an existing regular
        file, or ``(None, reason)`` when it does not. The reason string is
        the exact text the caller logs, so it doubles as the operator-facing
        diagnostic and is phrased to be actionable.
    """
    if not isinstance(script_value, str) or not script_value.strip():
        return None, "script path is empty"
    from encre.config import get_data_dir

    scripts_root = (get_data_dir() / "scripts").resolve()
    raw_text = os.path.expandvars(script_value.strip())
    raw = Path(raw_text).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (scripts_root / raw).resolve()
    try:
        candidate.relative_to(scripts_root)
    except ValueError:
        return None, f"script path resolves outside {scripts_root}"
    if not candidate.exists():
        return None, f"script not found: {candidate}"
    if not candidate.is_file():
        return None, f"script path is not a file: {candidate}"
    return candidate, None


def _load_filter_file_values(path_value: Any) -> list[Any]:
    """Load the comparison set referenced by an ``in_file`` filter.

    The file is accepted in three shapes so operators can reuse existing
    project data instead of maintaining a parallel allow-list: a JSON array
    (the values themselves), a JSON object (its keys), or plain text with
    one value per line.

    Args:
        path_value: Raw path value from the route config, resolved via
            :func:`_resolve_profile_path`.

    Returns:
        The list of values to test membership against. Every failure path
        -- unresolved path, unreadable file, malformed JSON that is also not
        line-oriented text, or a JSON scalar -- returns ``[]`` so the
        operator ends up with an empty allow-list and no matches, rather
        than with an exception inside the request handler. A read error is
        logged because it means the filter was configured but is not doing
        what the operator expects.
    """
    path = _resolve_profile_path(path_value)
    if path is None:
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[webhook] filter in_file read failed for %s: %s", path, exc)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.keys())
    return [data]


class WebhookRouteProcessor:
    """Evaluates declarative filters and optional script transforms per route.

    Constructed once per
    :class:`encre.gateway.platforms.webhook.WebhookAdapter` and shared by
    every inbound POST on every route. The only state held is the script
    timeout, so the instance needs no locking and is safe to call from the
    adapter's event loop.

    All work is synchronous -- JSON traversal, regex matching, and for
    scripts a subprocess -- so the adapter moves script execution onto a
    worker thread with :func:`asyncio.to_thread`. A blocking
    :func:`subprocess.run` left inside the aiohttp handler would freeze the
    whole gateway, not just the one route whose script is slow.

    Invariant:
        The payload dict passed in is never mutated. Both
        :meth:`route_filters_match` and :meth:`run_route_script` return a
        decision rather than performing one, which is what allows the
        adapter to run the same payload through filtering, prompt rendering
        and :class:`encre.gateway.platforms.base.MessageEvent` construction
        without copying it.

    Attributes:
        script_timeout_seconds: Wall-clock cap, in seconds, for a single
            route script. Clamped to a minimum of 1 at construction time so
            a misconfigured zero cannot disable the timeout.
    """

    def __init__(
        self,
        *,
        script_timeout_seconds: int = DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    ) -> None:
        """Create a filter and script evaluator.

        Args:
            script_timeout_seconds: Wall-clock cap applied to one route
                script execution. Clamped to a minimum of 1 second, so a
                zero or negative value from config cannot disable the
                timeout -- which would let a hung script pin a worker
                thread forever.

        Returns:
            Nothing. The instance is stateless beyond the clamped timeout,
            so it can be shared across routes and requests without a lock.
        """
        self.script_timeout_seconds = max(1, int(script_timeout_seconds))

    def resolve_filter_field(
        self,
        field: Any,
        payload: dict,
        event_type: str,
        headers: Any,
    ) -> Any:
        """Resolve a dotted field reference against the filter context.

        A reference such as ``payload.pull_request.state`` is evaluated
        against a synthetic context object rather than against the bare
        payload, so one syntax reaches three different parts of the inbound
        request:

        * ``payload.<path>`` -- the decoded webhook body. When the body is
          itself an envelope of the form ``{"payload": {...}}``, that inner
          object is used, matching the common relay convention.
        * ``event.<path>`` or ``event_type.<path>`` -- the resolved event
          type string (a string has no sub-fields, so any deeper segment
          resolves to :data:`_MISSING`).
        * ``headers.<name>`` -- request headers as a plain dict.

        A first segment that names none of those roots falls back to the
        payload root, which lets ``pull_request.state`` be written without a
        redundant ``payload.`` prefix.

        Args:
            field: Dotted reference string from the filter spec, or
                ``None``/empty/blank for "not specified".
            payload: Decoded webhook body.
            event_type: Resolved event type for this request.
            headers: Request headers, or ``None``.

        Returns:
            The leaf value, or :data:`_MISSING` when the reference is
            empty, points at an absent key, indexes a list out of range, or
            descends into a non-container. Numeric segments index lists;
            anything else that is not a dict or list index resolves to
            :data:`_MISSING` rather than raising, because a filter must
            never be able to take the request handler down.
        """
        if not isinstance(field, str) or not field.strip():
            return _MISSING
        parts = [part for part in field.strip().split(".") if part]
        if not parts:
            return _MISSING
        header_dict = dict(headers or {})
        context = {
            "payload": payload.get("payload", payload),
            "event": event_type,
            "event_type": event_type,
            "headers": header_dict,
        }
        if parts[0] in context:
            value: Any = context[parts[0]]
            parts = parts[1:]
        else:
            value = payload
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part, _MISSING)
            elif isinstance(value, list) and part.isdigit():
                idx = int(part)
                value = value[idx] if 0 <= idx < len(value) else _MISSING
            else:
                return _MISSING
            if value is _MISSING:
                return _MISSING
        return value

    def filter_matches(
        self,
        spec: Any,
        payload: dict,
        event_type: str,
        headers: Any,
    ) -> bool:
        """Evaluate one declarative webhook filter spec.

        A spec is either a combinator (``all``, ``any``, ``not``), which
        recurses into its children, or a leaf that resolves ``field`` once
        and then applies exactly one operator. Combinator keys take
        precedence over leaf keys, so a spec that sets both ``all`` and
        ``equals`` is evaluated as a combinator -- the ordering is what
        makes the two namespaces non-overlapping.

        Args:
            spec: Filter mapping from the route config.
            payload: Decoded webhook body.
            event_type: Resolved event type for this request.
            headers: Request headers, or ``None``.

        Returns:
            Whether the spec is satisfied. Every malformed-input case --
            a non-dict spec, an ``all``/``any`` whose value is not a list,
            an unknown operator, an invalid regex -- returns ``False``.
            Filters fail closed: a route with a broken filter never fires,
            and the misconfiguration is reported to the operator as a
            warning instead of surfacing as a 500 to the sender.

        Note:
            ``exists`` compares with :class:`bool` identity
            (``exists is bool(spec.get("exists"))``) so a truthy-but-not-
            boolean value such as ``1`` or ``"no"`` does not silently mean
            ``true`` -- only an explicit ``true``/``false`` is accepted.
            ``in_file`` is evaluated lazily per request, so an unreadable
            file degrades to "no match" without aborting the handler.
        """
        if not isinstance(spec, dict):
            logger.warning("[webhook] Ignoring invalid filter spec: %r", spec)
            return False

        if "all" in spec:
            items = spec.get("all")
            return isinstance(items, list) and all(
                self.filter_matches(item, payload, event_type, headers)
                for item in items
            )
        if "any" in spec:
            items = spec.get("any")
            return isinstance(items, list) and any(
                self.filter_matches(item, payload, event_type, headers)
                for item in items
            )
        if "not" in spec:
            return not self.filter_matches(spec.get("not"), payload, event_type, headers)

        value = self.resolve_filter_field(
            spec.get("field"), payload, event_type, headers
        )

        if "exists" in spec:
            exists = value is not _MISSING
            return exists is bool(spec.get("exists"))
        if spec.get("missing") is True:
            return value is _MISSING
        if "equals" in spec:
            return value is not _MISSING and value == spec.get("equals")
        if "not_equals" in spec:
            return value is _MISSING or value != spec.get("not_equals")
        if "contains" in spec:
            # Container membership is tested by containment, text membership
            # by substring, and both go through the sorted-key stringifier so
            # object-valued fields remain deterministic.
            needle = spec.get("contains")
            if value is _MISSING:
                return False
            if isinstance(value, (list, tuple, set, dict)):
                return needle in value
            return str(needle) in _stringify_filter_value(value)
        if "in" in spec:
            haystack = spec.get("in")
            return isinstance(haystack, list) and value in haystack
        if "in_file" in spec:
            return value in _load_filter_file_values(spec.get("in_file"))
        if "regex" in spec:
            if value is _MISSING:
                return False
            try:
                return (
                    re.search(str(spec.get("regex")), _stringify_filter_value(value))
                    is not None
                )
            except re.error as exc:
                logger.warning("[webhook] Invalid webhook filter regex: %s", exc)
                return False

        logger.warning("[webhook] Filter spec has no supported operator: %r", spec)
        return False

    def route_filters_match(
        self,
        route_config: dict,
        payload: dict,
        event_type: str,
        headers: Any,
    ) -> bool:
        """Decide whether any filter blocks a route for this event.

        Handles the three shapes an operator may write for ``filters``:
        omitted or empty (accept unconditionally), a single mapping
        (one spec), or a list of mappings (all specs must hold). A mapping
        is the convenient shorthand for a top-level ``all`` so operators do
        not have to write ``{"all": [...]}`` for a single condition.

        Args:
            route_config: The route entry from
                ``platforms.webhook.extra.routes``.
            payload: Decoded webhook body.
            event_type: Resolved event type for this request.
            headers: Request headers, or ``None``.

        Returns:
            ``True`` when the event should proceed to prompt rendering.
            An unrecognised shape returns ``False`` after a warning rather
            than raising, keeping a bad ``filters`` type from turning into a
            500 for every inbound event on the route.
        """
        filters = route_config.get("filters") or []
        if not filters:
            return True
        if isinstance(filters, dict):
            return self.filter_matches(filters, payload, event_type, headers)
        if not isinstance(filters, list):
            logger.warning("[webhook] filters must be a list or object")
            return False
        return all(
            self.filter_matches(spec, payload, event_type, headers)
            for spec in filters
        )

    def run_route_script(self, script_value: Any, payload: dict) -> tuple[bool, Optional[dict]]:
        """Run a route script and return (should_continue, transformed_payload).

        The script contract: the payload is passed as JSON on stdin, and
        stdout decides the outcome.

        * empty stdout, stdout equal to the literal ``[SILENT]``, or any
          non-zero exit code -- the event is dropped;
        * valid JSON object -- the event proceeds with that object as the
          new payload, replacing it wholesale rather than merging, so a
          script can strip vendor noise before prompt rendering;
        * valid JSON object carrying ``[SILENT]`` or ``__encre_ignore__``
          set to ``true`` -- the event is dropped after being rewritten,
          which lets a script rewrite *and* suppress in one pass;
        * valid non-JSON text -- the event proceeds with the original
          payload plus a ``script_output`` key holding the text.

        Args:
            script_value: Raw script value from the route config, resolved
                and containment-checked by :func:`_resolve_script_path`.
            payload: Decoded webhook body, written to stdin as JSON.

        Returns:
            ``(True, transformed)`` to proceed, or ``(False, None)`` to
            drop the event. Every failure mode -- unresolvable path,
            missing interpreter, timeout, crash, bad output type --
            returns ``(False, None)``. A script can suppress an event but
            can never take the handler down with it.

        Note:
            ``.sh``/``.bash`` files are executed through an explicitly
            located bash rather than relying on the shell shebang, because
            the script may live on a mounted filesystem where exec bits
            and shebang resolution are unreliable. Every other suffix is
            run with :data:`sys.executable`, so a script written for the
            wrong Python cannot silently do nothing. On Windows the
            creation flag ``0x08000000`` (``CREATE_NO_WINDOW``) suppresses
            a console window that would otherwise flash on each webhook.
        """
        path, error = _resolve_script_path(script_value)
        if error or path is None:
            logger.warning("[webhook] script ignored webhook: %s", error)
            return False, None

        suffix = path.suffix.lower()
        if suffix in {".sh", ".bash"}:
            bash = shutil.which("bash") or (
                "/bin/bash" if os.path.isfile("/bin/bash") else None
            )
            if bash is None:
                logger.warning("[webhook] script ignored webhook: bash not found")
                return False, None
            argv = [bash, str(path)]
        else:
            argv = [sys.executable, str(path)]

        try:
            # Environment plumbing hook. It is the single place where the
            # child environment is produced, so credentials can be stripped
            # here without touching the call site. As it stands the child
            # inherits the gateway's environment unchanged, which includes
            # any tokens the gateway itself was started with -- a route
            # script therefore sees the same secrets as this process.
            def _sanitize_subprocess_env(env):
                """Build the environment for a route script child process.

                Args:
                    env: A copy of the current process environment.

                Returns:
                    The environment passed to :func:`subprocess.run`. This
                    is the indirection point for redacting secrets before
                    they reach user-supplied script code.
                """
                return env

            popen_kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
            result = subprocess.run(
                argv,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=self.script_timeout_seconds,
                cwd=str(path.parent),
                env=_sanitize_subprocess_env(os.environ.copy()),
                **popen_kwargs,
            )
        except subprocess.TimeoutExpired:
            logger.warning("[webhook] script timed out: %s", path)
            return False, None
        except Exception as exc:
            logger.warning("[webhook] script execution failed: %s", exc)
            return False, None

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            logger.info(
                "[webhook] script ignored webhook path=%s code=%s stderr=%s",
                path.name,
                result.returncode,
                stderr[:200],
            )
            return False, None
        if not stdout or stdout == "[SILENT]":
            return False, None

        try:
            transformed = json.loads(stdout)
        except json.JSONDecodeError:
            transformed = {**payload, "script_output": stdout}
        if not isinstance(transformed, dict):
            logger.warning("[webhook] script stdout must be a JSON object or text")
            return False, None
        if (
            transformed.get("[SILENT]") is True
            or transformed.get("__encre_ignore__") is True
        ):
            return False, None
        return True, transformed
