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

"""Encre Agent cron scheduler adapter.

This package is the Python-facing shim for the cron scheduler that lives
inside Encre Agent Core (the Rust engine). Callers that want to schedule
one-shot or recurring jobs on the harness go through
:meth:`resolve_cron_scheduler` and receive an ``EncreScheduler`` instance,
which is where all the actual job model, persistence, and dispatch logic
resides.

Keeping the construction indirection here — rather than importing
``EncreScheduler`` directly at every call site — gives the harness one
place to swap the implementation (for example, to a no-op stub in tests)
without touching downstream callers.

Public API::

    from encre.cron import resolve_cron_scheduler

    scheduler = resolve_cron_scheduler()
    # scheduler.create_job(...) / scheduler.list_jobs() / ...
"""

from __future__ import annotations

from .scheduler_provider import resolve_cron_scheduler

__all__ = [
    "resolve_cron_scheduler",
]
