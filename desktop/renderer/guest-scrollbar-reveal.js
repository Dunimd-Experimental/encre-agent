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
 * Proximity-revealed scrollbars for <webview> guest documents.
 *
 * The guest page is its own document: the host renderer's reveal listener
 * (ui/scrollbar-reveal.ts) cannot reach it, so the main process injects this
 * script into every webview guest on document ready (see main.ts).  The
 * behaviour mirrors the host implementation exactly — pointer within 28px of
 * the right edge reveals the vertical bar, within 28px of the bottom edge
 * reveals the horizontal one; the matching scrollbar CSS is applied by the
 * main process via `contents.insertCSS` so it also survives strict page CSP.
 *
 * Plain vanilla JS on purpose: this runs as a guest-page script, not part of
 * the renderer bundle.
 */
(function () {
  "use strict";
  if (window.__encreGuestScrollbarReveal) return;
  window.__encreGuestScrollbarReveal = true;

  /** Pointer distance from the scrollbar edge that reveals the bar. */
  var REVEAL_DISTANCE = 28;
  /** Tolerance past the outer edge so the bar doesn't blink off. */
  var EDGE_TOLERANCE = 6;
  /** Ancestor walk depth cap. */
  var MAX_ANCESTORS = 12;

  var MARK = "data-scroll-reveal";
  var marked = [];
  var frame = 0;
  var lastX = -1;
  var lastY = -1;

  function unmarkAll() {
    for (var i = 0; i < marked.length; i++) marked[i].removeAttribute(MARK);
    marked = [];
  }

  function update(x, y, start) {
    var next = [];
    var el = start;
    var depth = 0;

    while (el && depth < MAX_ANCESTORS) {
      if (el instanceof HTMLElement) {
        var rect = el.getBoundingClientRect();
        var insideY = y >= rect.top && y <= rect.bottom;
        var insideX = x >= rect.left && x <= rect.right;

        // Vertical bar: pointer hugs the RIGHT edge.
        var nearRight =
          insideY &&
          x >= rect.right - REVEAL_DISTANCE &&
          x <= rect.right + EDGE_TOLERANCE;

        // Horizontal bar: pointer hugs the BOTTOM edge.
        var nearBottom =
          insideX &&
          y >= rect.bottom - REVEAL_DISTANCE &&
          y <= rect.bottom + EDGE_TOLERANCE;

        if (
          (nearRight && el.scrollHeight > el.clientHeight + 1) ||
          (nearBottom && el.scrollWidth > el.clientWidth + 1)
        ) {
          next.push(el);
        }
      }
      el = el.parentElement;
      depth++;
    }

    for (var j = 0; j < next.length; j++) {
      if (!next[j].hasAttribute(MARK)) next[j].setAttribute(MARK, "");
    }
    for (var k = 0; k < marked.length; k++) {
      if (next.indexOf(marked[k]) === -1) marked[k].removeAttribute(MARK);
    }
    marked = next;
  }

  window.addEventListener(
    "mousemove",
    function (ev) {
      var x = ev.clientX;
      var y = ev.clientY;
      if (x === lastX && y === lastY) return;
      lastX = x;
      lastY = y;
      if (frame) return;
      var target = ev.target;
      frame = window.requestAnimationFrame(function () {
        frame = 0;
        update(lastX, lastY, target instanceof Element ? target : null);
      });
    },
    { passive: true }
  );

  // Leaving the window must not leave bars dangling.
  document.documentElement.addEventListener("mouseleave", function () {
    if (frame) {
      window.cancelAnimationFrame(frame);
      frame = 0;
    }
    unmarkAll();
  });
  window.addEventListener("blur", function () {
    if (frame) {
      window.cancelAnimationFrame(frame);
      frame = 0;
    }
    unmarkAll();
  });
})();
