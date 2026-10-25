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
Package-level entry point for the ``ea-skill-byted-ark-trainer`` plugin.

This module serves as the public interface for the Encre Agent plugin loader.
The plugin loader discovers this package by importing the ``create_plugin``
factory function and calling it to instantiate the ``_SkillPlugin`` class
defined in :mod:`plugin`.

The ``__all__`` list explicitly exports ``create_plugin`` so that consumers
of this package only see the factory function, keeping internal classes
private by convention.
"""

from ea_skill_byted_ark_trainer.plugin import create_plugin

__all__ = ["create_plugin"]
