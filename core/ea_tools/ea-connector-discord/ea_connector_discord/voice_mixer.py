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

"""
Continuous PCM audio mixer for Discord voice channels.

discord.py ships no audio mixer: ``VoiceClient.play()`` accepts exactly one
:class:`discord.AudioSource` and raises ``ClientException`` if called while
already playing.  One opus stream per voice connection, one source feeding it.

This module adds software mixing *upstream* of that single stream.  A
:class:`VoiceMixer` is itself a ``discord.AudioSource`` that discord.py polls
every 20 ms through :meth:`VoiceMixer.read`.  Internally it sums the 20 ms PCM
frames of any number of child sources, clamps to int16 range, and returns one
blended frame.  discord.py never sees that several streams were combined
underneath; it simply encodes and sends the single mixed frame.

That gives one voice connection, at the same time:

* an always-on low-volume **ambient/idle loop** (the "thinking" sound) while the
  agent is running tools, and
* a **speech** channel (TTS replies, verbal acknowledgements) that plays *over*
  the ambient bed, automatically **ducking** the ambient gain down while speech
  is active and ramping it back up when speech ends -- the smooth voice-mode
  feel rather than stop-and-swap.

Design notes
------------
* The mixer is installed **once** per guild on join (``vc.play(mixer)``) and runs
  continuously until the bot leaves.  Children come and go; the mixer itself
  never stops, so there is no ``is_playing()`` race between an acknowledgement
  and the final reply.
* Frame geometry is Discord-native: 48 kHz, 2 channels, signed 16-bit LE,
  20 ms per frame, which is ``discord.opus.Encoder.FRAME_SIZE`` bytes
  (3840 = 960 samples * 2 channels * 2 bytes).
* Mixing is a single vectorised int32 add plus clip per 20 ms frame (numpy,
  already a core dependency of the voice path).  CPU cost is negligible.
* :meth:`VoiceMixer.read` is invoked from discord.py's audio sender **thread**,
  while children are added and removed from the asyncio event loop thread, so
  all shared state is guarded by a plain ``threading.Lock``.

The mixer never touches the inbound receive path: it only produces the bot's
*outgoing* stream.  The :class:`VoiceReceiver` in the sibling adapter module
decodes incoming SSRCs only, so mixer output cannot echo back into
transcription.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).  Thanks to Hermes Agent for
the inspiration on this module.

Public API: :class:`VoiceMixer`, :class:`MixerChild`, :func:`decode_to_pcm`,
:func:`synth_ambient_pcm`, and the frame-geometry constants.
"""

import logging
import threading
from typing import TYPE_CHECKING, List, Optional

from encre.tools._encoding import decode_bytes

if TYPE_CHECKING:  # numpy is an optional ("voice" extra) dep -- never import at runtime top-level
    import numpy as np

logger = logging.getLogger(__name__)


def _require_numpy():
    """Import numpy lazily and return the module.

    numpy ships in the optional ``voice`` extra, not in the base install, so
    this module must import cleanly without it (the Discord adapter imports this
    file unconditionally).  Callers that actually mix audio call this instead;
    when the voice extra is absent they get a clear error at the point of use
    rather than a top-level ``ImportError`` that would break the whole adapter
    import.

    Returns:
        Any: The imported ``numpy`` module.

    Raises:
        ImportError: If numpy is not installed in the current environment.
    """
    import numpy as np  # noqa: PLC0415 — intentional lazy import
    return np


# Discord-native frame geometry (matches discord.opus.Encoder).
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2                       # bytes per sample (s16)
FRAME_LENGTH_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_LENGTH_MS // 1000   # 960
FRAME_SIZE = SAMPLES_PER_FRAME * CHANNELS * SAMPLE_WIDTH    # 3840 bytes
SILENCE_FRAME = b"\x00" * FRAME_SIZE


