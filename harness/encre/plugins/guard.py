#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Plugin isolation helpers: call third-party code without letting it kill the app.

Every surface where plugin-provided code touches the host goes through one of
the three wrappers in this module.  A hostile or buggy plugin may:

* raise anything at all -- including ``BaseException`` subclasses such as
  ``SystemExit`` (a plugin calling ``sys.exit()`` must not terminate the host);
* return malformed data;
* run forever.

The wrappers absorb all of the first two cases and report them as a warning,
returning an explicit default so the caller keeps going.  Cooperative
cancellation (``asyncio.CancelledError``) and a real keyboard interrupt are
*never* swallowed -- they are re-raised so cancellation still propagates.

True hard isolation for untrusted code is the process transport (see
:mod:`encre.plugins.transport`), which these helpers do not replace.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from encre.logging_config import get_logger

logger = get_logger("encre.plugins.guard")

#: Exceptions that must always propagate.  ``CancelledError`` is a
#: ``BaseException`` since 3.8, so a plain ``except BaseException`` would
#: silently swallow cancellation -- which would hang every shutdown path.
FATAL: tuple[type[BaseException], ...] = (asyncio.CancelledError, KeyboardInterrupt)

_MISSING = object()

#: Distinct marker for "the call failed and the default was substituted", so a
#: caller can tell success from absorption even when a call legitimately
#: returns a falsy value such as ``None``.
SENTINEL = object()


def _default(default: Any) -> Any:
    return None if default is _MISSING else default


def is_fatal(exc: BaseException) -> bool:
    """True when *exc* must propagate instead of being absorbed."""
    return isinstance(exc, FATAL)


def absorb(
    fn: Callable[..., Any],
    *args: Any,
    default: Any = _MISSING,
    context: str = "plugin",
    log: Literal["debug", "warning"] = "warning",
    **kwargs: Any,
) -> Any:
    """Call ``fn(*args, **kwargs)``; return *default* when it raises.

    Absorbs ``BaseException`` except the fatal subset, so ``SystemExit`` and
    ``OSError``/``ImportError`` from a plugin all degrade to *default*.
    """
    try:
        return fn(*args, **kwargs)
    except FATAL:
        raise
    except BaseException as exc:
        (logger.warning if log == "warning" else logger.debug)(
            "%s failed: %s", context, exc, exc_info=True
        )
        return _default(default)


async def absorb_async(
    coro: Awaitable[Any],
    *,
    timeout: float | None = None,
    default: Any = _MISSING,
    context: str = "plugin",
) -> Any:
    """Await *coro*; return *default* when it raises or exceeds *timeout*.

    ``asyncio.TimeoutError`` is reported at ``warning`` level with the budget
    consumed, so a plugin that stops cooperating is visible but cannot stall
    the caller.
    """
    try:
        return await asyncio.wait_for(coro, timeout) if timeout else await coro
    except TimeoutError:
        logger.warning("%s timed out after %ss", context, timeout)
        return _default(default)
    except FATAL:
        raise
    except BaseException as exc:
        logger.warning("%s failed: %s", context, exc, exc_info=True)
        return _default(default)
