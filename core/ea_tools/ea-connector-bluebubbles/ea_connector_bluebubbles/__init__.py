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

"""Package namespace for the BlueBubbles (iMessage) platform adapter.

This subpackage is the implementation surface of the
``ea-connector-bluebubbles`` connector plugin.  The adapter bridges Apple's
iMessage onto the Encre Agent gateway by speaking to a locally hosted
BlueBubbles server: outbound messages go to the BlueBubbles REST API and
inbound ``new-message`` / ``updated-message`` events arrive as POSTs on a
localhost webhook listener the adapter registers at connect time.

Importing this package re-exports the adapter submodule so consumers can
reach the implementation as :mod:`ea_connector_bluebubbles.adapter`.  The
adapter class implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` and is bridged
into :class:`encre.gateway.run.GatewayRunner` at plugin activation time by
:mod:`ea_connector_bluebubbles.plugin` through the gateway-adapter surface.
"""

from . import adapter
