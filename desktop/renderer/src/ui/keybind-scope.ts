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

/**
 * View scope for every configurable keybind.
 *
 * A keybind is only meaningful in some UI contexts: `Ctrl+Z` "undo last
 * message" belongs to a session view, not to the settings panel; `Ctrl+Alt+N`
 * "new automation" belongs to the automation view, not to a chat. Without a
 * scope gate, `bindKeyboardShortcuts` fires the action no matter which view
 * is foregrounded, so a mis-timed shortcut could click a hidden button in a
 * background session or mutate a panel that isn't even rendered.
 *
 * `scopeAllows(id)` is the single gate consulted by the keydown dispatcher
 * before an action runs. It also feeds `preventDefault`: a shortcut whose
 * scope isn't satisfied must NOT eat the key, so the browser's native
 * handler (e.g. `Ctrl+Z` in a settings form) keeps working.
 */

import { getState } from "../core/state.js";

export type KeybindScope =
  | "global"      // fires from any view
  | "chat"        // not in settings, not in automation (welcome screen OK)
  | "session"     // chat view AND an active session is loaded
  | "automation"  // automation view is foregrounded
  | "settings";   // settings panel is foregrounded

/**
 * Authoritative scope map. The default keybinds in `harness/encre/keybinds.py`
 * are user-editable, but scopes are a UI-layer concern — they describe where
 * an action is even valid, not what the action does. Keeping the map here
 * (rather than in the backend defaults) means the user cannot accidentally
 * un-scope a shortcut by editing their keybinds file.
 *
 * Unknown ids default to `"global"` — an unmapped binding is at worst as
 * intrusive as the current pre-scope behavior, never more.
 */
export const KEYBIND_SCOPES: Record<string, KeybindScope> = {
  // ── Application ─────────────────────────────────────────────────────
  quit: "global",
  devtools: "global",
  reload: "global",
  fullscreen: "global",

  // ── Session creation (allowed at welcome screen too) ────────────────
  new_session: "chat",
  new_temp_chat: "chat",

  // ── Session operations on an existing session ───────────────────────
  next_session: "session",
  prev_session: "session",
  delete_session: "session",
  rename_session: "session",
  export_session: "session",

  // ── Messages ────────────────────────────────────────────────────────
  edit_last_message: "session",
  copy_last_response: "session",
  undo_message: "session",
  delete_message: "session",
  retry: "session",
  retry_detailed: "session",
  retry_concise: "session",

  // ── Input area (operates on the composer) ───────────────────────────
  attach_file: "session",
  upload_file: "session",
  history_prev: "session",
  history_next: "session",

  // ── Modes (operates on the composer) ────────────────────────────────
  toggle_plan_mode: "session",
  toggle_spec_mode: "session",
  cancel: "session",

  // ── Navigation ──────────────────────────────────────────────────────
  // `view_chat` / `view_automation` switch views — they must be reachable
  // from anywhere, including settings and automation.
  view_chat: "global",
  view_automation: "global",
  // Sidebar toggle requires an existing conversation per product rule.
  toggle_sidebar: "session",

  // ── Search ──────────────────────────────────────────────────────────
  search_global: "global",
  search_settings: "settings",

  // ── Settings ────────────────────────────────────────────────────────
  open_settings: "global",
  settings_general: "global",
  settings_models: "global",
  settings_skills: "global",
  settings_mcp: "global",
  settings_agent: "global",
  settings_library: "global",
  settings_terminal: "global",
  settings_editor: "global",
  settings_review: "global",
  settings_browser: "global",
  settings_memory: "global",
  settings_usage: "global",
  settings_about: "global",
  settings_gateway: "global",
  settings_plugins: "global",
  settings_shortcuts: "global",
  settings_storage: "global",
  settings_developer: "global",

  // ── Panels (session inner sidebar — needs an active session) ────────
  toggle_terminal: "session",
  toggle_browser: "session",
  toggle_editor: "session",
  toggle_review: "session",
  toggle_info: "session",
  toggle_files: "session",
  prev_tab: "session",
  next_tab: "session",

  // ── Automation ──────────────────────────────────────────────────────
  // Only "open" is global — the other four only make sense once the
  // automation panel is foregrounded.
  automation_open: "global",
  automation_new: "automation",
  automation_run: "automation",
  automation_toggle: "automation",
  automation_history: "automation",

  // ── Workspace ───────────────────────────────────────────────────────
  workspace_open: "global",
  workspace_close: "global",

  // ── Notifications ───────────────────────────────────────────────────
  notifications_open: "global",
  notifications_clear: "global",

  // ── Appearance ──────────────────────────────────────────────────────
  toggle_theme: "global",
  toggle_language: "global",

  // ── General ─────────────────────────────────────────────────────────
  show_shortcuts: "global",
};

export function keybindScope(id: string): KeybindScope {
  return KEYBIND_SCOPES[id] ?? "global";
}

function settingsVisible(): boolean {
  return document.getElementById("app")?.classList.contains("settings-mode") ?? false;
}

function automationVisible(): boolean {
  const av = document.getElementById("automation-view");
  return av ? !av.classList.contains("hidden") : false;
}

/** Chat view = not settings, not automation. Includes the welcome screen. */
function chatViewVisible(): boolean {
  return !settingsVisible() && !automationVisible();
}

/**
 * Whether a keybind is allowed to fire in the current UI context.
 * Returns `true` for unscoped / global bindings.
 */
export function scopeAllows(id: string): boolean {
  switch (keybindScope(id)) {
    case "global": return true;
    case "settings": return settingsVisible();
    case "automation": return automationVisible();
    case "chat": return chatViewVisible();
    case "session": return chatViewVisible() && !!getState().sessionId;
  }
  return true;
}

/**
 * Whether the dispatcher should `preventDefault` for a keybind match.
 * Same predicate as `scopeAllows`, but surfaced separately so the call
 * site reads clearly: a key that won't dispatch must not be eaten either.
 */
export function scopePreventsDefault(id: string): boolean {
  return scopeAllows(id);
}

/**
 * Drop keybind entries whose id no longer exists in `KEYBIND_SCOPES`.
 *
 * Stale entries survive in persisted state: the backend's encrypted
 * `keybinds.json` outlives removed features, and the localStorage fallback
 * mirrors it. Since translations for removed ids are gone too, the panel
 * would render raw keys like `shortcuts.workspace_reindex`. Both persisted
 * entry points in `stream.ts` run through here so the store only ever holds
 * ids that the current build actually knows.
 *
 * Returns the input object untouched when nothing is stale, so downstream
 * equality checks (subscribe comparisons) see the same reference.
 */
export function pruneStaleKeybinds<T>(cfg: T): T {
  const c = cfg as unknown as { keybinds?: unknown[] } | null;
  if (!c || !Array.isArray(c.keybinds)) return cfg;
  const kept = c.keybinds.filter(
    (e): e is Record<string, unknown> => !!e && typeof e === "object" && typeof (e as { id?: unknown }).id === "string" && (e as { id: string }).id in KEYBIND_SCOPES,
  );
  if (kept.length === c.keybinds.length) return cfg;
  return { ...(c as object), keybinds: kept } as unknown as T;
}
