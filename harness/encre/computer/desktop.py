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

from encre.logging_config import get_logger

"""Desktop automation session: screenshots, mouse/keyboard, OCR and UI automation.

Wraps ``mss`` (screen capture) and ``pyautogui`` (input) on top of
optional Windows-only capabilities -- Win32 DPI awareness, the
``PrintWindow`` foreground-window capture, the Windows UIAutomation
(UIA) accessibility tree, and OCR via ``Windows.Media.Ocr`` or
``pytesseract``. The session tracks the physical (raw pixel) and
logical (DPI-scaled) screen sizes so coordinates from screenshots and
from the input backend line up on HiDPI displays.
"""

import base64
import functools
import importlib
import io
import logging
import os
import random
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, ClassVar

logger = logging.getLogger("encre.computer.desktop")

_PLATFORM = sys.platform

#: Default long-side cap for model-facing screenshots.  1568 px is the
#: sweet spot for current vision encoders (Claude / GPT / Gemini) --
#: providers downsample larger images anyway, so sending full 4K
#: captures wastes context without adding detail.
DEFAULT_SCREENSHOT_MAX_LONG_SIDE = 1568

#: Default JPEG quality for model-facing screenshots.  Set to 0 to
#: keep the legacy lossless PNG encoding.
DEFAULT_SCREENSHOT_JPEG_QUALITY = 85


def _enable_windows_dpi_awareness() -> None:
    """Make the current process per-monitor DPI aware on Windows so
    screenshot pixels and mouse coordinates use the same coordinate system.

    Without this, on a HiDPI display mss returns physical pixels while
    pyautogui returns logical (scaled) pixels, and clicks miss their
    targets by the scale factor.
    """
    if _PLATFORM != "win32":
        return
    try:
        import ctypes

        # PROCESS_PER_MONITOR_DPI_AWARE = 2 -- Win 8.1+. Fall back to
        # SetProcessDPIAware for older systems.
        shcore = ctypes.windll.shcore
        try:
            shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        logger.debug("exception suppressed", exc_info=True)


_enable_windows_dpi_awareness()


# ---------------------------------------------------------------------------
# Platform keyboard backends
# ---------------------------------------------------------------------------

#: pyautogui's ``typewrite`` / ``press`` feed keystrokes through the active
#: input method: with a CJK IME open (the default for most of our users)
#: typed characters become *composition text* and a following Enter only
#: commits the composition instead of reaching the target app.  The native
#: backends below inject keys below the IME layer instead:
#:
#: * Windows: ``SendInput`` -- ``KEYEVENTF_UNICODE`` (VK_PACKET) for text,
#:   VK + scan code for control keys.
#: * macOS:   ``CGEventKeyboardSetUnicodeString`` for text, HID virtual
#:   key codes for control keys (Quartz ships with pyautogui on Darwin).
#: * Linux:   pyautogui (XTEST) for ASCII, ``xdotool type`` / clipboard
#:   paste for the rest; Wayland goes through ``wdotool`` (see the
#:   ``_wayland_*`` helpers).

_WIN_VK: dict[str, tuple[int, bool, int]] = {
    # name -> (virtual key code, extended-key flag, shift-state mask)
    "enter": (0x0D, False, 0), "return": (0x0D, False, 0),
    "tab": (0x09, False, 0),
    "esc": (0x1B, False, 0), "escape": (0x1B, False, 0),
    "space": (0x20, False, 0),
    "backspace": (0x08, False, 0),
    "insert": (0x2D, True, 0), "delete": (0x2E, True, 0), "del": (0x2E, True, 0),
    "home": (0x24, True, 0), "end": (0x23, True, 0),
    "pageup": (0x21, True, 0), "pagedown": (0x22, True, 0),
    "up": (0x26, True, 0), "arrowup": (0x26, True, 0),
    "down": (0x28, True, 0), "arrowdown": (0x28, True, 0),
    "left": (0x25, True, 0), "arrowleft": (0x25, True, 0),
    "right": (0x27, True, 0), "arrowright": (0x27, True, 0),
    "shift": (0x10, False, 0),
    "ctrl": (0x11, False, 0), "control": (0x11, False, 0),
    "alt": (0x12, False, 0), "option": (0x12, False, 0),
    "win": (0x5B, True, 0), "meta": (0x5B, True, 0),
    "cmd": (0x5B, True, 0), "command": (0x5B, True, 0), "super": (0x5B, True, 0),
    "capslock": (0x14, False, 0),
    "menu": (0x5D, True, 0), "apps": (0x5D, True, 0),
    "printscreen": (0x2C, False, 0),
}
for _fkey in range(1, 25):
    _WIN_VK[f"f{_fkey}"] = (0x6F + _fkey, False, 0)

_WIN_KEYEVENTF_EXTENDEDKEY = 0x0001
_WIN_KEYEVENTF_KEYUP = 0x0002
_WIN_KEYEVENTF_UNICODE = 0x0004


def _win_resolve_key(key: str) -> tuple[int, bool, int] | None:
    """Map a friendly key name (or single character) to its virtual key.

    Returns ``(vk, extended, shift_mask)`` or ``None`` when unknown --
    callers fall back to pyautogui's broader key table in that case.
    """
    name = key.strip().lower()
    if name in _WIN_VK:
        return _WIN_VK[name]
    if len(name) == 1:
        ch = ord(name)
        if 0x30 <= ch <= 0x39 or 0x41 <= ch <= 0x5A or 0x61 <= ch <= 0x7A:
            return (ord(name.upper()), False, 0)
        import ctypes

        vk_scan = ctypes.windll.user32.VkKeyScanW(ch)
        if vk_scan != -1 and (vk_scan & 0xFF):
            state = (vk_scan >> 8) & 0x07
            if state & 0x06:  # ctrl/alt combos are not expressible here
                return None
            return (vk_scan & 0xFF, False, state & 0x01)
    return None


@functools.lru_cache(maxsize=1)
def _win_input_structs() -> tuple[type, type, int]:
    """Build (and cache) the ctypes structures used by ``SendInput``.

    The union must include MOUSEINPUT: ``SendInput`` validates ``cbSize``
    against the real ``sizeof(INPUT)`` (40 bytes on x64 -- a KEYBDINPUT-
    only union yields 32 and the call is silently rejected).
    """
    import ctypes
    from ctypes import wintypes

    class KbdInput(ctypes.Structure):
        _fields_: ClassVar = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MouseInput(ctypes.Structure):
        _fields_: ClassVar = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class InputUnion(ctypes.Union):
        _fields_: ClassVar = [("mi", MouseInput), ("ki", KbdInput)]

    class Input(ctypes.Structure):
        _fields_: ClassVar = [("type", wintypes.DWORD), ("union", InputUnion)]

    return KbdInput, Input, ctypes.sizeof(Input)


def _win_send_raw(vk: int, scan: int, flags: int) -> None:
    """Inject one keyboard event; ``SendInput`` must confirm delivery."""
    import ctypes

    kbd, inp_cls, inp_size = _win_input_structs()
    event = inp_cls()
    event.type = 1  # INPUT_KEYBOARD
    event.union.ki = kbd(vk, scan, flags, 0, 0)
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), inp_size)
    if sent != 1:
        raise RuntimeError(
            f"SendInput rejected vk=0x{vk:02X} scan=0x{scan:02X} flags=0x{flags:02X}"
        )


def _win_send_vk(vk: int, extended: bool = False, up: bool = False) -> None:
    """Inject one virtual-key press/release with its scan code."""
    import ctypes

    flags = _WIN_KEYEVENTF_KEYUP if up else 0
    if extended:
        flags |= _WIN_KEYEVENTF_EXTENDEDKEY
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
    _win_send_raw(vk, scan, flags)


def _win_tap_vk(vk: int, extended: bool = False) -> None:
    """Press and release one key with a natural hold time."""
    _win_send_vk(vk, extended)
    time.sleep(random.uniform(0.03, 0.08))
    _win_send_vk(vk, extended, up=True)


def _win_type_text(text: str, interval: float = 0.02) -> None:
    """Type through ``KEYEVENTF_UNICODE``: characters bypass the IME (no
    composition is opened, so a following Enter reaches the app) and the
    full Unicode range is supported -- including CJK text that pyautogui's
    ``typewrite`` silently cannot type."""
    for ch in text:
        if ch == "\n":
            _win_tap_vk(0x0D)
        elif ch == "\t":
            _win_tap_vk(0x09)
        elif ch == "\r":
            continue
        else:
            codes = [ord(ch)]
            if codes[0] > 0xFFFF:  # split astral chars into a UTF-16 pair
                code = codes[0] - 0x10000
                codes = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
            for code in codes:
                _win_send_raw(0, code, _WIN_KEYEVENTF_UNICODE)
                _win_send_raw(0, code, _WIN_KEYEVENTF_UNICODE | _WIN_KEYEVENTF_KEYUP)
        time.sleep(interval)


def _win_press_key(key: str) -> None:
    """Press one control key natively; unknown names defer to pyautogui."""
    resolved = _win_resolve_key(key)
    if resolved is None:
        import pyautogui

        pyautogui.press(key)
        return
    vk, extended, shift = resolved
    if shift:
        _win_send_vk(0x10)
    _win_tap_vk(vk, extended)
    if shift:
        _win_send_vk(0x10, up=True)