class MixerChild:
    """A single audio stream feeding into :class:`VoiceMixer`.

    Wraps raw 48 kHz / stereo / s16le PCM bytes.  :meth:`read_frame` hands back
    one 20 ms frame at a time, optionally looping, with a per-child gain and an
    optional linear fade-in applied.

    The child is not thread-safe on its own; the caller is expected to hold the
    owning :class:`VoiceMixer` lock, which serialises playback against
    add/remove operations issued from the event loop.

    Attributes:
        name: Human-readable label for the child stream.
        loop: Whether playback restarts from the beginning after finishing.
        gain: Linear gain multiplier applied to every returned frame.
        is_speech: Marks the child as a speech clip (used for ducking logic).
        fade_frames: Number of leading frames ramped in to avoid a click.
    """

    __slots__ = (
        "name", "_pcm", "_pos", "loop", "gain",
        "is_speech", "fade_frames", "_fade_done", "_finished",
    )

    def __init__(
        self,
        name: str,
        pcm: bytes,
        *,
        loop: bool = False,
        gain: float = 1.0,
        is_speech: bool = False,
        fade_in_ms: int = 0,
    ):
        """Wrap a PCM buffer as a frame-stepping child source.

        The buffer is zero-padded to a whole number of frames so looping is
        seamless and a trailing partial frame cannot click at the wrap point.

        Args:
            name: Label used for diagnostics and for identifying the stream
                kind inside the mixer.
            pcm: Raw 48 kHz / stereo / s16le PCM bytes.
            loop: Replay from the start when the buffer is exhausted instead of
                finishing.
            gain: Linear multiplier applied to every frame returned.
            is_speech: Whether this child counts as speech for ducking
                purposes.
            fade_in_ms: Duration of a linear fade-in applied to the leading
                frames so a loud child does not click on start.

        Returns:
            None.
        """
        # Pad to a whole number of frames so looping is seamless and the final
        # partial frame doesn't click.
        remainder = len(pcm) % FRAME_SIZE
        if remainder:
            pcm = pcm + b"\x00" * (FRAME_SIZE - remainder)
        self.name = name
        self._pcm = pcm
        self._pos = 0
        self.loop = loop
        self.gain = float(gain)
        self.is_speech = is_speech
        # Linear fade-in over N frames avoids a click when a loud child starts.
        self.fade_frames = max(0, fade_in_ms // FRAME_LENGTH_MS)
        self._fade_done = 0
        self._finished = False

    @property
    def finished(self) -> bool:
        """Whether the child has produced its final frame.

        Returns:
            bool: True once :meth:`read_frame` has stopped yielding frames.
        """
        return self._finished

    def read_frame(self) -> "Optional[np.ndarray]":
        """Return the next 20 ms frame as a float32 ndarray, or None if done.

        Advances an internal cursor through the PCM buffer, applying the
        configured per-child gain and an optional linear fade-in.  When the
        buffer is exhausted the child either loops back to the start (if
        ``loop`` is set and the buffer is non-empty) or marks itself finished
        and returns ``None``.

        Returns:
            Optional[np.ndarray]: The next frame as a float32 ndarray (gain and
            fade applied) ready for mixing, or ``None`` once the stream is
            finished.
        """
        if self._finished:
            return None
        if self._pos >= len(self._pcm):
            if self.loop and self._pcm:
                self._pos = 0
            else:
                self._finished = True
                return None

        np = _require_numpy()
        chunk = self._pcm[self._pos:self._pos + FRAME_SIZE]
        self._pos += FRAME_SIZE
        if len(chunk) < FRAME_SIZE:
            chunk = chunk + b"\x00" * (FRAME_SIZE - len(chunk))

        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)

        gain = self.gain
        if self.fade_frames and self._fade_done < self.fade_frames:
            self._fade_done += 1
            gain *= self._fade_done / self.fade_frames

        if gain != 1.0:
            samples = samples * gain
        return samples


