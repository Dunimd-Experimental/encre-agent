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

"""Market catalog service — the local face of the central repository.

The central repository index ships as the ``ea-cwh`` data package: a
validated ``catalog.json`` describing every published plugin (display
fields, pinned version, sha256 artifacts).  This module is the ONLY
reader the rest of the gateway uses.

Read order (never blocks the market first paint on the network):

1. the ``ea-cwh`` package installed in this environment (authoritative);
2. the last known-good cache at ``<data>/market_catalog.json``, written
   whenever a read from (1) succeeded;
3. an empty catalog (the market then shows only locally-discovered
   plugins, exactly as before the central repository existed).

A daily background refresher upgrades ``ea-cwh`` via pip and broadcasts
the new payload to connected clients only when the index fingerprint
(version + generation stamp) actually changed.  In frozen (packaged)
builds the pip step is skipped — the bundled environment is immutable,
so the index stays at whatever version shipped with the app.
"""

import asyncio
import contextlib
import importlib
import importlib.metadata
import json
import logging
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from encre.paths import get_data_dir

INDEX_PACKAGE_DIST = "ea-cwh"
INDEX_PACKAGE_MODULE = "ea_cwh"
REFRESH_INTERVAL_SECONDS = 24 * 3600

#: Reserved PyPI namespaces the installer will unpack/pip-install from.
#: Matches ``ea_cwh.RESERVED_PREFIXES`` — community ``ea-plugin-`` plus the
#: vendor ``ea-tool-`` / ``ea-skill-`` namespaces.
RESERVED_PREFIXES = ("ea-plugin-", "ea-tool-", "ea-skill-")

logger = logging.getLogger("encre.market_catalog")

#: Last catalog payload + the cache-file stamp it was built from. Reading and
#: validating the ea-cwh document costs real time (import + schema check) and
#: happens on every connect, install and update probe; the file only changes
#: when a refresh writes it, so a stamp-keyed memo is safe.
_CATALOG_CACHE: dict[str, Any] | None = None
_CATALOG_CACHE_KEY: tuple[Any, ...] | None = None

#: When the last central-index check ran (persisted across restarts, so a
#: fresh launch does not re-run pip/PyPI just because the process restarted).
_CHECK_STAMP_FILENAME = "market_index_checked.json"


def _cache_file() -> Path:
    """Path of the last known-good catalog cache under the data tree."""
    return get_data_dir("market_catalog.json", ensure=False)


def _cache_stamp() -> tuple[int, int]:
    """Cheap identity of the catalog cache file (mtime, size)."""
    try:
        stat = _cache_file().stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (0, 0)


def invalidate_catalog_cache() -> None:
    """Drop the memo after an index refresh wrote new content."""
    global _CATALOG_CACHE, _CATALOG_CACHE_KEY
    _CATALOG_CACHE = None
    _CATALOG_CACHE_KEY = None


def _check_stamp_file() -> Path:
    """Path of the persisted last-check timestamp."""
    return get_data_dir(_CHECK_STAMP_FILENAME, ensure=False)


def read_last_check() -> float:
    """Unix timestamp of the last central-index check (0 when unknown)."""
    try:
        data = json.loads(_check_stamp_file().read_text(encoding="utf-8"))
        return float(data.get("checked_at", 0.0)) if isinstance(data, dict) else 0.0
    except (OSError, ValueError, TypeError):
        return 0.0


def write_last_check(timestamp: float) -> None:
    """Persist the last-check timestamp atomically (best effort)."""
    try:
        target = _check_stamp_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"checked_at": float(timestamp)}), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        logger.debug("market_catalog: check stamp write failed: %s", exc)


