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

"""Every client message the server can dispatch must actually round-trip.

``EncreWSHandler`` parses an inbound frame with ``parse_client_message`` and
then looks the parsed object's class up in ``_DISPATCH``.  Both halves are
easy to get wrong quietly:

* a dataclass without ``from_dict`` raises inside the parser, and the receive
  loop turns that into a generic ``parse_error`` the user never connects to
  the button they pressed;
* a class missing from ``_DISPATCH`` used to fall through the loop with no log
  at all.

That is exactly how the plan/spec review buttons were dead for weeks:
``ClientPlanApprove`` / ``ClientPlanReject`` / ``ClientSpecApprove`` /
``ClientSpecReject`` were the only four client messages with no ``from_dict``,
so every 执行/取消 click died in the parser and no ``[plan]`` line ever reached
the log.
"""

import inspect
import json

import pytest

from encre.server.protocol import parse_client_message
from encre.transport.ws import EncreWSHandler

_DISPATCH = EncreWSHandler._DISPATCH


def _dispatch_classes():
    """Every message class the server claims it can handle."""
    return sorted(_DISPATCH, key=lambda c: c.__name__)


def test_dispatch_table_is_populated():
    """Guard the fixture itself: an empty table would vacuously pass the rest."""
    assert len(_DISPATCH) > 100
    assert all(callable(h) for h in _DISPATCH.values())


@pytest.mark.parametrize("cls", _dispatch_classes(), ids=lambda c: c.__name__)
def test_wire_round_trips_to_the_dispatched_class(cls):
    """``{"type": ...}`` must parse into exactly the class the dispatch keys on."""
    wire = json.dumps({"type": cls().type})

    msg = parse_client_message(wire)

    assert type(msg) is cls, (
        f"{cls.__name__}: parse_client_message returned "
        f"{type(msg).__name__ if msg else None!r} -- a dataclass that cannot be "
        f"built from the wire will never reach its handler"
    )
    assert _DISPATCH[cls] is not None


def test_review_decisions_are_dispatchable():
    """The four review buttons' messages specifically."""
    for name in ("plan_approve", "plan_reject", "spec_approve", "spec_reject"):
        cls = next((c for c in _DISPATCH if c.__name__ == f"Client{name.title().replace('_', '')}"), None)
        assert cls is not None, f"{name} has no dispatch entry"
        assert hasattr(cls, "from_dict"), f"{cls.__name__} is missing from_dict"
        msg = parse_client_message(json.dumps({"type": name, "session_id": "s1", "review_id": "abc"}))
        assert type(msg) is cls


def test_handler_names_exist_on_the_server():
    """Dispatch entries must be real methods, not stale attribute names."""
    for cls, handler in _DISPATCH.items():
        assert callable(handler), f"{cls.__name__} maps to a non-callable"
        assert getattr(EncreWSHandler, handler.__name__, None) is not None or inspect.isfunction(handler)
