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
 * Tool-call detail panel.
 *
 * Manages the right-hand "detail" panel that shows the parameters and result of
 * the currently selected tool call in a chat message. Rendering is throttled via
 * `requestAnimationFrame` so that dense `tool_call_delta` streams do not thrash
 * the layout, and the panel reacts to state changes through a global subscription.
 */

import { getState, subscribe } from "../core/state.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { t } from "./i18n.js";

/**
 * Renders and manages the tool-call detail panel.
 *
 * The panel is shown only when a tool call is "active" (selected by the user).
 * It displays the tool name, execution status, parameters and — once available —
 * the tool result, escaping all content to avoid HTML injection.
 */
export class Tools {
  private panel: HTMLElement;
  private content: HTMLElement;
  /** Tool whose detail markup is currently painted (scroll preservation). */
  private paintedToolId: string | null = null;

  /**
   * Constructor: grabs the panel/container elements and subscribes to state.
   *
   * Registers a subscription so the panel re-renders (throttled) whenever the
   * global chat state changes.
   */
  constructor() {
    this.panel = document.getElementById("detail-panel")!;
    this.content = document.getElementById("detail-content")!;
    // With no tool selected the panel is hidden and has nothing to update;
    // scheduling a frame per streamed token was pure churn. Still render on
    // the transition back to "no selection" so the panel hides.
    let wasActive = !!getState().activeToolId;
    subscribe(() => {
      const active = !!getState().activeToolId;
      if (active || wasActive) this.requestRender();
      wasActive = active;
    }, ["activeToolId", "messages"]);
  }

  /** RAF-throttled render — prevents layout thrash during dense tool_call_delta streams. */
  requestRender(): void {
    scheduleRender("tools", () => this.render(), 40);
  }

  /**
   * Renders the detail panel for the currently active tool call.
   *
   * Hides the panel when no tool is active or the referenced tool call cannot be
   * found, otherwise shows the name, status icon, parameters and result.
   */
  render(): void {
    const state = getState();
    if (!state.activeToolId) {
      this.panel.classList.add("hidden");
      return;
    }

    const toolCall = this.findToolCall(state.activeToolId);
    if (!toolCall) {
      this.panel.classList.add("hidden");
      return;
    }

    this.panel.classList.remove("hidden");
    const statusIcon = toolCall.status === "done" ? "✓" : toolCall.status === "pending" ? "◌" : "●";
    const statusClass = toolCall.status === "done" ? "done" : toolCall.status === "pending" ? "pending" : "running";
    // Keep the reader's place while one tool's params/result streams;
    // selecting another tool starts at the top.
    const prevTop = this.content.scrollTop;
    const sameTool = this.paintedToolId === state.activeToolId;
    this.paintedToolId = state.activeToolId;
    this.content.innerHTML = `
      <div class="detail-header">
        <h3>${toolCall.name}</h3>
        <span class="detail-status ${statusClass}">${statusIcon}</span>
        <button class="btn-icon btn-icon--md" id="btn-detail-close">&times;</button>
      </div>
      <div class="detail-section">
        <h4>${t("toolsPanel.parameters")}</h4>
        <pre>${this.escapeHtml(JSON.stringify(toolCall.params, null, 2))}</pre>
      </div>
      ${
        toolCall.result
          ? `<div class="detail-section">
              <h4>${t("toolsPanel.result")}</h4>
              <pre class="${toolCall.isError ? "error" : ""}">${this.escapeHtml(toolCall.result)}</pre>
            </div>`
          : `<div class="detail-section"><p>${t("chat.running")}</p></div>`
      }
    `;
    this.content.scrollTop = sameTool ? prevTop : 0;

    const btn = document.getElementById("btn-detail-close");
    btn?.addEventListener("click", () => {
      (window as any).__state_setActiveToolId?.(null);
    });
  }

  /**
   * Searches the message list for a tool call with the given id.
   *
   * @param id - The unique tool-call identifier to look up.
   * @returns The matching tool call, or `null` if not found.
   */
  private findToolCall(id: string) {
    const state = getState();
    for (const msg of state.messages) {
      for (const tc of msg.toolCalls) {
        if (tc.id === id) return tc;
      }
    }
    return null;
  }

  /** Escapes a string for safe insertion into `innerHTML` by using the DOM. */
  private escapeHtml(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }
}
