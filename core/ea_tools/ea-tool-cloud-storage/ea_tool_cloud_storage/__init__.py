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

"""Package: ea_tool_cloud_storage

Public entry points for the ``ea-tool-cloud-storage`` plugin.

This package re-exports the plugin factory (``create_plugin``) and the
``cloud_storage`` tool object. Importing this package does not require
boto3, google-cloud-storage, or azure-storage-blob -- those heavyweight
SDKs are imported lazily inside the executor functions only when the
respective provider is actually requested.
"""

from __future__ import annotations

from ea_tool_cloud_storage.tool import cloud_storage_tool
from ea_tool_cloud_storage.plugin import create_plugin

__all__ = [cloud_storage_tool, 'create_plugin']
