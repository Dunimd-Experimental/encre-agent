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
 * Agents view module.
 *
 * Renders the read-only "active agents" panel of the Encre Agent Desktop client.
 * The view is currently a static placeholder that explains how to spawn and
 * manage agents; it is mounted once and re-rendered by the {@link Agents}
 * class whenever the view is (re)opened.
 */

import { t } from "./i18n.js";

/**
 * Renders and manages the "Active Agents" view inside the renderer.
 *
 * The component owns the `#agents-view` container and paints a localized
 * placeholder describing how agents work. It is intentionally simple and does
 * not yet bind to a live agent registry.
 */
export class Agents {
  private el: HTMLElement;

  /**
   * Creates the Agents view and performs the initial render.
   *
   * Looks up the `#agents-view` container from the DOM and immediately draws
   * the placeholder content.
   */
  constructor() {
    this.el = document.getElementById("agents-view")!;
    this.render();
  }

  /**
   * Renders the localized "active agents" placeholder into the container.
   *
   * Injects the static markup (header, count badge and empty-state hint) and
   * refreshes any lucide icons that were injected as inline SVG.
   */
  render(): void {
    this.el.innerHTML = `
      <div class="agents-container">
        <div class="agents-header">
          <h2>${t("agents.activeAgents")}</h2>
          <span class="agents-count">${t("agents.activeCount", { count: 0 })}</span>
        </div>
        <div class="agents-list">
          <div class="si-empty-center">
            <i data-lucide="bot" class="lucide"></i>
            <span class="si-empty-title">${t("agents.noActiveAgents")}</span>
            <span class="si-empty-sub">${t("agents.agentsHint")}</span>
          </div>
        </div>
      </div>
    `;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.el });
    }
  }
}
