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

"""Microsoft Teams platform adapter for Encre's IM gateway.

This module is one of the vendor adapters under
:mod:`encre.gateway.platforms`.  It bridges the Bot Framework message flow
(inbound webhook) and the Graph / activity REST flow (outbound delivery)
behind the single platform-adapter contract implemented by
:class:`encre.gateway.platforms.base.BasePlatformAdapter`.

Inbound: the microsoft-teams-apps SDK owns authentication and activity
parsing.  Because the SDK's default server adapter unconditionally imports
fastapi/uvicorn, this module supplies :class:`_AiohttpBridgeAdapter` so the
SDK registers its routes into an :mod:`aiohttp` application owned by the
gateway process.  Inbound message activities are normalised into a
:class:`encre.gateway.platforms.base.MessageEvent` and handed to the
:class:`encre.gateway.run.GatewayRunner` message handler, with attachments
downloaded and cached into the gateway media cache.

Outbound: agent text produced by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer` arrives at
:meth:`TeamsAdapter.send`, is markdown-formatted and chunked against the
Teams message limit, and posted through the SDK.  Adaptive Card approval
prompts use the same path via :meth:`TeamsAdapter.send_exec_approval` and
route back through :meth:`TeamsAdapter._on_card_action`.

Proactive delivery without a live gateway process is provided by
:func:`_standalone_send` for ``encre cron`` style callers.  Long-form
meeting summaries are delivered by :class:`TeamsSummaryWriter` either
through a Teams incoming webhook or directly through the Microsoft Graph.

Configuration:
    ``config.yaml`` -> ``platforms.teams.extra`` with ``client_id``,
    ``client_secret``, ``tenant_id`` and optionally ``port``.  Environment
    overrides: ``TEAMS_CLIENT_ID``, ``TEAMS_CLIENT_SECRET``,
    ``TEAMS_TENANT_ID``, ``TEAMS_PORT``, ``TEAMS_SERVICE_URL``,
    ``TEAMS_HOME_CHANNEL``, ``TEAMS_ALLOWED_USERS``,
    ``TEAMS_ALLOW_ALL_USERS``, ``TEAMS_DELIVERY_MODE``,
    ``TEAMS_INCOMING_WEBHOOK_URL``, ``TEAMS_GRAPH_ACCESS_TOKEN``,
    ``TEAMS_TEAM_ID``, ``TEAMS_CHANNEL_ID``, ``TEAMS_CHAT_ID``.

Requires:
    pip install microsoft-teams-apps aiohttp
"""

import asyncio
import html
import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

# httpx is imported lazily: only
# :meth:`TeamsSummaryWriter._write_summary_via_incoming_webhook` ever
# constructs an ``AsyncClient``.  Importing it at module scope pulled the
# whole httpx + httpcore stack (about 37 ms, about 15 MB) into every process
# that performed plugin discovery, including ones that never instantiate the
# Teams adapter.  ``from __future__ import annotations`` above keeps the
# ``httpx.AsyncBaseTransport`` annotation valid as an unresolved string;
# nothing in the codebase calls ``typing.get_type_hints()`` on this class,
# so the annotation never has to resolve to a real symbol.

from aiohttp import web
AIOHTTP_AVAILABLE = True

from microsoft_teams.apps import App, ActivityContext
from microsoft_teams.common.http.client import ClientOptions
from microsoft_teams.api import MessageActivity, ConversationReference
from microsoft_teams.api.activities.typing import TypingActivityInput
from microsoft_teams.api.activities.invoke.adaptive_card import AdaptiveCardInvokeActivity
from microsoft_teams.api.models.adaptive_card import (
    AdaptiveCardActionCardResponse,
    AdaptiveCardActionMessageResponse,
)
from microsoft_teams.api.models.invoke_response import InvokeResponse, AdaptiveCardInvokeResponse
from microsoft_teams.apps.http.adapter import (
    HttpMethod,
    HttpRequest,
    HttpResponse,
    HttpRouteHandler,
)
from microsoft_teams.cards import AdaptiveCard, ExecuteAction, TextBlock
TEAMS_SDK_AVAILABLE = True

from encre.gateway.config import Platform, PlatformConfig
from encre.gateway.platforms.helpers import MessageDeduplicator
from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_url,
    cache_media_bytes,
)

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 3978
# Bot Framework activities are JSON payloads well under 1 MiB; an explicit
# aiohttp client_max_size keeps oversized/chunked request bodies bounded.
_MAX_BODY_BYTES = 1_048_576
_WEBHOOK_PATH = "/api/messages"


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce a config or environment value to a boolean.

    Teams settings arrive from three places with three different shapes:
    ``bool`` from ``config.yaml``, ``str`` from environment variables, and
    ``None`` when unset.  Accepting any of them here keeps the callers free
    of per-source branching.

    Args:
        value: The raw value, of any type.
        default: Returned when ``value`` is a string that names no known
            boolean.  Real ``bool`` input is always returned unchanged, so
            ``False`` is never confused with "unset".

    Returns:
        ``True`` for ``1``/``true``/``yes``/``on``, ``False`` for
        ``0``/``false``/``no``/``off``, otherwise ``default``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_port(value: Any, *, default: int = _DEFAULT_PORT) -> int:
    """Coerce a config or environment port value to an integer.

    Args:
        value: The raw value, typically a string read from
            ``TEAMS_PORT``.
        default: Returned when ``value`` cannot be converted.

    Returns:
        The parsed port, or ``default`` when parsing fails.  A bad value
        must never abort adapter construction: falling back to
        :data:`_DEFAULT_PORT` is preferable to a crash in plugin discovery.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class _StaticAccessTokenProvider:
    """Minimal token-provider shim so outbound Graph delivery can reuse the shared client."""

    def __init__(self, access_token: str):
        """Store a static bearer token.

        Args:
            access_token: A pre-acquired Microsoft Graph access token, or any
                value that stringifies to empty when absent.
        """
        self._access_token = str(access_token or "").strip()

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return the stored token to satisfy the Graph client protocol.

        Args:
            force_refresh: Ignored; a static token cannot be refreshed.

        Returns:
            The stored token.

        Raises:
            ValueError: When no token was supplied, since Graph delivery has
                no other way to authenticate.
        """
        del force_refresh
        if not self._access_token:
            raise ValueError("TEAMS_GRAPH_ACCESS_TOKEN is required for graph delivery mode.")
        return self._access_token

    def clear_cache(self) -> None:
        """No-op cache clear, matching the real token provider interface.

        Args:
            None.

        Returns:
            ``None``.
        """
        return None


