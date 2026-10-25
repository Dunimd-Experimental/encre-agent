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

import { Workspace } from "../session/workspace.js";
import { AutomationPanel } from "../session/iclaw.js";
import { getState } from "../core/state.js";

export type AppMode = "normal" | "iwork";

/**
 * ModeTransitionManager — single orchestrator for mode switching (normal ⇄
 * iwork) and for the automation PAGE (a demoted content-area view).
 *
 * Automation is NOT a mode anymore: it is an overlay page on the content
 * area, orthogonal to the mode state.  It can be opened from normal AND from
 * iwork; in both cases the current mode (seg highlight, workspaceMode, the
 * workspace itself) stays exactly as it is.  Closing the page simply reveals
 * whatever was underneath.
 *
 * Animating is not this class's job. Each branch below only changes *state* —
 * entering/exiting a workspace, opening the automation page — by toggling the
 * classes the stylesheet listens for. The moves themselves are declared in
 * CSS (see "Unified Motion" in styles.css), which means:
 *
 *   - Switching no longer waits on an animation. `refreshAllData`,
 *     `cleanupContentArea`, `chat.render()` and the backend round-trips all
 *     run immediately; the view slides under them.
 *   - Nothing here writes inline positioning, so nothing can be left behind
 *     by an interrupted switch. Rapid switching simply re-targets the CSS
 *     transition from wherever it had reached.
 */
export class ModeTransitionManager {
  private _workspace: Workspace;
  private _automationPanel: AutomationPanel;
  private _onModeChange: () => void;
  /** Invoked when the automation page closes, before it slides away, so the
   *  in-panel sub-agent detail (breadcrumb, activeExecution, header buttons)
   *  can be torn down first and not leak into whatever is revealed. */
  private _onLeaveAutomation: () => void;
  /** Invoked when the automation page opens over the workspace (iwork) —
   *  used by the app to close the task-overview view before the page covers
   *  the area. */
  private _onLeaveWorkspace: () => void;
  private _transitioning = false;

  constructor(opts: {
    workspace: Workspace;
    automationPanel: AutomationPanel;
    onModeChange: () => void;
    onLeaveAutomation?: () => void;
    onLeaveWorkspace?: () => void;
  }) {
    this._workspace = opts.workspace;
    this._automationPanel = opts.automationPanel;
    this._onModeChange = opts.onModeChange;
    this._onLeaveAutomation = opts.onLeaveAutomation ?? (() => {});
    this._onLeaveWorkspace = opts.onLeaveWorkspace ?? (() => {});
  }

  get isTransitioning(): boolean {
    return this._transitioning;
  }

  /** The active MODE — automation is a page, never a mode. */
  getCurrentMode(): AppMode {
    return getState().workspaceMode === "iwork" ? "iwork" : "normal";
  }

  /** True while the automation PAGE covers the content area. */
  isAutomationOpen(): boolean {
    return this._automationPanel.isActive;
  }

  async switchMode(target: AppMode): Promise<void> {
    const current = this.getCurrentMode();
    if (target === current || this._transitioning) return;
    this._transitioning = true;

    // Suppress workspace's internal onModeChange — we call it ourselves
    // at the right moment to avoid double-cleanup and to make sure the
    // welcome animation fires exactly once per transition.
    const origOnModeChange = this._workspace.onModeChange;
    this._workspace.onModeChange = null;

    try {
      if (target === "iwork") {
        // Slide the automation page away first (if open) — it must never
        // cover the workspace that is about to come back.
        if (this._automationPanel.isActive) {
          this._onLeaveAutomation();
          this._automationPanel.hide();
        }
        await this._workspace.enter();
        origOnModeChange?.();
      } else { // normal
        if (this._automationPanel.isActive) {
          this._onLeaveAutomation();
          this._automationPanel.hide();
        }
        await this._workspace.exit();
        origOnModeChange?.();
      }
    } finally {
      this._workspace.onModeChange = origOnModeChange;
      this._transitioning = false;
    }
  }

  /** Opens the automation PAGE. Never touches the mode: the workspace (if
   *  active) stays alive underneath and returns unchanged on close. */
  openAutomation(): void {
    if (this._automationPanel.isActive) return;
    this._onLeaveWorkspace();
    this._automationPanel.show();
  }

  /** Closes the automation PAGE. Reveals whatever mode was underneath. */
  closeAutomation(): void {
    if (!this._automationPanel.isActive) return;
    this._onLeaveAutomation();
    this._automationPanel.hide();
  }

  /** Keyboard shortcut / tray toggle for the automation PAGE. */
  toggleAutomation(): void {
    if (this._automationPanel.isActive) {
      this.closeAutomation();
    } else {
      this.openAutomation();
    }
  }
}
