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

"""Chunked media upload driver for the QQ Bot v2 API.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

The QQ Bot v2 ``/files`` endpoint accepts media either as a fetchable URL or
as inline base64, but the inline path is capped at roughly 10 MB. Larger
media must go through a three-step chunked protocol::

    1. POST /v2/{users|groups}/{id}/upload_prepare
       -> upload_id, block_size, and a list of pre-signed COS part URLs
    2. for each part:
         PUT the part bytes to its pre-signed COS URL, then
         POST /v2/{users|groups}/{id}/upload_part_finish to acknowledge
    3. POST /v2/{users|groups}/{id}/files with {"upload_id": ...}
       -> the file_info token consumed by a RichMedia message

This module owns steps 2 and 3 plus the hashing and part slicing. Step 1 is
also implemented here (``_prepare``) so the whole protocol lives in one place.

Error-code semantics, per the QQ Bot v2 spec:

* ``40093002`` -- daily cumulative upload quota exceeded. Not retryable;
  surfaced as :exc:`UploadDailyLimitExceededError` so callers can compose a
  user-facing reply.
* ``40093001`` -- ``upload_part_finish`` transient failure. Retryable until
  the server-provided ``retry_timeout`` elapses.

Exceptions:

* :exc:`UploadDailyLimitExceededError` -- daily quota hit (non-retryable).
* :exc:`UploadFileTooLargeError` -- file exceeds the per-file limit.
* ``RuntimeError`` -- generic upload failure (network, part PUT, complete).

Collaborators:
    Instantiated by
    :meth:`ea_connector_qqbot.adapter.QQAdapter._upload_local_file`
    with the adapter's bound ``_api_request`` and ``httpx.AsyncClient.put``.
    Those callables are injected rather than imported so this module has no
    dependency on the adapter and stays unit-testable.
"""

import asyncio
import functools
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .constants import FILE_UPLOAD_TIMEOUT

logger = logging.getLogger(__name__)


# Error codes
_BIZ_CODE_DAILY_LIMIT = 40093002     # upload_prepare: daily cumulative limit
_BIZ_CODE_PART_RETRYABLE = 40093001  # upload_part_finish: transient

# Part upload tuning
_DEFAULT_CONCURRENT_PARTS = 1
_MAX_CONCURRENT_PARTS = 10

_PART_UPLOAD_TIMEOUT = 300.0        # 5 minutes per COS PUT
_PART_UPLOAD_MAX_RETRIES = 2
_PART_FINISH_RETRY_INTERVAL = 1.0
_PART_FINISH_DEFAULT_TIMEOUT = 120.0
_PART_FINISH_MAX_TIMEOUT = 600.0

_COMPLETE_UPLOAD_MAX_RETRIES = 2
_COMPLETE_UPLOAD_BASE_DELAY = 2.0

# First 10,002,432 bytes used for the ``md5_10m`` hash (per QQ API spec).
_MD5_10M_SIZE = 10_002_432


# Exceptions

class UploadDailyLimitExceededError(Exception):
    """Raised when ``upload_prepare`` returns biz_code ``40093002``.

    The bot's daily cumulative upload quota is exhausted. This is a hard
    platform limit that resets on the portal's own schedule, so retrying
    cannot help.

    The exception carries the file name and byte size because the caller
    typically needs to tell the operator *which* file tripped the limit and
    how large it was, rather than surfacing a raw biz code.

    Attributes:
        file_name: Display name of the upload that hit the quota.
        file_size: Size in bytes.
        file_size_human: :attr:`file_size` formatted via :func:`format_size`.
    """

    def __init__(self, file_name: str, file_size: int, message: str = "") -> None:
        """Record which file tripped the quota and build the error text.

        Args:
            file_name: Display name of the upload that hit the limit.
            file_size: Size in bytes, exposed as :attr:`file_size`.
            message: Override text; when empty a default describing the file
                and its size is generated.

        Note:
            This error is non-retryable. Waiting does not clear a daily quota,
            so callers surface it to the user instead of backing off.
        """
        self.file_name = file_name
        self.file_size = file_size
        super().__init__(
            message or f"Daily upload limit exceeded for {file_name!r}"
        )

    @property
    def file_size_human(self) -> str:
        """The :attr:`file_size` rendered as a human-readable string."""
        return format_size(self.file_size)


