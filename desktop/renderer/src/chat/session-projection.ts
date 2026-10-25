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

import type { SessionEntryData } from "../core/types.js";

/** Deterministic hue from a workspace name (mirrors the backend icon
 *  generator), shared by every icon-rendering surface. */
export function nameHue(name: string): number {
  let h = 0;
  const n = String(name || "");
  for (let i = 0; i < n.length; i++) {
    h = ((h * 31) + n.charCodeAt(i)) >>> 0;
  }
  return h % 360;
}

function escIconText(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * Workspace icon markup for a session row — shared by the sidebar session
 * list, the workspace manager archive view and (mirrored) the tray popup so
 * every surface renders the same icons.
 *
 * With `force` (workspace/iWork rows), an icon is ALWAYS returned: the real
 * icon when the workspace is known, a deterministic letter avatar derived
 * from the path when the workspace list has not loaded it yet, and a neutral
 * default avatar when the session carries no workspace path at all (legacy
 * data) — the icon therefore never disappears from any surface.
 */
export function workspaceIconHtml(
  wsPath: string,
  workspaces: readonly { path: string; name: string; icon_data?: string }[],
  force = false,
): string {
  const norm = (p: string) => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const w = workspaces.find((x) => norm(x.path) === norm(wsPath));
  if (w) {
    const initial = (w.name.trim()[0] || "?").toUpperCase();
    const inner = w.icon_data
      ? `<img class="session-ws-icon-img" src="${w.icon_data}" alt="" draggable="false">`
      : `<span class="session-ws-icon-letter">${escIconText(initial)}</span>`;
    const bgAttr = w.icon_data ? "" : ` style="background:hsl(${nameHue(w.name)}, 62%, 46%)"`;
    return `<span class="session-ws-icon"${bgAttr}>${inner}</span>`;
  }
  if (!wsPath) {
    if (!force) return "";
    // Workspace row whose attribution is missing (legacy data): neutral
    // avatar so the icon never disappears.
    return `<span class="session-ws-icon" style="background:hsl(0, 0%, 42%)"><span class="session-ws-icon-letter">W</span></span>`;
  }
  const base = String(wsPath).split(/[\\/]/).filter(Boolean).pop() || wsPath;
  const initial = (base[0] || "?").toUpperCase();
  return `<span class="session-ws-icon" style="background:hsl(${nameHue(base)}, 62%, 46%)"><span class="session-ws-icon-letter">${escIconText(initial)}</span></span>`;
}

export interface TraySessionData {
  normal: SessionEntryData[];
  iwork: SessionEntryData[];
  /** Workspace records (path/name/icon_data only) so the tray popup can
   *  render each iWork session's owning-workspace icon. */
  workspaces: Array<{ path: string; name: string; icon_data?: string }>;
}

/** Produces one stable workspace key for all renderer-side consumers. */
export function normalizeWorkspacePath(path: string): string {
  return path.trim().replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

/** Keeps one newest entry per session id. */
export function dedupeSessions(sessions: readonly SessionEntryData[]): SessionEntryData[] {
  const byId = new Map<string, SessionEntryData>();

  for (const session of sessions) {
    if (!session?.session_id) continue;
    const previous = byId.get(session.session_id);
    if (!previous || (session.last_active || 0) >= (previous.last_active || 0)) {
      byId.set(session.session_id, session);
    }
  }

  return [...byId.values()];
}

/**
 * Builds the tray popup session data. Both normal and iwork channels are
 * flattened, deduplicated session lists — mirroring the unified sidebar
 * "All tasks" view, so the tray no longer groups iwork sessions by
 * workspace tree.
 */
export function buildTraySessionData(
  normalSessions: readonly SessionEntryData[],
  iworkSessions: readonly SessionEntryData[],
  workspaces?: readonly { path: string; name: string; icon_data?: string }[],
): TraySessionData {
  // Only sessions with at least one message count as history — brand-new
  // empty sessions must not appear in the tray, mirroring the sidebar. The
  // tray is a quick-switch surface, not an archive: shipping thousands of
  // rows over IPC on every state change is pure structured-clone cost.
  const active = (sessions: readonly SessionEntryData[]) => {
    const out: SessionEntryData[] = [];
    const seen = new Set<string>();
    for (const session of sessions) {
      const sid = session.session_id;
      if (!sid || seen.has(sid)) continue;
      seen.add(sid);
      if ((session.message_count || 0) <= 0) continue;
      out.push(session);
      if (out.length >= 100) break;
    }
    return out;
  };
  return {
    normal: active(normalSessions),
    iwork: active(iworkSessions),
    // Only the fields the tray needs for icon rendering travel over IPC.
    workspaces: (workspaces || [])
      .filter((w) => w && w.path && w.name)
      .map((w) => ({ path: w.path, name: w.name, icon_data: w.icon_data })),
  };
}
