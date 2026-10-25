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
 * Hover tooltip content for tabs.
 *
 * Shared by the session sidebar tabs and the Encre Agent Desktop Subwindow (ESD) sub-app tab bar so both
 * surface exactly the same information, in exactly the same order, with
 * exactly the same layout: header row (icon + title), then one field per
 * line with a muted label column — all flush left — and over-long values
 * truncated with an ellipsis instead of wrapping to a second line. The
 * permissions of the page, when any were requested, are always the very
 * last block.
 *
 *   header    [favicon] 页面标题
 *   大小      12.4 KB
 *   修改时间   2026-09-10 17:30
 *   位置      D:\…\file.ts
 *   [camera] 摄像头  [mic] 麦克风
 */

import { t } from "../features/i18n.js";
import { setTooltipModel, type TooltipModel, type TooltipPermission } from "./tooltip.js";

export interface TabTooltipData {
  /** Header: page title, file name, or tab label. */
  title: string;
  /** Header icon: page favicon or file-type icon. */
  icon?: string;
  /** Location — a URL for browser tabs, a path for file tabs. */
  location?: string;
  /** True when `location` is a URL (labelled "URL") instead of a path. */
  isUrl?: boolean;
  /** File size in bytes. */
  size?: number;
  /** Last-modified timestamp in ms. */
  mtime?: number;
  /** Permissions actually requested by the page, shown at the very bottom. */
  permissions?: Array<{ name: string; granted: boolean }>;
}

/** Human readable file size, base 1024. */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** Localised date + time, or "" when the timestamp is unusable. */
export function formatTimestamp(ms: number, locale?: string): string {
  if (!Number.isFinite(ms) || ms <= 0) return "";
  try {
    return new Date(ms).toLocaleString(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return new Date(ms).toLocaleString();
  }
}

/**
 * Electron permission string → lucide icon name.
 * Keys are lower-cased because Electron reports them inconsistently.
 */
const PERMISSION_ICONS: Record<string, string> = {
  camera: "camera",
  videocapture: "camera",
  microphone: "mic",
  audiocapture: "mic",
  media: "video",
  geolocation: "map-pin",
  notifications: "bell",
  "display-capture": "monitor",
  "clipboard-read": "clipboard",
  "clipboard-sanitized-write": "clipboard",
  midi: "music",
  midisysex: "music",
  fullscreen: "maximize",
  pointerlock: "mouse-pointer-2",
  openexternal: "external-link",
  "persistent-storage": "hard-drive",
  serial: "usb",
  bluetooth: "bluetooth",
  usb: "usb",
  hid: "usb",
  sensors: "compass",
  "idle-detection": "eye",
  "local-fonts": "type",
  "file-system": "folder-open",
};

/** Electron permission string → `tabTooltip.perm.*` leaf name. */
const PERMISSION_KEYS: Record<string, string> = {
  camera: "camera",
  videocapture: "camera",
  microphone: "microphone",
  audiocapture: "microphone",
  media: "media",
  geolocation: "geolocation",
  notifications: "notifications",
  "display-capture": "displayCapture",
  "clipboard-read": "clipboard",
  "clipboard-sanitized-write": "clipboard",
  midi: "midi",
  midisysex: "midi",
  fullscreen: "fullscreen",
  pointerlock: "pointerLock",
  openexternal: "openExternal",
  "persistent-storage": "persistentStorage",
  serial: "serial",
  bluetooth: "bluetooth",
  usb: "usb",
  hid: "hid",
  sensors: "sensors",
  "idle-detection": "idleDetection",
  "local-fonts": "localFonts",
  "file-system": "fileSystem",
};

/** De-duplicate, translate and order the permission chips. */
export function buildPermissions(
  list: Array<{ name: string; granted: boolean }> | undefined,
): TooltipPermission[] | undefined {
  if (!list || !list.length) return undefined;
  const seen = new Set<string>();
  const out: TooltipPermission[] = [];
  for (const item of list) {
    const raw = String(item.name || "");
    const key = PERMISSION_KEYS[raw.toLowerCase()] || "unknown";
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      icon: PERMISSION_ICONS[raw.toLowerCase()] || "shield",
      label: t(`tabTooltip.perm.${key}`),
      granted: item.granted !== false,
    });
  }
  return out.length ? out : undefined;
}

/** File name part of a path, for either separator. */
export function baseName(p: string): string {
  const clean = (p || "").replace(/[\\/]+$/, "");
  const idx = Math.max(clean.lastIndexOf("/"), clean.lastIndexOf("\\"));
  return idx >= 0 ? clean.slice(idx + 1) : clean;
}

