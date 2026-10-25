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
 */

/**
 * Settings → 插件 panel.
 *
 * This page IS the plugin market: one `Market` instance mounted with
 * `mode: "installed"`, so the top bar, the category tabs, the search box,
 * the tag chips, the rows and the plugin detail page are the very same
 * components as the store.  The only differences are the data plane behind
 * them — this machine's registry instead of the central index — and the two
 * header actions the store alone needs (已安装 leads here; 添加插件 belongs
 * to the store).
 *
 * Nothing about the list is re-implemented here: install/update,
 * enable/disable, uninstall, search, filtering and the detail page all run
 * through the market's own code paths, so the two surfaces cannot drift.
 */

import { Market } from "../features/market.js";

let _store: Market | null = null;

/** Renders the plugins panel (called on panel switch and snapshot change). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function renderPluginPanelImpl(this: any): void {
  const panel = this.panels.plugins as HTMLElement | undefined;
  if (!panel) return;
  if (!_store) _store = new Market(panel, { mode: "installed", embedded: true });
  _store.mount();
}
