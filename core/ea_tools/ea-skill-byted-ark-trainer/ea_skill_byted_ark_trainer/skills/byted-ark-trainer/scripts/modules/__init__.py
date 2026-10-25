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

"""
Modules package for the ARK Trainer Helper CLI.

This package groups the command implementations used by
``ark_trainer_helper.py``. Each submodule corresponds to a top-level
CLI subcommand:

- :mod:`modules.job` -- training job management (status, get-model, register-heartbeat)
- :mod:`modules.model` -- foundation model listing and version queries
- :mod:`modules.endpoint` -- inference endpoint CRUD operations
- :mod:`modules.train` -- model evaluation and RFT data collection utilities

The main script imports specific command functions from each submodule
rather than importing the whole package, which keeps the dependency
graph explicit and avoids circular imports.
"""