/**
 * Address-bar style URL for display.
 *
 * The raw `location.href` percent-encodes every non-ASCII character, so a
 * Chinese query rendered verbatim is a wall of `%E8%87%AA…` gibberish that
 * looks nothing like what the page's own address bar shows.  Decode the
 * pieces the way a browser address bar does: path, query values and hash
 * arrive human-readable while the URL structure (`?`, `#`, `&`, reserved
 * characters) stays intact.  `decodeURI` deliberately leaves `%23` encoded
 * so an encoded `#` cannot be mistaken for the real fragment separator.
 * Anything unparseable falls back to a best-effort decode of the raw string.
 */
export function formatUrlForDisplay(raw: string): string {
  const url = (raw || "").trim();
  if (!url) return "";
  try {
    const u = new URL(url);
    let pathname = u.pathname;
    let hash = u.hash;
    try { pathname = decodeURI(pathname); } catch { /* keep encoded */ }
    try { hash = decodeURI(hash); } catch { /* keep encoded */ }
    // URLSearchParams values are already percent-decoded on iteration.
    const params: string[] = [];
    for (const [k, v] of u.searchParams) params.push(`${k}=${v}`);
    const search = params.length ? `?${params.join("&")}` : "";
    return `${u.origin}${pathname}${search}${hash}`;
  } catch {
    try { return decodeURI(url); } catch { return url; }
  }
}

/**
 * Hard ceiling for the tooltip header title.
 *
 * The CSS ellipsis on `.tt-title` is the primary cut, but it leans on the
 * whole flex shrink chain holding up; when any link in that chain fails the
 * CJK title smears past the fixed-width panel instead of stopping.  The cap
 * makes the truncation deterministic: 64 code points, then an ellipsis.
 */
const MAX_TOOLTIP_TITLE = 64;
function capTitle(text: string): string {
  const s = (text || "").trim();
  if (s.length <= MAX_TOOLTIP_TITLE) return s;
  return `${Array.from(s).slice(0, MAX_TOOLTIP_TITLE).join("")}…`;
}

/** Turn raw tab data into the tooltip model both tab bars render. */
export function buildTabTooltip(data: TabTooltipData, intlLocale?: string): TooltipModel {
  const rows: TooltipModel["rows"] = [];
  const location = (data.location || "").trim();
  // URLs render address-bar style (percent-decoded); the CSS ellipsis on the
  // row is the truncation mechanism, so over-long URLs cut cleanly at the
  // panel edge instead of ballooning it.
  const locationText = data.isUrl ? formatUrlForDisplay(location) : location;
  if (locationText && location !== "about:blank" && !location.startsWith("chrome-error://")) {
    // No label: an https:// URL is plainly a URL and an absolute path is
    // plainly a path — captioning either one adds nothing.
    rows.push({ value: locationText });
  }
  if (typeof data.size === "number" && data.size >= 0) {
    const sizeText = formatFileSize(data.size);
    if (sizeText) rows.push({ label: t("tabTooltip.size"), value: sizeText });
  }
  if (typeof data.mtime === "number" && data.mtime > 0) {
    const timeText = formatTimestamp(data.mtime, intlLocale);
    if (timeText) rows.push({ label: t("tabTooltip.modified"), value: timeText });
  }
  // The header title can fall back to the URL when a page ships no title —
  // decode it there too, so the fallback never prints encoded gibberish.
  const titleText = data.isUrl && data.title === location
    ? formatUrlForDisplay(data.title)
    : data.title;
  return {
    title: capTitle(titleText || locationText || ""),
    icon: data.icon,
    rows,
    permissions: buildPermissions(data.permissions),
  };
}

/**
 * Attach a tab's hover tooltip.
 *
 * The structured panel is only worth showing when the tab has something to
 * say beyond its own label — a page URL, a file path, a size, a timestamp,
 * requested permissions. A tab that would merely echo the text already
 * printed on it falls back to the same compact one-liner every other icon in
 * the app uses (exactly like the close button's "close tab"), instead of
 * opening an empty panel.
 */
export function applyTabTooltip(
  el: HTMLElement,
  data: TabTooltipData,
  intlLocale?: string,
): void {
  const model = buildTabTooltip(data, intlLocale);
  const hasDetail = model.rows.length > 0 || (model.permissions?.length ?? 0) > 0;
  if (hasDetail) {
    el.removeAttribute("data-tooltip");
    el.removeAttribute("data-tooltip-icon");
    setTooltipModel(el, model);
    return;
  }
  setTooltipModel(el, null);
  if (model.title) el.setAttribute("data-tooltip", model.title);
  else el.removeAttribute("data-tooltip");
  // Keep the leading icon on the compact variant too.
  if (model.icon) el.setAttribute("data-tooltip-icon", model.icon);
  else el.removeAttribute("data-tooltip-icon");
}
