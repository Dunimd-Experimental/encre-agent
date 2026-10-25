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

"""QQ Bot inline keyboards, approval rendering, and interaction decoding.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module owns both halves of QQ Bot's interactive button protocol. On the
outbound side it serialises Encre's approval and update-confirmation flows
into the ``keyboard`` field of a message body; on the inbound side it decodes
the ``INTERACTION_CREATE`` gateway dispatch that QQ fires when a user taps a
button.

Lifecycle of a single button press::

    build_*_keyboard()          -> InlineKeyboard            outbound
    send_with_keyboard()        -> POST .../messages          outbound
    user taps a button          -> INTERACTION_CREATE dispatch
    parse_interaction_event()   -> InteractionEvent           inbound
    parse_*_button_data()       -> (session_key, decision)    inbound
    approval / update resolver  -> unblocks the waiting agent

The bot must ACK every interaction with ``PUT /interactions/{id}`` promptly,
otherwise QQ shows an error indicator on the button and the user perceives a
dead click. The ACK itself lives in the adapter; this module only produces the
payloads it needs.

button_data encoding::

    approve:<session_key>:<decision>      decision in allow-once|allow-always|deny
    update_prompt:<answer>                answer in y|n

The ``session_key`` segment is greedy because a key such as
``agent:main:qqbot:c2c:<openid>`` already contains colons.

QQ Bot v2 keyboard shape::

    InlineKeyboard
        content: KeyboardContent
            rows: List[KeyboardRow]
                buttons: List[KeyboardButton]
                    id           unique identifier within the keyboard
                    render_data  label / visited_label / style
                    action       type (1=callback, 2=link), data, permission,
                                 click_limit
                    group_id     mutual-exclusion group

Public API:
    :class:`InlineKeyboard`, :class:`ApprovalRequest`, :class:`ApprovalSender`,
    :class:`InteractionEvent`, :func:`build_approval_keyboard`,
    :func:`build_approval_text`, :func:`build_update_prompt_keyboard`,
    :func:`parse_approval_button_data`, :func:`parse_update_prompt_button_data`,
    :func:`parse_interaction_event`.

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# button_data prefixes + patterns

# Prefix used in button_data to identify approval decisions.
APPROVAL_BUTTON_PREFIX = "approve:"

# Prefix used in button_data to identify update-prompt answers.
UPDATE_PROMPT_PREFIX = "update_prompt:"

# Pattern: approve:<session_key>:<decision>
# session_key may itself contain colons (e.g. agent:main:qqbot:c2c:OPENID),
# so the session_key group is greedy but trails the decision.
# This regex captures exactly three groups: full_session_key and decision.
_APPROVAL_DATA_RE = re.compile(
    r"^approve:(.+):(allow-once|allow-always|deny)$"
)

# Pattern: update_prompt:y | update_prompt:n
# Simple two-option pattern -- only accepts 'y' or 'n'.
_UPDATE_PROMPT_RE = re.compile(r"^update_prompt:(y|n)$")


# Keyboard dataclasses

@dataclass
class KeyboardButtonPermission:
    """Who is allowed to tap a given button.

    QQ Bot v2 exposes three permission scopes. Only ``2`` is used by Encre,
    since every button it sends is meant to be actionable by anyone in the
    conversation rather than gated to the message author.

    Keeping this a dedicated dataclass (rather than an inline int) means the
    wire format can be extended later -- for example to a user-id allowlist --
    without touching every call site that builds a button.

    Attributes:
        type: Permission type code. ``2`` = all users can click. ``0`` =
            creator only, ``1`` = a specified user list.
    """

    type: int = 2

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the ``permission`` object expected by the QQ API."""
        return {"type": self.type}