class UploadFileTooLargeError(Exception):
    """Raised when a file exceeds the platform's per-file size limit.

    Distinct from the daily-limit error: a single oversized file cannot be
    rescued by waiting, and the operator needs the size alongside the limit
    to decide whether to trim the payload or split it.

    Attributes:
        file_name: Display name of the rejected file.
        file_size: Size in bytes.
        limit_bytes: The platform limit in bytes, or 0 when the platform did
            not report one.
        file_size_human: :attr:`file_size` formatted via :func:`format_size`.
        limit_human: :attr:`limit_bytes` formatted, or ``"unknown"``.
    """

    def __init__(
        self,
        file_name: str,
        file_size: int,
        limit_bytes: int = 0,
        message: str = "",
    ) -> None:
        """Record the oversized file and the limit the platform enforced.

        Args:
            file_name: Display name of the rejected file.
            file_size: Size in bytes.
            limit_bytes: Platform per-file limit, or 0 when the platform did
                not report one; in that case the message omits the limit.
            message: Override text; when empty a default is generated from
                the file name, its size and the reported limit.
        """
        self.file_name = file_name
        self.file_size = file_size
        self.limit_bytes = limit_bytes
        limit_str = f" ({format_size(limit_bytes)})" if limit_bytes else ""
        super().__init__(
            message
            or (
                f"File {file_name!r} ({format_size(file_size)}) "
                f"exceeds platform limit{limit_str}"
            )
        )

    @property
    def file_size_human(self) -> str:
        """The :attr:`file_size` rendered as a human-readable string."""
        return format_size(self.file_size)

    @property
    def limit_human(self) -> str:
        """The :attr:`limit_bytes` rendered, or ``"unknown"`` if absent."""
        return format_size(self.limit_bytes) if self.limit_bytes else "unknown"


# Progress tracking

@dataclass
class _UploadProgress:
    """Shared mutable counter across concurrently uploading parts.

    Each part increments ``completed_parts`` and ``uploaded_bytes`` after its
    acknowledge call succeeds. The values are plain ints with no locking; they
    are only ever read for debug logging, so an occasional stale read across
    the event loop boundary is harmless here.
    """

    total_parts: int = 0
    total_bytes: int = 0
    completed_parts: int = 0
    uploaded_bytes: int = 0


# Prepare-response shape

@dataclass
class _PreparePart:
    """One part of an ``upload_prepare`` response.

    Attributes:
        index: 1-based part index, echoed back to ``upload_part_finish``.
        presigned_url: COS object URL to PUT this part's bytes to.
        block_size: Per-part block size; zero when the response omits it, in
            which case the response-level ``block_size`` applies.
    """

    index: int
    presigned_url: str
    block_size: int = 0


@dataclass
class _PrepareResult:
    """Normalized ``upload_prepare`` response.

    Attributes:
        upload_id: Opaque handle tying every part acknowledgement and the
            final ``complete_upload`` call together.
        block_size: Default block size used to compute part offsets.
        parts: Ordered list of :class:`_PreparePart`.
        concurrency: Server-advised parallelism, clamped to
            :data:`_MAX_CONCURRENT_PARTS` before use.
        retry_timeout: Server-provided budget for retrying
            ``upload_part_finish``; zero means "use the local default".
    """

    upload_id: str
    block_size: int
    parts: List[_PreparePart]
    concurrency: int = _DEFAULT_CONCURRENT_PARTS
    retry_timeout: float = 0.0


def _parse_prepare_response(raw: Dict[str, Any]) -> _PrepareResult:
    """Normalize an ``upload_prepare`` response into a :class:`_PrepareResult`.

    The portal returns either the payload directly or nested under a
    ``data`` key, and part fields are spelled two ways (``part_index`` /
    ``index``, ``presigned_url`` / ``url``). Tolerating both shapes keeps the
    parser working if the portal changes envelope conventions.

    Args:
        raw: The parsed JSON body of the ``upload_prepare`` response.

    Returns:
        A fully populated :class:`_PrepareResult`.

    Raises:
        ValueError: The response has no usable ``upload_id``, or the part
            list is missing, empty, or not a list. The message includes a
            truncated preview of the response to aid diagnosis.
    """
    src = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    upload_id = str(src.get("upload_id", ""))
    if not upload_id:
        raise ValueError(
            f"upload_prepare response missing upload_id: {str(raw)[:200]}"
        )
    block_size = int(src.get("block_size", 0))
    raw_parts = src.get("parts") or src.get("part_list") or []
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError(
            f"upload_prepare response missing parts: {str(raw)[:200]}"
        )
    parts: List[_PreparePart] = []
    for p in raw_parts:
        if not isinstance(p, dict):
            continue
        parts.append(
            _PreparePart(
                index=int(p.get("part_index") or p.get("index") or 0),
                presigned_url=str(
                    p.get("presigned_url") or p.get("url") or ""
                ),
                block_size=int(p.get("block_size", 0)),
            )
        )
    return _PrepareResult(
        upload_id=upload_id,
        block_size=block_size,
        parts=parts,
        concurrency=int(src.get("concurrency", _DEFAULT_CONCURRENT_PARTS)) or _DEFAULT_CONCURRENT_PARTS,
        retry_timeout=float(src.get("retry_timeout", 0.0) or 0.0),
    )


