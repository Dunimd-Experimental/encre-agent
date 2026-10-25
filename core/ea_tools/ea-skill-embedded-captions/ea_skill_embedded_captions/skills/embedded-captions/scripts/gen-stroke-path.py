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

"""Generalised draw-on path generator for single-line (pen-path) SVG fonts.

Lays out ANY word in a single-line SVG font (Hershey/EMS style) and emits one
continuous-dash-revealable path ``d`` string. Because the glyph data IS the
pen path, stroke-order reveal is exact by construction for any text, without
per-word tuning.

Usage:
    gen-stroke-path.py <font.svg> <text> <target_width_px> <baseline_y> <x0>

Output:
    Prints the path ``d`` string to stdout, and layout diagnostics to stderr.
"""

import re
import sys

# ---------------------------------------------------------------------------
# Argument parsing — positional args from the command line
# ---------------------------------------------------------------------------
font_path: str = sys.argv[1]
text: str = sys.argv[2]
target_w: float = float(sys.argv[3])
baseline_y: float = float(sys.argv[4])
x0: float = float(sys.argv[5])

# ---------------------------------------------------------------------------
# Parse the SVG font file into a character→(advance, path-d) mapping
# ---------------------------------------------------------------------------
svg: str = open(font_path).read()
glyphs: dict[str, tuple[float, str]] = {}

for m in re.finditer(
    r'<glyph\s+unicode="(.)"[^>]*?horiz-adv-x="([\d.]+)"(?:[^>]*?d="([^"]*)")?',
    svg,
):
    ch: str = m.group(1)
    adv: float = float(m.group(2))
    d: str = m.group(3) or ""
    glyphs[ch] = (adv, d)

# Use the space glyph's advance width as a fallback for any character not
# present in the font (e.g. punctuation or unsupported glyphs).
space_adv: float = glyphs.get(" ", (300, ""))[0]

# ---------------------------------------------------------------------------
# Compute the total advance width in font units, then derive the scale factor
# that maps font units to the requested pixel width.
# ---------------------------------------------------------------------------
total: float = 0.0
for ch in text:
    total += glyphs.get(ch, (space_adv, ""))[0]

s: float = target_w / total  # scale: font-units → pixels

# ---------------------------------------------------------------------------
# Walk the text character-by-character, transforming each glyph's path
# coordinates from font-space to screen-space, and building the output path.
# ---------------------------------------------------------------------------
out: list[str] = []
cursor: float = 0.0
NUM: re.Pattern[str] = re.compile(r"[-\d.]+")

for ch in text:
    adv, d = glyphs.get(ch, (space_adv, ""))
    if d:
        # Glyph paths are polylines only (M/L commands). Transform every
        # coordinate pair: add the cursor offset (plus any glyph-local
        # x-shift), scale, and flip the y-axis because SVG-font coordinate
        # systems use an upward y direction while screen coordinates go down.
        toks: list[tuple[str, str, str]] = re.findall(
            r"([ML])\s+([-\d.]+)\s+([-\d.]+)", d
        )
        for cmd, xs, ys in toks:
            x = x0 + (cursor + float(xs)) * s
            y = baseline_y - float(ys) * s  # flip: SVG-font y is UP
            out.append(f"{cmd} {x:.1f} {y:.1f}")
    cursor += adv

# Print the assembled path d-string to stdout
print(" ".join(out))

# Emit diagnostics to stderr so they don't pollute the path output
print(
    f"[gen] chars={len(text)} scale={s:.3f} width={total * s:.0f}px "
    f"subpaths={sum(1 for o in out if o.startswith('M'))}",
    file=sys.stderr,
)