@dataclass
class KeyboardButtonAction:
    """What happens when the button is tapped.

    Two action kinds exist in QQ Bot v2. Encre only uses callbacks, because a
    callback is the only kind that routes back to the bot as an
    ``INTERACTION_CREATE`` event; a link action would leave the
    conversation entirely.

    Attributes:
        type: Action type. ``1`` = callback, ``2`` = link.
        data: Payload delivered verbatim as ``button_data`` inside the
            interaction event. Ignored by the API when ``type`` is ``2``.
        permission: Who may click this button.
        click_limit: Maximum clicks per user. ``1`` makes the button
            single-use, which is what approval buttons want -- a second tap
            must not re-resolve an already-answered request.
    """

    type: int
    data: str
    permission: KeyboardButtonPermission = field(
        default_factory=KeyboardButtonPermission
    )
    click_limit: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the ``action`` object expected by the QQ API."""
        return {
            "type": self.type,
            "data": self.data,
            "permission": self.permission.to_dict(),
            "click_limit": self.click_limit,
        }


@dataclass
class KeyboardButtonRenderData:
    """Visual presentation of a button before and after a tap.

    QQ keeps the button in place after it is tapped and swaps in
    ``visited_label`` rather than removing the widget. That lets the user see
    that they did act, while the ``click_limit`` of 1 prevents a repeat.

    Attributes:
        label: Text shown before the button is clicked.
        visited_label: Text shown after the click; the button stays rendered
            in a greyed state instead of disappearing.
        style: Visual weight. ``0`` = grey background, ``1`` = blue
            background. Encre uses blue for affirmative actions and grey for
            deny/cancel so the intended action is readable at a glance.
    """

    label: str
    visited_label: str
    style: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the ``render_data`` object expected by the QQ API."""
        return {
            "label": self.label,
            "visited_label": self.visited_label,
            "style": self.style,
        }


@dataclass
class KeyboardButton:
    """One button occupying a slot in a keyboard row.

    Attributes:
        id: Identifier unique within the keyboard. Also echoed back as
            ``button_id`` in the interaction event, so it can be used to
            correlate a tap with the button it came from.
        render_data: Appearance before and after a click.
        action: Callback payload, click permission, and click limit.
        group_id: Buttons sharing a ``group_id`` are mutually exclusive --
            tapping one greys out every other button in the group. This is
            what stops a user from approving and then denying the same
            request. Use ``"default"`` for buttons that should act
            independently.
    """

    id: str
    render_data: KeyboardButtonRenderData
    action: KeyboardButtonAction
    group_id: str = "default"

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the ``buttons[]`` entry expected by the QQ API."""
        return {
            "id": self.id,
            "render_data": self.render_data.to_dict(),
            "action": self.action.to_dict(),
            "group_id": self.group_id,
        }


@dataclass
class KeyboardRow:
    """A single horizontal row of buttons.

    QQ lays out a keyboard as stacked rows and renders at most five buttons
    per row, so builders keep rows small rather than packing everything into
    one line.

    Attributes:
        buttons: Buttons in this row, ordered left to right.
    """

    buttons: List[KeyboardButton] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the ``rows[]`` entry expected by the QQ API."""
        return {"buttons": [b.to_dict() for b in self.buttons]}