def _win_hotkey(keys: list[str]) -> None:
    """Press a combo natively: all downs in order, then ups in reverse."""
    resolved = [_win_resolve_key(k) for k in keys]
    if any(item is None for item in resolved):
        import pyautogui

        pyautogui.hotkey(*keys)  # unknown name in the combo: defer wholesale
        return
    for vk, extended, shift in resolved:
        if shift:
            _win_send_vk(0x10)
        _win_send_vk(vk, extended)
        time.sleep(0.015)
    time.sleep(0.03)
    for vk, extended, shift in reversed(resolved):
        _win_send_vk(vk, extended, up=True)
        if shift:
            _win_send_vk(0x10, up=True)


_MAC_VK: dict[str, int] = {
    # HIToolbox virtual key codes (Events.h).
    "enter": 0x24, "return": 0x24,
    "tab": 0x30, "space": 0x31,
    "backspace": 0x33, "esc": 0x35, "escape": 0x35,
    "delete": 0x75, "del": 0x75, "forwarddelete": 0x75,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "up": 0x7E, "arrowup": 0x7E, "down": 0x7D, "arrowdown": 0x7D,
    "left": 0x7B, "arrowleft": 0x7B, "right": 0x7C, "arrowright": 0x7C,
    "shift": 0x38, "ctrl": 0x3B, "control": 0x3B,
    "alt": 0x3A, "option": 0x3A,
    "win": 0x37, "meta": 0x37, "cmd": 0x37, "command": 0x37, "super": 0x37,
    "capslock": 0x39, "help": 0x72,
}
for _fkey, _vk in enumerate(
    (
        0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64,
        0x65, 0x6D, 0x67, 0x6F, 0x69, 0x6B, 0x71, 0x6A,
        0x40, 0x4F, 0x50, 0x5A,
    ),
    start=1,
):
    _MAC_VK[f"f{_fkey}"] = _vk


def _mac_send_vk(vk: int, up: bool = False) -> None:
    """Post one virtual-key event at the HID event tap."""
    import Quartz

    event = Quartz.CGEventCreateKeyboardEvent(None, vk, not up)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _mac_type_text(text: str, interval: float = 0.02) -> None:
    """Type through ``CGEventKeyboardSetUnicodeString``: characters arrive
    below the input method (no composition) and cover the full Unicode
    range, which pyautogui's ``typewrite`` cannot on macOS."""
    import Quartz

    for ch in text:
        if ch == "\n":
            _mac_send_vk(0x24)
        elif ch == "\t":
            _mac_send_vk(0x30)
        elif ch == "\r":
            continue
        else:
            for down in (True, False):
                # kVK_None: the event carries no key code, only its text.
                event = Quartz.CGEventCreateKeyboardEvent(None, 0xFF, down)
                Quartz.CGEventKeyboardSetUnicodeString(event, 1, ch)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(interval)


def _mac_press_key(key: str) -> None:
    """Press one control key natively; unknown names defer to pyautogui."""
    name = key.strip().lower()
    if name in _MAC_VK:
        _mac_send_vk(_MAC_VK[name])
        time.sleep(random.uniform(0.03, 0.08))
        _mac_send_vk(_MAC_VK[name], up=True)
    elif len(key) == 1:
        _mac_type_text(key, interval=0.0)
    else:
        import pyautogui

        pyautogui.press(key)


