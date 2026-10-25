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

"""Email tool (SMTP / IMAP).

Composes and sends email via SMTP and reads the inbox via IMAP, with
optional attachment handling.

Supported actions:

* ``send``: Compose and send an email via SMTP (with optional TLS).
* ``read``: Fetch recent emails from a folder via IMAP.
* ``search``: Search emails by IMAP criteria.
* ``list_folders``: List available IMAP folders.

All network operations (SMTP, IMAP) are executed in a thread executor
to avoid blocking the asyncio event loop during the synchronous I/O
calls made by the ``smtplib`` and ``imaplib`` standard library modules.
"""

import asyncio
import email
import imaplib
import json
import smtplib
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parsedate_to_datetime
from typing import Any

from encre.tools.base import build_tool


def _decode_mime(s: str | None) -> str:
    """Decode a MIME-encoded email header string into readable text.

    Email headers (Subject, From, etc.) may be encoded in RFC 2047
    format with non-ASCII characters. This function decodes them back
    to plain text, falling back to UTF-8 when the charset is not
    specified.

    Args:
        s: The MIME-encoded header string, or ``None``.

    Returns:
        The decoded string, or an empty string if the input is
        ``None`` or empty.
    """
    if not s:
        return ""
    parts = decode_header(s)
    return "".join(
        part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, charset in parts
    )


