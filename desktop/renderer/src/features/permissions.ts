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
 * Runtime permission prompting.
 *
 * Implements the renderer-side "ask the user" branch of Encre's tool-permission
 * policy. Static allow/deny decisions are resolved once by the Rust backend and
 * never reach this layer; this module only handles the interactive prompt that
 * appears when a tool invocation needs explicit user consent, recording the
 * decision back into the backend policy table so later calls are silent.
 */

import { setPendingPermission } from "../core/state.js";
import { t } from "./i18n.js";
import { Dialog } from "../ui/dialog.js";

/**
 * Unified permission prompt.
 *
 * The user only ever sees this dialog for the "ask" branch.  Static
 * allow/deny decisions are decided by Rust in a single call and never
 * surface here.  When the user clicks allow, the answer is recorded
 * in the Rust policy table so subsequent invocations of the same tool
 * are silent.
 *
 * Visually the prompt reuses the standard ``Dialog.confirm`` shell
 * (same overlay/card/buttons as the right-click "Delete" confirmation),
 * keeping a single design language across the app.
 */
export class Permissions {
  private active = false;

  /**
   * Shows the permission prompt for a tool invocation.
   *
   * @param toolName - Name of the tool requesting access.
   * @param reason   - Human-readable justification shown to the user.
   * @param cb       - Callback invoked with `"allow"` (one-off), `"allow_session"`
   *                   (remember for this session) or `"deny"`.
   *
   * Reuses the standard install/confirm dialog shell. When the user allows
   * with "always allow this session", the answer is recorded in the Rust
   * policy table so subsequent invocations of the same tool are silent.
   *
   * If a prompt is already open the new request is rejected immediately
   * (`"deny"`) so the agent loop is never blocked.
   */
  show(
    toolName: string,
    reason: string,
    cb: (decision: "allow" | "allow_session" | "deny") => void,
    container?: HTMLElement,
  ): void {
    if (this.active) {
      // A previous request is still open -- fall through to its
      // resolver with "deny" so the agent loop is not blocked.
      cb("deny");
      return;
    }
    this.active = true;
    setPendingPermission({ tool: toolName, reason });

    const title = t("permissions.title", { name: toolName });
    const descKey = `permissions.settings.desc.${toolName}`;
    const translatedDesc = t(descKey as any);
    const description = translatedDesc === descKey ? reason : translatedDesc;
    const body = t("permissions.body", { name: toolName, description });

    // Three-way choice: deny / always-allow-this-session / allow-once.
    Dialog.tripleConfirm(
      title,
      body,
      {
        primary: t("permissions.allowOnce"),
        tertiary: t("permissions.allowSession"),
        secondary: t("permissions.deny"),
        priority: "critical",
        container,
      },
    )
      .then((choice) => {
        this.active = false;
        setPendingPermission(null);
        cb(choice === "primary" ? "allow" : choice === "tertiary" ? "allow_session" : "deny");
      })
      .catch(() => {
        this.active = false;
        setPendingPermission(null);
        cb("deny");
      });
  }

  /**
   * Dismisses any pending permission prompt and clears the pending state.
   *
   * The underlying dialog removes itself on user interaction, so this only
   * resets the internal `active` flag and the pending-permission record.
   */
  hide(): void {
    // No persistent overlay to clean up -- Dialog.confirmInstall
    // removes itself when the user clicks a button or presses Esc.
    setPendingPermission(null);
    this.active = false;
  }
}
