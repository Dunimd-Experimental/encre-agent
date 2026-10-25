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
 */

/**
 * The two progress bars the app draws — the same 3px track and green fill,
 * distinguished only by what they claim:
 *
 *   `indeterminate` — we are waiting on something outside our control (the
 *   backend warming up, an index refresh), so the sweep loops and no end is
 *   implied.
 *   `completing`    — the action does run to its end (install, remove, switch),
 *   so the bar fills exactly once and holds full until the reply lands.
 *
 * Neither invents a percentage.  A caller that shows `completing` owes it
 * PROGRESS_HOLD_MS of uninterrupted screen time — see the note on that value.
 */

/** One full pass of the `completing` fill, and therefore the shortest time a
 *  completing bar may stay on screen.  Mirrors `--progress-hold` in
 *  styles.css; a lifecycle reply usually lands well before this, and painting
 *  the new state then would cut the bar off half-run. */
export const PROGRESS_HOLD_MS = 1100;

/** Backstop for a lost lifecycle reply: without it the held repaint would never
 *  be released and the surface would freeze on a full bar. */
export const PROGRESS_WATCHDOG_MS = 30_000;

export function progressBar(kind: "indeterminate" | "completing"): string {
  return `<div class="workflow-progress-bar">
  <div class="workflow-progress-fill ${kind}"></div>
</div>`;
}
