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

"""Package namespace for the Signal platform adapter.

This subpackage is the implementation surface of the ``ea-connector-signal``
connector plugin.  The adapter talks to a ``signal-cli`` HTTP daemon: it
consumes an SSE event stream for inbound envelopes, sends over the REST API,
and refreshes decaying typing indicators on a cadence.  Rate-limit constants
and the pacing scheduler live in :mod:`ea_connector_signal.signal_rate_limit`;
markdown-to-Signal body-range conversion lives in
:mod:`ea_connector_signal.signal_format`.

Importing this package re-exports the adapter submodule so consumers can
reach the implementation as :mod:`ea_connector_signal.adapter`.  The
adapter class implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` and is bridged
into :class:`encre.gateway.run.GatewayRunner` at plugin activation time by
:mod:`ea_connector_signal.plugin` through the gateway-adapter surface.
"""

from . import adapter