async def _email_execute(**kwargs: Any) -> str:
    """Execute an email operation and return the result.

    This is the internal executor function bound to the ``email``
    tool's ``execute`` field. It dispatches to the appropriate
    handler based on the ``action`` parameter.

    All network operations (SMTP send, IMAP read/search/list) are
    wrapped in a synchronous function and dispatched to a thread
    executor via ``asyncio.get_event_loop().run_in_executor()``. This
    is necessary because the ``smtplib`` and ``imaplib`` modules are
    synchronous and would block the asyncio event loop if called
    directly.

    Args:
        **kwargs: Arbitrary keyword arguments passed through by the
            tool dispatcher. Recognised keys:

            * ``action`` (str): One of ``"send"``, ``"read"``,
              ``"search"``, or ``"list_folders"``.
            * ``host`` (str): SMTP or IMAP server hostname.
            * ``port`` (int): Server port (default 587 for SMTP).
            * ``username`` (str): Email account username.
            * ``password`` (str): Email account password or app
              password.
            * ``use_tls`` (bool): Use STARTTLS for SMTP (default
              true).
            * ``to`` (str): Recipient address for ``send``.
            * ``cc`` (str): CC recipients, comma-separated.
            * ``bcc`` (str): BCC recipients, comma-separated.
            * ``subject`` (str): Email subject.
            * ``body`` (str): Email body text.
            * ``folder`` (str): IMAP folder name (default ``"INBOX"``).
            * ``max_emails`` (int): Maximum emails to fetch in
              ``read`` (default 10).
            * ``mark_seen`` (bool): Mark fetched emails as read
              (default false).
            * ``criteria`` (str): IMAP search criteria for ``search``.
            * ``max_results`` (int): Maximum search results (default
              10).

    Returns:
        A string containing the operation result, or an error message.
    """
    action = kwargs.get("action", "")

    if action == "send":
        host = kwargs.get("host", "")
        port = kwargs.get("port", 587)
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        use_tls = kwargs.get("use_tls", True)
        to = kwargs.get("to", "")
        subject = kwargs.get("subject", "")
        body = kwargs.get("body", "")
        cc = kwargs.get("cc", "")
        bcc = kwargs.get("bcc", "")

        if not all([host, username, password, to, subject]):
            return "Missing required fields: host, username, password, to, subject"

        # Build the MIME multipart message with a plain-text body.
        msg = MIMEMultipart("alternative")
        msg["From"] = formataddr((username, username))
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Compile the full recipient list including CC and BCC.
        # BCC recipients are included in the recipient list but not
        # in the message headers, so they receive the email without
        # other recipients seeing the BCC header.
        recipients = [to]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(",") if addr.strip()])
        if bcc:
            recipients.extend([addr.strip() for addr in bcc.split(",") if addr.strip()])

        loop = asyncio.get_event_loop()

        def _send() -> str:
            """Send the email via SMTP in a thread executor.

            This inner function is called synchronously by the thread
            executor. It opens an SMTP connection, optionally upgrades
            to TLS via STARTTLS, authenticates, and sends the message.

            Returns:
                A success message or an error description.
            """
            try:
                with smtplib.SMTP(host, port, timeout=30) as server:
                    if use_tls:
                        server.starttls()
                    server.login(username, password)
                    server.sendmail(username, recipients, msg.as_string())
                return "Email sent successfully"
            except Exception as e:
                return f"Failed to send email: {e}"

        return await loop.run_in_executor(None, _send)

    elif action == "read":
        host = kwargs.get("host", "")
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        folder = kwargs.get("folder", "INBOX")
        max_emails = kwargs.get("max_emails", 10)
        mark_seen = kwargs.get("mark_seen", False)

        if not all([host, username, password]):
            return "Missing required fields: host, username, password"

        loop = asyncio.get_event_loop()

        def _read() -> str:
            """Read emails from an IMAP folder in a thread executor.

            Connects to the IMAP server via SSL, logs in, selects the
            target folder, fetches the most recent messages, and
            extracts subject, sender, date, and body preview from
            each.

            The folder is opened in read-only mode unless ``mark_seen``
            is true, in which case fetched messages are marked as read.

            Returns:
                A JSON string containing the email results, or an
                error message.
            """
            try:
                conn = imaplib.IMAP4_SSL(host, timeout=30)
                conn.login(username, password)
                conn.select(folder, readonly=not mark_seen)
                _, data = conn.search(None, "ALL")
                if not data or not data[0]:
                    conn.logout()
                    return "No emails found"
                # Take only the most recent N messages from the
                # message ID list.
                msg_ids = data[0].split()[-max_emails:]
                results = []
                for mid in reversed(msg_ids):
                    _, msg_data = conn.fetch(mid, "(RFC822)")
                    if not msg_data or msg_data[0] is None:
                        continue
                    raw_email = msg_data[0][1]
                    em = email.message_from_bytes(raw_email)
                    subject = _decode_mime(em["Subject"])
                    sender = _decode_mime(em["From"])
                    date = str(parsedate_to_datetime(em["Date"]) if em["Date"] else "")
                    body_text = ""
                    if em.is_multipart():
                        for part in em.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body_text = payload.decode("utf-8", errors="replace")[:2000]
                                    break
                    else:
                        payload = em.get_payload(decode=True)
                        if payload:
                            body_text = payload.decode("utf-8", errors="replace")[:2000]
                    results.append({
                        "subject": subject,
                        "from": sender,
                        "date": date,
                        "body_preview": body_text[:500],
                    })
                conn.logout()
                return json.dumps(results, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"Failed to read emails: {e}"

        return await loop.run_in_executor(None, _read)

    elif action == "search":
        host = kwargs.get("host", "")
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        folder = kwargs.get("folder", "INBOX")
        criteria = kwargs.get("criteria", "ALL")
        max_results = kwargs.get("max_results", 10)

        if not all([host, username, password]):
            return "Missing required fields: host, username, password"

        loop = asyncio.get_event_loop()

        def _search() -> str:
            """Search emails by IMAP criteria in a thread executor.

            Connects to the IMAP server via SSL, selects the folder in
            read-only mode, and searches using the provided IMAP
            criteria string (e.g. ``"FROM user@example.com"``).
            Returns the most recent results with subject, sender, and
            date.

            Returns:
                A JSON string containing the search results, or an
                error message.
            """
            try:
                conn = imaplib.IMAP4_SSL(host, timeout=30)
                conn.login(username, password)
                conn.select(folder, readonly=True)
                _, data = conn.search(None, criteria)
                if not data or not data[0]:
                    conn.logout()
                    return "No emails matching criteria"
                msg_ids = data[0].split()[-max_results:]
                results = []
                for mid in reversed(msg_ids):
                    _, msg_data = conn.fetch(mid, "(RFC822)")
                    if not msg_data or msg_data[0] is None:
                        continue
                    raw_email = msg_data[0][1]
                    em = email.message_from_bytes(raw_email)
                    results.append({
                        "subject": _decode_mime(em["Subject"]),
                        "from": _decode_mime(em["From"]),
                        "date": str(parsedate_to_datetime(em["Date"]) if em["Date"] else ""),
                    })
                conn.logout()
                return json.dumps(results, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"Failed to search emails: {e}"

        return await loop.run_in_executor(None, _search)

    elif action == "list_folders":
        host = kwargs.get("host", "")
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")

        if not all([host, username, password]):
            return "Missing required fields: host, username, password"

        loop = asyncio.get_event_loop()

        def _list_folders() -> str:
            """List IMAP folders in a thread executor.

            Connects to the IMAP server via SSL, logs in, and
            retrieves the list of available folders. The IMAP LIST
            command returns a quoted string for each folder path; this
            function parses out the folder name portion.

            Returns:
                A JSON string containing the folder names, or an
                error message.
            """
            try:
                conn = imaplib.IMAP4_SSL(host, timeout=30)
                conn.login(username, password)
                _, folders = conn.list()
                conn.logout()
                names = []
                for folder_data in (folders or []):
                    # IMAP LIST returns entries like:
                    #  * LIST (\HasNoChildren) "/" "INBOX"
                    # We extract the quoted folder name after the
                    # delimiter.
                    parts = folder_data.decode().split(' "/" ')
                    if len(parts) == 2:
                        names.append(parts[1].strip('"'))
                return json.dumps(names, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"Failed to list folders: {e}"

        return await loop.run_in_executor(None, _list_folders)

    return f"Unknown action: {action}. Supported: send, read, search, list_folders"


email_tool = build_tool(
    name="email",
    description=(
        "Send and read emails via SMTP/IMAP -- compose and send messages, read the "
        "inbox, search by criteria, or list available folders. "
        "WHEN to use: sending notifications or reports via email, reading inbox "
        "messages, searching for specific emails by sender/subject/date. "
        "WHEN NOT to use: for chat/messaging platforms (use the platform gateway "
        "tools), for file transfer (use ssh upload/download or cloud_storage), or "
        "for real-time communication (email is asynchronous). "
        "TIPS: use app-specific passwords (not the account password) for Gmail/"
        "Outlook; set use_tls=true for port 587 and use_ssl for port 993; keep "
        "max_emails low (10-20) to avoid large responses. "
        "PITFALLS: some providers block SMTP from unknown IPs; IMAP search criteria "
        "syntax varies by server; large attachments may exceed size limits."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "read", "search", "list_folders"],
                "description": "Action to perform: 'send' (compose and send via SMTP), 'read' (fetch recent emails from a folder via IMAP), 'search' (search emails by IMAP criteria), 'list_folders' (list available IMAP folders). Required.",
            },
            "host": {"type": "string", "description": "SMTP or IMAP server hostname (e.g. 'smtp.gmail.com', 'imap.gmail.com'). Required for send/read/search/list_folders unless configured globally."},
            "port": {"type": "integer", "description": "Server port. Common: 587 (SMTP STARTTLS), 465 (SMTP SSL), 993 (IMAP SSL), 143 (IMAP plaintext). Optional; defaults are inferred from the action."},
            "username": {"type": "string", "description": "Email account username (typically the full email address). Required for authentication."},
            "password": {"type": "string", "description": "Email account password or app-specific password. For Gmail/Outlook with 2FA enabled, use an app password, not the account password."},
            "use_tls": {"type": "boolean", "description": "Use STARTTLS for SMTP. Default true. Set false only for plaintext SMTP (not recommended)."},
            "to": {"type": "string", "description": "Recipient email address for 'send'. Multiple recipients: comma-separated. Required for send."},
            "cc": {"type": "string", "description": "CC recipients, comma-separated. Optional for send."},
            "bcc": {"type": "string", "description": "BCC recipients, comma-separated. Optional for send."},
            "subject": {"type": "string", "description": "Email subject line. Required for send."},
            "body": {"type": "string", "description": "Email body text (plain text). Required for send."},
            "folder": {"type": "string", "description": "IMAP folder name for 'read' or 'search'. Default 'INBOX'. Use 'list_folders' to discover available folder names."},
            "max_emails": {"type": "integer", "description": "Maximum number of emails to fetch in 'read'. Default 10. Lower this for faster responses."},
            "mark_seen": {"type": "boolean", "description": "Mark fetched emails as seen (read) in the IMAP store. Default false -- set true to mark messages as read after fetching."},
            "criteria": {"type": "string", "description": "IMAP search criteria for 'search', e.g. 'FROM user@example.com', 'SUBJECT \"report\"', 'SINCE 01-Jan-2026'. Consult RFC 3501 for the full syntax."},
            "max_results": {"type": "integer", "description": "Maximum number of search results to return. Default 10."},
        },
        "required": ["action"],
    },
    execute=_email_execute,
    intents=["general", "system"],
    category="communication",
    semantic_type="network",
    cost_level="low",
    retryability="auto",
    is_concurrency_safe=lambda _: True,
    is_destructive=lambda args: args.get("action", "") == "send",
)
