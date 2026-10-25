#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Plugin fault reporting for the model-facing side of a failed tool call.

The user-facing card (``desktop/renderer/src/chat/plugin_error_card.ts``) and
this module are two views of the same event and must never disagree:

* the card shows three calm English sentences, the plugin's own support
  channels (e-mail first, then site, repository, docs) and the app /
  plugin-system versions and the time of the failure;
* the model gets the **complete** error text, the same identities, the
  plugin's help-docs URL and the two ``manage`` calls that can actually fix
  the situation.

``annotate_tool_fault`` is the single funnel.  It is idempotent (a payload
already carrying a report is left alone), never raises, and returns the tool
result unchanged for core tools so only plugin faults grow a tail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: Marker that identifies a payload this module already produced.  Kept short
#: and machine-readable: the frontend never shows it, the model does.
FAULT_MARKER = "[plugin_fault]"

#: The one support channel the model is told about, and why it is one.  An
#: agent cannot write an e-mail, open an issue or argue with a maintainer, so
#: ``email``/``homepage``/``repository`` are noise in the payload — they stay
#: on the human-facing card and out of the model's.  The help-docs URL is the
#: exception: the agent can read it and point the user there.
MODEL_FACING_CONTACT = "docs"


def utc_timestamp(moment: Any = None) -> str:
    """ISO-8601 UTC timestamp for a ``datetime``/epoch-seconds value."""
    if isinstance(moment, datetime):
        dt = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    else:
        try:
            seconds = float(moment) if moment is not None else 0.0
        except (TypeError, ValueError):
            seconds = 0.0
        dt = datetime.fromtimestamp(seconds, tz=UTC) if seconds else datetime.now(UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plugin_registry() -> Any:
    """The live plugin registry, or ``None`` outside a plugin-enabled process."""
    try:
        from encre.agent import _get_shared_plugin_registry

        return _get_shared_plugin_registry()
    except Exception:
        return None


def app_version() -> str:
    """Version of the running Encre agent (the ``encre`` distribution)."""
    from encre.plugins.registry import _harness_version

    return _harness_version()


def describe_fault(
    tool_name: str,
    *,
    plugin: str = "",
    plugin_version: str = "",
    contact: dict[str, str] | None = None,
    app: str = "",
    plugin_system: str = "",
    occurred_at: str = "",
) -> str:
    """Build the model-facing fault report for a failed plugin tool call.

    Every field is optional: a missing plugin or version degrades to a shorter
    line rather than a wrong one.  ``manage`` is always mentioned because it
    is the only recovery path the agent has.

    Of the plugin's contact channels only the help-docs URL is reported.  An
    agent cannot write an e-mail or open an issue, so e-mail, homepage and
    repository carry nothing it can act on; those stay on the human-facing
    card.  The docs URL is different — the agent can read it and hand the user
    a troubleshooting link, so it is the one channel worth passing along.
    """
    lines: list[str] = [FAULT_MARKER]
    lines.append(f"tool: {tool_name or 'unknown'}")
    if plugin:
        lines.append(f"plugin: {plugin}" + (f" (version {plugin_version})" if plugin_version else ""))
    lines.append(f"app: encre {app or 'unknown'}")
    lines.append(f"plugin_system: encre-plugin-system {plugin_system or app or 'unknown'}")
    lines.append(f"occurred_at: {occurred_at or utc_timestamp()}")
    docs = (contact.get(MODEL_FACING_CONTACT) or "").strip() if contact else ""
    if docs:
        lines.append(f"{MODEL_FACING_CONTACT}: {docs}")
    reload_args = f'name="{plugin}"' if plugin else ""
    lines.append(
        "recovery: call the manage tool to recover -- "
        f'manage(action="plugin_reload"{", " + reload_args if reload_args else ""}) restarts this plugin, '
        'manage(action="plugin_restart") restarts the whole plugin system. '
        "Do not retry the failed call before the plugin has been reloaded."
    )
    lines.append(
        "The tool output above is the complete error; it is not truncated. "
        "Relay the plugin name and the three recovery options to the user, and do not "
        "invent a workaround inside this plugin."
    )
    return "\n".join(lines)


def annotate_tool_fault(
    result: str,
    *,
    tool_name: str,
    is_error: bool,
    registry: Any = None,
    occurred_at: str = "",
) -> str:
    """Return ``result`` with a fault report appended when the call failed.

    Only plugin-provided tools are annotated: a core tool that fails is a
    normal tool error whose text the recovery engine already shaped, and the
    desktop keeps its own card for it.  Failures that carry no plugin (unknown
    tool, permission denied, cancelled) are left untouched as well, because
    there is no plugin to blame and no ``manage`` call that would help.
    """
    text = result if isinstance(result, str) else str(result or "")
    if not is_error or FAULT_MARKER in text:
        return text
    if not tool_name:
        return text
    reg = registry if registry is not None else _plugin_registry()
    if reg is None:
        return text
    try:
        owner = reg.tool_owner(tool_name)
        if not owner:
            return text
        manifest = reg.get_manifest(owner)
        report = describe_fault(
            tool_name,
            plugin=owner,
            plugin_version=getattr(manifest, "version", ""),
            contact=reg.contact(owner),
            app=app_version(),
            plugin_system=app_version(),
            occurred_at=occurred_at or utc_timestamp(),
        )
    except Exception:
        # Fault reporting must never turn a tool failure into a crash.
        return text
    return f"{text}\n\n{report}"