class TeamsSummaryWriter:
    """Pipeline-facing Teams outbound delivery surface.

    This stays inside the existing Teams platform plugin so the meeting-pipeline
    PR can reuse one Teams integration surface instead of introducing a second
    adapter elsewhere in the gateway core.
    """

    def __init__(
        self,
        platform_config: PlatformConfig | None = None,
        *,
        graph_client: Any | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Bind the delivery dependencies.

        Args:
            platform_config: Optional platform configuration used as a base
                layer for delivery settings.  Its ``extra`` mapping, token
                and home channel are folded in before any per-call config.
            graph_client: Optional pre-built Graph client, mainly for tests.
                When ``None``, :meth:`_build_graph_client` constructs one.
            transport: Optional HTTP transport injected for tests; passed
                straight through to the httpx client.
        """
        self._platform_config = platform_config
        self._graph_client = graph_client
        self._transport = transport

    async def write_summary(
        self,
        payload: Any,
        config: dict[str, Any] | None,
        existing_record: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Deliver a meeting summary and report the delivery record.

        This is the entry point used by the meeting pipeline.  It selects a
        delivery mode, suppresses a duplicate send when a record already
        exists, and delegates to the mode-specific implementation.

        Args:
            payload: The rendered meeting summary object, exposing ``title``,
                ``summary``, ``key_decisions``, ``action_items`` and
                ``risks``.
            config: Per-call delivery overrides, merged on top of the
                platform config and the environment.
            existing_record: A previous successful delivery record.  When
                present and ``force_resend`` is not set, it is returned
                unchanged so the pipeline treats the summary as already
                delivered.

        Returns:
            A dict describing the delivery: the mode used and the target
            identifiers, message id and status the provider returned.

        Raises:
            ValueError: When the resolved mode is neither ``incoming_webhook``
                nor ``graph``, or when a required value for the chosen mode
                is missing.
        """
        merged = self._resolve_delivery_config(config)
        if existing_record and not _parse_bool(merged.get("force_resend"), default=False):
            return dict(existing_record)

        mode = str(merged.get("delivery_mode") or merged.get("mode") or "").strip().lower()
        if not mode:
            if merged.get("incoming_webhook_url"):
                mode = "incoming_webhook"
            elif merged.get("chat_id") or (
                merged.get("team_id") and merged.get("channel_id")
            ):
                mode = "graph"
        if mode == "incoming_webhook":
            return await self._write_summary_via_incoming_webhook(payload, merged)
        if mode == "graph":
            return await self._write_summary_via_graph(payload, merged)
        raise ValueError(
            "Teams delivery_mode must be 'incoming_webhook' or 'graph'."
        )

    def _resolve_delivery_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        """Merge delivery settings from the three configuration sources.

        Precedence, lowest to highest: the platform configuration ``extra``
        mapping, then the environment, then the per-call ``config``.  This
        matches the rest of the gateway, where a per-call override always
        wins and the environment wins over the file.

        Args:
            config: Per-call overrides, or ``None``.

        Returns:
            The merged dict.  The platform ``token`` and ``home_channel``
            are promoted to ``access_token`` and ``channel_id`` so both
            delivery modes see a uniform key set.
        """
        merged: dict[str, Any] = {}
        platform_cfg = self._platform_config
        if platform_cfg is not None:
            merged.update(dict(platform_cfg.extra or {}))
            if platform_cfg.token and "access_token" not in merged:
                merged["access_token"] = platform_cfg.token
            if platform_cfg.home_channel:
                merged.setdefault("channel_id", platform_cfg.home_channel.chat_id)
        merged.update(dict(config or {}))

        env_defaults = {
            "delivery_mode": os.getenv("TEAMS_DELIVERY_MODE", ""),
            "incoming_webhook_url": os.getenv("TEAMS_INCOMING_WEBHOOK_URL", ""),
            "access_token": os.getenv("TEAMS_GRAPH_ACCESS_TOKEN", ""),
            "team_id": os.getenv("TEAMS_TEAM_ID", ""),
            "channel_id": os.getenv("TEAMS_CHANNEL_ID", ""),
            "chat_id": os.getenv("TEAMS_CHAT_ID", ""),
        }
        for key, value in env_defaults.items():
            if value and not merged.get(key):
                merged[key] = value
        return merged

    async def _write_summary_via_incoming_webhook(
        self,
        payload: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Post a summary markdown payload to a Teams incoming webhook.

        Args:
            payload: The rendered summary object.
            config: Resolved delivery config; must contain a webhook URL.

        Returns:
            Dict with the mode, the URL used, the HTTP status and a
            ``delivered`` flag.

        Raises:
            ValueError: When ``incoming_webhook_url`` is missing.
            httpx.HTTPStatusError: When the webhook rejects the payload.
        """
        # Lazy import: see the module-level note on httpx.  This plugin is
        # imported during every CLI plugin discovery pass, but nearly all of
        # those processes never reach this method.
        import httpx
        webhook_url = str(config.get("incoming_webhook_url") or "").strip()
        if not webhook_url:
            raise ValueError("TEAMS_INCOMING_WEBHOOK_URL is required for incoming_webhook mode.")
        body = {"text": self._render_summary_markdown(payload)}
        async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
            response = await client.post(webhook_url, json=body)
            response.raise_for_status()
        return {
            "delivery_mode": "incoming_webhook",
            "webhook_url": webhook_url,
            "status_code": response.status_code,
            "delivered": True,
        }

    async def _write_summary_via_graph(
        self,
        payload: Any,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Post a summary through the Microsoft Graph chat or channel API.

        A ``chat_id`` delivers to a private chat; otherwise ``team_id`` and
        ``channel_id`` together target a team channel.  Graph accepts HTML
        bodies, so the payload is rendered with :meth:`_render_summary_html`.

        Args:
            payload: The rendered summary object.
            config: Resolved delivery config.

        Returns:
            Dict with the mode, the target type, the identifiers used and
            the ``id``/``webUrl`` Graph returned for the created message.

        Raises:
            ValueError: When neither a ``chat_id`` nor a ``team_id`` and
                ``channel_id`` pair is configured.
        """
        graph_client = self._build_graph_client(config)
        chat_id = str(config.get("chat_id") or "").strip()
        if chat_id:
            path = f"/chats/{quote(chat_id, safe='')}/messages"
            response = await graph_client.post_json(
                path,
                json_body={"body": {"contentType": "html", "content": self._render_summary_html(payload)}},
            )
            return {
                "delivery_mode": "graph",
                "target_type": "chat",
                "chat_id": chat_id,
                "message_id": (response or {}).get("id"),
                "web_url": (response or {}).get("webUrl"),
            }

        team_id = str(config.get("team_id") or "").strip()
        channel_id = str(config.get("channel_id") or "").strip()
        if not team_id or not channel_id:
            raise ValueError(
                "Graph delivery mode requires chat_id, or both team_id and channel_id."
            )
        path = (
            f"/teams/{quote(team_id, safe='')}/channels/"
            f"{quote(channel_id, safe='')}/messages"
        )
        response = await graph_client.post_json(
            path,
            json_body={"body": {"contentType": "html", "content": self._render_summary_html(payload)}},
        )
        return {
            "delivery_mode": "graph",
            "target_type": "channel",
            "team_id": team_id,
            "channel_id": channel_id,
            "message_id": (response or {}).get("id"),
            "web_url": (response or {}).get("webUrl"),
        }

    def _build_graph_client(self, config: dict[str, Any]) -> Any:
        """Construct or return the Microsoft Graph client.

        A statically supplied client (tests) wins.  Otherwise a static
        bearer token from the config is wrapped in
        :class:`_StaticAccessTokenProvider`; when no token is present the
        credentials in the environment are used instead.

        Args:
            config: Resolved delivery config.

        Returns:
            A Graph client bound to the selected token provider.
        """
        if self._graph_client is not None:
            return self._graph_client

        from tools.microsoft_graph_auth import MicrosoftGraphTokenProvider
        from tools.microsoft_graph_client import MicrosoftGraphClient

        access_token = str(config.get("access_token") or "").strip()
        if access_token:
            return MicrosoftGraphClient(
                _StaticAccessTokenProvider(access_token),
                transport=self._transport,
            )
        return MicrosoftGraphClient(
            MicrosoftGraphTokenProvider.from_env(),
            transport=self._transport,
        )

    def _render_summary_markdown(self, payload: Any) -> str:
        """Render the summary as markdown for an incoming webhook.

        Incoming webhooks only accept ``text`` as markdown, so the sections
        are laid out as a heading followed by three bulleted lists.

        Args:
            payload: The rendered summary object.

        Returns:
            The markdown string.
        """
        lines = [
            f"**{self._title(payload)}**",
            "",
            f"Summary: {self._text(getattr(payload, 'summary', None), 'No summary available.')}",
            "",
            "Key decisions:",
            *self._bullet_lines(getattr(payload, "key_decisions", None)),
            "",
            "Action items:",
            *self._bullet_lines(getattr(payload, "action_items", None)),
            "",
            "Risks:",
            *self._bullet_lines(getattr(payload, "risks", None)),
        ]
        return "\n".join(lines)

    def _render_summary_html(self, payload: Any) -> str:
        """Render the summary as HTML for the Graph API.

        Every interpolated value is HTML-escaped, because the payload comes
        from meeting transcription text that the user did not author for
        this context.

        Args:
            payload: The rendered summary object.

        Returns:
            The HTML string.
        """
        sections = [
            ("Summary", [self._text(getattr(payload, "summary", None), "No summary available.")]),
            ("Key decisions", list(getattr(payload, "key_decisions", None) or [])),
            ("Action items", list(getattr(payload, "action_items", None) or [])),
            ("Risks", list(getattr(payload, "risks", None) or [])),
        ]
        blocks = [f"<h2>{html.escape(self._title(payload))}</h2>"]
        for heading, items in sections:
            blocks.append(f"<h3>{html.escape(heading)}</h3>")
            if len(items) == 1 and heading == "Summary":
                blocks.append(f"<p>{html.escape(str(items[0]))}</p>")
                continue
            if items:
                rendered = "".join(f"<li>{html.escape(str(item))}</li>" for item in items if str(item).strip())
                blocks.append(rendered and f"<ul>{rendered}</ul>" or "<p>None</p>")
            else:
                blocks.append("<p>None</p>")
        return "".join(blocks)

    @staticmethod
    def _title(payload: Any) -> str:
        """Return the display title for a summary.

        Falls back to the meeting reference id when no title is set, and to
        a placeholder string when neither is available, so the rendered
        output never has an empty heading.

        Args:
            payload: The rendered summary object.

        Returns:
            The title string.
        """
        title = getattr(payload, "title", None)
        if title:
            return str(title)
        meeting_ref = getattr(payload, "meeting_ref", None)
        meeting_id = getattr(meeting_ref, "meeting_id", None) if meeting_ref else None
        return f"Meeting {meeting_id or 'summary'}"

    @staticmethod
    def _text(value: Any, default: str) -> str:
        """Return a non-empty string for a summary field.

        Args:
            value: The raw field value, possibly ``None`` or whitespace.
            default: Returned when ``value`` renders as empty.

        Returns:
            The stripped string, or ``default``.
        """
        text = str(value or "").strip()
        return text or default

    @classmethod
    def _bullet_lines(cls, values: Any) -> list[str]:
        """Render a summary list field as markdown bullets.

        Blank entries are dropped rather than rendered as empty bullets, and
        a wholly empty field becomes a single ``- None`` bullet so the
        section is never absent from the layout.

        Args:
            values: Iterable of items, possibly ``None``.

        Returns:
            The rendered bullet lines.
        """
        items = [str(item).strip() for item in (values or []) if str(item).strip()]
        return [f"- {item}" for item in items] or ["- None"]


class _AiohttpBridgeAdapter:
    """HttpServerAdapter that bridges the Teams SDK into an aiohttp server.

    Without a custom adapter, ``App()`` unconditionally imports fastapi/uvicorn
    and allocates a ``FastAPI()`` instance.  This bridge captures the SDK's
    route registrations and wires them into our own aiohttp ``Application``.
    """

    def __init__(self, aiohttp_app: "web.Application"):
        """Store the aiohttp application that receives the SDK routes.

        Args:
            aiohttp_app: The application created by
                :meth:`TeamsAdapter.connect`; the bridge only borrows it and
                never starts or stops it.
        """
        self._aiohttp_app = aiohttp_app

    def register_route(self, method: "HttpMethod", path: str, handler: "HttpRouteHandler") -> None:
        """Register an SDK route handler as an aiohttp route.

        Adapts the SDK's ``HttpRequest``/``HttpResponse`` contract onto
        :mod:`aiohttp` request and response objects, so the SDK can stay
        transport-agnostic.

        Args:
            method: The HTTP method to bind.
            path: The route path, e.g. ``/api/messages``.
            handler: An SDK call that turns an ``HttpRequest`` into an
                ``HttpResponse`` mapping.
        """

        async def _aiohttp_handler(request: "web.Request") -> "web.Response":
            """Translate one aiohttp request through the SDK handler.

            The SDK returns a plain dict rather than a response object, so
            the mapping is converted back into a JSON :class:`web.Response`
            here.  A response body of ``None`` means the SDK wants an empty
            body, which is preserved instead of being serialised as JSON.
            """
            body = await request.json()
            headers = dict(request.headers)
            result: "HttpResponse" = await handler(HttpRequest(body=body, headers=headers))
            status = result.get("status", 200)
            resp_body = result.get("body")
            if resp_body is not None:
                return web.Response(
                    status=status,
                    body=json.dumps(resp_body),
                    content_type="application/json",
                )
            return web.Response(status=status)

        self._aiohttp_app.router.add_route(method, path, _aiohttp_handler)

    def serve_static(self, path: str, directory: str) -> None:
        """Unsupported static hosting; kept to satisfy the adapter protocol.

        The gateway never serves static assets from a platform adapter, so
        this is a no-op rather than an error.

        Args:
            path: Ignored.
            directory: Ignored.

        Returns:
            ``None``.
        """
        pass

    async def start(self, port: int) -> None:
        """Unsupported by the bridge; the adapter owns the server lifecycle.

        Args:
            port: Ignored.

        Returns:
            Never returns.

        Raises:
            NotImplementedError: Always; calling this means the caller
                bypassed :meth:`TeamsAdapter.connect`.
        """
        raise NotImplementedError("aiohttp server is managed by the adapter")

    async def stop(self) -> None:
        """Unsupported by the bridge; the adapter owns the server lifecycle.

        Args:
            None.

        Returns:
            ``None``.
        """
        pass


def check_requirements() -> bool:
    """Report whether the Teams dependencies are importable.

    This is the passive probe: it must be cheap and side-effect free, so it
    only inspects the module-level availability flags.  Lazy installation is
    the job of :func:`check_teams_requirements`, which is called from
    :meth:`TeamsAdapter.connect`.

    Args:
        None.

    Returns:
        ``True`` when both the Teams SDK and aiohttp are importable.
    """
    return TEAMS_SDK_AVAILABLE and AIOHTTP_AVAILABLE


def validate_config(config) -> bool:
    """Report whether the configuration carries the minimum credentials.

    The environment is checked first and the ``extra`` mapping second,
    matching :meth:`TeamsAdapter.__init__` so validation and construction
    never disagree about which value wins.

    Args:
        config: Platform configuration carrying an ``extra`` mapping.

    Returns:
        ``True`` when a client id, client secret and tenant id are all
        present.
    """
    extra = getattr(config, "extra", {}) or {}
    client_id = os.getenv("TEAMS_CLIENT_ID") or extra.get("client_id", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET") or extra.get("client_secret", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID") or extra.get("tenant_id", "")
    return bool(client_id and client_secret and tenant_id)


def is_connected(config) -> bool:
    """Report whether Teams is minimally configured.

    Thin wrapper over :func:`validate_config` kept because the registry
    entry exposes a separate ``is_connected`` callable.

    Args:
        config: Platform configuration carrying an ``extra`` mapping.

    Returns:
        ``True`` when the minimum credentials are present.
    """
    return validate_config(config)


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry's env-enablement hook BEFORE adapter
    construction, so ``gateway status`` and ``get_connected_platforms()``
    reflect env-only configuration without instantiating the Teams SDK.
    Returns ``None`` when Teams isn't minimally configured.

    The special ``home_channel`` key in the returned dict becomes a proper
    ``HomeChannel`` dataclass on the ``PlatformConfig`` via the core hook.
    """
    client_id = os.getenv("TEAMS_CLIENT_ID", "").strip()
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "").strip()
    tenant_id = os.getenv("TEAMS_TENANT_ID", "").strip()
    if not (client_id and client_secret and tenant_id):
        return None
    seed: dict = {
        "client_id": client_id,
        "client_secret": client_secret,
        "tenant_id": tenant_id,
    }
    port = os.getenv("TEAMS_PORT", "").strip()
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass
    service_url = os.getenv("TEAMS_SERVICE_URL", "").strip()
    if service_url:
        seed["service_url"] = service_url
    home = os.getenv("TEAMS_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("TEAMS_HOME_CHANNEL_NAME", "Home"),
        }
    return seed


# Bot Framework default service URL for the global Teams endpoint.  Some
# regional/government tenants need a different host (e.g.
# ``https://smba.infra.gov.teams.microsoft.us/``) which can be supplied via
# ``TEAMS_SERVICE_URL`` or ``extra['service_url']``.
_DEFAULT_TEAMS_SERVICE_URL = "https://smba.trafficmanager.net/teams/"

# Allowlist of Bot Framework service hosts that may receive a freshly
# minted bearer token.  Operator-supplied URLs are matched against this
# allowlist to block SSRF / token-exfiltration via a tampered env var.
_ALLOWED_TEAMS_SERVICE_HOSTS = frozenset({
    "smba.trafficmanager.net",
    "smba.infra.gov.teams.microsoft.us",
})

# Conservative pattern for Bot Framework conversation IDs.  Real values
# combine digits, colons, hyphens, dots, '@', and the ``thread.skype`` /
# ``thread.tacv2`` suffixes; reject anything outside this set so a hostile
# value cannot path-traverse out of ``/v3/conversations/<id>/activities``.
import re as _re_teams
_TEAMS_CONV_ID_RE = _re_teams.compile(r"^[A-Za-z0-9:@\-_.]+$")


def _validate_teams_service_url(raw: str) -> Optional[str]:
    """Return a normalized service URL or ``None`` if it is not allowed.

    Requires ``https://`` and a host in ``_ALLOWED_TEAMS_SERVICE_HOSTS``.
    The trailing slash is added if absent so callers can append
    ``v3/conversations/...`` without double slashes.
    """
    if not raw:
        return None
    try:
        from urllib.parse import urlparse

        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme != "https":
        return None
    if parsed.hostname not in _ALLOWED_TEAMS_SERVICE_HOSTS:
        return None
    normalized = raw if raw.endswith("/") else raw + "/"
    return normalized


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Acquire a Bot Framework bearer token and POST a single message activity.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (e.g. ``encre cron`` running as a
    separate process from ``encre gateway``).  Without this hook,
    ``deliver=teams`` cron jobs fail with ``No live adapter for platform``.

    Configuration: requires ``TEAMS_CLIENT_ID``, ``TEAMS_CLIENT_SECRET``,
    ``TEAMS_TENANT_ID``, ``TEAMS_HOME_CHANNEL`` (the conversation ID), and
    optionally ``TEAMS_SERVICE_URL`` (Bot Framework service host; must be
    a known Bot Framework endpoint, see ``_ALLOWED_TEAMS_SERVICE_HOSTS``).

    Security: ``service_url`` is validated against an allowlist of known
    Bot Framework hosts to block SSRF / token-exfiltration via a tampered
    env var.  ``chat_id`` is validated to match the documented Bot
    Framework ID character set so it cannot escape the URL path.

    ``media_files`` and ``force_document`` are accepted for signature
    parity but not implemented for the standalone path; messages with
    attachments will send as text-only.  The live adapter handles
    attachments via the SDK.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    client_id = os.getenv("TEAMS_CLIENT_ID") or extra.get("client_id", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET") or extra.get("client_secret", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID") or extra.get("tenant_id", "")
    if not (client_id and client_secret and tenant_id):
        return {"error": "Teams standalone send: TEAMS_CLIENT_ID, TEAMS_CLIENT_SECRET, and TEAMS_TENANT_ID are all required"}

    raw_service_url = (
        os.getenv("TEAMS_SERVICE_URL")
        or extra.get("service_url", "")
        or _DEFAULT_TEAMS_SERVICE_URL
    )
    service_url = _validate_teams_service_url(raw_service_url)
    if service_url is None:
        return {"error": (
            f"Teams standalone send: TEAMS_SERVICE_URL host is not on the "
            f"Bot Framework allowlist; expected one of "
            f"{sorted(_ALLOWED_TEAMS_SERVICE_HOSTS)}"
        )}

    # Bot Framework conversation IDs are restricted to a known character
    # set; anything else means a tampered chat_id trying to break out of
    # the URL path.
    if not chat_id:
        return {"error": "Teams standalone send: chat_id (conversation ID) is required"}
    if not _TEAMS_CONV_ID_RE.match(chat_id):
        return {"error": "Teams standalone send: chat_id contains characters outside the Bot Framework conversation ID set"}
    if not _TEAMS_CONV_ID_RE.match(tenant_id):
        return {"error": "Teams standalone send: TEAMS_TENANT_ID contains characters outside the expected set"}

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    activities_url = f"{service_url}v3/conversations/{chat_id}/activities"

    if not AIOHTTP_AVAILABLE:
        return {"error": "Teams standalone send: aiohttp not installed"}

    try:
        import aiohttp as _aiohttp

        # Per-request timeouts so a slow STS endpoint cannot starve the
        # subsequent activity POST of its budget.
        per_request_timeout = _aiohttp.ClientTimeout(total=15.0)
        async with _aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://api.botframework.com/.default",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=per_request_timeout,
            ) as token_resp:
                if token_resp.status >= 400:
                    body = await token_resp.text()
                    return {"error": f"Teams standalone send: token request failed ({token_resp.status}): {body[:300]}"}
                token_payload = await token_resp.json()
            access_token = token_payload.get("access_token")
            if not access_token:
                return {"error": "Teams standalone send: token response missing access_token"}

            activity = {
                "type": "message",
                "text": message,
                "textFormat": "markdown",
            }
            async with session.post(
                activities_url,
                json=activity,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=per_request_timeout,
            ) as send_resp:
                if send_resp.status >= 400:
                    body = await send_resp.text()
                    return {"error": f"Teams standalone send: activity post failed ({send_resp.status}): {body[:300]}"}
                send_payload = await send_resp.json()
        return {
            "success": True,
            "message_id": send_payload.get("id"),
        }
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("Teams standalone send raised", exc_info=True)
        return {"error": f"Teams standalone send failed: {e}"}


# ``check_requirements`` is the passive probe used as the registry
# ``check_fn`` and by ``gateway status``, so it must never trigger a pip
# install.  ``check_teams_requirements`` is the active lazy installer, called
# from :meth:`TeamsAdapter.connect`; it installs ``platform.teams`` on
# demand and rebinds the SDK globals, mirroring
# ``check_slack_requirements`` in gateway/platforms/slack.py.
def check_teams_requirements() -> bool:
    """Ensure the Teams SDK is importable, lazy-installing it on first use.

    Lazy-installs ``microsoft-teams-apps`` via
    ``tools.lazy_deps.ensure("platform.teams")`` if not present, then rebinds
    all module-level SDK globals on success. Returns True once the SDK (and
    aiohttp) are importable, False if they couldn't be installed/imported.
    """
    if TEAMS_SDK_AVAILABLE and AIOHTTP_AVAILABLE:
        return True

    def _import() -> dict:
        """Import the SDK and package every module-level name to rebind.

        ``tools.lazy_deps.ensure_and_bind`` calls this and assigns each
        returned key onto the module globals, so the top-of-module imports
        are repaired in place rather than being shadowed by a local import.

        Returns:
            Mapping of module global name to the imported symbol.
        """
        from aiohttp import web as _web
        from microsoft_teams.apps import App, ActivityContext
        from microsoft_teams.common.http.client import ClientOptions
        from microsoft_teams.api import MessageActivity, ConversationReference
        from microsoft_teams.api.activities.typing import TypingActivityInput
        from microsoft_teams.api.activities.invoke.adaptive_card import (
            AdaptiveCardInvokeActivity,
        )
        from microsoft_teams.api.models.adaptive_card import (
            AdaptiveCardActionCardResponse,
            AdaptiveCardActionMessageResponse,
        )
        from microsoft_teams.api.models.invoke_response import (
            InvokeResponse,
            AdaptiveCardInvokeResponse,
        )
        from microsoft_teams.apps.http.adapter import (
            HttpMethod,
            HttpRequest,
            HttpResponse,
            HttpRouteHandler,
        )
        from microsoft_teams.cards import AdaptiveCard, ExecuteAction, TextBlock

        return {
            "web": _web,
            "AIOHTTP_AVAILABLE": True,
            "App": App,
            "ActivityContext": ActivityContext,
            "ClientOptions": ClientOptions,
            "MessageActivity": MessageActivity,
            "ConversationReference": ConversationReference,
            "TypingActivityInput": TypingActivityInput,
            "AdaptiveCardInvokeActivity": AdaptiveCardInvokeActivity,
            "AdaptiveCardActionCardResponse": AdaptiveCardActionCardResponse,
            "AdaptiveCardActionMessageResponse": AdaptiveCardActionMessageResponse,
            "InvokeResponse": InvokeResponse,
            "AdaptiveCardInvokeResponse": AdaptiveCardInvokeResponse,
            "HttpMethod": HttpMethod,
            "HttpRequest": HttpRequest,
            "HttpResponse": HttpResponse,
            "HttpRouteHandler": HttpRouteHandler,
            "AdaptiveCard": AdaptiveCard,
            "ExecuteAction": ExecuteAction,
            "TextBlock": TextBlock,
            "TEAMS_SDK_AVAILABLE": True,
        }

    from tools.lazy_deps import ensure_and_bind

    return ensure_and_bind("platform.teams", _import, globals(), prompt=False)


class TeamsAdapter(BasePlatformAdapter):
    """Microsoft Teams adapter built on the microsoft-teams-apps SDK.

    Owns one aiohttp webhook server for the lifetime of a connection.  The
    SDK is responsible for auth and activity parsing; this adapter is
    responsible for the HTTP surface, for turning inbound activities into
    gateway :class:`MessageEvent` objects, and for pushing outbound text and
    adaptive cards through the SDK.

    Invariants:
        * ``_conv_refs`` is keyed by conversation id and only ever read on
          the outbound path.  A missing entry is not an error: the adapter
          falls back to sending by chat id.
        * Outbound calls are guarded by ``_app`` being set.  Sending before
          :meth:`connect` returns a failed :class:`SendResult` rather than
          raising, so a caller cannot take down a turn.
    """

    MAX_MESSAGE_LENGTH = 28000  # Teams text message limit (~28 KB)
    splits_long_messages = True  # send() chunks via truncate_message()

    def __init__(self, config: PlatformConfig):
        """Resolve credentials and initialize adapter state.

        No I/O happens here: :meth:`connect` performs the lazy SDK install
        and the server startup.

        Args:
            config: Platform configuration.  Its ``extra`` mapping supplies
                ``client_id``, ``client_secret``, ``tenant_id`` and
                ``port``; the environment wins where both are set.
        """
        super().__init__(config, Platform("teams"))
        extra = config.extra or {}
        self._client_id = extra.get("client_id") or os.getenv("TEAMS_CLIENT_ID", "")
        self._client_secret = extra.get("client_secret") or os.getenv("TEAMS_CLIENT_SECRET", "")
        self._tenant_id = extra.get("tenant_id") or os.getenv("TEAMS_TENANT_ID", "")
        self._port = _coerce_port(
            extra.get("port") or os.getenv("TEAMS_PORT", str(_DEFAULT_PORT))
        )
        self._app: Optional["App"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._dedup = MessageDeduplicator(max_size=1000)
        # chat_id -> ConversationReference, captured from inbound messages
        # so outbound cards are sent with the right conversation type
        # (personal / group chat / channel).
        self._conv_refs: Dict[str, Any] = {}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Install the SDK if needed, then start the webhook server.

        Lazy installation is attempted first, so a process that starts
        without the SDK can still come up.  Once the SDK is importable the
        adapter creates the aiohttp application, registers the SDK routes
        into it through :class:`_AiohttpBridgeAdapter`, binds the message
        and card-action handlers, and starts a TCP site on all interfaces.

        Args:
            is_reconnect: Whether this call follows a reconnect.  The server
                bind is unconditional, so the flag is accepted for
                signature parity but does not change the path taken.

        Returns:
            ``True`` when the webhook server is listening.  ``False`` when
            the SDK or aiohttp could not be made available, when
            credentials are missing, or when starting the site raised.

        Raises:
            Nothing; every failure is recorded with
            :meth:`_set_fatal_error` so the runner can decide whether to
            retry.
        """
        # Lazy-install the Teams SDK on demand (parity with Slack/Discord/etc.),
        # then re-check the module globals it rebinds.
        check_teams_requirements()
        if not TEAMS_SDK_AVAILABLE:
            self._set_fatal_error(
                "MISSING_SDK",
                "microsoft-teams-apps could not be installed. Run: pip install microsoft-teams-apps",
                retryable=False,
            )
            return False

        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error(
                "MISSING_SDK",
                "aiohttp not installed. Run: pip install aiohttp",
                retryable=False,
            )
            return False

        if not self._client_id or not self._client_secret or not self._tenant_id:
            self._set_fatal_error(
                "MISSING_CREDENTIALS",
                "TEAMS_CLIENT_ID, TEAMS_CLIENT_SECRET, and TEAMS_TENANT_ID are all required",
                retryable=False,
            )
            return False

        try:
            # Create the aiohttp application first: the bridge adapter wires
            # the SDK's routes into it during ``initialize()``.
            # client_max_size caps Bot Framework activity bodies, which are
            # JSON and well under 1 MiB; the explicit limit keeps oversized
            # or chunked bodies from being buffered without bound on a
            # 0.0.0.0 bind (same pattern as webhook.py / raft, #58536/#58902).
            aiohttp_app = web.Application(client_max_size=_MAX_BODY_BYTES)
            aiohttp_app.router.add_get("/health", lambda _: web.Response(text="ok"))

            self._app = App(
                client_id=self._client_id,
                client_secret=self._client_secret,
                tenant_id=self._tenant_id,
                http_server_adapter=_AiohttpBridgeAdapter(aiohttp_app),
                client=ClientOptions(headers={"User-Agent": "Encre"}),
            )

            # Register message handler before initialize()
            @self._app.on_message
            async def _handle_message(ctx: ActivityContext[MessageActivity]):
                """Route one inbound message activity to the adapter."""
                await self._on_message(ctx)

            @self._app.on_card_action
            async def _handle_card_action(
                ctx: ActivityContext[AdaptiveCardInvokeActivity],
            ) -> InvokeResponse[AdaptiveCardActionMessageResponse]:
                """Route one inbound adaptive card button click to the adapter."""
                return await self._on_card_action(ctx)

            # initialize() calls register_route() on the bridge, which adds
            # POST /api/messages to aiohttp_app automatically
            await self._app.initialize()

            self._runner = web.AppRunner(aiohttp_app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            await site.start()

            self._running = True
            self._mark_connected()
            logger.info(
                "[teams] Webhook server listening on 0.0.0.0:%d%s",
                self._port,
                _WEBHOOK_PATH,
            )
            return True

        except Exception as e:
            self._set_fatal_error(
                "CONNECT_FAILED",
                f"Teams connection failed: {e}",
                retryable=True,
            )
            logger.error("[teams] Failed to connect: %s", e)
            return False

    async def disconnect(self) -> None:
        """Stop the webhook server and release the SDK app.

        Idempotent: every handle is checked before use, so a second call and
        a call before :meth:`connect` are both no-ops rather than errors.

        Args:
            None.

        Returns:
            Nothing.
        """
        self._running = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        self._mark_disconnected()
        logger.info("[teams] Disconnected")

    async def _fetch_attachment_bytes(self, url: str, timeout: float = 30.0) -> bytes:
        """Download attachment bytes with SSRF protection.

        Teams file attachments carry pre-authenticated SharePoint download
        URLs (no extra auth header needed). Validates the URL against the
        SSRF guard and follows redirects through the shared redirect guard,
        matching the cache_*_from_url helpers in encre.gateway.platforms.base.
        """
        from tools.url_safety import is_safe_url
        from encre.gateway.platforms.base import _ssrf_redirect_guard

        if not is_safe_url(url):
            raise ValueError("Blocked unsafe attachment URL (SSRF protection)")

        import httpx

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            event_hooks={"response": [_ssrf_redirect_guard]},
        ) as client:
            response = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; EncreAgent/1.0)"},
            )
            response.raise_for_status()
            return response.content

    async def _on_message(self, ctx: ActivityContext[MessageActivity]) -> None:
        """Normalise one inbound Teams message into a gateway event.

        Runs the self-message and duplicate filters, caches the conversation
        reference for proactive sends, strips @mention markup, maps the
        Bot Framework conversation type onto the gateway's chat types, and
        downloads attachments into the gateway media cache.

        Args:
            ctx: The SDK activity context for a ``MessageActivity``.

        Returns:
            Nothing; the normalised event is handed to
            :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`.
        """
        activity = ctx.activity

        # Self-message filter
        bot_id = self._app.id if self._app else None
        if bot_id and getattr(activity.from_, "id", None) == bot_id:
            return

        # Deduplication
        msg_id = getattr(activity, "id", None)
        if msg_id and self._dedup.is_duplicate(msg_id):
            return

        # Cache the conversation reference for proactive sends (approval cards, etc.)
        conv_id = getattr(activity.conversation, "id", None)
        if conv_id:
            self._conv_refs[conv_id] = ctx.conversation_ref

        # Extract the message body, then strip bot @mentions.
        text = ""
        if hasattr(activity, "text") and activity.text:
            text = activity.text
        # Strip <at>BotName</at> HTML tags that Teams prepends for @mentions
        if "<at>" in text:
            import re
            text = re.sub(r"<at>[^<]*</at>\s*", "", text).strip()

        # Determine chat type from conversation
        conv = activity.conversation
        conv_type = getattr(conv, "conversation_type", None) or ""
        if conv_type == "personal":
            chat_type = "dm"
        elif conv_type == "groupChat":
            chat_type = "group"
        elif conv_type == "channel":
            chat_type = "channel"
        else:
            chat_type = "dm"

        # Build source
        from_account = activity.from_
        user_id = getattr(from_account, "aad_object_id", None) or getattr(from_account, "id", "")
        user_name = getattr(from_account, "name", None) or ""

        source = self.build_source(
            chat_id=conv.id,
            chat_name=getattr(conv, "name", None) or "",
            chat_type=chat_type,
            user_id=str(user_id),
            user_name=user_name,
            guild_id=getattr(conv, "tenant_id", None) or self._tenant_id,
        )

        # Handle attachments (images, documents, video, audio)
        media_urls = []
        media_types = []
        media_kinds = []
        for att in getattr(activity, "attachments", None) or []:
            content_url = getattr(att, "content_url", None)
            content_type = (getattr(att, "content_type", None) or "").lower()
            att_name = getattr(att, "name", None) or ""

            # Skip non-file payloads: Teams mirrors the message body as a
            # text/html attachment on every message, and adaptive/hero cards
            # arrive as application/vnd.microsoft.card.* attachments.
            if content_type in ("text/html", "text/plain") and not content_url:
                continue
            if content_type.startswith("application/vnd.microsoft.card"):
                continue

            if content_type == "application/vnd.microsoft.teams.file.download.info":
                # File consent-free download: content carries a pre-authed
                # SharePoint downloadUrl plus the real file type.
                content = getattr(att, "content", None)
                if not isinstance(content, dict):
                    content = getattr(content, "__dict__", None) or {}
                download_url = content.get("downloadUrl") or content.get("download_url")
                file_type = (content.get("fileType") or content.get("file_type") or "").lstrip(".")
                if not download_url:
                    continue
                filename = att_name or (f"document.{file_type}" if file_type else "document")
                try:
                    data = await self._fetch_attachment_bytes(download_url)
                    cached = cache_media_bytes(data, filename=filename, mime_type="")
                    if cached:
                        media_urls.append(cached.path)
                        media_types.append(cached.media_type)
                        media_kinds.append(cached.kind)
                    else:
                        logger.warning(
                            "[teams] Unsupported document type for attachment '%s', skipping",
                            filename,
                        )
                except Exception as e:
                    logger.warning("[teams] Failed to cache file attachment '%s': %s", filename, e)
                continue

            if content_url and content_type.startswith("image/"):
                try:
                    cached = await cache_image_from_url(content_url)
                    if cached:
                        media_urls.append(cached)
                        media_types.append(content_type)
                        media_kinds.append("image")
                except Exception as e:
                    logger.warning("[teams] Failed to cache image attachment: %s", e)
                continue

            if content_url:
                # Direct-URL non-image attachment (video/audio/document).
                try:
                    data = await self._fetch_attachment_bytes(content_url)
                    cached = cache_media_bytes(
                        data, filename=att_name, mime_type=content_type
                    )
                    if cached:
                        media_urls.append(cached.path)
                        media_types.append(cached.media_type)
                        media_kinds.append(cached.kind)
                except Exception as e:
                    logger.warning(
                        "[teams] Failed to cache attachment '%s' (%s): %s",
                        att_name or content_url, content_type, e,
                    )

        # Classification precedence: DOCUMENT beats PHOTO, VIDEO and AUDIO
        # for mixed attachments.  run.py's image handling keys off the
        # per-path image/* mime types regardless of message_type, but
        # document-context injection gates strictly on MessageType.DOCUMENT
        # (same precedence as Email/Signal, PR #44695).
        if "document" in media_kinds:
            msg_type = MessageType.DOCUMENT
        elif "image" in media_kinds:
            msg_type = MessageType.PHOTO
        elif "video" in media_kinds:
            msg_type = MessageType.VIDEO
        elif "audio" in media_kinds:
            msg_type = MessageType.AUDIO
        else:
            msg_type = MessageType.TEXT

        event = MessageEvent(
            text=text,
            source=source,
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            message_id=msg_id,
        )
        await self.handle_message(event)

    async def _send_card(self, chat_id: str, card: "AdaptiveCard") -> "Any":
        """Send an adaptive card to a conversation.

        A cached :class:`ConversationReference` is used when available,
        because a reference preserves the conversation type and is what the
        SDK needs to deliver a card to a channel or group chat.  Without
        one the card is sent by chat id, which is correct for personal
        chats.

        Args:
            chat_id: Conversation or chat identifier.
            card: The adaptive card to send.

        Returns:
            The SDK send result, or ``None`` when no app is available.
        """
        from microsoft_teams.api import MessageActivityInput

        conv_ref = self._conv_refs.get(chat_id)
        if conv_ref and self._app:
            activity = MessageActivityInput().add_card(card)
            return await self._app.activity_sender.send(activity, conv_ref)
        elif self._app:
            return await self._app.send(chat_id, card)
        return None

    async def _on_card_action(
        self, ctx: "ActivityContext[AdaptiveCardInvokeActivity]"
    ) -> "InvokeResponse[AdaptiveCardActionMessageResponse]":
        """Resolve one adaptive card approval button click.

        Validates the action payload, enforces the allowlist, resolves the
        gateway approval, and returns a card describing the outcome so the
        clicker sees the result in the same conversation.

        Args:
            ctx: The SDK activity context for an
                ``AdaptiveCardInvokeActivity``.

        Returns:
            An ``InvokeResponse`` carrying an adaptive card or a plain
            message describing what happened.
        """
        from tools.approval import resolve_gateway_approval, has_blocking_approval

        action = ctx.activity.value.action
        data = action.data or {}
        encre_action = data.get("encre_action", "")
        session_key = data.get("session_key", "")

        if not encre_action or not session_key:
            return InvokeResponse(
                status=200,
                body=AdaptiveCardActionMessageResponse(value="Unknown action."),
            )

        # Only authorized users may click approval buttons.  Default deny:
        # require either TEAMS_ALLOWED_USERS or an explicit
        # TEAMS_ALLOW_ALL_USERS=true opt-in.  Without one of those set the
        # bot would treat every clicker as authorized, meaning any Teams
        # user who could message the bot could approve dangerous commands.
        allowed_csv = os.getenv("TEAMS_ALLOWED_USERS", "").strip()
        allow_all = os.getenv("TEAMS_ALLOW_ALL_USERS", "").strip().lower() in {"1", "true", "yes"}

        if not allow_all:
            if not allowed_csv:
                logger.warning(
                    "[teams] card action rejected: TEAMS_ALLOWED_USERS not configured "
                    "and TEAMS_ALLOW_ALL_USERS not set 鈥?default deny"
                )
                return InvokeResponse(
                    status=200,
                    body=AdaptiveCardActionMessageResponse(
                        value="鉀?Approval buttons require TEAMS_ALLOWED_USERS to be configured."
                    ),
                )
            from_account = ctx.activity.from_
            clicker_id = getattr(from_account, "aad_object_id", None) or getattr(from_account, "id", "")
            allowed_ids = {uid.strip() for uid in allowed_csv.split(",") if uid.strip()}
            if "*" not in allowed_ids and clicker_id not in allowed_ids:
                logger.warning("[teams] Unauthorized card action by %s 鈥?ignoring", clicker_id)
                return InvokeResponse(
                    status=200,
                    body=AdaptiveCardActionMessageResponse(value="鉀?Not authorized."),
                )

        choice_map = {
            "approve_once": "once",
            "approve_session": "session",
            "approve_always": "always",
            "deny": "deny",
        }
        choice = choice_map.get(encre_action)
        if not choice:
            return InvokeResponse(
                status=200,
                body=AdaptiveCardActionMessageResponse(value="Unknown action."),
            )

        if not has_blocking_approval(session_key):
            return InvokeResponse(
                status=200,
                body=AdaptiveCardActionCardResponse(
                    value=AdaptiveCard()
                    .with_version("1.4")
                    .with_body([TextBlock(text="鈿狅笍 Approval already resolved or expired.", wrap=True)])
                ),
            )

        resolve_gateway_approval(session_key, choice)

        label_map = {
            "once": "鉁?Allowed (once)",
            "session": "鉁?Allowed (session)",
            "always": "鉁?Always allowed",
            "deny": "鉂?Denied",
        }
        cmd = data.get("cmd", "")
        desc = data.get("desc", "")
        body = []
        if cmd:
            body.append(TextBlock(text="鈿狅笍 Command Approval Required", wrap=True, weight="Bolder"))
            body.append(TextBlock(text=f"```\n{cmd}\n```", wrap=True))
        if desc:
            body.append(TextBlock(text=f"Reason: {desc}", wrap=True, isSubtle=True))
        body.append(TextBlock(text=label_map[choice], wrap=True, weight="Bolder"))

        return InvokeResponse(
            status=200,
            body=AdaptiveCardActionCardResponse(
                value=AdaptiveCard().with_version("1.4").with_body(body)
            ),
        )

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
        allow_permanent: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """Send an adaptive card approval prompt with Allow and Deny buttons.

        Builds the card with one button per permitted choice, then sends it
        with :meth:`_send_card`.  The command text shown in the card body is
        truncated to a fixed budget so a long command cannot blow up the
        card payload.

        Args:
            chat_id: Conversation or chat identifier to send the card to.
            command: The command the user is being asked about.
            session_key: Gateway session key that owns the approval.
            description: Human reason shown under the command.
            metadata: Accepted for contract parity; unused.
            allow_permanent: Whether to offer the "Always Allow" button.
            smart_denied: When true, only a one-off allow button is offered.

        Returns:
            A :class:`SendResult` with the created message id on success.
        """
        if not self._app:
            return SendResult(success=False, error="Teams app not initialized")

        cmd_preview = command[:2000] + "..." if len(command) > 2000 else command
        # Truncated to the button data payload budget; enough to rebuild
        # the card body after the click round trip.
        btn_data_base = {
            "session_key": session_key,
            "cmd": command[:200] + "..." if len(command) > 200 else command,
            "desc": description,
        }

        actions = [ExecuteAction(
             title="Allow Once", verb="encre_approve",
             data={**btn_data_base, "encre_action": "approve_once"}, style="positive",
        )]
        if not smart_denied:
            actions.append(ExecuteAction(
                title="Allow Session", verb="encre_approve",
                data={**btn_data_base, "encre_action": "approve_session"},
            ))
            if allow_permanent:
                actions.append(ExecuteAction(
                    title="Always Allow", verb="encre_approve",
                    data={**btn_data_base, "encre_action": "approve_always"},
                ))
        actions.append(ExecuteAction(
            title="Deny", verb="encre_approve",
            data={**btn_data_base, "encre_action": "deny"}, style="destructive",
        ))
        body = [
            TextBlock(text="鈿狅笍 Command Approval Required", wrap=True, weight="Bolder"),
            TextBlock(text=f"```\n{cmd_preview}\n```", wrap=True),
            TextBlock(text=f"Reason: {description}", wrap=True, isSubtle=True),
        ]
        if smart_denied:
            body.append(TextBlock(
                text="Smart DENY: owner override applies to this one operation only.", wrap=True
            ))
        card = AdaptiveCard().with_version("1.4").with_body(body).with_actions(actions)

        try:
            result = await self._send_card(chat_id, card)
            message_id = getattr(result, "id", None) if result else None
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("[teams] send_exec_approval failed: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send agent output to ``chat_id``, chunked to the message limit.

        This is the method called by
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.  Content
        is markdown-formatted and chunked against
        :attr:`MAX_MESSAGE_LENGTH`, then each chunk is posted as a separate
        activity.

        Args:
            chat_id: Conversation or chat identifier.
            content: Agent output, still containing markdown.
            reply_to: Optional numeric message id to thread the reply
                under.  When threading fails the adapter falls back to a
                flat send rather than dropping the message.
            metadata: Accepted for contract parity; unused.

        Returns:
            A :class:`SendResult`; ``message_id`` is the id of the last
            chunk sent, since Teams assigns one id per activity.

        Raises:
            Nothing; a failed send is returned as a retryable failure.
        """
        if not self._app:
            return SendResult(success=False, error="Teams app not initialized")

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted)
        last_message_id = None

        for chunk in chunks:
            try:
                if reply_to and reply_to.isdigit() and reply_to != "0":
                    try:
                        result = await self._app.reply(chat_id, reply_to, chunk)
                    except Exception as reply_err:
                        # Group chats 400 on threaded sends; the Teams SDK
                        # doesn't expose typed HTTP errors, so fall back on
                        # any exception and log for diagnostics.
                        logger.debug(
                            "Teams reply() failed, falling back to flat send: %s",
                            reply_err,
                        )
                        result = await self._app.send(chat_id, chunk)
                else:
                    result = await self._app.send(chat_id, chunk)
                last_message_id = getattr(result, "id", None)
            except Exception as e:
                return SendResult(success=False, error=str(e), retryable=True)

        return SendResult(success=True, message_id=last_message_id)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Send a Teams typing indicator.

        Args:
            chat_id: Conversation or chat identifier.
            metadata: Accepted for contract parity; unused.

        Returns:
            Nothing.  A failed indicator is swallowed: a typing signal is a
            best-effort nicety and must never fail the caller's turn.
        """
        if not self._app:
            return
        try:
            await self._app.send(chat_id, TypingActivityInput())
        except Exception:
            pass

    async def _send_media_attachment(
        self,
        chat_id: str,
        source: str,
        default_mime: str,
        caption: Optional[str] = None,
        media_label: str = "media",
    ) -> SendResult:
        """Send any media file/URL as a Teams attachment.

        Remote ``http(s)://`` URLs are attached by reference; local paths
        (with optional ``file://`` prefix) are base64-encoded into a data
        URI. MIME type is guessed from the path/extension, falling back to
        ``default_mime``. Shared by send_image / send_video / send_voice /
        send_document so every media kind uses the same Attachment path.
        """
        if not self._app:
            return SendResult(success=False, error="Teams app not initialized")

        try:
            import base64
            import mimetypes
            from microsoft_teams.api import Attachment, MessageActivityInput

            if source.startswith("http://") or source.startswith("https://"):
                content_url = source
                mime_type = mimetypes.guess_type(source.split("?")[0])[0] or default_mime
            else:
                # Local path: inline the bytes as a base64 data URI so the
                # attachment carries no external URL.
                path = source.removeprefix("file://")
                mime_type = mimetypes.guess_type(path)[0] or default_mime
                with open(path, "rb") as f:
                    content_url = f"data:{mime_type};base64,{base64.b64encode(f.read()).decode()}"

            attachment = Attachment(content_type=mime_type, content_url=content_url)
            activity = MessageActivityInput().add_attachments(attachment)
            if caption:
                activity = activity.add_text(caption)

            conv_ref = self._conv_refs.get(chat_id)
            if conv_ref:
                result = await self._app.activity_sender.send(activity, conv_ref)
            else:
                result = await self._app.send(chat_id, activity)

            return SendResult(success=True, message_id=getattr(result, "id", None))
        except Exception as e:
            logger.error("[teams] send_%s failed: %s", media_label, e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image by URL or local path.

        Args:
            chat_id: Conversation or chat identifier.
            image_url: http(s) URL or local path.
            caption: Optional text accompanying the image.
            reply_to: Accepted for contract parity; unused.
            metadata: Accepted for contract parity; unused.

        Returns:
            A :class:`SendResult`.
        """
        return await self._send_media_attachment(
            chat_id=chat_id,
            source=image_url,
            default_mime="image/png",
            caption=caption,
            media_label="image",
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image from a local file path.

        Thin wrapper over :meth:`send_image`, which already handles local
        paths by base64-encoding them into a data URI.

        Args:
            chat_id: Conversation or chat identifier.
            image_path: Local file path.
            caption: Optional accompanying text.
            reply_to: Accepted for contract parity; unused.
            **kwargs: Accepted and ignored.

        Returns:
            A :class:`SendResult`.
        """
        return await self.send_image(
            chat_id=chat_id,
            image_url=image_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video attachment.

        Args:
            chat_id: Conversation or chat identifier.
            video_path: http(s) URL or local path.
            caption: Optional accompanying text.
            reply_to: Accepted for contract parity; unused.
            metadata: Accepted for contract parity; unused.
            **kwargs: Accepted and ignored.

        Returns:
            A :class:`SendResult`.
        """
        return await self._send_media_attachment(
            chat_id=chat_id,
            source=video_path,
            default_mime="video/mp4",
            caption=caption,
            media_label="video",
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio attachment.

        Args:
            chat_id: Conversation or chat identifier.
            audio_path: http(s) URL or local path.
            caption: Optional accompanying text.
            reply_to: Accepted for contract parity; unused.
            metadata: Accepted for contract parity; unused.
            **kwargs: Accepted and ignored.

        Returns:
            A :class:`SendResult`.
        """
        return await self._send_media_attachment(
            chat_id=chat_id,
            source=audio_path,
            default_mime="audio/mpeg",
            caption=caption,
            media_label="voice",
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an arbitrary document attachment.

        Args:
            chat_id: Conversation or chat identifier.
            file_path: http(s) URL or local path.
            caption: Optional accompanying text.
            file_name: Accepted for contract parity; the real name comes
                from the source path or URL.
            reply_to: Accepted for contract parity; unused.
            metadata: Accepted for contract parity; unused.
            **kwargs: Accepted and ignored.

        Returns:
            A :class:`SendResult`.
        """
        return await self._send_media_attachment(
            chat_id=chat_id,
            source=file_path,
            default_mime="application/octet-stream",
            caption=caption,
            media_label="document",
        )

    async def get_chat_info(self, chat_id: str) -> dict:
        """Return a minimal chat descriptor.

        The adapter does not cache conversation metadata beyond the reference
        map, so the identifier is echoed back with an unknown type.

        Args:
            chat_id: Conversation or chat identifier.

        Returns:
            Dict with ``name``, ``type`` and ``chat_id``.
        """
        return {"name": chat_id, "type": "unknown", "chat_id": chat_id}


# -- Interactive setup -------------------------------------------------------

def interactive_setup() -> None:
    """Interactive ``encre gateway setup`` flow for the Teams platform.

    Walks the operator through the Teams CLI bootstrap and records the
    credentials in the data-dir ``.env``.  Lazy-imports the setup helpers so
    this plugin stays importable in non-CLI contexts that have no terminal.

    Args:
        None.

    Returns:
        Nothing.  Returns early when the operator declines to reconfigure,
        or when a required credential is left blank.

    Note:
        Restricting access is offered as the recommended path; choosing open
        access records ``TEAMS_ALLOW_ALL_USERS=true``, which is what
        :meth:`_on_card_action` checks before honoring an approval click.
    """
    from encre.config import (
        get_env_value,
        save_env_value,
    )
    from encre.setup import (
        prompt,
        prompt_yes_no,
        print_info,
        print_success,
        print_warning,
    )

    existing_id = get_env_value("TEAMS_CLIENT_ID")
    if existing_id:
        print_info(f"Teams: already configured (app ID: {existing_id})")
        if not prompt_yes_no("Reconfigure Teams?", False):
            return

    print_info("You'll need the Teams CLI. If you haven't already:")
    print_info("  npm install -g @microsoft/teams.cli@preview")
    print_info("  teams login")
    print()
    print_info("Then expose port 3978 publicly (devtunnel / ngrok / cloudflared),")
    print_info("and create your bot:")
    print_info("  teams app create --name \"Encre\" --endpoint \"https://<tunnel>/api/messages\"")
    print()
    print_info("The CLI will print CLIENT_ID, CLIENT_SECRET, and TENANT_ID. Paste them below.")
    print()

    client_id = prompt("Client ID", default=existing_id or "")
    if not client_id:
        print_warning("Client ID is required 鈥?skipping Teams setup")
        return
    save_env_value("TEAMS_CLIENT_ID", client_id.strip())

    client_secret = prompt("Client secret", default=get_env_value("TEAMS_CLIENT_SECRET") or "", password=True)
    if not client_secret:
        print_warning("Client secret is required 鈥?skipping Teams setup")
        return
    save_env_value("TEAMS_CLIENT_SECRET", client_secret.strip())

    tenant_id = prompt("Tenant ID", default=get_env_value("TEAMS_TENANT_ID") or "")
    if not tenant_id:
        print_warning("Tenant ID is required 鈥?skipping Teams setup")
        return
    save_env_value("TEAMS_TENANT_ID", tenant_id.strip())

    print()
    print_info("To find your AAD object ID for the allowlist: teams status --verbose")
    if prompt_yes_no("Restrict access to specific users? (recommended)", True):
        allowed = prompt(
            "Allowed AAD object IDs (comma-separated)",
            default=get_env_value("TEAMS_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("TEAMS_ALLOWED_USERS", allowed.replace(" ", ""))
            print_success("Allowlist configured")
        else:
            save_env_value("TEAMS_ALLOWED_USERS", "")
    else:
        save_env_value("TEAMS_ALLOW_ALL_USERS", "true")
        print_warning("鈿狅笍  Open access 鈥?anyone who can message the bot can command it.")

    print()
    print_success("Teams configuration saved to data dir/.env")
    print_info("Install the app in Teams:  teams app install --id <teamsAppId>")
    print_info("Restart the gateway:       encre gateway restart")


# -- Plugin entry point ------------------------------------------------------

def _build_adapter(config):
    """Construct the adapter for the platform registry entry.

    Registered as ``adapter_factory`` on the :class:`PlatformEntry` below
    and invoked by :class:`encre.gateway.run.GatewayRunner` once
    configuration is validated.

    Args:
        config: A :class:`encre.gateway.config.PlatformConfig` for Teams.

    Returns:
        A configured :class:`TeamsAdapter` instance.
    """
    return TeamsAdapter(config)
