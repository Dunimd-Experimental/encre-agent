#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Lazy re-export of the ``ea-tool-device`` plugin package."""

from ea_tool_device.tool import device_tool
from ea_tool_device.plugin import create_plugin

__all__ = [device_tool, 'create_plugin']
