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
 * Plugin lifecycle facts the chat layer needs but does not own.
 *
 * The backend pushes one snapshot per plugin on every lifecycle change
 * (`plugin_changed`).  Management surfaces read their own copy from that
 * event; the fault card cannot, because it renders inside a pure string
 * function with no state access.  This module is the one place both kinds of
 * consumer read from, so a plugin's identity, version and support channels
 * are always the same numbers.
 *
 * The full last snapshot is kept here as well, readable through
 * `pluginStates()`.  The frame goes out on connect and after warmup, so a
 * surface that only subscribes when the user opens it would otherwise sit on
 * a "loading" state forever, waiting for a push that already happened.
 */

import type { PluginStateEntry } from "./types.js";

/** Emitted after a snapshot arrives, so open cards can repaint. */
export const PLUGIN_STATES_CHANGED = "encre-plugin-info-changed";

/** The subset of a lifecycle entry a card cares about. */
export interface PluginInfo {
  name: string;
  version: string;
  /** Short human-friendly label; blank falls back to `name` when rendered. */
  display_name?: string;
  author?: string;
  email?: string;
  homepage?: string;
  repository?: string;
  docs?: string;
  icon?: string;
}

let _byName: Map<string, PluginInfo> = new Map();
let _states: PluginStateEntry[] | null = null;

function normalize(entry: Partial<PluginInfo> & { name?: string }): PluginInfo | null {
  const name = (entry.name || "").trim();
  if (!name) return null;
  return {
    name,
    version: entry.version || "",
    display_name: entry.display_name,
    author: entry.author,
    email: entry.email,
    homepage: entry.homepage,
    repository: entry.repository,
    docs: entry.docs,
    icon: entry.icon,
  };
}

/** Replaces the snapshot. Idempotent; an empty list clears it. */
export function setPluginInfo(entries: Array<Partial<PluginInfo>>): void {
  const next = new Map<string, PluginInfo>();
  for (const entry of entries || []) {
    const info = normalize(entry);
    if (info) next.set(info.name, info);
  }
  _byName = next;
  // Same array, full fidelity: management surfaces that need tier, state,
  // installed/enabled read it back through `pluginStates()` instead of
  // keeping a second copy that can lag behind this one.
  _states = (entries || []) as PluginStateEntry[];
  window.dispatchEvent(new CustomEvent(PLUGIN_STATES_CHANGED));
}

/** The last complete lifecycle snapshot, `null` while no frame has arrived. */
export function pluginStates(): PluginStateEntry[] | null {
  return _states;
}

/** Look up one plugin by manifest name. */
export function pluginInfo(name: string | undefined): PluginInfo | undefined {
  if (!name) return undefined;
  return _byName.get(name);
}

/**
 * The support channels of a plugin, in the order the fault card walks them:
 * e-mail first, then the official site, then the repository, then docs.
 */
export function pluginContact(name: string | undefined): { kind: string; value: string }[] {
  const info = pluginInfo(name);
  if (!info) return [];
  const order: Array<[string, string | undefined]> = [
    ["email", info.email],
    ["homepage", info.homepage],
    ["repository", info.repository],
    ["docs", info.docs],
  ];
  return order
    .filter(([, value]) => !!value && !!value.trim())
    .map(([kind, value]) => ({ kind, value: value!.trim() }));
}
