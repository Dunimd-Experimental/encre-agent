#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Lazy re-export of the ``ea-tool-cron`` plugin package."""

from ea_tool_cron.tool import cron_tool
from ea_tool_cron.plugin import create_plugin

__all__ = [cron_tool, 'create_plugin']
