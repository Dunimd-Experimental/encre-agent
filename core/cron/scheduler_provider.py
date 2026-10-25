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

"""Lazy constructor for the cron scheduler.

The actual scheduler class (``encre.scheduler.EncreScheduler``) lives in
the harness distribution, not here. Importing it eagerly at module load
time would force the entire harness into memory just to touch this file,
which is expensive and creates a hard dependency for anything that
happens to import ``encre.cron`` for a lightweight reason.

Instead, :func:`resolve_cron_scheduler` performs the import inside the
function body, so the harness is only pulled in when a caller actually
asks for a scheduler. This mirrors the general pattern used throughout
the codebase of deferring heavy imports to the point of use.

The returned instance is a fresh scheduler each call. Callers that need
a long-lived scheduler (the common case) should call this once at startup
and hold onto the reference.
"""

from __future__ import annotations

from typing import Any


def resolve_cron_scheduler() -> Any:
    """Build and return a new cron scheduler instance.

    The concrete class returned is :class:`encre.scheduler.EncreScheduler`.
    Its public surface covers the full job lifecycle used by the harness
    and the desktop UI:

    * :meth:`EncreScheduler.schedule` and :meth:`EncreScheduler.schedule_workflow`
      create new jobs (one-shot, cron, or workflow-based).
    * :meth:`EncreScheduler.list_jobs`, :meth:`EncreScheduler.get_job`,
      :meth:`EncreScheduler.get_execution_history` for read access.
    * :meth:`EncreScheduler.cancel`, :meth:`EncreScheduler.cancel_all`,
      :meth:`EncreScheduler.toggle_job`, :meth:`EncreScheduler.update_job`,
      :meth:`EncreScheduler.delete_job` for management.
    * :meth:`EncreScheduler.on_job_complete` and
      :meth:`EncreScheduler.on_job_progress` register lifecycle callbacks
      that the harness uses to notify the UI about background work.

    The import is deferred to call time so that loading this module does
    not force the entire harness distribution into memory. See the module
    docstring for details.

    Returns:
        A new :class:`EncreScheduler` instance. Construction is cheap,
        but the scheduler opens file handles for its job store, so
        callers should construct it once and hold onto the reference
        rather than re-instantiating per request.

    Raises:
        ImportError: If the harness distribution is not installed. In
            production builds this should never happen because the
            harness is a required dependency; it can occur in stripped
            test environments that install only the core package.
    """
    from encre.scheduler import EncreScheduler

    return EncreScheduler()