def read_index() -> dict[str, Any] | None:
    """Read the catalog straight from the installed ``ea-cwh`` package.

    Returns:
        ``{"index_version", "generated_at", "plugins"}`` or ``None`` when
        the package is absent or its document fails schema validation.
    """
    try:
        ea_cwh = importlib.import_module(INDEX_PACKAGE_MODULE)
        doc = ea_cwh.catalog()  # validates; raises ValueError on violations
        return {
            "index_version": ea_cwh.index_version(),
            "generated_at": doc.get("generated_at", ""),
            "plugins": doc.get("plugins") or [],
        }
    except Exception as exc:
        logger.debug("market_catalog: ea-cwh read failed: %s", exc)
        return None


def load_catalog() -> dict[str, Any]:
    """Return the market catalog payload (index → cache → empty).

    A successful index read is mirrored into the cache file so the next
    boot — or a broken upgrade — can still render the store offline.

    Frozen (packaged) builds invert the order: the bundled ``ea-cwh`` is a
    build-time snapshot that can never be pip-upgraded, while the cache is
    what the daily HTTPS refresh keeps current (see
    :func:`refresh_index`) — so the refreshed cache wins there.

    Returns:
        ``{"source": "index"|"cache"|"none", "index_version": str,
        "generated_at": str, "plugins": list[dict]}``, shaped for the
        ``market_catalog`` WebSocket frame.
    """
    global _CATALOG_CACHE, _CATALOG_CACHE_KEY
    frozen = bool(getattr(sys, "frozen", False))
    key = (frozen, *_cache_stamp())
    if _CATALOG_CACHE is not None and _CATALOG_CACHE_KEY == key:
        return _CATALOG_CACHE

    empty = {"source": "none", "index_version": "", "generated_at": "", "plugins": []}
    if frozen:
        cached = _read_cache()
        if cached is not None:
            payload: dict[str, Any] = {"source": "cache", **cached}
        else:
            index = read_index()
            payload = {"source": "index", **index} if index is not None else empty
    else:
        index = read_index()
        if index is not None:
            _write_cache(index)
            payload = {"source": "index", **index}
        else:
            cached = _read_cache()
            payload = {"source": "cache", **cached} if cached is not None else empty

    _CATALOG_CACHE = payload
    _CATALOG_CACHE_KEY = (frozen, *_cache_stamp())
    return payload


def _fingerprint(payload: dict[str, Any]) -> tuple[str, str]:
    """Identity of a catalog state: (index version, generation stamp)."""
    return (str(payload.get("index_version", "")), str(payload.get("generated_at", "")))


def _http_get_json(url: str) -> dict[str, Any]:
    """GET a JSON document over HTTPS (PyPI's public API)."""
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_bytes(url: str) -> bytes:
    """GET a binary artifact (PyPI file download)."""
    import urllib.request

    with urllib.request.urlopen(url, timeout=300) as resp:
        return resp.read()


def version_gt(candidate: str, current: str) -> bool:
    """True when dotted version ``candidate`` is newer than ``current``.

    Compares dot-separated segments numerically where both sides are
    digits, lexically otherwise; missing trailing segments count as "0"
    (so ``1.2`` equals ``1.2.0``).  Deliberately a heuristic, not full
    PEP440 — the index pins one plain version per plugin and this only
    needs to decide "update available?".
    """
    a = re.split(r"[._+-]", str(candidate or "").strip())
    b = re.split(r"[._+-]", str(current or "").strip())
    for i in range(max(len(a), len(b))):
        sa = a[i] if i < len(a) else "0"
        sb = b[i] if i < len(b) else "0"
        if sa == sb:
            continue
        if sa.isdigit() and sb.isdigit():
            return int(sa) > int(sb)
        return sa > sb
    return False


def has_update(name: str, installed_version: str) -> dict[str, Any] | None:
    """The index entry when it pins a strictly newer version for ``name``.

    Used by the market install path so clicking an updatable plugin
    actually pulls the new wheel instead of re-activating the old one.
    Yanked entries never count as an update.
    """
    entry = next(
        (e for e in load_catalog().get("plugins") or [] if e.get("name") == name),
        None,
    )
    if entry is None or entry.get("yanked"):
        return None
    if version_gt(str(entry.get("version", "")), installed_version):
        return entry
    return None


