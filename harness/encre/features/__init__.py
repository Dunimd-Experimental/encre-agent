"""Feature adapter layer.

This package is the **only** place the kernel/turn runner may reach into the
feature packages (``improve``, ``memdir``, ``device``).  It exists so that
``encre.loop`` never imports a feature module at module scope:

* ``loop`` asks :mod:`encre.features.adapters` for a class or function by name.
* The real import happens on first attribute access and is cached.
* ``import encre.loop.wiring`` therefore no longer pulls
  ``encre.improve`` / ``encre.memdir`` / ``encre.device`` into the process.

The layering rule: anything below ``encre.loop`` may not import feature
packages at all; ``encre.features`` is the declared seam, and it is the only
subpackage allowed to do so.  Enforced by ``harness/tests/test_architecture.py``
and ``harness/tools/arch_check.py``.
"""

from __future__ import annotations

__all__ = ["adapters"]