# Chunked upload driver

ApiRequestFn = Callable[..., Awaitable[Dict[str, Any]]]
"""Signature of the adapter's ``_api_request`` callable.

The bound method is injected instead of imported so this module never imports
the adapter. That avoids a circular import (the adapter imports this module)
and keeps the driver testable with a fake request function.
"""


class ChunkedUploader:
    """Drive the prepare -> PUT parts -> complete chunked upload sequence.

    The class holds only collaborators and stateless configuration; all file
    I/O happens inside the coroutine methods. It is safe to instantiate a
    fresh uploader per upload, which is what the adapter does.

    All methods assume a running asyncio event loop. Blocking filesystem work
    is dispatched to the default executor so the loop stays responsive while
    large files are read.

    Args:
        api_request: Bound ``_api_request(method, path, body=...,
            timeout=...)`` coroutine. Must raise ``RuntimeError`` whose string
            form contains the biz_code on API failure, because error routing
            keys off that substring.
        http_put: Coroutine ``(url, data, headers, timeout) -> response``
            used for the COS part PUTs. Typically ``httpx.AsyncClient.put``.
        log_tag: Prefix attached to every log line, so concurrent adapter
            instances stay distinguishable in the log.
    """

    def __init__(
        self,
        api_request: ApiRequestFn,
        http_put: Callable[..., Awaitable[Any]],
        log_tag: str = "QQBot",
    ) -> None:
        """Store collaborators; perform no I/O and open no connections.

        Keeping the constructor side-effect free lets the adapter construct
        an uploader inside ``async with`` blocks and reuse a single event
        loop without pre-warming any network state.

        Args:
            api_request: Injected REST call hook. See the class docstring for
                the required signature and error contract.
            http_put: Injected HTTP PUT hook used for the COS part uploads.
            log_tag: Prefix for log lines produced by this uploader.
        """
        self._api_request = api_request
        self._http_put = http_put
        self._log_tag = log_tag

    async def upload(
        self,
        chat_type: str,
        target_id: str,
        file_path: str,
        file_type: int,
        file_name: str,
    ) -> Dict[str, Any]:
        """Run the full chunked upload and return the ``complete_upload`` response.

        Pipeline:

        1. Hash the file (``md5``, ``sha1``, ``md5_10m``) in an executor, so
           multi-hundred-MB reads do not block the event loop.
        2. ``upload_prepare`` to obtain the upload handle, block size, and
           pre-signed COS URLs.
        3. PUT every part and acknowledge each one, bounded by the
           server-advised concurrency.
        4. ``complete_upload`` with retry, returning the ``file_info`` token.

        Args:
            chat_type: ``"c2c"`` for a private chat, ``"group"`` for a group.
                Any other value is rejected because the path prefixes differ.
            target_id: User openid (c2c) or group_openid (group).
            file_path: Absolute path to a local file on disk.
            file_type: A ``MEDIA_TYPE_*`` constant.
            file_name: Original filename reported to the API for display.

        Returns:
            The raw ``complete_upload`` response dict, containing the
            ``file_info`` token the caller embeds in a RichMedia message.

        Raises:
            ValueError: ``chat_type`` is neither ``"c2c"`` nor ``"group"``.
            UploadDailyLimitExceededError: biz_code ``40093002`` at prepare.
            UploadFileTooLargeError: File exceeds the platform limit.
            RuntimeError: Any other API or I/O failure.
        """
        if chat_type not in {"c2c", "group"}:
            raise ValueError(
                f"ChunkedUploader: unsupported chat_type {chat_type!r}"
            )

        path = Path(file_path)
        file_size = path.stat().st_size

        logger.info(
            "[%s] Chunked upload start: file=%s size=%s type=%d",
            self._log_tag, file_name, format_size(file_size), file_type,
        )

        # Step 1: compute hashes (blocking I/O -> executor).
        hashes = await asyncio.get_running_loop().run_in_executor(
            None, _compute_file_hashes, file_path, file_size
        )

        # Step 2: upload_prepare.
        prepare = await self._prepare(
            chat_type, target_id, file_type, file_name, file_size, hashes
        )
        max_concurrent = min(prepare.concurrency, _MAX_CONCURRENT_PARTS)
        retry_timeout = min(
            prepare.retry_timeout if prepare.retry_timeout > 0 else _PART_FINISH_DEFAULT_TIMEOUT,
            _PART_FINISH_MAX_TIMEOUT,
        )
        logger.info(
            "[%s] Prepared: upload_id=%s block_size=%s parts=%d concurrency=%d",
            self._log_tag, prepare.upload_id, format_size(prepare.block_size),
            len(prepare.parts), max_concurrent,
        )

        progress = _UploadProgress(
            total_parts=len(prepare.parts),
            total_bytes=file_size,
        )

        # Step 3: PUT each part + notify.
        tasks: List[Callable[[], Awaitable[None]]] = [
            functools.partial(
                self._upload_one_part,
                chat_type=chat_type,
                target_id=target_id,
                file_path=file_path,
                file_size=file_size,
                upload_id=prepare.upload_id,
                rsp_block_size=prepare.block_size,
                part=part,
                retry_timeout=retry_timeout,
                progress=progress,
            )
            for part in prepare.parts
        ]
        await _run_with_concurrency(tasks, max_concurrent)

        logger.info(
            "[%s] All %d parts uploaded, completing…",
            self._log_tag, len(prepare.parts),
        )

        # Step 4: complete_upload (retry on transient errors).
        return await self._complete(chat_type, target_id, prepare.upload_id)

    # ------------------------------------------------------------------
    # Step 1 -- upload_prepare
    # ------------------------------------------------------------------

    async def _prepare(
        self,
        chat_type: str,
        target_id: str,
        file_type: int,
        file_name: str,
        file_size: int,
        hashes: Dict[str, str],
    ) -> _PrepareResult:
        """Call ``upload_prepare`` and return the normalized :class:`_PrepareResult`.

        The three digests are required by the portal to deduplicate uploads it
        has already seen; only the hash pair is checked, not the bytes.

        Args:
            chat_type: ``"c2c"`` or ``"group"``, selects the path prefix.
            target_id: Recipient openid or group_openid.
            file_type: A ``MEDIA_TYPE_*`` constant.
            file_name: Display filename.
            file_size: Total file size in bytes.
            hashes: Dict with ``md5``, ``sha1``, and ``md5_10m`` hex digests.

        Returns:
            Parsed :class:`_PrepareResult`.

        Raises:
            UploadDailyLimitExceededError: biz_code ``40093002`` present in
                the raised ``RuntimeError`` message.
            RuntimeError: Any other API failure, re-raised unchanged.
            ValueError: The response fails to parse.
        """
        base = "/v2/users" if chat_type == "c2c" else "/v2/groups"
        path = f"{base}/{target_id}/upload_prepare"
        body = {
            "file_type": file_type,
            "file_name": file_name,
            "file_size": file_size,
            "md5": hashes["md5"],
            "sha1": hashes["sha1"],
            "md5_10m": hashes["md5_10m"],
        }
        try:
            raw = await self._api_request(
                "POST", path, body=body, timeout=FILE_UPLOAD_TIMEOUT
            )
        except RuntimeError as exc:
            err_msg = str(exc)
            if f"{_BIZ_CODE_DAILY_LIMIT}" in err_msg:
                raise UploadDailyLimitExceededError(
                    file_name, file_size, err_msg
                ) from exc
            raise
        return _parse_prepare_response(raw)

    # ------------------------------------------------------------------
    # Step 2 -- PUT one part + part_finish
    # ------------------------------------------------------------------

    async def _upload_one_part(
        self,
        chat_type: str,
        target_id: str,
        file_path: str,
        file_size: int,
        upload_id: str,
        rsp_block_size: int,
        part: _PreparePart,
        retry_timeout: float,
        progress: _UploadProgress,
    ) -> None:
        """Read one slice of the file, PUT it to COS, then acknowledge it.

        Offset and length math: parts are 1-based, so the byte offset of part
        ``n`` is ``(n - 1) * rsp_block_size``. The final part is clamped to
        ``file_size - offset`` so a file whose size is not a multiple of the
        block size does not read past EOF. The per-part ``block_size`` wins
        over the response-level value when both are present.

        The part digest is computed over exactly the bytes that were PUT, so
        the server can verify chunk integrity independently.

        Args:
            chat_type: ``"c2c"`` or ``"group"``, for the acknowledge path.
            target_id: Recipient openid or group_openid.
            file_path: Path of the file being sliced.
            file_size: Total file size in bytes.
            upload_id: Handle from ``upload_prepare``.
            rsp_block_size: Response-level block size.
            part: The :class:`_PreparePart` to upload.
            retry_timeout: Budget for the acknowledge retry loop.
            progress: Shared counter, updated after a successful acknowledge.

        Raises:
            RuntimeError: The COS PUT exhausted retries, the acknowledge call
                failed permanently, or the file read came up short.
        """
        part_index = part.index
        # Per-part block_size wins; fall back to the response-level value.
        actual_block_size = part.block_size if part.block_size > 0 else rsp_block_size
        offset = (part_index - 1) * rsp_block_size
        length = min(actual_block_size, file_size - offset)

        # Read this slice of the file (blocking -> executor).
        data = await asyncio.get_running_loop().run_in_executor(
            None, _read_file_chunk, file_path, offset, length
        )
        md5_hex = hashlib.md5(data).hexdigest()

        logger.debug(
            "[%s] Part %d/%d: uploading %s (offset=%d md5=%s)",
            self._log_tag, part_index, progress.total_parts,
            format_size(length), offset, md5_hex,
        )

        await self._put_to_presigned_url(
            part.presigned_url, data, part_index, progress.total_parts
        )
        await self._part_finish_with_retry(
            chat_type, target_id, upload_id,
            part_index, length, md5_hex, retry_timeout,
        )

        progress.completed_parts += 1
        progress.uploaded_bytes += length
        logger.debug(
            "[%s] Part %d/%d done (%d/%d total)",
            self._log_tag, part_index, progress.total_parts,
            progress.completed_parts, progress.total_parts,
        )

    async def _put_to_presigned_url(
        self,
        url: str,
        data: bytes,
        part_index: int,
        total_parts: int,
    ) -> None:
        """PUT *data* to a pre-signed COS URL with bounded retry.

        Each attempt is wrapped in :func:`asyncio.wait_for` so a hung socket
        cannot stall the whole upload forever; the deadline is generous
        because these are large payloads. Non-2xx responses are treated as
        failures rather than inspected further, since COS returns opaque
        bodies that carry no actionable detail.

        Backoff is exponential from a 1s base, doubling per attempt, with no
        jitter -- a short burst of part uploads benefits from tight retries.

        Args:
            url: Pre-signed COS object URL for this part.
            data: The raw part bytes.
            part_index: 1-based part index, for log correlation.
            total_parts: Total part count, for log correlation.

        Raises:
            RuntimeError: All attempts failed, or the response status was
                outside the 2xx range.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(_PART_UPLOAD_MAX_RETRIES + 1):
            try:
                resp = await asyncio.wait_for(
                    self._http_put(
                        url,
                        data=data,
                        headers={"Content-Length": str(len(data))},
                    ),
                    timeout=_PART_UPLOAD_TIMEOUT,
                )
                # Caller's http_put is expected to return an httpx-like response.
                status = getattr(resp, "status_code", 0)
                if 200 <= status < 300:
                    logger.debug(
                        "[%s] PUT part %d/%d: %d OK",
                        self._log_tag, part_index, total_parts, status,
                    )
                    return
                body_preview = ""
                try:
                    body_preview = getattr(resp, "text", "")[:200]
                except Exception:  # pragma: no cover -- defensive
                    pass
                raise RuntimeError(
                    f"COS PUT returned {status}: {body_preview}"
                )
            except Exception as exc:
                last_exc = exc
                if attempt < _PART_UPLOAD_MAX_RETRIES:
                    delay = 1.0 * (2 ** attempt)
                    logger.warning(
                        "[%s] PUT part %d/%d attempt %d failed, retry in %.1fs: %s",
                        self._log_tag, part_index, total_parts,
                        attempt + 1, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"Part {part_index}/{total_parts} upload failed after "
            f"{_PART_UPLOAD_MAX_RETRIES + 1} attempts: {last_exc}"
        )

    async def _part_finish_with_retry(
        self,
        chat_type: str,
        target_id: str,
        upload_id: str,
        part_index: int,
        block_size: int,
        md5: str,
        retry_timeout: float,
    ) -> None:
        """Call ``upload_part_finish``, retrying biz_code ``40093001``.

        The portal can reject an acknowledge because it has not yet observed
        the corresponding COS PUT -- the object store and the API are not
        synchronously consistent. ``40093001`` is the signal for that case,
        so the loop retries at a fixed 1s interval until the server-provided
        budget elapses. Any other error is treated as permanent and re-raised
        immediately, because only one code is documented as retryable.

        The clock is ``asyncio.get_running_loop().time()`` so the elapsed
        check does not shift with wall-clock adjustments.

        Args:
            chat_type: ``"c2c"`` or ``"group"``, selects the path prefix.
            target_id: Recipient openid or group_openid.
            upload_id: Handle from ``upload_prepare``.
            part_index: 1-based index of the part just uploaded.
            block_size: Number of bytes uploaded for that part.
            md5: Hex digest of the part bytes actually sent.
            retry_timeout: Maximum total seconds to keep retrying.

        Raises:
            RuntimeError: A non-retryable API error, or the retry budget was
                exhausted without acknowledgement.
        """
        base = "/v2/users" if chat_type == "c2c" else "/v2/groups"
        path = f"{base}/{target_id}/upload_part_finish"
        body = {
            "upload_id": upload_id,
            "part_index": part_index,
            "block_size": block_size,
            "md5": md5,
        }

        loop = asyncio.get_running_loop()
        start = loop.time()
        attempt = 0
        while True:
            try:
                await self._api_request(
                    "POST", path, body=body, timeout=FILE_UPLOAD_TIMEOUT
                )
                return
            except RuntimeError as exc:
                err_msg = str(exc)
                if f"{_BIZ_CODE_PART_RETRYABLE}" not in err_msg:
                    raise
                elapsed = loop.time() - start
                if elapsed >= retry_timeout:
                    raise RuntimeError(
                        f"upload_part_finish persistent retry timed out "
                        f"after {retry_timeout:.0f}s ({attempt} retries): {exc}"
                    ) from exc
                attempt += 1
                logger.debug(
                    "[%s] part_finish retryable error, attempt %d, "
                    "elapsed=%.1fs: %s",
                    self._log_tag, attempt, elapsed, exc,
                )
                await asyncio.sleep(_PART_FINISH_RETRY_INTERVAL)

    # ------------------------------------------------------------------
    # Step 3 -- complete_upload
    # ------------------------------------------------------------------

    async def _complete(
        self,
        chat_type: str,
        target_id: str,
        upload_id: str,
    ) -> Dict[str, Any]:
        """Call ``complete_upload`` with exponential retry.

        This reuses the ordinary ``/files`` endpoint -- the one used for the
        simple URL-based upload -- but signals the chunked path by sending
        only ``upload_id`` and no ``url`` / ``file_data``. The portal then
        assembles the previously PUT parts and returns the ``file_info``
        token.

        Completion can fail transiently because part assembly on the portal
        side lags the last acknowledgement. Backoff doubles from a 2s base.

        Args:
            chat_type: ``"c2c"`` or ``"group"``, selects the path prefix.
            target_id: Recipient openid or group_openid.
            upload_id: Handle from ``upload_prepare``.

        Returns:
            The parsed ``complete_upload`` response, containing ``file_info``.

        Raises:
            RuntimeError: Every attempt failed.
        """
        base = "/v2/users" if chat_type == "c2c" else "/v2/groups"
        path = f"{base}/{target_id}/files"
        body = {"upload_id": upload_id}

        last_exc: Optional[Exception] = None
        for attempt in range(_COMPLETE_UPLOAD_MAX_RETRIES + 1):
            try:
                return await self._api_request(
                    "POST", path, body=body, timeout=FILE_UPLOAD_TIMEOUT
                )
            except Exception as exc:
                last_exc = exc
                if attempt < _COMPLETE_UPLOAD_MAX_RETRIES:
                    delay = _COMPLETE_UPLOAD_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "[%s] complete_upload attempt %d failed, "
                        "retry in %.1fs: %s",
                        self._log_tag, attempt + 1, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"complete_upload failed after "
            f"{_COMPLETE_UPLOAD_MAX_RETRIES + 1} attempts: {last_exc}"
        )


# Helpers (module-level for testability)

def format_size(size_bytes: int) -> str:
    """Return a human-readable size string, e.g. ``"12.3 MB"``.

    Steps through binary (1024-based) units and stops at the first unit where
    the value is below 1024, so the result always carries one decimal place.
    Values at or above 1 TB are reported in TB.

    Args:
        size_bytes: Size in bytes.

    Returns:
        A string such as ``"4.2 KB"`` or ``"120.0 B"``.
    """
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _read_file_chunk(file_path: str, offset: int, length: int) -> bytes:
    """Read exactly *length* bytes from *file_path* starting at *offset*.

    Runs in a thread-pool executor (see
    :meth:`ChunkedUploader._upload_one_part`) so large reads do not block the
    event loop. A short read is treated as a hard error rather than silently
    accepted: a truncated file would produce a part whose digest the portal
    rejects, and failing here gives a clearer message.

    Args:
        file_path: Path of the file to slice.
        offset: Byte offset of the chunk start.
        length: Exact number of bytes expected.

    Returns:
        The requested bytes.

    Raises:
        IOError: Fewer than *length* bytes were read (truncated file).
    """
    with open(file_path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
        if len(data) != length:
            raise IOError(
                f"Short read from {file_path}: expected {length} bytes at "
                f"offset {offset}, got {len(data)} (file may be truncated)"
            )
        return data


def _compute_file_hashes(file_path: str, file_size: int) -> Dict[str, str]:
    """Compute ``md5``, ``sha1``, and ``md5_10m`` in a single pass.

    The portal wants three digests for upload deduplication. ``md5_10m`` is
    special: it is the MD5 of only the first ~10 MB (exactly
    :data:`_MD5_10M_SIZE` bytes), letting the portal short-circuit large
    duplicate uploads cheaply. Running all three accumulators over one read
    pass avoids three full scans of a potentially large file.

    Files smaller than the 10 MB threshold get the full-file MD5 reported as
    ``md5_10m``, because the prefix and the whole file coincide.

    Args:
        file_path: Path of the file to hash.
        file_size: File size in bytes, used to decide whether the partial
            digest applies.

    Returns:
        Dict with hex-string values under ``md5``, ``sha1``, and ``md5_10m``.
    """
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    md5_10m = hashlib.md5()

    need_10m = file_size > _MD5_10M_SIZE
    bytes_read = 0

    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            if need_10m:
                remaining = _MD5_10M_SIZE - bytes_read
                if remaining > 0:
                    md5_10m.update(chunk[:remaining])
            bytes_read += len(chunk)

    full_md5 = md5.hexdigest()
    return {
        "md5": full_md5,
        "sha1": sha1.hexdigest(),
        # For small files the "10m" hash is just the full md5.
        "md5_10m": md5_10m.hexdigest() if need_10m else full_md5,
    }


async def _run_with_concurrency(
    tasks: List[Callable[[], Awaitable[None]]],
    concurrency: int,
) -> None:
    """Run thunks with at most *concurrency* in flight at once.

    Wraps every thunk in a semaphore-guarded coroutine and awaits them all.
    Raising the concurrency limit protects the portal and local socket pool
    from a file that would otherwise fan out into hundreds of parallel part
    PUTs; clamping to a minimum of 1 keeps the function usable when the
    caller supplies a non-positive value.

    Args:
        tasks: Zero-argument coroutines, one per upload part.
        concurrency: Upper bound on simultaneously executing tasks.

    Raises:
        BaseException: Propagated from the first failing thunk.
    """
    concurrency = max(concurrency, 1)
    sem = asyncio.Semaphore(concurrency)

    async def _wrap(thunk: Callable[[], Awaitable[None]]) -> None:
        """Acquire a semaphore slot, run the thunk, then release the slot.

        Wrapping every thunk uniformly keeps the concurrency bound exact even
        when thunks raise -- the slot is released by the context manager
        rather than by each caller.
        """
        async with sem:
            await thunk()

    await asyncio.gather(*(_wrap(t) for t in tasks))