class VoiceMixer:
    """A continuous ``discord.AudioSource`` that mixes N child streams.

    Use :meth:`set_ambient` to install or replace the looping idle bed and
    :meth:`play_speech` to layer a one-shot clip over it (ducking the ambient
    while it plays).  Both are safe to call from the asyncio loop thread while
    discord.py drains :meth:`read` from its sender thread.

    The mixer runs for the lifetime of a voice connection: it is installed with
    a single ``vc.play(mixer)`` call and is never stopped until the bot leaves
    the channel.  Because :meth:`read` always returns a full frame, discord.py's
    player stays alive, which is what removes the ``is_playing()`` race between
    a short acknowledgement and the final reply.

    Attributes:
        _ambient_gain: Resting gain of the looping ambient bed.
        _duck_gain: Ambient gain while speech is ducked underneath.
        _speech_gain: Gain applied to speech clips.
        _speech_active: Whether at least one speech clip is currently live.
    """

    # discord.AudioSource subclasses set is_opus()==False to receive PCM.
    def is_opus(self) -> bool:  # pragma: no cover - trivial
        """Return False so discord.py delivers raw PCM rather than opus.

        Returns:
            bool: Always ``False``; the mixer emits decoded PCM frames and lets
            discord.py handle encoding.
        """
        return False

    def __init__(
        self,
        *,
        ambient_gain: float = 0.18,
        duck_gain: float = 0.06,
        speech_gain: float = 1.0,
        duck_release_ms: int = 400,
    ):
        """Create an empty mixer with the given gain curve.

        No ambient bed and no speech clips exist yet; the mixer immediately
        yields silence frames until :meth:`set_ambient` or :meth:`play_speech`
        is called.

        Args:
            ambient_gain: Resting loudness of the looping idle bed.
            duck_gain: Bed loudness while speech is active underneath.
            speech_gain: Loudness applied to one-shot speech clips.
            duck_release_ms: How long the ambient bed takes to swell back to
                resting gain after speech ends.

        Returns:
            None.
        """
        self._lock = threading.Lock()
        self._ambient: Optional[MixerChild] = None
        self._speech: List[MixerChild] = []
        self._ambient_gain = float(ambient_gain)
        self._duck_gain = float(duck_gain)
        self._speech_gain = float(speech_gain)
        # When speech ends, ramp the ambient back up over this many frames
        # instead of jumping, so the bed swells back smoothly.
        self._duck_release_frames = max(1, duck_release_ms // FRAME_LENGTH_MS)
        self._duck_release_left = 0
        self._closed = False
        # Tracks whether speech is currently active, for external callers that
        # want to avoid double-ducking or know when a reply is mid-flight.
        self._speech_active = False

    # ------------------------------------------------------------------
    # Ambient (idle / "thinking") bed
    # ------------------------------------------------------------------

    def set_ambient(self, pcm: Optional[bytes], *, gain: Optional[float] = None) -> None:
        """Install (or clear, with ``pcm=None``) the looping ambient bed.

        Passing ``pcm=None`` (or empty bytes) removes the ambient entirely.
        When a new bed is installed it fades in over 200 ms and inherits the
        currently effective ambient gain so an active duck is preserved.

        Args:
            pcm: Raw PCM bytes for the bed, or ``None`` to clear it.
            gain: Optional override for the resting ambient gain.

        Returns:
            None.
        """
        with self._lock:
            if gain is not None:
                self._ambient_gain = float(gain)
            if not pcm:
                self._ambient = None
                return
            self._ambient = MixerChild(
                "ambient", pcm, loop=True,
                gain=self._effective_ambient_gain(), fade_in_ms=200,
            )

    def _effective_ambient_gain(self) -> float:
        """Return the ambient gain respecting the current duck state.

        Called under :attr:`_lock`; it exists so that a bed installed while
        speech is still active starts out ducked rather than jumping to full
        resting volume.

        Returns:
            float: ``_duck_gain`` while speech is active, else
            ``_ambient_gain``.
        """
        return self._duck_gain if self._speech_active else self._ambient_gain

    # ------------------------------------------------------------------
    # Speech (TTS replies, verbal acks) layered over the ambient bed
    # ------------------------------------------------------------------

    def play_speech(self, pcm: bytes, *, gain: Optional[float] = None,
                    fade_in_ms: int = 40) -> None:
        """Layer a one-shot speech clip over the ambient bed (ducks ambient).

        Appends the clip to the live speech list, marks speech active, cancels
        any pending duck release, and immediately lowers the ambient bed's gain
        so the speech sits on top of the bed.

        Args:
            pcm: Raw PCM bytes of the speech clip.
            gain: Optional per-clip gain overriding the configured speech gain.
            fade_in_ms: Linear fade-in duration to avoid a start click.

        Returns:
            None.
        """
        if not pcm:
            return
        with self._lock:
            child = MixerChild(
                "speech", pcm, loop=False,
                gain=self._speech_gain if gain is None else float(gain),
                is_speech=True, fade_in_ms=fade_in_ms,
            )
            self._speech.append(child)
            self._speech_active = True
            self._duck_release_left = 0
            if self._ambient is not None:
                self._ambient.gain = self._duck_gain

    @property
    def speech_active(self) -> bool:
        """Whether a speech clip is currently playing.

        Read under the instance lock so a polling caller never observes a
        partially-updated duck state.

        Returns:
            bool: True while at least one speech child is live.
        """
        with self._lock:
            return self._speech_active

    def stop_speech(self) -> None:
        """Drop any in-flight speech immediately and release the duck.

        Clears the speech list and begins a smooth ramp of the ambient bed back
        up to its resting gain.

        Returns:
            None.
        """
        with self._lock:
            self._speech.clear()
            self._begin_duck_release_locked()

    def _begin_duck_release_locked(self) -> None:
        """Mark speech inactive and start the ambient gain ramp-up.

        Must be called while holding ``_lock``.

        Returns:
            None.
        """
        self._speech_active = False
        self._duck_release_left = self._duck_release_frames

    # ------------------------------------------------------------------
    # AudioSource interface -- called from discord.py's sender thread
    # ------------------------------------------------------------------

    def read(self) -> bytes:
        """Return one 20 ms mixed PCM frame (always FRAME_SIZE bytes).

        Returning a non-empty frame keeps discord.py's player alive; we never
        return ``b""`` because that would stop the single underlying stream and
        we want the mixer to run continuously for the lifetime of the
        connection.

        The frame is produced by summing all live speech clips, then summing the
        ambient bed (whose gain is ramped during duck release), clipping to
        int16 range, and emitting raw little-endian bytes.

        Returns:
            bytes: Exactly ``FRAME_SIZE`` mixed PCM bytes, or a silence frame
            when the mixer is closed or no sources are live.
        """
        with self._lock:
            if self._closed:
                return SILENCE_FRAME

            np = _require_numpy()
            acc: "Optional[np.ndarray]" = None

            # Speech children (drop exhausted ones; release duck when last ends)
            if self._speech:
                still_live: List[MixerChild] = []
                for child in self._speech:
                    frame = child.read_frame()
                    if frame is None:
                        continue
                    acc = frame if acc is None else acc + frame
                    still_live.append(child)
                self._speech = still_live
                if not self._speech and self._speech_active:
                    self._begin_duck_release_locked()

            # Ambient bed -- ramp gain back up during duck-release.
            if self._ambient is not None:
                if self._duck_release_left > 0 and not self._speech_active:
                    self._duck_release_left -= 1
                    frac = 1.0 - (self._duck_release_left / self._duck_release_frames)
                    self._ambient.gain = (
                        self._duck_gain
                        + (self._ambient_gain - self._duck_gain) * frac
                    )
                elif not self._speech_active and self._duck_release_left == 0:
                    self._ambient.gain = self._ambient_gain
                amb = self._ambient.read_frame()
                if amb is not None:
                    acc = amb if acc is None else acc + amb

            if acc is None:
                return SILENCE_FRAME

            np.clip(acc, -32768, 32767, out=acc)
            return acc.astype(np.int16).tobytes()

    def cleanup(self) -> None:  # called by discord.py when playback stops
        """Tear down the mixer, releasing all sources and the ambient bed.

        discord.py invokes this when playback stops; afterward :meth:`read`
        yields a silence frame only.

        Returns:
            None.
        """
        with self._lock:
            self._closed = True
            self._ambient = None
            self._speech.clear()


# ----------------------------------------------------------------------
# PCM helpers
# ----------------------------------------------------------------------

def decode_to_pcm(path: str, *, timeout: float = 30.0) -> Optional[bytes]:
    """Decode any audio file to 48 kHz / stereo / s16le PCM via ffmpeg.

    Invokes the system ``ffmpeg`` binary to transcode the input file into raw
    PCM on stdout.  ffmpeg is already a hard requirement of the voice path
    (see ``VoiceReceiver.pcm_to_wav``), so this relies on it being present.

    Args:
        path: Filesystem path of the source audio file.
        timeout: Maximum seconds to wait for ffmpeg before giving up.

    Returns:
        Optional[bytes]: Raw PCM bytes on success, or ``None`` on any failure
        (missing binary, timeout, or non-zero ffmpeg exit code).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", path,
                "-f", "s16le",
                "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS),
                "pipe:1",
            ],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("decode_to_pcm failed for %s: %s", path, e)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ffmpeg decode failed for %s (rc=%d): %s",
            path, proc.returncode, decode_bytes(proc.stderr or b"")[:200],
        )
        return None
    return proc.stdout or None


def synth_ambient_pcm(seconds: float = 4.0) -> bytes:
    """Synthesise a subtle looping ambient bed (no asset file required).

    A soft, slowly-pulsing low pad: two detuned sine partials with a gentle
    tremolo, plus a touch of filtered noise.  Designed to loop seamlessly
    (whole number of cycles, zero-crossing endpoints) and to sit quietly under
    speech.  Mono content is duplicated to stereo.

    Args:
        seconds: Loop length in seconds; base frequencies are quantised to
            whole cycles across this duration so the loop wraps without a click.

    Returns:
        bytes: Stereo s16le PCM bytes of the synthesised ambient bed.
    """
    np = _require_numpy()
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE

    # Choose base frequencies that complete whole cycles over the loop so the
    # wrap point is click-free.
    def _whole_cycle_freq(target: float) -> float:
        """Quantise *target* to a frequency with an integer number of cycles.

        Args:
            target: Desired frequency in Hz.

        Returns:
            float: Nearest frequency whose period divides *seconds* exactly, so
            the loop wrap is sample-accurate.
        """
        cycles = max(1, round(target * seconds))
        return cycles / seconds

    f1 = _whole_cycle_freq(110.0)
    f2 = _whole_cycle_freq(110.5)
    trem = _whole_cycle_freq(0.5)   # ~0.5 Hz tremolo

    pad = (
        0.55 * np.sin(2 * np.pi * f1 * t)
        + 0.45 * np.sin(2 * np.pi * f2 * t)
    )
    tremolo = 0.6 + 0.4 * (0.5 * (1 + np.sin(2 * np.pi * trem * t)))
    signal = pad * tremolo

    # Smooth filtered noise for air, kept very low.
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    kernel = np.ones(64) / 64.0
    noise = np.convolve(noise, kernel, mode="same")
    signal = signal + 0.08 * noise

    # Normalise to a modest peak (mixer applies the real ambient gain on top).
    peak = float(np.max(np.abs(signal))) or 1.0
    signal = (signal / peak) * 0.5

    mono16 = (signal * 32767.0).astype(np.int16)
    stereo16 = np.repeat(mono16[:, None], CHANNELS, axis=1).reshape(-1)
    return stereo16.tobytes()
