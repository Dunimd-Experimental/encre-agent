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
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

"""Structured logging configuration for Encre.

This module is the single entry point for configuring how Encre emits logs. It
prefers `loguru <https://github.com/Delgan/loguru>`_ when that package is
installed, but transparently falls back to the standard-library :mod:`logging`
module so that downstream consumers are never forced to depend on loguru.

The public API is intentionally tiny and stable:

* :func:`setup_logging` -- install handlers (stderr and, optionally, a rotating
  file) and, when loguru is present, optionally intercept stdlib logging.
* :func:`get_logger` -- obtain a logger that exposes a loguru-like surface
  regardless of which backend is active.
* :func:`get_log_file` / :func:`get_log_dir` / :func:`get_recent_logs` --
  helpers for locating and tailing the on-disk log file.

When loguru is unavailable the :class:`_StdlibLogger` adapter wraps a stdlib
logger so that callers can still call ``logger.success(...)``,
``logger.trace(...)`` and pass ``extra=`` without branching on the backend.

Note on file layout: the live log file used by ``encre.server.service`` at
runtime is ``<data_dir>/yimd.log`` (returned by :func:`get_log_file`), whereas
:func:`setup_logging` writes its own rotating file to
``<data_dir>/logs/encre.log`` unless an explicit path is given.
"""

import logging
import sys
from typing import Any

from loguru import logger as _loguru_logger  # type: ignore[import-untyped]

_LOGURU_AVAILABLE: bool = True