@dataclass
class KeyboardContent:
    """Container holding the rows that make up one keyboard.

    Attributes:
        rows: Button rows, ordered top to bottom.
    """

    rows: List[KeyboardRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the ``content`` object expected by the QQ API."""
        return {"rows": [r.to_dict() for r in self.rows]}


@dataclass
class InlineKeyboard:
    """Root keyboard payload, serialised into ``MessageToCreate.keyboard``.

    This is the object handed to the adapter's ``send_with_keyboard()`` and
    emitted as the ``keyboard`` key of the outbound message body. Everything
    else in this module nests inside it.

    Attributes:
        content: The row container for the keyboard.
    """

    content: KeyboardContent = field(default_factory=KeyboardContent)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the ``keyboard`` field of an outbound message body."""
        return {"content": self.content.to_dict()}


# INTERACTION_CREATE parsing

def parse_approval_button_data(button_data: str) -> Optional[tuple[str, str]]:
    """Decode an approval ``button_data`` string into ``(session_key, decision)``.

    The session key routes the decision back to the exact pending approval
    that an agent thread is blocked on. Decisions are a fixed closed set, so
    anything outside it is rejected at the caller rather than here.

    Because ``session_key`` can contain colons, the regex anchors the decision
    against the end of the string and lets the key group absorb the rest.

    Args:
        button_data: The raw ``data.resolved.button_data`` value from an
            ``INTERACTION_CREATE`` event payload.

    Returns:
        ``(session_key, decision)`` where ``decision`` is one of
        ``"allow-once"``, ``"allow-always"``, or ``"deny"``. Returns
        ``None`` when the payload does not match the approval pattern, so the
        caller can fall through to the update-prompt parser.

    Example::

        >>> parse_approval_button_data("approve:agent:main:qqbot:c2c:abc123:allow-once")
        ("agent:main:qqbot:c2c:abc123", "allow-once")
        >>> parse_approval_button_data("update_prompt:y")
        None
    """
    m = _APPROVAL_DATA_RE.match(button_data or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def parse_update_prompt_button_data(button_data: str) -> Optional[str]:
    """Decode an update-prompt ``button_data`` string into ``'y'`` or ``'n'``.

    Args:
        button_data: The raw ``data.resolved.button_data`` value from an
            ``INTERACTION_CREATE`` event payload.

    Returns:
        ``'y'`` for confirm, ``'n'`` for cancel. Returns ``None`` when the
        payload does not match the update-prompt pattern.

    Example::

        >>> parse_update_prompt_button_data("update_prompt:y")
        'y'
        >>> parse_update_prompt_button_data("approve:...")
        None
    """
    m = _UPDATE_PROMPT_RE.match(button_data or "")
    if not m:
        return None
    return m.group(1)


# Keyboard builders

def _make_callback_button(
    btn_id: str,
    label: str,
    visited_label: str,
    data: str,
    style: int,
    group_id: str,
) -> KeyboardButton:
    """Build a callback button with Encre's default interaction policy.

    Centralises the settings every Encre button shares: ``type=1`` callback
    action, ``click_limit=1`` single-use behaviour, and the default
    all-users permission. Builders that need a different policy construct a
    :class:`KeyboardButton` directly instead.

    Args:
        btn_id: Unique button identifier, echoed back as ``button_id``.
        label: Text shown before the button is tapped.
        visited_label: Text shown after the tap, while the button stays
            greyed in place.
        data: Payload delivered as ``button_data`` in ``INTERACTION_CREATE``.
        style: Visual style; ``0`` grey, ``1`` blue.
        group_id: Mutual-exclusion group; buttons in the same group grey each
            other out when one is tapped.

    Returns:
        A fully populated :class:`KeyboardButton`.
    """
    return KeyboardButton(
        id=btn_id,
        render_data=KeyboardButtonRenderData(
            label=label,
            visited_label=visited_label,
            style=style,
        ),
        action=KeyboardButtonAction(type=1, data=data),
        group_id=group_id,
    )


def build_approval_keyboard(session_key: str, *, allow_permanent: bool = True) -> InlineKeyboard:
    """Build the tool-approval keyboard, dropping the permanent option when unavailable.

    Produces a single row of two or three callback buttons sharing
    ``group_id="approval"``. The shared group enforces mutual exclusion on the
    QQ side: tapping one button greys out its siblings, so a single request
    can only ever be resolved once even if the user keeps tapping.

    Each button's ``button_data`` embeds the ``session_key`` and a decision
    token:

    * ``allow-once`` -- permit this single execution, remember nothing.
    * ``allow-always`` -- whitelist the tool for the remainder of the session.
    * ``deny`` -- reject the execution.

    The permanent option is conditional because a smart-denied operation has
    no meaningful permanent scope; offering it there would let a user grant
    authority they did not intend to grant.

    Args:
        session_key: Key routing the decision back to the pending approval in
            the approval resolver. Must match the ``session_key`` that was
            registered when the approval was requested.
        allow_permanent: Include the ``allow-always`` button. Set ``False``
            for flows where a permanent whitelist is inappropriate.

    Returns:
        An :class:`InlineKeyboard` with one row of two or three buttons.

    button_data format::

        approve:<session_key>:allow-once
        approve:<session_key>:allow-always
        approve:<session_key>:deny
    """
    buttons = [
        # Primary action: allow once (blue, single-use).
        _make_callback_button(
            btn_id="allow", label="Allow once", visited_label="Already allowed",
            data=f"{APPROVAL_BUTTON_PREFIX}{session_key}:allow-once",
            style=1, group_id="approval",
        )
    ]
    if allow_permanent:
        # Secondary action: allow always (blue, single-use).
        buttons.append(_make_callback_button(
            btn_id="always", label="Always allow", visited_label="Already allowed",
            data=f"{APPROVAL_BUTTON_PREFIX}{session_key}:allow-always",
            style=1, group_id="approval",
        ))
    # Tertiary action: deny (grey, single-use -- visually distinct).
    buttons.append(_make_callback_button(
        btn_id="deny", label="Deny", visited_label="Already denied",
        data=f"{APPROVAL_BUTTON_PREFIX}{session_key}:deny",
        style=0, group_id="approval",
    ))
    return InlineKeyboard(content=KeyboardContent(rows=[KeyboardRow(buttons=buttons)]))


def build_update_prompt_keyboard() -> InlineKeyboard:
    """Build a confirm/cancel keyboard for Encre update prompts.

    Emits one row of two buttons sharing ``group_id="update_prompt"`` so the
    pair is mutually exclusive. The detached update process is blocked on a
    single answer, so the mutual-exclusion group guarantees exactly one
    answer can ever be recorded.

    Returns:
        An :class:`InlineKeyboard` with one row of two buttons.

    button_data format::

        update_prompt:y   (for confirm)
        update_prompt:n   (for cancel)
    """
    return InlineKeyboard(
        content=KeyboardContent(
            rows=[
                KeyboardRow(buttons=[
                    # Confirm button (blue).
                    _make_callback_button(
                        btn_id="yes",
                        label="Confirm",
                        visited_label="Confirmed",
                        data=f"{UPDATE_PROMPT_PREFIX}y",
                        style=1,
                        group_id="update_prompt",
                    ),
                    # Cancel button (grey).
                    _make_callback_button(
                        btn_id="no",
                        label="Cancel",
                        visited_label="Cancelled",
                        data=f"{UPDATE_PROMPT_PREFIX}n",
                        style=0,
                        group_id="update_prompt",
                    ),
                ]),
            ]
        )
    )


# ApprovalRequest + text builder

@dataclass
class ApprovalRequest:
    """Structured payload describing a pending approval request.

    One instance drives both halves of the outbound message: the markdown
    body rendered by :func:`build_approval_text` and the button row built by
    :func:`build_approval_keyboard`. Keeping them derived from a single struct
    means the text and the buttons can never drift out of sync.

    Two distinct shapes are supported, distinguished by which fields are set:

    * **Exec approval** -- ``command_preview`` and/or ``cwd`` are populated.
      Renders as a terminal-style block with the command in a code fence.
    * **Plugin approval** -- ``tool_name`` is populated. Renders as a
      tool-centric block with a severity-coloured icon.

    Attributes:
        session_key: Routes the decision back to the caller waiting in the
            approval resolver. Must match the key registered at request time.
        title: Short headline shown at the top of the message.
        description: Longer context explaining why the approval is needed.
        command_preview: Command text about to run (exec approvals only).
        cwd: Working directory for the command (exec approvals only).
        tool_name: Name of the requesting tool (plugin approvals only).
        severity: Selects the severity icon colour. ``"critical"`` is red,
            ``"info"`` is blue, anything else is yellow.
        timeout_sec: Seconds until the request expires and is auto-denied.
            Shown to the user so they know how long they have to act.
        allow_permanent: Whether the ``allow-always`` button is offered.
    """

    session_key: str
    title: str
    description: str = ""
    command_preview: str = ""
    cwd: str = ""
    tool_name: str = ""
    severity: str = ""
    timeout_sec: int = 120
    allow_permanent: bool = True


def build_approval_text(req: ApprovalRequest) -> str:
    """Render an :class:`ApprovalRequest` into markdown message body text.

    Dispatches on which of the two approval shapes the request represents.
    The discriminator is the presence of ``command_preview`` or ``cwd``,
    which unambiguously separates an exec approval from a plugin approval.

    Args:
        req: The structured approval request to render.

    Returns:
        A markdown string ready to include in the outbound message body.
    """
    # Exec approval: has command preview or working directory.
    if req.command_preview or req.cwd:
        return _build_exec_text(req)
    # Plugin approval: tool name or severity-based rendering.
    return _build_plugin_text(req)


def _build_exec_text(req: ApprovalRequest) -> str:
    """Render an exec-style approval request with a command preview.

    The command is wrapped in a fenced code block so QQ renders it as a
    literal block rather than interpreting embedded markdown. Because a
    request body is also the payload a user quotes back, the preview is
    truncated well below the message limit to avoid pushing the whole
    approval -- including the timeout line -- out of view.

    Only fields that were actually populated appear in the output, so a
    request with no working directory does not print an empty "cwd" line.

    Args:
        req: The approval request to render.

    Returns:
        Markdown body for the exec-approval message.
    """
    lines: List[str] = ["馃攼 **鍛戒护鎵ц瀹℃壒**", ""]
    if req.command_preview:
        # Truncate command preview to prevent excessively long messages.
        preview = req.command_preview[:300]
        lines.append(f"```\n{preview}\n```")
    if req.cwd:
        lines.append(f"馃搧 鐩綍: {req.cwd}")
    if req.title and req.title != req.command_preview:
        lines.append(f"馃搵 {req.title}")
    if req.description:
        lines.append(f"馃摑 {req.description}")
    lines.append("")
    lines.append(f"⏱️ Timeout: {req.timeout_sec}s")
    return "\n".join(lines)


def _build_plugin_text(req: ApprovalRequest) -> str:
    """Render a plugin-style approval request with a severity indicator.

    The leading icon encodes severity so a user scanning many queued
    approvals can triage by colour before reading the body: red for
    critical, blue for informational, yellow for everything in between.

    Args:
        req: The approval request to render.

    Returns:
        Markdown body for the plugin-approval message.

    Icon mapping:
        ``severity="critical"`` -> red icon.
        ``severity="info"`` -> blue icon.
        anything else -> yellow icon.
    """
    # Select icon color based on severity level.
    icon = (
        "馃敶" if req.severity == "critical"
        else "馃數" if req.severity == "info"
        else "馃煛"
    )
    lines: List[str] = [f"{icon} **瀹¨壒璇锋眰**", ""]
    lines.append(f"馃搵 {req.title}")
    if req.description:
        lines.append(f"馃摑 {req.description}")
    if req.tool_name:
        lines.append(f"馃敡 宸ュ叿: {req.tool_name}")
    lines.append("")
    lines.append(f"⏱️ Timeout: {req.timeout_sec}s")
    return "\n".join(lines)


# ApprovalSender

PostMessageFn = Callable[..., Awaitable[Dict[str, Any]]]
"""Signature of an async POST to ``/v2/{users|groups}/{id}/messages``.

Implementations accept a body dict and return the raw API response dict. The
caller posts an approval message through one of these two callables, selected
by chat type. Injecting the callables keeps :class:`ApprovalSender` decoupled
from the adapter, so the sending logic can be unit-tested with fakes.
"""


class ApprovalSender:
    """Send an approval request message with an inline keyboard attached.

    Combines :func:`build_approval_text` and
    :func:`build_approval_keyboard` and routes the result to the correct
    endpoint for the chat type. All outbound failures are swallowed and
    reported as ``False`` rather than raised, because an approval prompt that
    cannot be delivered should never take down the caller's send pipeline.

    The adapter supplies its C2C and group posting helpers as the two
    callables.

    Usage::

        sender = ApprovalSender(post_c2c, post_group, log_tag="QQBot")
        success = await sender.send("c2c", openid, approval_req, msg_id)

    Attributes:
        _post_c2c: Coroutine posting a message to a C2C chat.
        _post_group: Coroutine posting a message to a group chat.
        _log_tag: Prefix attached to every log line this instance emits.
    """

    def __init__(
        self,
        post_c2c: PostMessageFn,
        post_group: PostMessageFn,
        log_tag: str = "QQBot",
    ) -> None:
        """Wire up the two chat-specific posting callables.

        Args:
            post_c2c: Async callable posting to a C2C chat. Signature:
                ``(chat_id, text, reply_to_msg_id, keyboard) -> response``.
            post_group: Async callable posting to a group chat, with the same
                signature as ``post_c2c``.
            log_tag: Prefix for every log line emitted by this instance.
        """
        self._post_c2c = post_c2c
        self._post_group = post_group
        self._log_tag = log_tag

    async def send(
        self,
        chat_type: str,
        chat_id: str,
        req: ApprovalRequest,
        msg_id: Optional[str] = None,
    ) -> bool:
        """Send an approval message to *chat_id* and report delivery.

        Builds the text and keyboard from *req*, then dispatches to the C2C
        or group posting callable selected by *chat_type*. Guild channels
        cannot render inline keyboards, so an unsupported chat type returns
        ``False`` without attempting a post.

        Args:
            chat_type: ``"c2c"`` for private chat or ``"group"`` for group
                chat.
            chat_id: User openid for c2c, group_openid for group.
            req: The structured approval request to render and send.
            msg_id: Message id to reply to. QQ requires a message reference
                on the first outbound message to a user the bot has not
                previously addressed, so callers pass the last inbound id.

        Returns:
            ``True`` when the message was posted, ``False`` on any failure or
            for a chat type that does not support inline keyboards.
        """
        # Build the approval text and keyboard from the request.
        text = build_approval_text(req)
        keyboard = build_approval_keyboard(req.session_key)

        logger.info(
            "[%s] Sending approval request to %s:%s (session=%.20s鈥?",
            self._log_tag, chat_type, chat_id, req.session_key,
        )

        try:
            # Route to the correct posting function based on chat type.
            if chat_type == "c2c":
                await self._post_c2c(chat_id, text, msg_id, keyboard)
            elif chat_type == "group":
                await self._post_group(chat_id, text, msg_id, keyboard)
            else:
                # Guild channels don't support inline keyboards.
                logger.warning(
                    "[%s] Approval: unsupported chat_type %r",
                    self._log_tag, chat_type,
                )
                return False
            logger.info(
                "[%s] Approval message sent to %s:%s",
                self._log_tag, chat_type, chat_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "[%s] Failed to send approval message to %s:%s: %s",
                self._log_tag, chat_type, chat_id, exc,
            )
            return False


# INTERACTION_CREATE event shape

@dataclass
class InteractionEvent:
    """Parsed representation of an ``INTERACTION_CREATE`` dispatch payload.

    The adapter parses the raw gateway dispatch into this dataclass before
    invoking the registered interaction callback. Fields default to empty
    strings or zero so a partially populated payload never raises during
    decoding -- QQ does not populate every field for every scene.

    Reference:
    https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html

    Attributes:
        id: Interaction event id. Required to send the
            ``PUT /interactions/{id}`` ACK.
        type: Event type code; ``11`` denotes a message button interaction.
        chat_type: Numeric scene code. ``0`` = guild, ``1`` = group,
            ``2`` = c2c.
        scene: Human-readable form of :attr:`chat_type`.
        group_openid: Group identifier, populated for group interactions.
        group_member_openid: The member who tapped the button, populated for
            group interactions.
        user_openid: User identifier, populated for c2c interactions.
        channel_id: Channel identifier, populated for guild interactions.
        guild_id: Guild identifier, populated for guild interactions.
        button_data: The ``button_data`` payload embedded in the tapped
            button's action.
        button_id: The ``id`` of the button that was tapped.
        resolver_user_id: Fallback user id from ``resolved.user_id``.
    """

    id: str = ""
    """Interaction event id -- required for the ``PUT /interactions/{id}`` ACK."""

    type: int = 0
    """Event type code (``11`` = message button)."""

    chat_type: int = 0
    """``0`` = guild, ``1`` = group, ``2`` = c2c."""

    scene: str = ""
    """``'guild'`` | ``'group'`` | ``'c2c'`` -- human-readable scene."""

    group_openid: str = ""
    group_member_openid: str = ""
    user_openid: str = ""
    channel_id: str = ""
    guild_id: str = ""

    button_data: str = ""
    button_id: str = ""
    resolver_user_id: str = ""

    @property
    def operator_openid(self) -> str:
        """Best available openid of the user who tapped the button.

        QQ populates a different identity field depending on the scene, so
        callers need a single resolution order rather than branching on
        ``scene`` themselves.

        Priority:

        1. ``group_member_openid`` -- group button taps.
        2. ``user_openid`` -- c2c button taps.
        3. ``resolver_user_id`` -- fallback from ``resolved.user_id``.

        Returns:
            The tapping user's openid, or an empty string when none of the
            fields were populated.
        """
        return (
            self.group_member_openid
            or self.user_openid
            or self.resolver_user_id
        )


def parse_interaction_event(raw: Dict[str, Any]) -> InteractionEvent:
    """Flatten a raw ``INTERACTION_CREATE`` dispatch into an :class:`InteractionEvent`.

    QQ nests the button payload two levels deep, under ``data.resolved``, and
    every numeric field may be absent or loosely typed. This function applies
    a tolerant ``str()`` / ``int()`` coercion to each field so a malformed or
    partial payload degrades to empty values rather than raising.

    The raw payload shape::

        {
            "id": "...",
            "chat_type": 2,           # 0=guild, 1=group, 2=c2c
            "group_openid": "...",
            "group_member_openid": "...",
            "user_openid": "...",
            "channel_id": "...",
            "guild_id": "...",
            "data": {
                "type": 11,
                "resolved": {
                    "button_data": "approve:session:allow-once",
                    "button_id": "allow",
                    "user_id": "..."
                }
            }
        }

    Args:
        raw: The ``d`` field of an ``INTERACTION_CREATE`` dispatch payload.

    Returns:
        A fully coerced :class:`InteractionEvent`.
    """
    data_raw = raw.get("data") or {}
    resolved = data_raw.get("resolved") or {}
    scene_code = int(raw.get("chat_type", 0) or 0)
    # Map numeric scene code to human-readable string.
    scene = {0: "guild", 1: "group", 2: "c2c"}.get(scene_code, "")
    return InteractionEvent(
        id=str(raw.get("id", "")),
        type=int(data_raw.get("type", 0) or 0),
        chat_type=scene_code,
        scene=scene,
        group_openid=str(raw.get("group_openid", "")),
        group_member_openid=str(raw.get("group_member_openid", "")),
        user_openid=str(raw.get("user_openid", "")),
        channel_id=str(raw.get("channel_id", "")),
        guild_id=str(raw.get("guild_id", "")),
        button_data=str(resolved.get("button_data", "")),
        button_id=str(resolved.get("button_id", "")),
        resolver_user_id=str(resolved.get("user_id", "")),
    )
