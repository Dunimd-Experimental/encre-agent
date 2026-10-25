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
 * Automation panel controller.
 *
 * Manages the "Automation" content-area page (a demoted view — it covers the
 * main slot like settings does, without touching the sidebar or the mode).
 *
 * Visibility is expressed as the shared `hidden` class plus the shared
 * sub-view motion: `showViewAnimated` / `hideViewAnimated` from ui/animate.ts
 * drive the fade-up entrance and the `.view-exit` dissolve declared in the
 * "Sub-view switch" block near the end of styles.css — the exact same motion
 * settings / search / agents use.
 *
 * This controller therefore animates nothing itself. It flips state, fires
 * its callbacks and lets the stylesheet move things. That is deliberate: the
 * old implementation drove the slide with inline transform/transition plus a
 * timer, and any interruption (a second click mid-flight, a minimised window
 * pausing rAF) could strand inline positioning on the shared main-area
 * elements — the layout residue this rewrite removes.
 */

import { showViewAnimated, hideViewAnimated } from "../ui/animate.js";

export class AutomationPanel {
  private _automationView: HTMLElement | null = null;

  /** Called each time the automation panel opens */
  public onShow: (() => void) | null = null;
  /** Called each time the automation panel hides */
  public onHide: (() => void) | null = null;

  constructor() {
    this._automationView = document.getElementById("automation-view");
  }

  /** Whether the automation view is currently visible. */
  get isActive(): boolean {
    return (
      !!this._automationView &&
      !this._automationView.classList.contains("hidden")
    );
  }

  /** Shows the automation view. No-op when already open.
   *
   *  Demoted view: automation no longer hides the sidebar — it renders as a
   *  plain content-area page (like settings), so the sidebar stays as-is.
   *  Entrance rides the shared sub-view fade-up (`@starting-style`). */
  show(): void {
    if (!this._automationView || this.isActive) return;
    showViewAnimated(this._automationView);
    this.onShow?.();
  }

  /** Hides the automation view. No-op when not open.
   *  Exit rides the shared `.view-exit` dissolve (ui/animate.ts). */
  hide(): void {
    if (!this._automationView || !this.isActive) return;
    hideViewAnimated(this._automationView);
    this.onHide?.();
  }

  /** Toggles automation panel visibility (show when hidden, hide when shown). */
  toggle(): void {
    if (this.isActive) {
      this.hide();
    } else {
      this.show();
    }
  }
}
