/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre. The Encre project belongs to the Dunimd Team.
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

import { getState } from "../core/state.js";

let _isMac: boolean | null = null;
function isMac(): boolean {
  if (_isMac === null) {
    _isMac = navigator.platform.includes("Mac") || navigator.userAgent.includes("Macintosh");
  }
  return _isMac;
}

const MOD_KEYS: Record<string, { mac: string; win: string }> = {
  ctrlcmd: { mac: "⌘", win: "Ctrl" },
  ctrl: { mac: "⌃", win: "Ctrl" },
  alt: { mac: "⌥", win: "Alt" },
  shift: { mac: "⇧", win: "Shift" },
};

function formatMod(mod: string): string {
  const m = MOD_KEYS[mod];
  if (!m) return mod.charAt(0).toUpperCase() + mod.slice(1);
  return isMac() ? m.mac : m.win;
}

const KEY_ALIASES: Record<string, string> = {
  escape: "Esc",
  backspace: "⌫",
  enter: "↵",
  up: "↑",
  down: "↓",
  left: "←",
  right: "→",
  tab: "Tab",
  space: "Space",
  "`": "`",
  ",": ",",
  ".": ".",
  ";": ";",
  "'": "'",
  "[": "[",
  "]": "]",
  "\\": "\\",
  "/": "/",
  "=": "=",
  "-": "-",
};

function formatKey(key: string): string {
  return KEY_ALIASES[key] || key.toUpperCase();
}

/**
 * Formats a `+`-separated shortcut pattern into a display label.
 *
 * @param pattern - Key pattern such as `ctrlcmd+shift+p`.
 * @returns A platform-appropriate string (e.g. `⌘⇧P` or `Ctrl+Shift+P`).
 */
export function formatShortcut(pattern: string): string {
  const parts = pattern.split("+");
  const key = parts.pop() || "";
  const mods = parts.map(formatMod);
  if (isMac()) {
    return [...mods, formatKey(key)].join("");
  }
  return [...mods, formatKey(key)].join("+");
}

/** Returns the current platform label, either `"mac"` or `"win"`. */
export function platformLabel(): "mac" | "win" {
  return isMac() ? "mac" : "win";
}

/**
 * Looks up the display label for a command id from the user's keybind settings.
 *
 * @param id - The command identifier (e.g. `send`, `newSession`).
 * @returns The formatted shortcut, or `null` if none is configured.
 */
export function lookupShortcut(id: string): string | null {
  const cfg = (getState().settings.keybinds as any);
  const binds: any[] = cfg?.keybinds || [];
  const entry = binds.find((b: any) => b.id === id);
  if (!entry || !entry.keys || entry.keys.length === 0) return null;
  return formatShortcut(entry.keys[0]);
}

/**
 * Appends a shortcut hint to an existing UI title.
 *
 * @param existing - The existing title text.
 * @param shortcut - The shortcut label to append (may be empty).
 * @returns `existing` unchanged when no shortcut is given, else `existing (shortcut)`.
 */
export function augmentTitle(existing: string, shortcut: string): string {
  if (!shortcut) return existing;
  return `${existing} (${shortcut})`;
}