def refresh_index() -> bool:
    """Bring the local catalog up to date with the central index.

    Frozen (packaged) builds have no pip: they refresh through PyPI's JSON
    API instead, downloading the newest ``ea-cwh`` wheel, verifying it
    against PyPI's attested sha256, extracting its ``catalog.json`` (schema
    validated) and writing the cache that :func:`load_catalog` prefers in
    that mode.  Dev builds simply pip-upgrade the installed package.

    Returns:
        ``True`` when a newer index was fetched successfully (the caller
        re-reads and compares fingerprints to tell whether to broadcast).
    """
    if getattr(sys, "frozen", False):
        return _refresh_index_frozen()

    # Ask PyPI for the published version first: `pip install --upgrade`
    # contacts the index, resolves and may build metadata even when nothing
    # changed. The JSON probe is one small request and lets an unchanged
    # index return without spawning pip at all.
    try:
        current = importlib.metadata.version(INDEX_PACKAGE_DIST)
    except Exception:
        current = ""
    try:
        info = _http_get_json(f"https://pypi.org/pypi/{INDEX_PACKAGE_DIST}/json")
        remote = str((info.get("info") or {}).get("version", ""))
    except Exception as exc:
        logger.debug("market_catalog: version probe failed: %s", exc)
        return False
    if remote and current and not version_gt(remote, current):
        return False

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", INDEX_PACKAGE_DIST]
    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        subprocess.run(cmd, check=True, timeout=300, **kwargs)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("market_catalog: index upgrade failed: %s", exc)
        return False
    # Make the freshly installed module shadow the cached import.
    importlib.invalidate_caches()
    sys.modules.pop(INDEX_PACKAGE_MODULE, None)
    invalidate_catalog_cache()
    return True


def _refresh_index_frozen() -> bool:
    """HTTPS-only index refresh for packaged builds (no pip available).

    Fetches the newest ``ea-cwh`` distribution metadata from PyPI's JSON
    API, downloads its wheel only when the version is newer than what we
    have, verifies the bytes against PyPI's attested sha256 (transport
    integrity), schema-validates the extracted ``catalog.json`` and writes
    the cache :func:`load_catalog` prefers in frozen mode.
    """
    import hashlib
    import io
    import zipfile

    try:
        data = _http_get_json(f"https://pypi.org/pypi/{INDEX_PACKAGE_DIST}/json")
        version = str(data.get("info", {}).get("version", ""))
        wheel = next((f for f in data.get("urls") or [] if str(f.get("filename", "")).endswith(".whl")), None)
        if not version or wheel is None:
            return False
        cached = _read_cache()
        current = (cached or read_index() or {}).get("index_version", "")
        if version == current:
            return False  # already fresh — skip the download
        attested = str((wheel.get("digests") or {}).get("sha256", "")).lower()
        raw = _http_get_bytes(str(wheel.get("url", "")))
        digest = hashlib.sha256(raw).hexdigest()
        if attested and attested != digest:
            logger.warning("market_catalog: index wheel failed PyPI digest attestation")
            return False
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            member = next((n for n in zf.namelist() if n.endswith("ea_cwh/catalog.json")), None)
            if member is None:
                return False
            doc = json.loads(zf.read(member).decode("utf-8"))
        problems = importlib.import_module(INDEX_PACKAGE_MODULE).validate_catalog(doc)
        if problems:
            logger.warning("market_catalog: fetched index catalog invalid: %s", problems[:3])
            return False
        _write_cache({"index_version": version, "generated_at": str(doc.get("generated_at", "")), "plugins": doc.get("plugins") or []})
        logger.info("market_catalog: packaged build refreshed index to %s", version)
        return True
    except Exception as exc:
        logger.warning("market_catalog: frozen index refresh failed: %s", exc)
        return False


