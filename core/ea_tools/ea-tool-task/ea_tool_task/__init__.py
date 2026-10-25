#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Lazy re-export of the ``ea-tool-task`` plugin package.

This module re-exports :data:`task_tool` (the callable tool object)
and :func:`create_plugin` (the plugin factory) so callers can write
either::

    from ea_tool_task import task_tool
    from ea_tool_task import create_plugin

or the more explicit::

    from ea_tool_task.tool import task_tool
    from ea_tool_task.plugin import create_plugin

without paying for the ``encre.plugins.types`` import until the plugin
loader actually needs it.
"""

from ea_tool_task.tool import task_tool
from ea_tool_task.plugin import create_plugin

__all__ = [task_tool, 'create_plugin']
