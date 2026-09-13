/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
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
 * Dialog textarea expand / collapse toggle.
 *
 * Reuses the composer's control verbatim — a plain `.btn-icon` with lucide
 * `maximize-2` / `minimize-2` — and replays the composer's own unfold sequence
 * (`App.setInputExpanded` in app.ts):
 *
 *   1. pin the height we animate FROM as an inline px value,
 *   2. force a synchronous reflow so the browser has a definite "before" value,
 *   3. only THEN arm the transition and move the target.
 *
 * Step 3 is the load-bearing one.  `height` is only free to interpolate when
 * the min/max clamps are lifted for the duration of the glide — the composer
 * does this with `min-height: 0; max-height: none` on `.prompt-input.expanded`.
 * Without it, arm-and-change in the same style recalculation lets the browser
 * resolve those bounds immediately: on unfold the lower bound jumps straight to
 * the expanded height, the *used* height is pinned there from frame zero and the
 * box snaps open, while on fold the upper bound jumps down and eats the first
 * half of the shrink.  Hence "only the collapse animated, and it stuttered".
 *
 * The only differences from the composer are placement and reveal: the toggle
 * sits in the top-right corner of `.input-wrapper--dialog` and stays invisible
 * until the pointer enters that corner.
 *
 * Usage: call `attachInputExpand(overlay)` right after the dialog markup has
 * been inserted into the document.
 */

import { t } from "../features/i18n.js";

/** Containers that own a multi-line textarea and therefore get the toggle. */
const WRAPPER_SELECTOR = ".input-wrapper--dialog";
const TEXTAREA_SELECTOR = "textarea.setting-rich-text";
/** Safety net for the height glide: must outlast `--duration-slow` (0.28s). */
const ANIM_TIMEOUT_MS = 420;

/**
 * Inject the expand/collapse toggle into every eligible dialog text container
 * found under `root`. Idempotent: containers already carrying the toggle are
 * skipped, so re-rendering a dialog never duplicates the control.
 */
export function attachInputExpand(root: ParentNode): void {
  root.querySelectorAll<HTMLElement>(WRAPPER_SELECTOR).forEach((wrap) => {
    if (wrap.querySelector(":scope > .dialog-expand-zone")) return;
    const area = wrap.querySelector<HTMLTextAreaElement>(TEXTAREA_SELECTOR);
    if (!area) return;

    wrap.classList.add("has-expand");

    // Mirror the composer markup 1:1 — same classes, same icons — so the
    // control is visually indistinguishable from the one under the composer.
    const zone = document.createElement("div");
    zone.className = "dialog-expand-zone";
    zone.innerHTML = `
      <button type="button" class="btn-icon dialog-input-expand" data-i18n-title="input.expand">
        <i data-lucide="maximize-2" class="lucide expand-icon"></i>
        <i data-lucide="minimize-2" class="lucide collapse-icon" style="display:none"></i>
      </button>`;

    const btn = zone.querySelector<HTMLButtonElement>(".dialog-input-expand")!;

    const sync = (): void => {
      const expanded = wrap.classList.contains("is-expanded");
      const expandIcon = btn.querySelector<HTMLElement>(".expand-icon");
      const collapseIcon = btn.querySelector<HTMLElement>(".collapse-icon");
      if (expandIcon) expandIcon.style.display = expanded ? "none" : "";
      if (collapseIcon) collapseIcon.style.display = expanded ? "" : "none";
      const key = expanded ? "input.collapse" : "input.expand";
      btn.setAttribute("data-i18n-title", key);
      btn.title = t(key);
      btn.setAttribute("aria-label", t(key));
    };

    // The textarea's at-rest min/max, captured while it is still collapsed
    // (i.e. before the first unfold).  `.is-expand-anim` lifts the clamps for
    // the duration of the glide, so they cannot be re-read at collapse time.
    let restMin = 0;
    let restMax = Number.POSITIVE_INFINITY;
    const rememberRestBounds = (): void => {
      const cs = window.getComputedStyle(area);
      restMin = Number.parseFloat(cs.minHeight) || 0;
      restMax =
        cs.maxHeight === "none"
          ? Number.POSITIVE_INFINITY
          : Number.parseFloat(cs.maxHeight) || Number.POSITIVE_INFINITY;
    };

    /**
     * Natural content height.  Only meaningful with `.is-expand-anim` applied,
     * since that is what lifts the clamps; measured with the transition off so
     * the probe never eases.
     */
    const contentHeight = (): number => {
      const prevH = area.style.height;
      area.style.height = "auto";
      const h = area.scrollHeight;
      area.style.height = prevH;
      return h;
    };

    /** Drops the temporary transition class once the glide has settled. */
    let settleTimer = 0;
    const stopAnim = (): void => {
      window.clearTimeout(settleTimer);
      area.classList.remove("is-expand-anim");
      area.removeEventListener("transitionend", onTransitionEnd);
    };
    const onTransitionEnd = (ev: TransitionEvent): void => {
      if (ev.target !== area || ev.propertyName !== "height") return;
      stopAnim();
    };

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const expanding = !wrap.classList.contains("is-expanded");

      // Read the animated-from height while the current state is still applied.
      const startH = area.getBoundingClientRect().height;

      // Arm the glide first — it is also what lifts the min/max clamps, so the
      // probe and the pin below are never squeezed by them.
      area.classList.remove("is-expand-anim");
      if (expanding) rememberRestBounds();
      area.classList.add("is-expand-anim");
      wrap.classList.remove("is-expanded");

      // Resolve the fold target BEFORE pinning the start height.  Reading
      // `scrollHeight` forces a layout, so if the probe ran after the pin it
      // would commit `height: auto` as the transition's "before" value — and
      // `auto` is not interpolable, which is what made the fold snap.
      const endH = expanding
        ? 0
        // CSS resolves min-height over max-height when the two cross, so mirror
        // that rather than a plain clamp.
        : Math.max(restMin, Math.min(contentHeight(), restMax));

      // Pin + commit a definite start height, so both endpoints are lengths.
      area.style.height = `${startH}px`;
      void area.offsetHeight;

      // Only now move the target: `.is-expanded` supplies the fixed unfold
      // height, the fold glides back to the measured content height.
      if (expanding) {
        wrap.classList.add("is-expanded");
      } else {
        area.style.height = `${endH}px`;
      }

      sync();
      area.focus();

      window.clearTimeout(settleTimer);
      area.addEventListener("transitionend", onTransitionEnd);
      settleTimer = window.setTimeout(stopAnim, ANIM_TIMEOUT_MS);
    });

    wrap.appendChild(zone);
    sync();

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: zone });
    }
  });
}
