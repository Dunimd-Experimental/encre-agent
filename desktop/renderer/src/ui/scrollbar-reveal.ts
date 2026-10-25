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

/**
 * Proximity-revealed scrollbars (app-wide, one listener).
 *
 * Scrollbars are painted invisible by default (see the Scrollbar block in
 * `styles.css`).  A single rAF-throttled `mousemove` listener walks up from
 * the event target and marks every scrollable ancestor with
 * `data-scroll-reveal` while the pointer sits within REVEAL_DISTANCE of the
 * scrollbar's own edge — right side for the vertical bar, bottom for the
 * horizontal one.  Removing the attribute hides the bar again.
 *
 * Because the mechanism is attribute-driven and walks the live DOM, it
 * automatically covers every scroller in the app — panels, dialogs, the
 * chat area, markdown renderers, dynamically created containers — with no
 * per-container registration.  The 4px scrollbar strip is the element's own
 * scrollbar box (plus `scrollbar-gutter: stable` on the unified gutter
 * list), so bars appearing/disappearing never shift layout.
 */

/** Pointer distance from the scrollbar edge that reveals the bar. */
const REVEAL_DISTANCE = 28;

/** A little tolerance past the outer edge so the bar doesn't blink off
 *  the moment the cursor crosses the container boundary. */
const EDGE_TOLERANCE = 6;

/** Ancestor walk depth cap — scroll containers are never deeper than this. */
const MAX_ANCESTORS = 12;

const MARK = "data-scroll-reveal";

/** Elements currently marked. */
let marked: Element[] = [];

/** rAF throttle handle. */
let frame = 0;

/** Last processed pointer position (skip work when it didn't move). */
let lastX = -1;
let lastY = -1;

function unmarkAll(): void {
  for (const el of marked) {
    el.removeAttribute(MARK);
  }
  marked = [];
}

function isVerticallyScrollable(el: HTMLElement): boolean {
  return el.scrollHeight > el.clientHeight + 1;
}

function isHorizontallyScrollable(el: HTMLElement): boolean {
  return el.scrollWidth > el.clientWidth + 1;
}

function update(x: number, y: number, start: Element | null): void {
  const next: Element[] = [];
  let el: Element | null = start;
  let depth = 0;

  while (el && depth < MAX_ANCESTORS) {
    if (el instanceof HTMLElement) {
      const rect = el.getBoundingClientRect();
      const insideY = y >= rect.top && y <= rect.bottom;
      const insideX = x >= rect.left && x <= rect.right;

      // Vertical bar: pointer hugs the RIGHT edge (plus tolerance so the
      // bar survives the cursor slipping into the gap beside the strip).
      const nearRight =
        insideY &&
        x >= rect.right - REVEAL_DISTANCE &&
        x <= rect.right + EDGE_TOLERANCE;

      // Horizontal bar: pointer hugs the BOTTOM edge, same tolerance.
      const nearBottom =
        insideX &&
        y >= rect.bottom - REVEAL_DISTANCE &&
        y <= rect.bottom + EDGE_TOLERANCE;

      if (
        (nearRight && isVerticallyScrollable(el)) ||
        (nearBottom && isHorizontallyScrollable(el))
      ) {
        next.push(el);
      }
    }
    el = el.parentElement;
    depth++;
  }

  // Diff against the previously marked set — attribute writes are what
  // trigger style recalc, so only touch elements that actually change.
  for (const el of next) {
    if (!el.hasAttribute(MARK)) {
      el.setAttribute(MARK, "");
    }
  }
  for (const el of marked) {
    if (!next.includes(el)) {
      el.removeAttribute(MARK);
    }
  }
  marked = next;
}

/** Module-level idempotence guard (the renderer bundle is a single module
 *  instance, so a boolean here is enough). */
let installed = false;

/**
 * Install the global listeners.  Idempotent; called once from the app
 * bootstrap next to `initTooltip()`.
 */
export function initScrollbarReveal(): void {
  if (installed) return;
  installed = true;

  window.addEventListener(
    "mousemove",
    (ev: MouseEvent) => {
      const x = ev.clientX;
      const y = ev.clientY;
      if (x === lastX && y === lastY) return;
      lastX = x;
      lastY = y;
      if (frame) return;
      const target = ev.target;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        update(lastX, lastY, target instanceof Element ? target : null);
      });
    },
    { passive: true }
  );

  // Leaving the window or losing focus must not leave bars dangling.
  const clear = () => {
    if (frame) {
      window.cancelAnimationFrame(frame);
      frame = 0;
    }
    unmarkAll();
  };
  document.documentElement.addEventListener("mouseleave", clear);
  window.addEventListener("blur", clear);
}
