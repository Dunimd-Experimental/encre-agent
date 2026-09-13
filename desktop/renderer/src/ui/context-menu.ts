/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 * 
 * DISCLAIMER: Users must comply with applicable AI regulations.
 * Non-compliance may result in service termination or legal liability.
 */

/** Minimum gap (px) kept between a floating layer and the window edge. */
export const FLOAT_MARGIN = 8;

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.max(min, Math.min(value, max));
}

/**
 * Clamps a physical `left` offset so a floating layer of `width` stays
 * fully inside the window: `left >= margin` and `left + width <= innerWidth - margin`.
 *
 * `left` is a *physical* offset — it never mirrors under `dir="rtl"`, so a
 * menu anchored to a trigger that sits on the far inline-end edge of the
 * window opens straight out of it. Every layer positioned from
 * `getBoundingClientRect()` must run its offset through here (or through
 * `clampFloatRight` when it is anchored by its right edge).
 */
export function clampFloatLeft(
  width: number,
  preferredLeft: number,
  margin: number = FLOAT_MARGIN,
): number {
  return clamp(preferredLeft, margin, window.innerWidth - width - margin);
}

/** Same as `clampFloatLeft`, for layers anchored by their `right` offset. */
export function clampFloatRight(
  width: number,
  preferredRight: number,
  margin: number = FLOAT_MARGIN,
): number {
  return clamp(preferredRight, margin, window.innerWidth - width - margin);
}

/** `clampFloatLeft` for an element that is already in the DOM. */
export function clampElLeft(
  el: HTMLElement,
  preferredLeft: number,
  margin: number = FLOAT_MARGIN,
): number {
  return clampFloatLeft(el.offsetWidth, preferredLeft, margin);
}

/** `clampFloatRight` for an element that is already in the DOM. */
export function clampElRight(
  el: HTMLElement,
  preferredRight: number,
  margin: number = FLOAT_MARGIN,
): number {
  return clampFloatRight(el.offsetWidth, preferredRight, margin);
}

/**
 * Nudges an already-visible layer that CSS positions (e.g. a
 * `.settings-dropdown` anchored with `inset-inline-end: 0`) back into the
 * viewport. Measures the real rect, so it works for both ltr and rtl, and
 * is a no-op when the layer already fits — ltr behaviour is unchanged.
 */
export function clampIntoViewport(
  el: HTMLElement,
  margin: number = FLOAT_MARGIN,
): void {
  // Clear any previous correction so we measure the authored position.
  el.style.insetInlineEnd = "";
  const rect = el.getBoundingClientRect();
  if (!rect.width) return;
  let dx = 0; // px to move: positive = towards the right edge.
  if (rect.right > window.innerWidth - margin) {
    dx = window.innerWidth - margin - rect.right;
  }
  if (rect.left + dx < margin) dx = margin - rect.left;
  if (dx === 0) return;
  const rtl = document.documentElement.dir === "rtl";
  // Growing the inline-end offset always pushes the layer towards the
  // inline-start side, which is the physical left under ltr and the
  // physical right under rtl — hence the sign flip.
  el.style.insetInlineEnd = `${rtl ? dx : -dx}px`;
}

/**
 * Positions and shows a context menu at (x, y) with viewport-boundary
 * awareness: flips left/above when the menu would overflow the window.
 * Call this instead of manually setting style.left/style.top.
 */
export function showContextMenu(menu: HTMLElement, x: number, y: number): void {
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  // Some callers hide shared menus via inline display:none; clear it so the
  // menu can be shown again (the class toggle alone would not override it).
  menu.style.display = "";
  menu.classList.remove("hidden");
  requestAnimationFrame(() => {
    const rect = menu.getBoundingClientRect();
    let left = x;
    if (rect.right > window.innerWidth) left = Math.max(FLOAT_MARGIN, x - rect.width);
    menu.style.left = `${clampFloatLeft(rect.width, left)}px`;
    let top = y;
    if (rect.bottom > window.innerHeight) top = Math.max(FLOAT_MARGIN, y - rect.height);
    menu.style.top = `${clamp(top, FLOAT_MARGIN, window.innerHeight - rect.height - FLOAT_MARGIN)}px`;
  });
}

/** Estimated row height (px) for a standard dropdown item. */
export const DROPDOWN_ITEM_H = 34;

/**
 * Caps a dropdown layer so at most `maxVisible` rows stay visible before a
 * scrollbar kicks in. Sets the `--dd-max-h` custom property the stylesheet
 * reads, so the cap survives subsequent reflows. Pass `itemH` when rows use
 * a non-standard height (e.g. two-line workspace entries).
 */
export function capDropdownHeight(
  menu: HTMLElement,
  maxVisible: number,
  itemH: number = DROPDOWN_ITEM_H,
): void {
  menu.style.setProperty("--dd-max-h", `${Math.max(1, maxVisible) * itemH}px`);
}
