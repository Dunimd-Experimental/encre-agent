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

"""Media (video/audio) processing tool (ffmpeg).

Converts, trims, extracts audio, transcribes and reports info for
video and audio files via ffmpeg.

The binary resolution strategy is layered: prefer a system
``ffmpeg``/``ffprobe`` on ``PATH`` (fastest and always up to date),
fall back to the ``static-ffmpeg`` PyPI package, and finally fall
back to a urllib-based self-heal download that bypasses the pip
package's certifi chain (which can be rejected by local proxies).
Binaries downloaded in the last step are cached under
``~/.cache/encre/ffmpeg_bins`` and reused on later calls.
"""

import asyncio
import contextlib
import json
import os
import shutil
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes


def _fetch_static_ffbin(name: str) -> str:
    """Self-heal: fetch a static ffmpeg/ffprobe binary via urllib.

    Used when the ``static-ffmpeg`` package's own download fails (e.g.
    its requests certifi chain is rejected by a local proxy). urllib
    uses the OS trust store, which survives most interception.
    Binaries are cached under ``~/.cache/encre/ffmpeg_bins`` and
    reused on later calls.

    The download is written to a ``.part`` file and renamed to the
    final path only after a complete transfer so a partial download
    does not get misidentified as a working binary on the next run.
    The zip is extracted in place and the temp zip is deleted when
    the extraction finishes (via ``contextlib.suppress(OSError)``
    so a race with the OS GC never raises).
    """
    import platform
    import sys
    import urllib.request
    import zipfile

    machine = platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        key, suffix = "win32", ".exe"
    elif sys.platform == "darwin":
        key, suffix = ("darwin_arm64" if is_arm else "darwin"), ""
    elif sys.platform == "linux":
        key, suffix = ("linux_arm64" if is_arm else "linux"), ""
    else:
        raise RuntimeError(f"no static ffmpeg build for {sys.platform}")

    cache = os.path.join(os.path.expanduser("~"), ".cache", "encre", "ffmpeg_bins", key)
    target = os.path.join(cache, name + suffix)
    if not os.path.isfile(target):
        os.makedirs(cache, exist_ok=True)
        url = f"https://github.com/zackees/ffmpeg_bins/raw/main/v8.0/{key}.zip"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        zip_path = os.path.join(cache, key + ".zip")
        part_path = zip_path + ".part"
        with urllib.request.urlopen(req, timeout=180) as resp, open(part_path, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        os.replace(part_path, zip_path)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                members = {os.path.basename(m): m for m in zf.namelist() if not m.endswith("/")}
                for want in ("ffmpeg" + suffix, "ffprobe" + suffix):
                    if want not in members:
                        raise RuntimeError(f"ffmpeg bundle missing {want}")
                    with zf.open(members[want]) as src, open(os.path.join(cache, want), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        finally:
            with contextlib.suppress(OSError):
                os.remove(zip_path)
    if os.name != "nt":
        os.chmod(target, 0o755)
    return target


def _ffmpeg_bin(name: str) -> str:
    """Resolve an ffmpeg/ffprobe executable.

    Prefers a system binary on PATH; otherwise falls back to the pip
    ``static-ffmpeg`` package, and finally to a urllib self-heal
    download (cached under ``~/.cache/encre/ffmpeg_bins``).

    When every resolution path fails we return ``name`` unchanged so
    the subprocess call still happens and the caller gets the real
    ``FileNotFoundError`` message from ``create_subprocess_exec``
    rather than a cryptic wrapper error.
    """
    found = shutil.which(name)
    if found:
        return found
    try:
        from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise

        ffmpeg_path, ffprobe_path = get_or_fetch_platform_executables_else_raise()
        return ffmpeg_path if name == "ffmpeg" else ffprobe_path
    except Exception:
        pass
    try:
        return _fetch_static_ffbin(name)
    except Exception:
        return name


async def _resolve_ffmpeg_bin(name: str) -> str:
    """Resolve the binary in a worker thread (static-ffmpeg may download).

    Running :func:`_ffmpeg_bin` inside an executor keeps the event
    loop responsive while ``static-ffmpeg`` (or the urllib fallback)
    downloads a binary on first use.
    """
    return await asyncio.get_event_loop().run_in_executor(None, _ffmpeg_bin, name)


async def _media_execute(**kwargs: Any) -> str:
    """Execute a media action and return a human-readable result.

    Dispatches on the ``action`` key to the matching per-action
    coroutine. Every error is returned as a string (either
    ``Error:``-prefixed or a domain-specific message) rather than
    raised, because the tool executor surfaces strings directly to
    the LLM.

    Args:
        **kwargs: Tool arguments. ``action`` (required, one of
            ``info``, ``convert``, ``extract_audio``,
            ``extract_frames``, ``compress``, ``trim``,
            ``screenshot``, ``audio_info``, ``transcribe``).
            ``file_path`` (required). Other keys depend on the
            action: ``output_path``, ``output_dir``, ``codec``,
            ``bitrate``, ``format``, ``fps``, ``quality``, ``start``,
            ``duration``, ``time``.

    Returns:
        A human-readable summary of the action's outcome, or an
        ``Error:``-prefixed string on failure.
    """
    action = kwargs.get("action", "")
    file_path = kwargs.get("file_path", "")
    if not file_path:
        return "Error: 'file_path' is required."

    if action == "info":
        return await _media_info(file_path)
    elif action == "convert":
        output_path = kwargs.get("output_path", "")
        codec = kwargs.get("codec", "")
        bitrate = kwargs.get("bitrate", "")
        return await _media_convert(file_path, output_path, codec, bitrate)
    elif action == "extract_audio":
        output_path = kwargs.get("output_path", "")
        format = kwargs.get("format", "mp3")
        return await _media_extract_audio(file_path, output_path, format)
    elif action == "extract_frames":
        output_dir = kwargs.get("output_dir", "")
        fps = kwargs.get("fps", 1)
        return await _media_extract_frames(file_path, output_dir, fps)
    elif action == "compress":
        output_path = kwargs.get("output_path", "")
        quality = kwargs.get("quality", 23)
        return await _media_compress(file_path, output_path, quality)
    elif action == "trim":
        output_path = kwargs.get("output_path", "")
        start = kwargs.get("start", 0)
        duration = kwargs.get("duration", 10)
        return await _media_trim(file_path, output_path, start, duration)
    elif action == "screenshot":
        output_path = kwargs.get("output_path", "")
        time = kwargs.get("time", "00:00:01")
        return await _media_screenshot(file_path, output_path, time)
    elif action == "audio_info":
        return await _media_audio_info(file_path)
    elif action == "transcribe":
        return await _media_transcribe(file_path)
    else:
        return f"Error: Unknown action '{action}'."


def _check_ffmpeg() -> None:
    """Placeholder kept for backwards compatibility with older call sites.

    Binary resolution is now lazy (see :func:`_ffmpeg_bin`), so no
    upfront check is needed at module import time.
    """
    pass


async def _run_ffmpeg(args: list[str], timeout: int = 300) -> str:
    """Run an ffmpeg / ffprobe command and return its output.

    If the first argument is ``ffmpeg`` or ``ffprobe`` (a bare name
    rather than a full path), we substitute the resolved binary so
    that static-ffmpeg or the urllib self-heal can transparently
    provide the executable when the system does not have one.

    The subprocess is launched with the framework's window-hiding
    kwargs so the desktop session does not flash a console window.
    The timeout is enforced with :func:`asyncio.wait_for`; on
    timeout the process is killed and a sentinel message is
    returned.

    Args:
        args: The command line (e.g. ``["ffmpeg", "-i", "src.mp4", "-y", "out.mp3"]``).
        timeout: Maximum wall-clock seconds to wait for the
            subprocess to finish.

    Returns:
        The stdout (preferred) or stderr of the subprocess, an
        ``Error:``-prefixed string on failure, or the literal
        string ``"(completed)"`` when the subprocess exits
        successfully without printing anything.
    """
    try:
        from encre.capabilities.process import (
            hidden_subprocess_kwargs as _hidden,
        )
        if args and args[0] in ("ffmpeg", "ffprobe"):
            args = [await _resolve_ffmpeg_bin(args[0]), *args[1:]]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hidden(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"Error: ffmpeg command timed out after {timeout}s"
        out = decode_bytes(stdout) if stdout else ""
        err = decode_bytes(stderr) if stderr else ""
        if proc.returncode != 0:
            return f"ffmpeg error (exit {proc.returncode}): {err[:500]}"
        return out or err or "(completed)"
    except FileNotFoundError:
        return "Error: ffmpeg not found. Install ffmpeg (https://ffmpeg.org/) or 'pip install static-ffmpeg'."
    except Exception as e:
        return f"Error running ffmpeg: {e}"


def _parse_ffprobe_output(text: str) -> dict[str, Any]:
    """Parse ffprobe's human-readable key/value output into a dict.

    Kept for compatibility with callers that feed ffprobe's
    ``default`` output format into a dict consumer. Note that
    :func:`_media_info` and :func:`_media_audio_info` use the JSON
    format directly and do not go through this helper.
    """
    info: dict[str, Any] = {}
    for line in text.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"')
            if key and val:
                info[key.lower().replace(" ", "_")] = val
    return info


async def _media_info(file_path: str) -> str:
    """Report metadata about an audio or video file.

    Runs ``ffprobe`` with ``-print_format json`` and unpacks the
    result into a compact dict containing duration, bitrate, format
    name, and (when present) the first video and audio streams'
    codec / resolution / sample-rate / channel info.

    The fps is evaluated from the ``r_frame_rate`` string (``"30000/1001"``)
    only when it actually looks like a fraction, otherwise it is
    reported as ``0`` so a malformed stream does not crash the tool.

    Args:
        file_path: Path to the source audio or video file.

    Returns:
        A JSON string with the extracted metadata, or an
        ``Error:``-prefixed string on failure.
    """
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    try:
        from encre.capabilities.process import (
            hidden_subprocess_kwargs as _hidden,
        )
        probe_bin = await _resolve_ffmpeg_bin("ffprobe")
        proc = await asyncio.create_subprocess_exec(
            probe_bin, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hidden(),
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return f"Error: ffprobe failed (exit {proc.returncode}). Is ffmpeg installed?"
        data = json.loads(decode_bytes(stdout))
    except FileNotFoundError:
        return "Error: ffprobe not found. Install ffmpeg (https://ffmpeg.org/) or 'pip install static-ffmpeg'."
    except json.JSONDecodeError:
        return "Error: Could not parse media file metadata."
    except Exception as e:
        return f"Error reading media info: {e}"

    result: dict[str, Any] = {
        "file": os.path.basename(file_path),
        "size_bytes": os.path.getsize(file_path),
    }

    fmt = data.get("format", {})
    if fmt:
        result.update({
            "duration_sec": round(float(fmt.get("duration", 0)), 2),
            "bitrate_kbps": fmt.get("bit_rate", ""),
            "format_name": fmt.get("format_name", ""),
        })

    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if video_streams:
        v = video_streams[0]
        result["video"] = {
            "codec": v.get("codec_name", ""),
            "width": v.get("width", 0),
            "height": v.get("height", 0),
            "fps": eval(v.get("r_frame_rate", "0/1")) if "/" in v.get("r_frame_rate", "") else 0,
            "bitrate_kbps": v.get("bit_rate", ""),
        }

    if audio_streams:
        a = audio_streams[0]
        result["audio"] = {
            "codec": a.get("codec_name", ""),
            "sample_rate": a.get("sample_rate", ""),
            "channels": a.get("channels", 0),
            "bitrate_kbps": a.get("bit_rate", ""),
        }

    return json.dumps(result, indent=2)


async def _media_convert(file_path: str, output_path: str, codec: str, bitrate: str) -> str:
    """Re-encode a media file with the given codec and bitrate.

    Args:
        file_path: Path to the source media file.
        output_path: Path to write the converted media to.
        codec: Target video codec (e.g. ``libx264``, ``libx265``);
            when empty, ffmpeg uses its default.
        bitrate: Target bitrate (e.g. ``1M``, ``500k``); when empty,
            ffmpeg uses its default.

    Returns:
        A success message including the source and destination
        paths, or an ``Error:``-prefixed string on failure.
    """
    if not output_path:
        return "Error: 'output_path' is required for convert."
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    args = ["ffmpeg", "-i", file_path]
    if codec:
        args.extend(["-c:v", codec])
    if bitrate:
        args.extend(["-b:v", bitrate])
    args.extend(["-y", output_path])
    result = await _run_ffmpeg(args)
    if "error" in result.lower():
        return result
    return f"Converted: {file_path} -> {output_path}"


async def _media_extract_audio(file_path: str, output_path: str, fmt: str) -> str:
    """Extract the audio track from a media file.

    When ``output_path`` is omitted we synthesise one by
    substituting the extension with the requested ``fmt``, so the
    caller does not need to remember the source file's basename.

    Args:
        file_path: Path to the source media file.
        output_path: Path to write the extracted audio to (may be
            empty; a default path is derived from the source).
        fmt: Target audio format (``mp3``, ``aac``, ``wav``,
            ``ogg``, ``flac``). The codec is chosen from a small
            table inside ffmpeg that is kept here so we do not have
            to shell out for a lookup.

    Returns:
        A success message including the output path, or an
        ``Error:``-prefixed string on failure.
    """
    if not output_path:
        base = os.path.splitext(file_path)[0]
        output_path = f"{base}.{fmt}"
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"

    codec_map = {"mp3": "libmp3lame", "aac": "aac", "wav": "pcm_s16le", "ogg": "libvorbis", "flac": "flac"}
    acodec = codec_map.get(fmt, "libmp3lame")

    args = ["ffmpeg", "-i", file_path, "-vn", "-acodec", acodec, "-y", output_path]
    result = await _run_ffmpeg(args, timeout=600)
    if "error" in result.lower():
        return result
    return f"Audio extracted: {output_path}"


async def _media_extract_frames(file_path: str, output_dir: str, fps: int) -> str:
    """Extract frames from a video at the given fps.

    Args:
        file_path: Path to the source video.
        output_dir: Directory to write PNG frames to. When empty,
            the source file's directory is used.
        fps: Frames per second to sample (default 1).

    Returns:
        A success message including the number of frames written,
        or an ``Error:``-prefixed string on failure.
    """
    if not output_dir:
        output_dir = os.path.dirname(file_path) or "."
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(file_path))[0]
    pattern = os.path.join(output_dir, f"{base}_frame_%04d.png")
    args = ["ffmpeg", "-i", file_path, "-vf", f"fps={fps}", pattern, "-y"]
    result = await _run_ffmpeg(args, timeout=600)
    if "error" in result.lower():
        return result

    frame_count = len([f for f in os.listdir(output_dir) if f.startswith(base + "_frame_")])
    return f"Extracted {frame_count} frames ({fps} fps) to {output_dir}/"


async def _media_compress(file_path: str, output_path: str, quality: int) -> str:
    """Compress a video with CRF-based quality control.

    Args:
        file_path: Path to the source video.
        output_path: Path to write the compressed video to. When
            empty, a ``_compressed`` suffix is appended to the
            source basename.
        quality: CRF value (0-51, lower is higher quality).

    Returns:
        A success message including the output path and the
        percentage size reduction, or an ``Error:``-prefixed string
        on failure.
    """
    if not output_path:
        base, ext = os.path.splitext(file_path)
        output_path = f"{base}_compressed{ext}"
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    args = ["ffmpeg", "-i", file_path, "-crf", str(quality), "-preset", "medium", "-y", output_path]
    result = await _run_ffmpeg(args, timeout=600)
    if "error" in result.lower():
        return result
    orig_size = os.path.getsize(file_path)
    new_size = os.path.getsize(output_path)
    ratio = (1 - new_size / orig_size) * 100 if orig_size else 0
    return f"Compressed: {output_path} ({ratio:.1f}% smaller, CRF={quality})"


async def _media_trim(file_path: str, output_path: str, start: int, duration: int) -> str:
    """Trim a media file to a segment using stream copy.

    Uses ``-c copy`` so the operation is fast and lossless: ffmpeg
    just rewrites the container without re-encoding the streams. The
    caller is responsible for choosing ``start`` / ``duration`` at a
    keyframe boundary; otherwise ffmpeg will silently snap to the
    nearest keyframe.

    Args:
        file_path: Path to the source media file.
        output_path: Path to write the trimmed clip to. When empty,
            a ``_trim`` suffix is appended to the source basename.
        start: Start time in seconds.
        duration: Clip length in seconds.

    Returns:
        A success message including the output path, or an
        ``Error:``-prefixed string on failure.
    """
    if not output_path:
        base, ext = os.path.splitext(file_path)
        output_path = f"{base}_trim{ext}"
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    args = ["ffmpeg", "-i", file_path, "-ss", str(start), "-t", str(duration), "-c", "copy", "-y", output_path]
    result = await _run_ffmpeg(args)
    if "error" in result.lower():
        return result
    return f"Trimmed: {output_path} ({duration}s starting at {start}s)"


async def _media_screenshot(file_path: str, output_path: str, time: str) -> str:
    """Capture a single frame from a video as a PNG.

    Args:
        file_path: Path to the source video.
        output_path: Path to write the PNG to. When empty, a
            ``_screenshot.png`` suffix is appended to the source
            basename.
        time: Timestamp (``HH:MM:SS``) at which to capture the
            frame.

    Returns:
        A success message including the output path, or an
        ``Error:``-prefixed string on failure.
    """
    if not output_path:
        base = os.path.splitext(file_path)[0]
        output_path = f"{base}_screenshot.png"
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    args = ["ffmpeg", "-i", file_path, "-ss", time, "-vframes", "1", "-y", output_path]
    result = await _run_ffmpeg(args)
    if "error" in result.lower():
        return result
    return f"Screenshot saved: {output_path} (at {time})"


async def _media_audio_info(file_path: str) -> str:
    """Report metadata about the audio track of a media file.

    Similar to :func:`_media_info` but restricts the ffprobe query
    to audio streams (``-select_streams a``) so the output is small
    even for a container with multiple video/audio/subtitle tracks.

    Args:
        file_path: Path to the source audio or video file.

    Returns:
        A JSON string with the extracted audio metadata, or an
        ``Error:``-prefixed string on failure.
    """
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    try:
        from encre.capabilities.process import (
            hidden_subprocess_kwargs as _hidden,
        )
        probe_bin = await _resolve_ffmpeg_bin("ffprobe")
        proc = await asyncio.create_subprocess_exec(
            probe_bin, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams",
            "-select_streams", "a", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hidden(),
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return "No audio streams found in file."
        data = json.loads(decode_bytes(stdout))
    except FileNotFoundError:
        return "Error: ffprobe not found. Install ffmpeg or 'pip install static-ffmpeg'."
    except Exception as e:
        return f"Error reading audio info: {e}"

    result: dict[str, Any] = {"file": os.path.basename(file_path)}
    fmt = data.get("format", {})
    if fmt:
        result.update({
            "duration_sec": round(float(fmt.get("duration", 0)), 2),
            "bitrate_kbps": fmt.get("bit_rate", ""),
            "format_name": fmt.get("format_name", ""),
        })
    streams = data.get("streams", [])
    if streams:
        a = streams[0]
        result["audio"] = {
            "codec": a.get("codec_name", ""),
            "sample_rate": a.get("sample_rate", ""),
            "channels": a.get("channels", 0),
            "bitrate_kbps": a.get("bit_rate", ""),
        }
    return json.dumps(result, indent=2)


async def _media_transcribe(file_path: str) -> str:
    """Transcribe speech from an audio file using Whisper.

    The import of :mod:`whisper` is deferred to call time because it
    pulls in torch and the whisper models -- importing it at module
    load would slow down every agent loop that does not need
    transcription.

    The ``base`` model is used as a reasonable default that balances
    accuracy against model download size and inference time.

    Args:
        file_path: Path to the source audio file.

    Returns:
        A JSON string with ``text``, ``language``, and ``duration_sec``,
        or an ``Error:``-prefixed string on failure.
    """
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"

    try:
        import whisper

        model = whisper.load_model("base")
        result = model.transcribe(file_path)
        return json.dumps({
            "text": result.get("text", "").strip(),
            "language": result.get("language", ""),
            "duration_sec": round(result.get("duration", 0), 2),
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error transcribing audio: {e}"


media_tool = build_tool(
    name="media",
    description=(
        "Process audio and video files with ffmpeg, plus optional Whisper-based "
        "speech transcription. "
        "Use this for `info`/`audio_info` (metadata), `convert` (re-encode), "
        "`extract_audio` (rip a track), `extract_frames` (frames to PNG), `compress` "
        "(CRF quality control), `trim` (clip a segment), `screenshot` (capture a "
        "frame), or `transcribe` (speech to text). "
        "Do NOT use this for static image operations (use image), live streaming, or "
        "real-time capture; and prefer a dedicated ASR service for long-form audio. "
        "Tips: set `output_path` to control where artifacts land; use `quality` (CRF) "
        "and `bitrate` to balance size vs. fidelity. "
        "Pitfalls: needs ffmpeg/ffprobe -- resolved from PATH, else the pip "
        "'static-ffmpeg' package, else auto-downloaded to ~/.cache/encre on first "
        "use; `transcribe` additionally requires the openai-whisper package and "
        "is much slower than ffmpeg operations."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["info", "convert", "extract_audio", "extract_frames",
                         "compress", "trim", "screenshot", "audio_info", "transcribe"],
                "description": "Media operation: info/audio_info (metadata), convert (re-encode), extract_audio (rip audio), extract_frames (frames to PNG), compress (CRF), trim (clip), screenshot (single frame), or transcribe (speech to text).",
            },
            "file_path": {
                "type": "string",
                "description": "Path to the source audio or video file to process.",
            },
            "output_path": {
                "type": "string",
                "description": "Destination path for the produced media artifact (convert, extract_audio, compress, trim, screenshot).",
            },
            "codec": {
                "type": "string",
                "description": "Target video codec for convert (e.g. libx264, libx265, libvpx).",
            },
            "bitrate": {
                "type": "string",
                "description": "Target bitrate for convert/compress (e.g. '1M', '500k').",
            },
            "format": {
                "type": "string",
                "enum": ["mp3", "aac", "wav", "ogg", "flac"],
                "description": "Audio format used by extract_audio; defaults to mp3.",
            },
            "output_dir": {
                "type": "string",
                "description": "Directory to write extracted PNG frames from extract_frames.",
            },
            "fps": {
                "type": "integer",
                "description": "Frames per second to extract via extract_frames; defaults to 1.",
            },
            "quality": {
                "type": "integer",
                "description": "CRF value for compress (0-51, lower is higher quality); defaults to 23.",
            },
            "start": {
                "type": "integer",
                "description": "Start time in seconds for trim.",
            },
            "duration": {
                "type": "integer",
                "description": "Clip length in seconds for trim; defaults to 10.",
            },
            "time": {
                "type": "string",
                "description": "Timestamp for screenshot (e.g. '00:01:30'); defaults to '00:00:01'.",
            },
        },
        "required": ["action", "file_path"],
    },
    execute=_media_execute,
    intents=["data", "general", "media", "research"],
    category="media",
    semantic_type="media",
    is_concurrency_safe=lambda data: data.get("action") in ("info", "audio_info"),
    is_destructive=lambda args: args.get("action", "") in ("convert", "extract_audio", "extract_frames", "compress", "trim", "screenshot"),
)