def install_from_index(entry: dict[str, Any]) -> None:
    """Install one catalogued plugin, pinned and hash-verified.

    Downloads exactly the version the index names, verifies the wheel's
    sha256 against the index artifacts BEFORE anything is installed (so a
    squatted or tampered distribution file can never reach the import
    step), then installs the verified local file.  Raises on any failure;
    the caller turns it into a ``market_install_error`` frame.

    Frozen (packaged) builds have no pip and no writable site-packages —
    the same pinned+digest-verified wheel is instead unpacked into the
    plugin install directory (see :func:`_unpack_install_from_index`),
    where the runtime scanner picks it up as a PENDING package; dynamic
    authorisation (:meth:`PluginRegistry.confirm_activation`, driven by
    the market's Install click) remains the last security line either way.

    Args:
        entry: A validated catalog entry (``pypi_package``, ``version``
            and ``artifacts`` with per-file ``sha256``).

    Raises:
        RuntimeError: digest/availability failure or a pip step failing.
    """
    import hashlib
    import shutil
    import tempfile

    if getattr(sys, "frozen", False):
        _unpack_install_from_index(entry)
        return
    pkg = str(entry.get("pypi_package", ""))
    version = str(entry.get("version", ""))
    if not pkg.startswith(RESERVED_PREFIXES) or not version:
        raise RuntimeError("catalog entry is missing a pinned package name or version")
    expected = {
        str(meta.get("sha256", "")).lower(): fname
        for fname, meta in (entry.get("artifacts") or {}).items()
        if fname.endswith(".whl") and isinstance(meta, dict)
    }
    if not expected:
        raise RuntimeError("catalog entry ships no wheel digests to verify against")

    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    tmp = Path(tempfile.mkdtemp(prefix="ea-market-"))
    try:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "download", f"{pkg}=={version}", "--only-binary", ":all:", "-d", str(tmp), "--quiet"],
                check=True,
                timeout=600,
                **kwargs,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(f"downloading {pkg}=={version} failed: {exc}") from exc
        verified: Path | None = None
        for candidate in sorted(tmp.glob("*.whl")):
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if digest in expected:
                verified = candidate
                break
        if verified is None:
            raise RuntimeError(f"no downloaded artifact for {pkg} matches the digests published in the index")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", str(verified), "--quiet"], check=True, timeout=600, **kwargs)
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(f"installing the verified wheel failed: {exc}") from exc
        importlib.invalidate_caches()
        logger.info("market_catalog: installed verified %s (digest of %s)", pkg, verified.name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _unpack_install_from_index(entry: dict[str, Any]) -> str:
    """Pip-free central install for packaged builds: download, verify, unpack.

    The wheel is fetched pinned-by-version, its bytes are matched against
    BOTH PyPI's attested digest and the digest the curated index promised,
    then it is unpacked into ``<data>/plugins/ea-plugin-<name>/`` together
    with a ``pyproject.toml`` re-rendered from the catalog entry — exactly
    the source layout the runtime plugin scanner reads, so the package
    lands in the same PENDING → confirm flow as every third-party plugin.

    Returns:
        The install directory path.

    Raises:
        RuntimeError: missing digests, unavailable artifact, digest
            mismatch, a non-plugin wheel, or unsafe paths inside it.
    """
    import configparser
    import hashlib
    import io
    import shutil
    import zipfile
    from pathlib import Path, PurePosixPath

    pkg = str(entry.get("pypi_package", ""))
    name = str(entry.get("name", ""))
    version = str(entry.get("version", ""))
    if not pkg.startswith(RESERVED_PREFIXES) or not version or not name:
        raise RuntimeError("catalog entry is missing a pinned package name or version")
    expected = {
        str(meta.get("sha256", "")).lower(): fname
        for fname, meta in (entry.get("artifacts") or {}).items()
        if fname.endswith(".whl") and isinstance(meta, dict)
    }
    if not expected:
        raise RuntimeError("catalog entry ships no wheel digests to verify against")

    data = _http_get_json(f"https://pypi.org/pypi/{pkg}/{version}/json")
    target = next((f for f in data.get("urls") or [] if str(f.get("filename", "")) in expected.values()), None)
    if target is None:
        raise RuntimeError(f"{pkg}=={version} is no longer available on PyPI")
    fname = str(target.get("filename"))
    want = next(d for d, f in expected.items() if f == fname)
    if str((target.get("digests") or {}).get("sha256", "")).lower() != want:
        raise RuntimeError("PyPI's copy no longer matches the digest the index promised")
    raw = _http_get_bytes(str(target.get("url", "")))
    if hashlib.sha256(raw).hexdigest() != want:
        raise RuntimeError("downloaded wheel does not match the index digest — refusing to unpack")

    staging: Path | None = None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for n in zf.namelist():
                parts = PurePosixPath(n)
                if parts.is_absolute() or ".." in parts.parts or (len(n) > 1 and n[1] == ":"):
                    raise RuntimeError(f"wheel contains unsafe path {n!r}")
            ep_member = next((n for n in zf.namelist() if n.endswith("entry_points.txt") and ".dist-info" in n), None)
            if ep_member is None:
                raise RuntimeError("wheel carries no entry_points.txt")
            cp = configparser.ConfigParser()
            cp.read_string(zf.read(ep_member).decode("utf-8"))
            if not cp.has_section("ea.plugins") or len(cp.options("ea.plugins")) != 1:
                raise RuntimeError("wheel is not a single Encre Agent plugin package")
            factory_target = cp.get("ea.plugins", cp.options("ea.plugins")[0]).strip()
            if ":" not in factory_target:
                raise RuntimeError("plugin entry point target is not 'module:factory'")

            plugins_root = get_data_dir("plugins", ensure=True)
            dest = plugins_root / pkg
            staging = plugins_root / f".{dest.name}.staging"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                out = staging / Path(*PurePosixPath(info.filename).parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(zf.read(info))
        (staging / "pyproject.toml").write_text(_gen_pyproject(entry, name, factory_target), encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    logger.info("market_catalog: unpacked %s==%s into %s (pending confirmation)", pkg, version, dest)
    return str(dest)


def _gen_pyproject(entry: dict[str, Any], name: str, factory_target: str) -> str:
    """Render the ``pyproject.toml`` a pip-less install leaves beside code.

    ``pyproject.toml`` is the directory scanner's single source of metadata
    truth; wheels don't carry one, but the catalog entry holds the very
    curated fields the author published, so they are re-emitted here —
    including the tier.  A community plugin is ``user`` (so it keeps the
    dynamic-authorisation gate); a vendor package published by the Encre
    build pipeline carries ``system-default`` in the index and auto-activates
    exactly like its bundled copy.  Anything unexpected falls back to
    ``user`` — the index can only ever relax, never escalate to mandatory.
    JSON string literals are valid TOML basic strings, which is why
    ``json.dumps`` doubles as the escaper.
    """

    def s(v: object) -> str:
        return json.dumps(str(v), ensure_ascii=False)

    tier = entry.get("tier") if entry.get("tier") in ("user", "system-default") else "user"
    lines = [
        "[project]",
        f"name = {s(name)}",
        f"version = {s(entry.get('version', ''))}",
        f"description = {s(entry.get('description', ''))}",
    ]
    if entry.get("author"):
        lines.append(f"authors = [{{ name = {s(entry['author'])} }}]")
    if entry.get("license"):
        lines.append(f"license = {{ text = {s(entry['license'])} }}")
    lines += ["", '[project.entry-points."ea.plugins"]', f"{s(name)} = {s(factory_target)}"]

    tool = [f"tier = {s(tier)}"]
    for dst, src in (("icon", "icon"), ("license", "license"), ("min_ea_version", "min_encre_version"),
                     ("display_name", "display_name")):
        if entry.get(src):
            tool.append(f"{dst} = {s(entry[src])}")
    for key in ("tags", "permissions"):
        vals = entry.get(key) or []
        if vals:
            tool.append(f"{key} = {json.dumps([str(v) for v in vals], ensure_ascii=False)}")
    for surface, items in (entry.get("provides") or {}).items():
        if items:
            tool.append(f"provides_{surface} = {json.dumps([str(i) for i in items], ensure_ascii=False)}")
    lines += ["", "[tool.ea]", *tool]
    return "\n".join(lines) + "\n"


class MarketCatalogRefresher:
    """Daily index upgrade + broadcast, wired into the server lifecycle.

    Args:
        on_update: Awaited with the new catalog payload whenever the index
            fingerprint changes after a refresh.
        interval: Seconds between refresh attempts.
        initial_delay: Seconds before the FIRST refresh attempt (default
            :attr:`FIRST_REFRESH_DELAY_SECONDS`, 30s).  Long enough to keep
            the pip subprocess out of the boot sequence, short enough that a
            dev environment without ``ea-cwh`` preinstalled does not wait a
            full day before the market can show anything.
    """

    FIRST_REFRESH_DELAY_SECONDS = 30.0

    def __init__(
        self,
        on_update: Callable[[dict[str, Any]], Awaitable[None]],
        interval: float = REFRESH_INTERVAL_SECONDS,
        initial_delay: float | None = None,
    ) -> None:
        self._on_update = on_update
        self._interval = interval
        if initial_delay is None:
            initial_delay = self.FIRST_REFRESH_DELAY_SECONDS
        self._initial_delay = initial_delay
        self._task: asyncio.Task[None] | None = None
        self._last_fingerprint: tuple[str, str] | None = None

    async def start(self) -> None:
        """Start the refresh loop (idempotent)."""
        if self._task is not None:
            return
        # Reading/validating the catalog is disk + CPU work; do it off the
        # event loop so server readiness is not delayed by it.
        payload = await asyncio.to_thread(load_catalog)
        self._last_fingerprint = _fingerprint(payload)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the refresh loop, suppressing its cancellation."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        """Refresh after the initial delay, then on the daily schedule.

        The first cycle fires ``initial_delay`` (default
        :attr:`FIRST_REFRESH_DELAY_SECONDS`) after start instead of waiting a
        full interval: a dev environment without ``ea-cwh`` preinstalled must
        not wait 24h before the market can show anything at all.  The
        short delay keeps the boot sequence (heavy plugin imports,
        adapter startup) free of the pip subprocess.
        """
        first = True
        while True:
            await asyncio.sleep(self._initial_delay if first else self._interval)
            first = False
            # Persisted across restarts: relaunching the app is not a reason
            # to re-check PyPI/pip when the last check is still fresh.  A
            # zero initial delay is the explicit "check now" mode used by
            # tests/manual callers, so it bypasses the throttle entirely.
            if self._initial_delay > 0:
                try:
                    last = await asyncio.to_thread(read_last_check)
                except Exception:
                    last = 0.0
                if last and time.time() - last < self._interval:
                    continue
                await asyncio.to_thread(write_last_check, time.time())
            try:
                upgraded = await asyncio.to_thread(refresh_index)
                if not upgraded:
                    continue
                # The upgrade may have replaced the ea-cwh module (dev) or the
                # cache file (frozen); drop the memo before re-reading so the
                # fingerprint reflects the new index.
                invalidate_catalog_cache()
                payload = await asyncio.to_thread(load_catalog)
                fp = _fingerprint(payload)
                if fp == self._last_fingerprint:
                    continue
                self._last_fingerprint = fp
                await self._on_update(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A failed cycle must not kill the daily schedule.
                logger.warning("market_catalog: refresh cycle failed: %s", exc)


def _write_cache(index: dict[str, Any]) -> None:
    """Mirror a good index read into the data-dir cache (best effort)."""
    try:
        target = _cache_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.debug("market_catalog: cache write failed: %s", exc)


def _read_cache() -> dict[str, Any] | None:
    """Load the cached catalog document (None when absent/corrupt)."""
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("plugins"), list):
            return {
                "index_version": str(data.get("index_version", "")),
                "generated_at": str(data.get("generated_at", "")),
                "plugins": data["plugins"],
            }
    except (OSError, json.JSONDecodeError):
        pass
    return None