class _StdlibLogger:
    """Thin wrapper that exposes a loguru-like API backed by stdlib ``logging``.

    The wrapper lets the rest of Encre call log methods that loguru provides but
    stdlib lacks (notably ``trace`` and ``success``) and to pass structured
    ``extra`` data uniformly. Each level method simply maps to the closest
    stdlib numeric level and delegates to :meth:`_log`.

    Attributes
    ----------
    _logger:
        The underlying :class:`logging.Logger` obtained from
        ``logging.getLogger(name)``.
    """

    def __init__(self, name: str = "encre") -> None:
        """Create the wrapper around a named stdlib logger.

        Args:
            name: Logger name passed to :func:`logging.getLogger`. Defaults to
                ``"encre"`` so the root Encre logger is used when no name is
                given.

        Returns:
            None.
        """
        self._logger = logging.getLogger(name)

    def _log(self, level: int, message: str, *args: Any, **kwargs: Any) -> None:
        """Emit a record at an explicit numeric level.

        Pops any ``extra`` mapping from the keyword arguments; if present, it is
        appended to the message text as a repr so the structured data is still
        visible through the stdlib formatter.

        Args:
            level: Numeric stdlib level (e.g. ``logging.INFO``).
            message: The log message, possibly with ``%``-style positional
                placeholders consumed by ``*args``.
            *args: Positional values for message formatting.
            **kwargs: Stdlib logging keyword arguments; ``extra`` is specially
                handled and inlined into the message.

        Returns:
            None.
        """
        extra: dict[str, Any] = kwargs.pop("extra", {}) or {}
        if extra:
            message = f"{message} | {extra!r}"
        self._logger.log(level, message, *args, **kwargs)

    def trace(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log at the finest ``trace`` level (below ``DEBUG``)."""
        self._log(logging.DEBUG - 5, message, *args, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a ``DEBUG``-level message."""
        self._log(logging.DEBUG, message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an ``INFO``-level message."""
        self._log(logging.INFO, message, *args, **kwargs)

    def success(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a ``SUCCESS``-level message (between ``INFO`` and ``WARNING``)."""
        self._log(logging.INFO + 5, message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a ``WARNING``-level message."""
        self._log(logging.WARNING, message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an ``ERROR``-level message."""
        self._log(logging.ERROR, message, *args, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an ``ERROR`` message and attach the current exception traceback.

        Delegates directly to the stdlib ``Logger.exception`` so the active
        ``sys.exc_info()`` is captured automatically.
        """
        self._logger.exception(message, *args, **kwargs)

    def critical(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a ``CRITICAL``-level message."""
        self._log(logging.CRITICAL, message, *args, **kwargs)


def _get_loguru_serializer(json_format: bool = False):
    """Return a loguru serialiser callable, or ``None`` for default formatting.

    When ``json_format`` is false the caller should use loguru's default text
    sink, so ``None`` is returned. When true, an inner serialiser is built that
    emits one JSON object per log line containing a fixed subset of fields plus
    any ``extra`` and ``exception`` data. The JSON import is attempted lazily so
    the function degrades gracefully if ``json`` were somehow unavailable.

    Args:
        json_format: Whether JSON line output is requested.

    Returns:
        A ``record -> str`` callable when JSON is requested (and ``json``
        imports), otherwise ``None``.
    """
    if not json_format:
        return None

    import json as _json

    def _serialize(record: Any) -> str:
        """Format a loguru record as a single JSON line.

        Builds a flat dictionary of the most useful fields, appends ``extra``
        and ``exception`` context when present, then serialises it. Timestamps
        are rendered in UTC with millisecond precision; ``ensure_ascii=False``
        keeps non-ASCII text (for example log messages in other languages)
        readable rather than escaped.

        Args:
            record: A loguru ``Record`` mapping as passed to the sink.

        Returns:
            The JSON document for this record plus a trailing newline.
        """
        subset: dict[str, Any] = {
            "timestamp": record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if record["time"] else "",
            "level": record["level"].name,
            "logger": record["name"],
            "function": record["function"],
            "line": record["line"],
            "message": record["message"],
        }
        if record["extra"]:
            subset["extra"] = dict(record["extra"])
        if record["exception"]:
            subset["exception"] = str(record["exception"])
        return _json.dumps(subset, ensure_ascii=False, default=str) + "\n"

    return _serialize


def get_log_dir():
    """Return the Encre data directory that hosts the active log file.

    The actual log file (``yimd.log``) lives at the root of the data directory
    (``~/.dunimd/encre/yimd.log``), not inside a ``logs/`` subdirectory -- that
    is the path written by ``encre.server.service`` at runtime.

    Returns:
        A :class:`pathlib.Path` to the Encre data directory.
    """
    from encre.config import get_data_dir
    return get_data_dir()


def get_log_file():
    """Return the full path to the active log file.

    This is the runtime path ``<data_dir>/yimd.log`` written by the server
    service, which differs from the rotating file created by
    :func:`setup_logging`.

    Returns:
        A :class:`pathlib.Path` to ``yimd.log`` inside the data directory.
    """
    from encre.config import get_data_dir
    return get_data_dir() / "yimd.log"


def get_recent_logs(n: int = 200) -> list[str]:
    """Return the last *n* lines from the active log file as a list of strings.

    Reads the file tail efficiently by seeking backwards in fixed chunks rather
    than loading the whole file, because the live log can exceed tens of
    megabytes on long-running installations. Lines are decoded as UTF-8 with
    invalid bytes replaced so a corrupt tail never raises.

    Returns an empty list when no log file exists (e.g. file logging is
    disabled) or when reading it fails for any reason.

    Args:
        n: Maximum number of trailing lines to return. Defaults to 200.

    Returns:
        A list of the final ``n`` log lines, in file order (oldest first).
    """
    log_file = get_log_file()
    if not log_file.exists():
        return []
    try:
        # Read the tail efficiently for large files (the live log can exceed
        # 50 MB on long-running installations).
        with open(log_file, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = 256 * 1024
            data = b""
            lines: list[bytes] = []
            # Walk backwards in chunks, stopping once we have more than enough
            # lines or we reach the start of the file.
            while size > 0 and len(lines) <= n + 1:
                read_size = min(chunk, size)
                size -= read_size
                fh.seek(size)
                data = fh.read(read_size) + data
                lines = data.splitlines()
        # Decode only the final n lines; replace undecodable bytes defensively.
        text_lines = [ln.decode("utf-8", errors="replace") for ln in lines[-n:]]
        return text_lines
    except Exception:
        return []


def setup_logging(
    level: str = "INFO",
    json_format: bool = False,
    log_file: str = "",
    rotation: str = "10 MB",
    retention: str = "7 days",
    intercept_stdlib: bool = True,
) -> None:
    """Configure Encre logging.

    When **loguru** is installed the function removes the default loguru
    handler, installs a new ``sys.stderr`` sink with the requested format, and
    optionally routes stdlib ``logging`` records through loguru (``intercept_stdlib``).

    When loguru is **not** installed a basic ``logging.StreamHandler`` is
    configured on the ``"encre"`` logger.  Downstream code that uses
    :func:`get_logger` will receive a ``_StdlibLogger`` wrapper so the API
    remains consistent.

    In both backends an optional file sink is added (a rotating loguru file or a
    stdlib :class:`~logging.FileHandler`) when ``log_file`` is provided or when
    the default data-directory path is used.

    Args:
        level: One of ``TRACE``, ``DEBUG``, ``INFO``, ``SUCCESS``,
            ``WARNING``, ``ERROR``, ``CRITICAL``.  Case-insensitive.
        json_format: Emit JSON lines instead of human-readable text.
        log_file: Optional path for a persistent log file.  If empty, defaults
            to ``<data_dir>/logs/encre.log`` (``~/.dunimd/encre/logs/encre.log``).
        rotation: When to rotate the log file (loguru syntax).
        retention: How long to keep rotated logs.
        intercept_stdlib: Redirect stdlib ``logging`` to loguru (only
            effective when loguru is available).

    Returns:
        None.
    """
    level = level.upper()

    if not log_file:
        from encre.config import get_data_dir
        _log_dir = get_data_dir() / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(_log_dir / "encre.log")

    if _LOGURU_AVAILABLE:
        assert _loguru_logger is not None
        _loguru_logger.remove()  # Remove default stderr handler

        # Determine format string
        if json_format:
            fmt_kwargs = {"serialize": True}
        else:
            fmt = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            )
            fmt_kwargs = {"format": fmt}

        _loguru_logger.add(
            sys.stderr,
            level=level,
            colorize=True,
            **fmt_kwargs,
        )

        if log_file:
            _loguru_logger.add(
                log_file,
                level=level,
                rotation=rotation,
                retention=retention,
                compression="gz",
                **fmt_kwargs,
            )

        if intercept_stdlib:
            # Re-route stdlib logging through loguru so third-party libraries
            # that use logging.Logger also appear in the unified loguru output.
            class _InterceptHandler(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    try:
                        # Map the stdlib level name onto loguru's own level.
                        lvl = _loguru_logger.level(record.levelname).name
                    except ValueError:
                        # Unknown level name: fall back to the raw numeric level.
                        lvl = record.levelno
                    # Walk up the call stack past logging internals so loguru
                    # reports the true caller's file/function/line, not ours.
                    frame = logging.currentframe()
                    depth = 2
                    while frame and frame.f_code.co_filename == logging.__file__:
                        frame = frame.f_back
                        depth += 1
                    _loguru_logger.opt(depth=depth, exception=record.exc_info).log(
                        lvl, record.getMessage()
                    )

            logging.basicConfig(handlers=[_InterceptHandler()], level=logging.NOTSET, force=True)
    else:
        # Fallback: stdlib logging
        yim_logger = logging.getLogger("encre")
        yim_logger.setLevel(getattr(logging, level, logging.INFO))
        yim_logger.handlers.clear()

        handler: logging.Handler
        if json_format:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(
                '{"timestamp": "%(asctime)s", "level": "%(levelname)s", '
                '"logger": "%(name)s", "function": "%(funcName)s", '
                '"line": %(lineno)d, "message": %(message)s}',
                datefmt="%Y-%m-%dT%H:%M:%S",
            ))
        else:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))

        yim_logger.addHandler(handler)

        if log_file:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(handler.formatter)
            yim_logger.addHandler(file_handler)


class _LoguruCompat:
    """Wrap a bound loguru logger with stdlib-logging call semantics.

    The harness logs with ``logger.warning("tpl %s", arg, exc_info=True)``
    — stdlib style.  Raw loguru neither interpolates ``%s`` args nor
    accepts ``exc_info``: every message ships as a literal template and
    ``exc_info`` calls raise.  The shim renders the message eagerly, maps
    ``exc_info`` onto ``.opt(exception=...)``, keeps ``extra=`` and fixes
    the caller frame via ``depth`` so file/function/line stay accurate.
    """

    __slots__ = ("_lg",)

    def __init__(self, lg: Any) -> None:
        self._lg = lg

    @staticmethod
    def _render(message: Any, args: tuple[Any, ...]) -> str:
        if not args:
            return message if isinstance(message, str) else str(message)
        try:
            return str(message) % args
        except Exception:
            # Malformed template/args mismatch: keep the payload visible
            # instead of raising out of a log call.
            return f"{message} | args={args!r}"

    def _dispatch(self, method: str, message: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        exc_info = kwargs.pop("exc_info", None)
        if method == "exception" and exc_info is None:
            exc_info = True
        kwargs.pop("stack_info", None)
        kwargs.pop("stacklevel", None)
        extra = kwargs.pop("extra", None)
        lg = self._lg.opt(depth=1, exception=exc_info if exc_info is not None else False)
        if extra:
            lg = lg.bind(**extra)
        target = "error" if method == "exception" else method
        getattr(lg, target)(self._render(message, args))

    def trace(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("trace", message, args, kwargs)

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("debug", message, args, kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("info", message, args, kwargs)

    def success(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("success", message, args, kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("warning", message, args, kwargs)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("error", message, args, kwargs)

    def critical(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("critical", message, args, kwargs)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("exception", message, args, kwargs)

    def log(self, level: Any, message: Any, *args: Any, **kwargs: Any) -> None:
        exc_info = kwargs.pop("exc_info", None)
        kwargs.pop("stack_info", None)
        kwargs.pop("stacklevel", None)
        extra = kwargs.pop("extra", None)
        lg = self._lg.opt(depth=1, exception=exc_info if exc_info is not None else False)
        if extra:
            lg = lg.bind(**extra)
        lg.log(level, self._render(message, args))

    def bind(self, **kw: Any) -> "_LoguruCompat":
        return _LoguruCompat(self._lg.bind(**kw))

    def opt(self, **kw: Any) -> "_LoguruCompat":
        return _LoguruCompat(self._lg.opt(**kw))

    def __getattr__(self, item: str) -> Any:
        return getattr(self._lg, item)


def get_logger(name: str = "encre") -> Any:
    """Return a logger for *name*.

    Returns a **loguru** logger (wrapped with stdlib-style call semantics —
    ``%s`` interpolation and ``exc_info``) when loguru is installed,
    otherwise a :class:`_StdlibLogger` wrapper that presents a compatible
    API.

    Args:
        name: Logger name, bound onto the loguru logger or used as the stdlib
            logger name.

    Returns:
        A logger object exposing a loguru-like API (``trace``/``success``/
        ``info``/... and ``extra=`` support).
    """
    if _LOGURU_AVAILABLE:
        assert _loguru_logger is not None
        return _LoguruCompat(_loguru_logger.bind(name=name))
    return _StdlibLogger(name)


# Module-level convenience logger for sub-modules that just want
# ``from encre.logging_config import logger``.
logger = get_logger("encre")
