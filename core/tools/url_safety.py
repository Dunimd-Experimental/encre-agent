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

"""SSRF-safe URL validation shared by gateway adapters.

Adapters fetch user-supplied URLs (attachments, link previews, avatar
downloads). This module rejects URLs that resolve to loopback,
private, link-local, or cloud-metadata addresses so a crafted URL
cannot turn the gateway into an SSRF pivot.

The module is a thin wrapper over
:class:`encre.ssrf.EncreSSRFGuard`, the canonical guard shared with
the Encre Agent Harness. Keeping a module-level singleton here
ensures all callers use the same configuration rather than each
instantiate their own with slightly different settings.
"""

from __future__ import annotations

from encre.ssrf import EncreSSRFGuard

# Process-wide guard instance. Instantiated at import time because
# the guard is lightweight and stateless; doing it lazily would
# just add a branch to every call.
_guard = EncreSSRFGuard()


def is_safe_url(url: str) -> bool:
    """Return ``True`` when *url* is safe to fetch (no SSRF risk).

    Delegates to :class:`encre.ssrf.EncreSSRFGuard`, the canonical
    SSRF guard shared with the Encre Agent Harness.

    The input check for ``isinstance(url, str)`` is deliberate: an
    adapter that accidentally passes a URL object or a list would
    otherwise trigger an unhelpful exception inside the guard rather
    than a clean ``False``.

    Args:
        url: The URL to validate.

    Returns:
        ``True`` for public http(s) URLs; ``False`` for anything
        that points at a private, loopback, link-local, or
        metadata endpoint.
    """
    if not url or not isinstance(url, str):
        return False
    return _guard.validate_url(url.strip())


def redirect_target_from_response(response: object) -> str | None:
    """Extract the redirect target from an HTTP response, if any.

    Works with aiohttp/httpx responses exposing a ``headers``
    mapping. The double-lookup (``Location`` then ``location``)
    handles libraries that normalise headers differently; both
    aiohttp and httpx expose them case-insensitively, but a
    duck-typed test response may not.

    Args:
        response: An HTTP response object (duck-typed).

    Returns:
        The ``Location`` header value, or ``None`` when the
        response did not carry one. Callers should re-validate the
        returned URL with :func:`is_safe_url` before fetching it —
        a public URL can redirect to an internal one.
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        location = headers.get("Location") or headers.get("location")
    except (AttributeError, TypeError):
        return None
    if location:
        return str(location)
    return None