def _mac_hotkey(keys: list[str]) -> None:
    """Press a combo natively: all downs in order, then ups in reverse."""
    import Quartz

    resolved = [_MAC_VK.get(k.strip().lower()) for k in keys]
    if any(vk is None for vk in resolved):
        import pyautogui

        pyautogui.hotkey(*keys)  # unknown name in the combo: defer wholesale
        return
    for vk in resolved:
        event = Quartz.CGEventCreateKeyboardEvent(None, vk, True)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.015)
    time.sleep(0.03)
    for vk in reversed(resolved):
        event = Quartz.CGEventCreateKeyboardEvent(None, vk, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _x11_type_text(text: str, interval: float = 0.02) -> None:
    """X11 typing: pyautogui (XTEST) covers printable ASCII; anything else
    goes through ``xdotool type`` (XTEST with keysym lookup) and falls
    back to a clipboard paste when xdotool is absent."""
    if text.isascii():
        import pyautogui

        pyautogui.typewrite(text, interval=interval)
        return
    import subprocess

    try:
        proc = subprocess.run(
            ["xdotool", "type", "--delay", str(int(interval * 1000)), "--", text],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        logger.debug("exception suppressed", exc_info=True)
    # Clipboard fallback: pyperclip ships with this package.  Restore the
    # previous clipboard contents afterwards when possible.
    import pyautogui
    import pyperclip

    previous: str | None = None
    with suppress(Exception):
        previous = pyperclip.paste()
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)
    if previous is not None:
        with suppress(Exception):
            pyperclip.copy(previous)


@dataclass
class DesktopScreenState:
    """Snapshot of the desktop: screenshot, dimensions, DPI and cursor pos.

    Tracks both physical (raw pixel) and logical (DPI-scaled) sizes so
    coordinates from screenshots and from the input backend line up.

    On Windows, when the foreground window can be captured via
    ``PrintWindow``, the ``active_window_*`` fields carry the
    window-specific bitmap so callers see the real pixels even for
    DirectX / Direct2D rendered content (Chromium, Electron, WPF, etc.).
    """

    width: int = 0
    height: int = 0
    screenshot_b64: str = ""
    cursor_x: int = 0
    cursor_y: int = 0
    platform: str = _PLATFORM
    dpi_scale_x: float = 1.0
    dpi_scale_y: float = 1.0
    logical_width: int = 0
    logical_height: int = 0

    # Active-window PrintWindow capture (Windows only).
    # These are empty / zero when no foreground window was captured.
    active_window_b64: str = ""
    active_window_left: int = 0
    active_window_top: int = 0
    active_window_width: int = 0
    active_window_height: int = 0

    # Encoded-image geometry.  When the screenshot is downscaled for
    # model consumption, ``image_width`` / ``image_height`` are the
    # encoded image's pixel size and ``image_scale`` maps image px ->
    # OS physical px (``physical = image * image_scale``).  1.0 means
    # the image is at native resolution.
    image_width: int = 0
    image_height: int = 0
    image_scale: float = 1.0


@dataclass
class ActiveWindowCapture:
    """Result of a foreground-window ``PrintWindow`` capture.

    ``png_bytes`` is the captured bitmap encoded as PNG. ``left`` /
    ``top`` are the window's screen position in physical pixels so
    callers can translate OCR bounding boxes (which are relative to
    the captured image) back into screen coordinates for clicks.
    """

    png_bytes: bytes
    left: int
    top: int
    width: int
    height: int


@dataclass
class DesktopLocateResult:
    """Result of a template/image match search on the screen."""

    found: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    confidence: float = 0.0


class EncreDesktopSession:
    """Desktop automation session: screenshots, mouse/keyboard, OCR, UIA."""

    def __init__(self):
        """Initialise idle tracking, cached screen state and DPI scale cache."""
        self._last_used = time.time()
        self._state = DesktopScreenState()
        self._dpi_x: float | None = None
        self._dpi_y: float | None = None

    def _check_mss(self) -> bool:
        """Return True if the ``mss`` screen-capture library is importable."""
        return importlib.util.find_spec("mss") is not None

    def _check_pyautogui(self) -> bool:
        """Return True if the ``pyautogui`` input library is importable."""
        return importlib.util.find_spec("pyautogui") is not None

    def _check_pillow(self) -> bool:
        """Return True if the ``PIL`` (Pillow) imaging library is importable."""
        return importlib.util.find_spec("PIL") is not None

    def _encode_screenshot(
        self,
        pil_img: Any,
        max_long_side: int = DEFAULT_SCREENSHOT_MAX_LONG_SIDE,
        jpeg_quality: int = DEFAULT_SCREENSHOT_JPEG_QUALITY,
    ) -> tuple[str, int, int]:
        """Encode a PIL screenshot to base64, downscaled and compressed.

        Returns ``(b64, out_w, out_h)``.  ``out_w`` / ``out_h`` are the
        encoded image's pixel size; callers should store
        ``image_scale = physical_w / out_w`` so coordinates read off
        the image map back to OS pixels.  ``max_long_side <= 0`` or
        ``jpeg_quality <= 0`` keep the legacy full-size PNG behaviour.
        """
        out_img = pil_img
        if max_long_side and max_long_side > 0:
            long_side = max(pil_img.width, pil_img.height)
            if long_side > max_long_side:
                ratio = max_long_side / long_side
                from PIL import Image

                out_img = pil_img.resize(
                    (max(1, round(pil_img.width * ratio)), max(1, round(pil_img.height * ratio))),
                    Image.LANCZOS,
                )
        buf = io.BytesIO()
        if jpeg_quality and jpeg_quality > 0:
            out_img.save(buf, format="JPEG", quality=jpeg_quality)
        else:
            out_img.save(buf, format="PNG")
        return (
            base64.b64encode(buf.getvalue()).decode("utf-8"),
            out_img.width,
            out_img.height,
        )

    def screenshot(self) -> DesktopScreenState:
        """Capture the primary monitor and update/return cached screen state.

        Backends by platform
        --------------------
        * **Windows / macOS / Linux X11** -- ``mss`` (fast, cross-platform
          via OS compositor / X11 shm).
        * **Linux Wayland** -- ``grim`` (``wlr-screencopy`` protocol,
          requires a compositor that supports it, e.g. Sway, Hyprland).
        * **Active window** -- platform-specific (Windows PrintWindow,
          macOS screencapture, Linux hyprctl/swaymsg/xdotool).
        """
        physical_w = physical_h = 0
        pil_img = None
        b64 = ""
        _mss_ok = False

        # ---- Try mss first (Windows, macOS, Linux X11) ----
        if self._check_mss() and self._check_pillow():
            try:
                import mss

                with mss.mss() as sct:
                    monitor = sct.monitors[1]
                    img = sct.grab(monitor)
                self._ensure_pillow()
                from PIL import Image

                pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                physical_w = monitor["width"]
                physical_h = monitor["height"]
                _mss_ok = True
            except Exception:
                _mss_ok = False

        # ---- Fallback: grim on Linux Wayland ----
        if not _mss_ok and _PLATFORM.startswith("linux"):
            try:
                import subprocess
                import tempfile

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    tmp_path = f.name
                r = subprocess.run(
                    ["grim", tmp_path],
                    capture_output=True,
                    timeout=10,
                )
                if r.returncode == 0:
                    with open(tmp_path, "rb") as f:
                        grim_bytes = f.read()
                    with suppress(OSError):
                        os.unlink(tmp_path)
                    if grim_bytes:
                        from PIL import Image

                        pil_img = Image.open(io.BytesIO(grim_bytes))
                        physical_w, physical_h = pil_img.size
                        _mss_ok = True
                else:
                    with suppress(OSError):
                        os.unlink(tmp_path)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)

        if not _mss_ok:
            raise RuntimeError(
                "No screenshot backend available. On X11 install mss (pip install mss). On Wayland install grim (system package)."
            )

        b64, img_w, img_h = self._encode_screenshot(pil_img)

        logical_w, logical_h = self._logical_size_fallback(physical_w, physical_h)
        scale_x, scale_y = self._compute_scale(physical_w, physical_h, logical_w, logical_h)

        self._state.width = physical_w
        self._state.height = physical_h
        self._state.logical_width = logical_w
        self._state.logical_height = logical_h
        self._state.dpi_scale_x = scale_x
        self._state.dpi_scale_y = scale_y
        self._state.screenshot_b64 = b64
        self._state.image_width = img_w
        self._state.image_height = img_h
        self._state.image_scale = (physical_w / img_w) if img_w else 1.0

        # On Windows, try to capture the foreground window via
        # PrintWindow -- this catches DirectX / Direct2D rendered
        # content that mss (which uses the OS compositor) may miss.
        aw = self._capture_active_window_png()
        if aw is not None:
            self._state.active_window_b64 = base64.b64encode(aw.png_bytes).decode("utf-8")
            self._state.active_window_left = aw.left
            self._state.active_window_top = aw.top
            self._state.active_window_width = aw.width
            self._state.active_window_height = aw.height

        self._last_used = time.time()
        return self._state

    def _logical_size_fallback(self, physical_w: int, physical_h: int) -> tuple[int, int]:
        """Return logical (pyautogui-visible) screen size."""
        if self._check_pyautogui():
            try:
                import pyautogui

                w, h = pyautogui.size()
                return int(w), int(h)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        return physical_w, physical_h

    def _compute_scale(self, phys_w: int, phys_h: int, log_w: int, log_h: int) -> tuple[float, float]:
        """Compute and cache physical/logical DPI scale factors."""
        sx = phys_w / log_w if log_w else 1.0
        sy = phys_h / log_h if log_h else 1.0
        self._dpi_x, self._dpi_y = sx, sy
        return sx, sy

    def _to_logical(self, x: int, y: int, coord_space: str) -> tuple[int, int]:
        """Convert a coordinate into pyautogui's logical coordinate system.

        The ``physical`` coordinate space is defined as *pixels of the
        last screenshot*.  When the screenshot was downscaled for model
        consumption the image px -> OS px factor is applied first, so
        coordinates read off a model-facing screenshot stay accurate.
        """
        if coord_space == "logical":
            return x, y
        if coord_space == "physical":
            sx = self._dpi_x or 1.0
            sy = self._dpi_y or 1.0
            img_scale = self._state.image_scale or 1.0
            return round(x * img_scale / sx), round(y * img_scale / sy)
        # auto: if dpi scale != 1 and the value looks "too big" for the
        # logical screen, assume it's physical.
        sx = self._dpi_x
        sy = self._dpi_y
        if sx is None or sy is None:
            return x, y
        if abs(sx - 1.0) < 1e-3 and abs(sy - 1.0) < 1e-3:
            return x, y
        try:
            import pyautogui

            lw, lh = pyautogui.size()
            if x > lw or y > lh:
                return round(x / sx), round(y / sy)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        return x, y

    def get_screen_size(self) -> dict[str, int]:
        """Return the screen size as ``{"width": w, "height": h}``."""
        if self._check_pyautogui():
            import pyautogui

            w, h = pyautogui.size()
            self._state.width = w
            self._state.height = h
        elif self._check_mss():
            import mss

            with mss.mss() as sct:
                m = sct.monitors[1]
                self._state.width = m["width"]
                self._state.height = m["height"]
        return {"width": self._state.width, "height": self._state.height}

    def get_cursor_position(self) -> dict[str, int]:
        """Return the current mouse cursor position as ``{"x": x, "y": y}``."""
        if not self._check_pyautogui():
            return {"x": 0, "y": 0}
        import pyautogui

        x, y = pyautogui.position()
        self._state.cursor_x = x
        self._state.cursor_y = y
        self._last_used = time.time()
        return {"x": x, "y": y}

    # ------------------------------------------------------------------
    # Linux Wayland input backend (via wdotool CLI)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_wayland() -> bool:
        """Return True if the session is running under Wayland."""
        import os

        return _PLATFORM.startswith("linux") and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"

    @staticmethod
    def _wayland_call(*args: str, timeout: int = 5) -> bool:
        """Run ``wdotool`` with the given args. Returns True on success."""
        import subprocess

        try:
            r = subprocess.run(
                ["wdotool", *args],
                capture_output=True,
                timeout=timeout,
            )
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _wayland_mouse_move(x: int, y: int) -> None:
        """Absolute mouse move via ``wdotool mousemove``."""
        EncreDesktopSession._wayland_call("mousemove", str(x), str(y))

    @staticmethod
    def _wayland_click(button: str = "left") -> None:
        """Mouse click via ``wdotool click``."""
        btn_map = {"left": "1", "middle": "2", "right": "3"}
        wbtn = btn_map.get(button, "1")
        EncreDesktopSession._wayland_call("click", wbtn)

    @staticmethod
    def _wayland_type(text: str) -> None:
        """Type text via ``wdotool type``."""
        # Escape single quotes for shell safety.
        safe = text.replace("'", "'\\''")
        import subprocess

        with suppress(Exception):
            subprocess.run(
                ["wdotool", "type", safe],
                capture_output=True,
                timeout=30,
            )

    @staticmethod
    def _wayland_key(key: str) -> None:
        """Press and release a key via ``wdotool key``."""
        EncreDesktopSession._wayland_call("key", key)

    @staticmethod
    def _wayland_hotkey(keys: list[str]) -> None:
        """Press key combo via ``wdotool key`` with ``+`` separator."""
        EncreDesktopSession._wayland_call("key", "+".join(keys))

    @staticmethod
    def _wayland_scroll(clicks: int) -> None:
        """Scroll via ``wdotool scroll`` (positive=down, negative=up)."""
        EncreDesktopSession._wayland_call("scroll", "0", str(clicks))

    @staticmethod
    def _wayland_drag(_x1: int, _y1: int, x2: int, y2: int) -> None:
        """Drag: press mouse, move, release via ``wdotool`` subcommands."""
        # wdotool doesn't have a drag primitive, so we sequence:
        # mousedown 1 → mousemove x2 y2 → mouseup 1
        EncreDesktopSession._wayland_call("mousedown", "1")
        EncreDesktopSession._wayland_call("mousemove", str(x2), str(y2))
        EncreDesktopSession._wayland_call("mouseup", "1")

    # ------------------------------------------------------------------
    # Input methods (cross-platform dispatch)
    # ------------------------------------------------------------------

    #: Pointer sampling interval while moving (min/max seconds), mirroring
    #: the browser backend's human-like trajectory pacing.
    _MOVE_FRAME_MS = (6, 14)

    @staticmethod
    def _smoothstep(t: float) -> float:
        """S-curve easing (3t^2 - 2t^3): slow start, fast middle, soft stop."""
        return t * t * (3.0 - 2.0 * t)

    def _human_move_to(self, lx: int, ly: int, hold_button: str | None = None, frame_ms: tuple[float, float] | None = None) -> None:
        """Move the real OS cursor to logical (lx, ly) along a human-like
        trajectory, mirroring the browser backend's ``_human_move_to``.

        Quadratic bezier with a random perpendicular bow (never a straight
        laser line), smoothstep velocity profile, randomized frame pacing,
        sub-pixel hand jitter and a ~20% chance of overshoot-then-correct.
        ``hold_button`` keeps a button pressed along the way (dragging);
        ``frame_ms`` overrides the per-frame pacing (used by drag to honour
        its ``duration`` argument).
        """
        import pyautogui

        x0, y0 = pyautogui.position()
        x, y = float(lx), float(ly)
        dx, dy = x - x0, y - y0
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 2:
            pyautogui.moveTo(lx, ly, _pause=False)
            return

        steps = max(6, min(28, int(dist / 22)))
        pacing = frame_ms if frame_ms is not None else self._MOVE_FRAME_MS
        # Control point: midpoint nudged along the perpendicular; the bow
        # scales with distance (capped) and flips side at random.
        bow = random.uniform(-0.18, 0.18) * min(dist, 600)
        ux, uy = -dy / dist, dx / dist
        cx = (x0 + x) / 2.0 + ux * bow
        cy = (y0 + y) / 2.0 + uy * bow

        # Never overshoot while a button is held -- dragging past the drop
        # target changes what gets selected.
        overshoot = hold_button is None and random.random() < 0.2
        tx, ty = x, y
        if overshoot:
            tx = x + dx / dist * random.uniform(2.0, 6.0)
            ty = y + dy / dist * random.uniform(2.0, 6.0)

        for i in range(1, steps + 1):
            e = self._smoothstep(i / steps)
            u = 1.0 - e
            # Hand jitter on every frame except the final one: the pointer
            # must settle exactly on the target.
            jx = 0.0 if i == steps else random.uniform(-0.6, 0.6)
            jy = 0.0 if i == steps else random.uniform(-0.6, 0.6)
            px = round(u * u * x0 + 2.0 * u * e * cx + e * e * tx + jx)
            py = round(u * u * y0 + 2.0 * u * e * cy + e * e * ty + jy)
            pyautogui.moveTo(px, py, _pause=False)
            time.sleep(random.uniform(*pacing) / 1000.0)

        if overshoot:
            pyautogui.moveTo(int(x), int(y), _pause=False)
            time.sleep(random.uniform(0.02, 0.05))

    def _aiming_pause(self) -> None:
        """A human hand stops for a beat before it presses."""
        time.sleep(random.uniform(0.04, 0.12))

    def move_mouse(self, x: int, y: int, coord_space: str = "auto") -> dict[str, int]:
        """Move the mouse to (x, y), converting from the given coord space."""
        lx, ly = self._to_logical(int(x), int(y), coord_space)
        if self._is_wayland():
            self._wayland_mouse_move(lx, ly)
        else:
            if not self._check_pyautogui():
                raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
            self._human_move_to(lx, ly)
        self._state.cursor_x = lx
        self._state.cursor_y = ly
        self._last_used = time.time()
        return {"x": lx, "y": ly}

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        coord_space: str = "auto",
    ) -> dict[str, Any]:
        """Click at (x, y) with the given button, or at the current position."""
        if self._is_wayland():
            if x is not None and y is not None:
                lx, ly = self._to_logical(int(x), int(y), coord_space)
                self._wayland_mouse_move(lx, ly)
            self._wayland_click(button)
            pos_x, pos_y = lx if x is not None else 0, ly if y is not None else 0
            self._state.cursor_x = pos_x
            self._state.cursor_y = pos_y
            self._last_used = time.time()
            return {"action": "click", "button": button, "x": pos_x, "y": pos_y}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        import pyautogui

        if x is not None and y is not None:
            lx, ly = self._to_logical(int(x), int(y), coord_space)
            # Human-like: glide to the target, pause to aim, then press.
            self._human_move_to(lx, ly)
            self._aiming_pause()
            pyautogui.mouseDown(button=button, _pause=False)
            pyautogui.mouseUp(button=button, _pause=False)
        else:
            pyautogui.mouseDown(button=button, _pause=False)
            pyautogui.mouseUp(button=button, _pause=False)
        pos = pyautogui.position()
        self._state.cursor_x = int(pos[0])
        self._state.cursor_y = int(pos[1])
        self._last_used = time.time()
        return {"action": "click", "button": button, "x": int(pos[0]), "y": int(pos[1])}

    def double_click(self, x: int | None = None, y: int | None = None, coord_space: str = "auto") -> dict[str, Any]:
        """Double-click at (x, y), or at the current cursor position."""
        if self._is_wayland():
            if x is not None and y is not None:
                lx, ly = self._to_logical(int(x), int(y), coord_space)
                self._wayland_mouse_move(lx, ly)
            self._wayland_click("left")
            self._wayland_click("left")
            self._last_used = time.time()
            return {"action": "double_click", "x": lx if x is not None else 0, "y": ly if y is not None else 0}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        import pyautogui

        if x is not None and y is not None:
            lx, ly = self._to_logical(int(x), int(y), coord_space)
            self._human_move_to(lx, ly)
        self._aiming_pause()
        # Two press/release pairs with a fast inter-click gap so the OS
        # registers them as one double-click event.
        for i in range(2):
            if i:
                time.sleep(random.uniform(0.02, 0.06))
            pyautogui.mouseDown(button="left", _pause=False)
            pyautogui.mouseUp(button="left", _pause=False)
        pos = pyautogui.position()
        self._state.cursor_x = int(pos[0])
        self._state.cursor_y = int(pos[1])
        self._last_used = time.time()
        return {"action": "double_click", "x": int(pos[0]), "y": int(pos[1])}

    def right_click(self, x: int | None = None, y: int | None = None, coord_space: str = "auto") -> dict[str, Any]:
        """Right-click at (x, y), or at the current cursor position."""
        if self._is_wayland():
            if x is not None and y is not None:
                lx, ly = self._to_logical(int(x), int(y), coord_space)
                self._wayland_mouse_move(lx, ly)
            self._wayland_click("right")
            self._last_used = time.time()
            return {"action": "right_click", "x": lx if x is not None else 0, "y": ly if y is not None else 0}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        import pyautogui

        if x is not None and y is not None:
            lx, ly = self._to_logical(int(x), int(y), coord_space)
            self._human_move_to(lx, ly)
            self._aiming_pause()
            pyautogui.mouseDown(button="right", _pause=False)
            pyautogui.mouseUp(button="right", _pause=False)
        else:
            pyautogui.mouseDown(button="right", _pause=False)
            pyautogui.mouseUp(button="right", _pause=False)
        pos = pyautogui.position()
        self._state.cursor_x = int(pos[0])
        self._state.cursor_y = int(pos[1])
        self._last_used = time.time()
        return {"action": "right_click", "x": int(pos[0]), "y": int(pos[1])}

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5, coord_space: str = "auto") -> dict[str, Any]:
        """Press at (x1, y1) and drag to (x2, y2) over ``duration`` seconds."""
        lx1, ly1 = self._to_logical(int(x1), int(y1), coord_space)
        lx2, ly2 = self._to_logical(int(x2), int(y2), coord_space)
        if self._is_wayland():
            self._wayland_drag(lx1, ly1, lx2, ly2)
            self._last_used = time.time()
            return {"action": "drag", "from": {"x": lx1, "y": ly1}, "to": {"x": lx2, "y": ly2}}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        import pyautogui

        # Glide to the start, press, then walk the bezier path with the
        # button held -- no straight laser-line drag. ``duration`` sets the
        # pace of the held-button walk.
        self._human_move_to(lx1, ly1)
        self._aiming_pause()
        pyautogui.mouseDown(button="left", _pause=False)
        hold_ms = max(4.0, min(40.0, duration * 1000.0 / 20.0))
        self._human_move_to(lx2, ly2, hold_button="left", frame_ms=(hold_ms, hold_ms * 1.6))
        pyautogui.mouseUp(button="left", _pause=False)
        pos = pyautogui.position()
        self._state.cursor_x = int(pos[0])
        self._state.cursor_y = int(pos[1])
        self._last_used = time.time()
        return {"action": "drag", "from": {"x": lx1, "y": ly1}, "to": {"x": lx2, "y": ly2}}

    def type_text(self, text: str, interval: float = 0.02) -> dict[str, Any]:
        """Type ``text`` character by character with an inter-key ``interval``."""
        if self._is_wayland():
            self._wayland_type(text)
            self._last_used = time.time()
            return {"action": "type", "text": text[:200]}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")

        if _PLATFORM == "win32":
            _win_type_text(text, interval)
        elif _PLATFORM == "darwin":
            _mac_type_text(text, interval)
        else:
            _x11_type_text(text, interval)
        self._last_used = time.time()
        return {"action": "type", "text": text[:200]}

    def press_key(self, key: str) -> dict[str, Any]:
        """Press and release a single named key (e.g. "enter", "tab")."""
        if self._is_wayland():
            self._wayland_key(key)
            self._last_used = time.time()
            return {"action": "press_key", "key": key}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")

        if _PLATFORM == "win32":
            _win_press_key(key)
        elif _PLATFORM == "darwin":
            _mac_press_key(key)
        else:
            import pyautogui

            pyautogui.press(key)
        self._last_used = time.time()
        return {"action": "press_key", "key": key}

    def hotkey(self, keys: list[str]) -> dict[str, Any]:
        """Press a key combination (e.g. ``["ctrl", "c"]``) simultaneously."""
        if self._is_wayland():
            self._wayland_hotkey(keys)
            self._last_used = time.time()
            return {"action": "hotkey", "keys": "+".join(keys)}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")

        if _PLATFORM == "win32":
            _win_hotkey(keys)
        elif _PLATFORM == "darwin":
            _mac_hotkey(keys)
        else:
            import pyautogui

            pyautogui.hotkey(*keys)
        self._last_used = time.time()
        return {"action": "hotkey", "keys": "+".join(keys)}

    def scroll(self, clicks: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        """Scroll ``clicks`` notches (positive=up), optionally at (x, y)."""
        if self._is_wayland():
            self._wayland_scroll(clicks)
            self._last_used = time.time()
            return {"action": "scroll", "clicks": clicks}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        import pyautogui

        if x is not None and y is not None:
            pyautogui.scroll(clicks, x, y)
        else:
            pyautogui.scroll(clicks)
        self._last_used = time.time()
        return {"action": "scroll", "clicks": clicks}

    def locate_on_screen(self, image_b64: str, confidence: float = 0.9) -> DesktopLocateResult:
        """Locate a base64 PNG template on screen and return its match result."""
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        if not self._check_pillow():
            raise RuntimeError("Pillow not installed. Run: pip install pillow") from None
        import pyautogui
        from PIL import Image

        try:
            img_data = base64.b64decode(image_b64)
            needle = Image.open(io.BytesIO(img_data))
        except Exception:
            return DesktopLocateResult(found=False)

        needle_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                needle.save(f, format="PNG")
                needle_path = f.name

            location = pyautogui.locateOnScreen(needle_path, confidence=confidence)
            if location is not None:
                x, y = pyautogui.center(location)
                return DesktopLocateResult(
                    found=True,
                    x=int(x),
                    y=int(y),
                    width=int(location.width),
                    height=int(location.height),
                    confidence=confidence,
                )
            return DesktopLocateResult(found=False)
        except Exception:
            return DesktopLocateResult(found=False)
        finally:
            if needle_path:
                with suppress(OSError):
                    os.unlink(needle_path)

    def screenshot_with_cursor(self) -> DesktopScreenState:
        """Capture a screenshot and annotate the current cursor position."""
        state = self.screenshot()
        if self._check_pyautogui():
            import pyautogui

            x, y = pyautogui.position()
            state.cursor_x = int(x)
            state.cursor_y = int(y)
        return state

    def _capture_active_window_png(self) -> ActiveWindowCapture | None:
        """Capture the foreground window's pixels as a PNG.

        Platform backends
        -----------------
        * **Windows** -- Win32 ``PrintWindow`` API (handles DirectX /
          Direct2D surfaces such as Chromium, Electron, WPF, games).
        * **macOS** -- ``screencapture`` CLI with ``osascript`` window
          bounds (built-in, handles Retina displays automatically).
        * **Linux** -- ``xdotool`` + ``import`` (ImageMagick) on X11;
          falls back to ``None`` on Wayland (no universal window-level
          capture protocol outside wlr-screencopy).

        Returns an :class:`ActiveWindowCapture` on success, or
        ``None`` on any failure (caller should fall back to the
        regular ``screenshot`` path).
        """
        if _PLATFORM == "win32":
            return self._win32_printwindow_capture()
        if _PLATFORM == "darwin":
            return self._macos_window_capture()
        if _PLATFORM.startswith("linux"):
            return self._linux_window_capture()
        return None

    # ------------------------------------------------------------------
    # Platform-specific active-window capture
    # ------------------------------------------------------------------

    def _win32_printwindow_capture(self) -> ActiveWindowCapture | None:
        """Windows: foreground window via Win32 ``PrintWindow`` + ``PW_RENDERFULLCONTENT``.

        ``mss.grab`` composites the screen from the OS compositor, which
        loses content for windows whose pixels are rendered through
        DirectX / Direct2D surfaces (Chromium / Electron, WPF, most
        games, some Qt apps) and for any window occluded by another.
        ``PrintWindow`` asks the window itself to paint into a memory
        DC, so it sees the same pixels the application drew.  We pass
        the ``PW_RENDERFULLCONTENT`` flag (``0x00000002``) so that
        DirectX / Direct2D windows are also rendered; without it,
        Chromium-based apps come back as a blank bitmap.
        """
        try:
            import ctypes

            from PIL import Image

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None

            class _RECT(ctypes.Structure):
                _fields_: ClassVar = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = _RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return None

            hdc_window = user32.GetWindowDC(hwnd)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
            hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
            gdi32.SelectObject(hdc_mem, hbitmap)

            # PW_RENDERFULLCONTENT = 0x2; fall back to legacy mode if
            # the application rejects the flag.
            painted = user32.PrintWindow(hwnd, hdc_mem, 0x00000002)
            if not painted:
                user32.PrintWindow(hwnd, hdc_mem, 0x00000000)

            class _BITMAPINFOHEADER(ctypes.Structure):
                _fields_: ClassVar = [
                    ("biSize", ctypes.c_uint32),
                    ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32),
                ]

            class _BITMAPINFO(ctypes.Structure):
                _fields_: ClassVar = [
                    ("bmiHeader", _BITMAPINFOHEADER),
                    ("bmiColors", ctypes.c_uint32 * 3),
                ]

            bmi = _BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height  # negative = top-down DIB
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB

            buf_len = width * height * 4
            raw = (ctypes.c_ubyte * buf_len)()
            got = gdi32.GetDIBits(
                hdc_mem,
                hbitmap,
                0,
                height,
                raw,
                ctypes.byref(bmi),
                0,
            )

            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)

            if not got:
                return None

            img = Image.frombuffer(
                "RGB",
                (width, height),
                bytes(raw),
                "raw",
                "BGRA",
                0,
                1,
            )
            out = io.BytesIO()
            img.save(out, format="PNG")
            return ActiveWindowCapture(
                png_bytes=out.getvalue(),
                left=int(rect.left),
                top=int(rect.top),
                width=width,
                height=height,
            )
        except Exception as exc:
            logger.debug("PrintWindow capture failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # macOS: active-window capture via screencapture CLI
    # ------------------------------------------------------------------

    def _macos_window_capture(self) -> ActiveWindowCapture | None:
        """macOS: foreground window via ``screencapture -l`` CLI.

        Uses ``osascript`` to read the frontmost window's bounding rect,
        then captures the **region** at those coordinates with
        ``screencapture -R``.  This avoids the pyobjc / CoreGraphics
        dependency while still giving us the exact window pixels
        (Retina-aware by default).

        Returns an :class:`ActiveWindowCapture` on success, or
        ``None`` on any failure (caller should fall back to full
        desktop screenshot).
        """
        try:
            import subprocess
            import tempfile

            # 1. Get the frontmost window's bounding rect via osascript.
            script = (
                'tell application "System Events"\n'
                "  set fp to first process whose frontmost is true\n"
                '  if (count of windows of fp) = 0 then return ""\n'
                "  set win to window 1 of fp\n"
                "  set pos to position of win\n"
                "  set sz to size of win\n"
                "  set x to item 1 of pos\n"
                "  set y to item 2 of pos\n"
                "  set w to item 1 of sz\n"
                "  set h to item 2 of sz\n"
                '  return (x as text) & " " & (y as text) & " "'
                '    & (w as text) & " " & (h as text)\n'
                "end tell"
            )
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return None
            parts = r.stdout.strip().split()
            if len(parts) != 4:
                return None
            left, top, width, height = map(int, parts)
            if width <= 0 or height <= 0:
                return None

            # 2. Capture the region with screencapture.
            #    -R x,y,w,h  captures a rectangle in screen coordinates.
            #    -r          native resolution (respects Retina).
            #    -x          no sound effect.
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_path = f.name
            screencap = subprocess.run(
                ["screencapture", "-R", f"{left},{top},{width},{height}", "-r", "-x", tmp_path],
                capture_output=True,
                timeout=10,
            )
            if screencap.returncode != 0:
                with suppress(OSError):
                    os.unlink(tmp_path)
                return None
            with open(tmp_path, "rb") as f:
                png_bytes = f.read()
            with suppress(OSError):
                os.unlink(tmp_path)

            if not png_bytes:
                return None

            return ActiveWindowCapture(
                png_bytes=png_bytes,
                left=left,
                top=top,
                width=width,
                height=height,
            )
        except Exception as exc:
            logger.debug("macOS window capture failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Linux: active-window capture via xdotool + ImageMagick (X11)
    # ------------------------------------------------------------------

    def _linux_window_capture(self) -> ActiveWindowCapture | None:
        """Linux: foreground window capture, compositor-aware.

        Strategies tried in order
        ------------------------
        1. **Hyprland** -- ``hyprctl -j activewindow`` → JSON with
           ``at`` (position) and ``size`` fields, then ``grim -g``
           captures the window rectangle.
        2. **Sway** -- ``swaymsg -t get_tree`` → JSON tree; walk to
           find the focused node, read its ``rect``, then ``grim -g``.
        3. **X11** -- ``xdotool getactivewindow`` + ``import -window
           <id>`` (ImageMagick).
        4. **Fallback** -- return ``None``; caller falls back to full
           desktop screenshot.

        Returns :class:`ActiveWindowCapture` on success, or ``None``.
        """
        try:
            import json as _json
            import os as _os
            import subprocess
            import tempfile

            compositor = _os.environ.get("XDG_CURRENT_DESKTOP", "").lower()

            left = top = width = height = 0
            png_bytes = b""

            # ----------------------------------------------------------
            # 1. Hyprland
            # ----------------------------------------------------------
            if compositor == "hyprland":
                r = subprocess.run(
                    ["hyprctl", "-j", "activewindow"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if r.returncode == 0 and r.stdout.strip():
                    data = _json.loads(r.stdout)
                    at = data.get("at") or data.get("at_workspace") or [0, 0]
                    size = data.get("size") or [0, 0]
                    left, top = int(at[0]), int(at[1])
                    width, height = int(size[0]), int(size[1])
                    if width > 0 and height > 0:
                        with tempfile.NamedTemporaryFile(
                            suffix=".png",
                            delete=False,
                        ) as f:
                            tmp_path = f.name
                        r_img = subprocess.run(
                            ["grim", "-g", f"{left},{top}x{width}x{height}", tmp_path],
                            capture_output=True,
                            timeout=10,
                        )
                        if r_img.returncode == 0:
                            with open(tmp_path, "rb") as f:
                                png_bytes = f.read()
                        with suppress(OSError):
                            _os.unlink(tmp_path)

            # ----------------------------------------------------------
            # 2. Sway
            # ----------------------------------------------------------
            if not png_bytes and compositor == "sway":
                r = subprocess.run(
                    ["swaymsg", "-t", "get_tree"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if r.returncode == 0 and r.stdout.strip():
                    tree = _json.loads(r.stdout)

                    def _find_focused(node):
                        if node.get("focused", False):
                            return node
                        for child in node.get("nodes", []):
                            found = _find_focused(child)
                            if found:
                                return found
                        for child in node.get("floating_nodes", []):
                            found = _find_focused(child)
                            if found:
                                return found
                        return None

                    focused = _find_focused(tree)
                    if focused:
                        rect = focused.get("rect", {})
                        left = int(rect.get("x", 0))
                        top = int(rect.get("y", 0))
                        width = int(rect.get("width", 0))
                        height = int(rect.get("height", 0))
                        if width > 0 and height > 0:
                            with tempfile.NamedTemporaryFile(
                                suffix=".png",
                                delete=False,
                            ) as f:
                                tmp_path = f.name
                            r_img = subprocess.run(
                                ["grim", "-g", f"{left},{top}x{width}x{height}", tmp_path],
                                capture_output=True,
                                timeout=10,
                            )
                            if r_img.returncode == 0:
                                with open(tmp_path, "rb") as f:
                                    png_bytes = f.read()
                            with suppress(OSError):
                                _os.unlink(tmp_path)

            # ----------------------------------------------------------
            # 3. X11 (via xdotool + ImageMagick)
            # ----------------------------------------------------------
            if not png_bytes:
                r_id = subprocess.run(
                    ["xdotool", "getactivewindow"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if r_id.returncode == 0 and r_id.stdout.strip():
                    win_id = r_id.stdout.strip()
                    r_geo = subprocess.run(
                        ["xdotool", "getwindowgeometry", "--shell", win_id],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    if r_geo.returncode == 0:
                        geo: dict[str, int] = {}
                        for line in r_geo.stdout.strip().splitlines():
                            if "=" in line:
                                k, v = line.split("=", 1)
                                geo[k.strip()] = int(v.strip())
                        left = geo.get("X", 0)
                        top = geo.get("Y", 0)
                        width = geo.get("WIDTH", 0)
                        height = geo.get("HEIGHT", 0)
                        if width > 0 and height > 0:
                            with tempfile.NamedTemporaryFile(
                                suffix=".png",
                                delete=False,
                            ) as f:
                                tmp_path = f.name
                            r_img = subprocess.run(
                                ["import", "-window", win_id, tmp_path],
                                capture_output=True,
                                timeout=10,
                            )
                            if r_img.returncode == 0:
                                with open(tmp_path, "rb") as f:
                                    png_bytes = f.read()
                            with suppress(OSError):
                                _os.unlink(tmp_path)

            if not png_bytes or width <= 0 or height <= 0:
                return None

            return ActiveWindowCapture(
                png_bytes=png_bytes,
                left=left,
                top=top,
                width=width,
                height=height,
            )
        except Exception as exc:
            logger.debug("Linux window capture failed: %s", exc)
            return None

    def take_screenshot_png(self) -> bytes:
        """Capture the primary monitor as raw PNG bytes.

        This is a thin convenience wrapper around :meth:`screenshot`
        that returns the PNG bytes directly (no base64) -- convenient
        for vision-language-model clients that want to ``POST`` the
        image to an API.
        """
        if not self._check_mss():
            raise RuntimeError("mss not installed. Run: pip install mss pillow")
        if not self._check_pillow():
            raise RuntimeError("Pillow not installed. Run: pip install pillow") from None
        import mss

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            img = sct.grab(monitor)

        from PIL import Image

        pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        self._last_used = time.time()
        return buf.getvalue()

    def triple_click(self, x: int | None = None, y: int | None = None, coord_space: str = "auto") -> dict[str, Any]:
        """Triple-click to select a line/paragraph (text fields, URLs).

        On Wayland, implemented as three sequential single clicks via
        ``wdotool``.  On other platforms uses ``pyautogui.click()``
        with ``clicks=3``.
        """
        if self._is_wayland():
            if x is not None and y is not None:
                lx, ly = self._to_logical(int(x), int(y), coord_space)
                self._wayland_mouse_move(lx, ly)
            for _ in range(3):
                self._wayland_click("left")
            self._last_used = time.time()
            return {"action": "triple_click", "x": lx if x is not None else 0, "y": ly if y is not None else 0}
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        import pyautogui

        if x is not None and y is not None:
            lx, ly = self._to_logical(int(x), int(y), coord_space)
        else:
            lx, ly = pyautogui.position()
        pyautogui.click(lx, ly, clicks=3, interval=0.05)
        self._last_used = time.time()
        return {"action": "triple_click", "x": int(lx), "y": int(ly)}

    def wait(self, ms: int) -> dict[str, Any]:
        """Block for ``ms`` milliseconds.

        Useful inside long-running computer-use loops to give the
        underlying app time to finish an animation / network call
        before the next screenshot.
        """
        if ms < 0:
            raise ValueError("wait: ms must be non-negative")
        time.sleep(ms / 1000.0)
        self._last_used = time.time()
        return {"action": "wait", "ms": int(ms)}

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    def clipboard_get(self) -> str:
        """Return the current clipboard text.

        Requires ``pyperclip`` (cross-platform) or falls back to the
        Tkinter clipboard on systems where pyperclip isn't installed.
        """
        try:
            import pyperclip  # type: ignore

            self._last_used = time.time()
            return str(pyperclip.paste() or "")
        except ImportError:
            logger.debug("exception suppressed", exc_info=True)
        # Tkinter fallback -- works on every platform with a display.
        try:
            import tkinter as tk  # type: ignore

            root = tk.Tk()
            root.withdraw()
            try:
                text = root.clipboard_get()
            finally:
                root.destroy()
            self._last_used = time.time()
            return str(text or "")
        except Exception as exc:
            raise RuntimeError("clipboard_get: install pyperclip for clipboard access") from exc

    def clipboard_set(self, text: str) -> dict[str, Any]:
        """Set the clipboard text. See :meth:`clipboard_get` for deps."""
        try:
            import pyperclip  # type: ignore

            pyperclip.copy(str(text))
            self._last_used = time.time()
            return {"action": "clipboard_set", "length": len(str(text))}
        except ImportError:
            logger.debug("exception suppressed", exc_info=True)
        try:
            import tkinter as tk  # type: ignore

            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(str(text))
            root.update()  # keep content available after window closes
            root.destroy()
            self._last_used = time.time()
            return {"action": "clipboard_set", "length": len(str(text))}
        except Exception as exc:
            raise RuntimeError("clipboard_set: install pyperclip for clipboard access") from exc

    # ------------------------------------------------------------------
    # File drop
    # ------------------------------------------------------------------

    def file_drop(self, x: int, y: int, paths: list[str], coord_space: str = "auto") -> dict[str, Any]:
        """Drop one or more files onto the window at (x, y).

        ``paths`` is a list of absolute file paths.  This is a thin
        wrapper over :meth:`drag` followed by the OS-level "release
        file" sequence, implemented via pyperclip / xdotool when
        available so the receiving app interprets the drop as a real
        file transfer rather than a phantom drag.
        """
        if not paths:
            raise ValueError("file_drop: at least one path is required")
        if not self._check_pyautogui():
            raise RuntimeError("pyautogui not installed. Run: pip install pyautogui")
        import pyautogui

        lx, ly = self._to_logical(int(x), int(y), coord_space)
        # Move to the drop site and release.  The "file" is communicated
        # to the receiving app via the platform drag-drop manager which
        # pyautogui can't synthesize portably, so on Windows we shell out
        # to PowerShell's DoDragDrop for true OLE drag-drop semantics.
        pyautogui.moveTo(lx, ly)
        pyautogui.mouseDown()
        pyautogui.moveTo(lx + 2, ly + 2)
        if _PLATFORM == "win32":
            self._win32_ole_file_drop(lx, ly, list(paths))
        pyautogui.mouseUp()
        self._last_used = time.time()
        return {
            "action": "file_drop",
            "x": lx,
            "y": ly,
            "files": list(paths),
        }

    @staticmethod
    def _win32_ole_file_drop(_x: int, _y: int, paths: list[str]) -> None:
        """Best-effort Windows OLE drag-drop.

        We hand the file list to PowerShell which uses
        ``System.Windows.Forms.DataObject`` to inject the drop.  Failures
        are swallowed -- the caller still gets a mouseUp so the UI doesn't
        appear frozen.
        """
        try:
            import subprocess

            ps_script = "Add-Type -AssemblyName System.Windows.Forms; $paths = [System.Collections.Specialized.StringCollection]::new(); "
            for p in paths:
                escaped = p.replace("'", "''")
                ps_script += f"$paths.Add('{escaped}'); "
            ps_script += (
                "$data = [System.Windows.Forms.DataObject]::new(); "
                "$data.SetFileDropList($paths); "
                "[System.Windows.Forms.SendKeys]::SendWait('^'); "
            )
            # We can't truly inject the drop from PowerShell without the
            # target HWND, so we just keep the data on the clipboard and
            # let the receiving app pick it up after the mouseUp.
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return

    # ------------------------------------------------------------------
    # OCR-driven "click visible text"
    # ------------------------------------------------------------------

    def click_text(
        self, text: str, *, fuzzy: bool = False, occurrence: int = 1, button: str = "left", coord_space: str = "auto"
    ) -> dict[str, Any]:
        """Find ``text`` on screen via OCR and click its center.

        Returns the same dict as :meth:`click` on success, or ``{
        'found': False, 'queried': text}`` if the text wasn't visible.
        Falls back to Windows UIA for apps where OCR is unreliable
        (e.g. text rendered into canvas / Direct2D surfaces).
        """
        match = self.find_text(text, fuzzy=fuzzy, occurrence=occurrence)
        if not match.get("found"):
            return match
        cx = int(match["center_x"])
        cy = int(match["center_y"])
        result = self.click(x=cx, y=cy, button=button, coord_space=coord_space)
        result["matched_text"] = match.get("text", text)
        result["source"] = match.get("source", "ocr")
        return result

    def find_text(self, text: str, *, fuzzy: bool = False, occurrence: int = 1) -> dict[str, Any]:
        """Find a text fragment on the desktop.

        Strategy:
            1. Try Windows UIA (accurate, runs over actual UI text).
            2. Fall back to OCR (works on every platform with a
               backend installed).

        ``occurrence`` is 1-based: when the same text appears multiple
        times the caller can ask for the 2nd, 3rd, etc. match.
        """
        if not text:
            return {"found": False, "queried": text}
        if occurrence < 1:
            raise ValueError("occurrence must be >= 1")

        # 1. UIA path (Windows only) -- cheap and accurate.
        if _PLATFORM == "win32":
            try:
                tree = self.accessibility_tree(max_depth=10, max_nodes=4000)
            except Exception:
                tree = []
            if tree and not (len(tree) == 1 and "error" in tree[0]):
                target = text.strip().lower()
                hits: list[dict[str, Any]] = []
                for n in tree:
                    name = n.get("name") or ""
                    if not name:
                        continue
                    if not self._text_matches(name, target, fuzzy):
                        continue
                    r = n["rect"]
                    if r["right"] <= r["left"] or r["bottom"] <= r["top"]:
                        continue
                    hits.append(
                        {
                            "text": name,
                            "x": r["left"],
                            "y": r["top"],
                            "width": r["right"] - r["left"],
                            "height": r["bottom"] - r["top"],
                            "center_x": (r["left"] + r["right"]) // 2,
                            "center_y": (r["top"] + r["bottom"]) // 2,
                            "control_type": n.get("control_type", ""),
                            "source": "uia",
                        }
                    )
                if hits:
                    idx = min(occurrence - 1, len(hits) - 1)
                    hit = hits[idx]
                    hit["found"] = True
                    hit["total_matches"] = len(hits)
                    return hit

        # 2. OCR fallback.
        state = self.screenshot_with_cursor()
        try:
            from encre.computer.ocr import ocr_image

            img_bytes = base64.b64decode(state.screenshot_b64)
            elements = ocr_image(img_bytes)
        except Exception:
            elements = []
        target = text.strip().lower()
        hits: list[dict[str, Any]] = []
        for e in elements:
            t = (e.get("text") or "").strip()
            if not t:
                continue
            if not self._text_matches(t, target, fuzzy):
                continue
            hits.append(
                {
                    "text": t,
                    "x": int(e["x"]),
                    "y": int(e["y"]),
                    "width": int(e["width"]),
                    "height": int(e["height"]),
                    "center_x": int(e["center_x"]),
                    "center_y": int(e["center_y"]),
                    "source": "ocr",
                }
            )
        if not hits:
            return {"found": False, "queried": text}
        idx = min(occurrence - 1, len(hits) - 1)
        hit = hits[idx]
        hit["found"] = True
        hit["total_matches"] = len(hits)
        return hit

    @staticmethod
    def _text_matches(haystack: str, needle: str, fuzzy: bool) -> bool:
        """Return True if ``needle`` matches ``haystack`` (substring or fuzzy)."""
        if not fuzzy:
            return needle in haystack.lower()
        # Cheap fuzzy: every whitespace-separated token of ``needle`` must
        # appear in ``haystack`` in order.  Good enough for UI labels and
        # far cheaper than Levenshtein on a hot path.
        h = haystack.lower()
        cursor = 0
        for tok in needle.split():
            pos = h.find(tok, cursor)
            if pos < 0:
                return False
            cursor = pos + len(tok)
        return True

    def is_idle(self, max_idle_seconds: int = 600) -> bool:
        """Return True if the session hasn't been used within the idle window."""
        return (time.time() - self._last_used) > max_idle_seconds

    # ------------------------------------------------------------------
    # Accessibility tree (Windows: UIAutomation)
    # ------------------------------------------------------------------

    def accessibility_tree(self, max_depth: int = 6, max_nodes: int = 500) -> list[dict[str, Any]]:
        """Walk the active window's UI automation tree.

        Platform backends
        -----------------
        * **Windows** -- UIAutomation via ``uiautomation`` package.
        * **macOS** -- Accessibility API via ``pyobjc-framework-ApplicationServices``.
        * **Linux** -- AT-SPI2 via ``pyatspi`` (requires ``at-spi2-core``).

        Returns a flat list of nodes, each with ``name``, ``control_type``,
        ``automation_id``, ``class_name``, ``rect`` (physical pixel screen
        coordinates), ``depth``, and ``focusable`` keys. Returns an error
        item on unsupported platforms.
        """
        if _PLATFORM == "win32":
            return self._windows_accessibility_tree(max_depth, max_nodes)
        if _PLATFORM == "darwin":
            return self._macos_accessibility_tree(max_depth, max_nodes)
        if _PLATFORM.startswith("linux"):
            return self._linux_accessibility_tree(max_depth, max_nodes)
        return [{"error": f"accessibility_tree not supported on {_PLATFORM}"}]

    # ------------------------------------------------------------------
    # Windows: UIAutomation
    # ------------------------------------------------------------------

    def _windows_accessibility_tree(
        self,
        max_depth: int = 6,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        """Windows UIAutomation accessibility tree."""
        import uiautomation as uia  # type: ignore

        try:
            root = uia.GetForegroundControl()
        except Exception as exc:
            return [{"error": f"Failed to acquire foreground control: {exc}"}]
        if root is None:
            return [{"error": "No foreground window detected"}]

        nodes: list[dict[str, Any]] = []
        self._walk_uia(root, 0, max_depth, max_nodes, nodes, uia)
        return nodes

    @staticmethod
    def _walk_uia(node, depth: int, max_depth: int, max_nodes: int, out: list[dict[str, Any]], uia) -> None:
        """Recursively flatten a UIA control subtree into ``out`` (bounded)."""
        if len(out) >= max_nodes or node is None or depth > max_depth:
            return
        try:
            rect = node.BoundingRectangle
            entry = {
                "name": node.Name or "",
                "control_type": getattr(node, "ControlTypeName", "") or "",
                "automation_id": getattr(node, "AutomationId", "") or "",
                "class_name": getattr(node, "ClassName", "") or "",
                "rect": {
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "right": int(rect.right),
                    "bottom": int(rect.bottom),
                },
                "depth": depth,
                "focusable": bool(getattr(node, "IsKeyboardFocusable", False)),
            }
            out.append(entry)
        except Exception:
            return
        try:
            children = list(node.GetChildren())
        except Exception:
            return
        for c in children:
            if len(out) >= max_nodes:
                return
            EncreDesktopSession._walk_uia(c, depth + 1, max_depth, max_nodes, out, uia)

    def find_element_by_name(self, name: str, control_type: str | None = None, max_depth: int = 8, max_nodes: int = 2000) -> dict[str, Any]:
        """Find a UIA element by accessible name. Returns center coords or error."""
        tree = self.accessibility_tree(max_depth=max_depth, max_nodes=max_nodes)
        if tree and "error" in tree[0]:
            return tree[0]
        target = name.strip().lower()
        for n in tree:
            if not n.get("name"):
                continue
            if target not in n["name"].lower():
                continue
            if control_type and control_type.lower() != n.get("control_type", "").lower():
                continue
            r = n["rect"]
            cx = (r["left"] + r["right"]) // 2
            cy = (r["top"] + r["bottom"]) // 2
            return {
                "found": True,
                "name": n["name"],
                "control_type": n["control_type"],
                "center_x": cx,
                "center_y": cy,
                "rect": r,
            }
        return {"found": False, "queried": name}

    # ------------------------------------------------------------------
    # macOS: Accessibility API via ApplicationServices / AX
    # ------------------------------------------------------------------

    def _macos_accessibility_tree(
        self,
        max_depth: int = 6,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        """macOS Accessibility API (AX) tree via pyobjc."""
        if max_depth < 1:
            return []
        import ApplicationServices as AppServices  # type: ignore
        from AppKit import NSWorkspace  # type: ignore

        try:
            front_app = NSWorkspace.sharedWorkspace().frontmostApplication()
            pid = front_app.processIdentifier()
            app_element = AppServices.AXUIElementCreateApplication(pid)
        except Exception as exc:
            return [{"error": f"Failed to acquire foreground app AX element: {exc}"}]

        if app_element is None:
            return [{"error": "No foreground application detected"}]

        nodes: list[dict[str, Any]] = []
        self._walk_macos_ax(
            app_element,
            0,
            max_depth,
            max_nodes,
            nodes,
            AppServices,
        )
        return nodes

    @staticmethod
    def _ax_value_to_str(value) -> str:
        """Safely convert an AX value to a string."""
        if value is None:
            return ""
        try:
            return str(value)
        except Exception:
            return ""

    @staticmethod
    def _ax_rect_to_dict(value, default_left=0, default_top=0) -> dict[str, int]:
        """Convert an AX value to a rect dict, falling back to defaults."""
        if value is None:
            return {"left": default_left, "top": default_top, "right": default_left, "bottom": default_top}
        try:
            # AXValue is an Objective-C struct; pyobjc maps it to a tuple
            # (x, y, w, h) when the type is kAXValueCGSizeType or
            # (x, y) for kAXValueCGPointType.
            # Try CGRect (x, y, w, h) first.
            if hasattr(value, "origin") and hasattr(value, "size"):
                x = int(getattr(value.origin, "x", 0))
                y = int(getattr(value.origin, "y", 0))
                w = int(getattr(value.size, "width", 0))
                h = int(getattr(value.size, "height", 0))
                return {
                    "left": x,
                    "top": y,
                    "right": x + w,
                    "bottom": y + h,
                }
            # Fallback: treat as a tuple/sequence of 4 numbers.
            if isinstance(value, tuple | list) and len(value) >= 4:
                x, y, w, h = int(value[0]), int(value[1]), int(value[2]), int(value[3])
                return {
                    "left": x,
                    "top": y,
                    "right": x + w,
                    "bottom": y + h,
                }
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        return {"left": default_left, "top": default_top, "right": default_left, "bottom": default_top}

    def _walk_macos_ax(self, element, depth: int, max_depth: int, max_nodes: int, out: list[dict[str, Any]], axapi) -> None:
        """Recursively flatten a macOS AX subtree into ``out`` (bounded)."""
        if len(out) >= max_nodes or element is None or depth > max_depth:
            return
        try:
            role = self._ax_value_to_str(axapi.AXUIElementCopyAttributeValue(element, "AXRole", None)[1])
            name = self._ax_value_to_str(axapi.AXUIElementCopyAttributeValue(element, "AXDescription", None)[1])
            if not name:
                name = self._ax_value_to_str(axapi.AXUIElementCopyAttributeValue(element, "AXTitle", None)[1])
            if not name:
                name = self._ax_value_to_str(axapi.AXUIElementCopyAttributeValue(element, "AXLabel", None)[1])
            position = axapi.AXUIElementCopyAttributeValue(
                element,
                "AXPosition",
                None,
            )[1]
            size = axapi.AXUIElementCopyAttributeValue(
                element,
                "AXSize",
                None,
            )[1]
            focused = axapi.AXUIElementCopyAttributeValue(
                element,
                "AXFocused",
                None,
            )[1]
            subrole = self._ax_value_to_str(axapi.AXUIElementCopyAttributeValue(element, "AXSubrole", None)[1])

            rect = self._ax_rect_to_dict(None)
            px, py, pw, ph = 0, 0, 0, 0
            if position is not None and hasattr(position, "x"):
                px = int(getattr(position, "x", 0))
                py = int(getattr(position, "y", 0))
            if size is not None and hasattr(size, "width"):
                pw = int(getattr(size, "width", 0))
                ph = int(getattr(size, "height", 0))
            if pw > 0 and ph > 0:
                rect = {"left": px, "top": py, "right": px + pw, "bottom": py + ph}

            entry = {
                "name": name or "",
                "control_type": role or "",
                "automation_id": subrole or "",
                "class_name": "",
                "rect": rect,
                "depth": depth,
                "focusable": bool(focused) if focused is not None else False,
            }
            out.append(entry)
        except Exception:
            return

        # Walk children.
        try:
            children = axapi.AXUIElementCopyAttributeValue(
                element,
                "AXChildren",
                None,
            )[1]
        except Exception:
            return
        if not isinstance(children, tuple | list):
            return
        for child in children:
            if len(out) >= max_nodes:
                return
            self._walk_macos_ax(
                child,
                depth + 1,
                max_depth,
                max_nodes,
                out,
                axapi,
            )

    # ------------------------------------------------------------------
    # Linux: AT-SPI2 accessibility tree via pyatspi / dbus
    # ------------------------------------------------------------------

    def _linux_accessibility_tree(
        self,
        max_depth: int = 6,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        """Linux AT-SPI2 accessibility tree via pyatspi.

        Requires ``pyatspi`` (usually ``pip install pyatspi``) and
        ``at-spi2-core`` runtime (``apt install at-spi2-core`` on Debian,
        ``dnf install at-spi2-core`` on Fedora).
        """
        if max_depth < 1:
            return []
        import pyatspi  # type: ignore

        try:
            desktop = pyatspi.Registry.getDesktop(0)
        except Exception as exc:
            return [{"error": f"Failed to get AT-SPI2 desktop: {exc}"}]
        if desktop is None:
            return [{"error": "No AT-SPI2 desktop available"}]

        nodes: list[dict[str, Any]] = []
        self._walk_atspi(
            desktop,
            0,
            max_depth,
            max_nodes,
            nodes,
            pyatspi,
        )
        return nodes

    @staticmethod
    def _atspi_rect_to_dict(acc, pyatspi) -> dict[str, int]:
        """Extract ``(x, y, w, h)`` from an accessible component."""
        try:
            if acc is None:
                return {"left": 0, "top": 0, "right": 0, "bottom": 0}
            # Try the Component interface for position + size.
            try:
                # pyatspi exposes ``acc.queryComponent()`` which returns
                # a Component interface with ``getPosition`` and ``getSize``.
                comp = acc.queryComponent()
                pos = comp.getPosition(pyatspi.DESKTOP_COORDS)
                sz = comp.getSize()
                x, y = int(pos.x), int(pos.y)
                w, h = int(sz.width), int(sz.height)
                return {"left": x, "top": y, "right": x + w, "bottom": y + h}
            except (LookupError, AttributeError):
                logger.debug("exception suppressed", exc_info=True)
            # Fallback: try ``acc.getExtents`` (some atspi versions).
            try:
                ext = acc.getExtents(pyatspi.DESKTOP_COORDS)
                if ext:
                    return {"left": int(ext.x), "top": int(ext.y), "right": int(ext.x + ext.width), "bottom": int(ext.y + ext.height)}
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        return {"left": 0, "top": 0, "right": 0, "bottom": 0}

    def _walk_atspi(self, acc, depth: int, max_depth: int, max_nodes: int, out: list[dict[str, Any]], pyatspi) -> None:
        """Recursively flatten an AT-SPI2 accessible subtree."""
        if len(out) >= max_nodes or acc is None or depth > max_depth:
            return
        try:
            name = acc.name or ""
            role_name = acc.getRoleName() or ""
            entry = {
                "name": name,
                "control_type": role_name,
                "automation_id": "",
                "class_name": "",
                "rect": self._atspi_rect_to_dict(acc, pyatspi),
                "depth": depth,
                "focusable": bool(acc.getState().contains(pyatspi.STATE_FOCUSABLE)),
            }
            out.append(entry)
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

        try:
            children = [acc[i] for i in range(acc.childCount)]
        except Exception:
            return
        for child in children:
            if len(out) >= max_nodes:
                return
            self._walk_atspi(
                child,
                depth + 1,
                max_depth,
                max_nodes,
                out,
                pyatspi,
            )

    @staticmethod
    def _ensure_pillow() -> None:
        """Raise a helpful error if Pillow is not installed."""
        if importlib.util.find_spec("PIL") is None:
            raise RuntimeError("Pillow not installed. Run: pip install pillow")
