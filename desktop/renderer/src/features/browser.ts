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

import { EALoader } from "./ealoader.js";
import { getState, isEnabled, subscribe } from "../core/state.js";
import { sendSetCdpUrl } from "../core/ws.js";
import { t, onLocaleChange, applyI18n } from "./i18n.js";
import { createAgentControlOverlay, AgentControlOverlayHandle } from "./agent-control.js";

interface BookmarkEntry {
  date_added: string;
  date_modified?: string;
  guid: string;
  id: string;
  name: string;
  type: "url" | "folder";
  url?: string;
  children?: BookmarkEntry[];
}

interface BookmarkRoot {
  children: BookmarkEntry[];
  date_added: string;
  date_modified: string;
  guid: string;
  id: string;
  name: string;
  type: "folder";
}

interface BookmarksData {
  checksum: string;
  roots: {
    bookmark_bar: BookmarkRoot;
    other: BookmarkRoot;
  };
  version: number;
}

const LOAD_TIMEOUT_MS = 30000;
/** Delay before the loading indicator appears, so fast loads never flash it. */
const LOADING_INDICATOR_DELAY_MS = 200;

/**
 * Page-side credential capture script, injected into every document of the
 * in-app browser while "auto-save passwords & keys" is enabled. A submitted
 * password / API-key form is stashed as a *pending* entry in sessionStorage
 * (which survives same-tab navigations), and is only promoted to the saved
 * queue once authentication actually succeeded: either the next document
 * loads without a password prompt, or a same-origin POST/PUT answers 2xx
 * (SPA logins). The renderer drains the queue via `__encreDrainCreds()` and
 * hands entries to the main process for encrypted persistence.
 */
const CRED_CAPTURE_SCRIPT = String.raw`(function () {
  if (window.__encreCredCap) return;
  window.__encreCredCap = true;

  var KEY_FIELD_RE = /api[_-]?key|apikey|secret|token|private[_-]?key|access[_-]?key|auth/i;
  var KEY_VALUE_RE = /^(sk|ghp|gho|xox[baprs]|AKIA|glpat)[-_][A-Za-z0-9_-]{10,}$|^[A-Za-z0-9+/=_-]{40,}$/;

  function readSS(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function writeSS(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }
  function removeSS(k) { try { sessionStorage.removeItem(k); } catch (e) {} }

  function queue(entry) {
    var q = [];
    try { q = JSON.parse(readSS('__encre_cred_queue') || '[]'); } catch (e) { q = []; }
    q.push(entry);
    writeSS('__encre_cred_queue', JSON.stringify(q));
  }

  function isKeyField(el) {
    var meta = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '')).toLowerCase();
    return KEY_FIELD_RE.test(meta);
  }

  function detect(form) {
    var inputs = Array.prototype.slice.call(form.querySelectorAll('input, textarea'));
    var pw = null, kf = null, user = null;
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      if (el.hidden) continue;
      var type = String(el.type || 'text').toLowerCase();
      if (type === 'hidden' || type === 'checkbox' || type === 'radio' || type === 'submit' || type === 'button') continue;
      if (type === 'password' && !pw) { pw = el; continue; }
      if ((type === 'text' || type === 'email' || type === 'tel') && !user && !isKeyField(el)) user = el;
      if (!kf && el.value && isKeyField(el)) kf = el;
    }
    var kind = 'password', secret = pw;
    if (!pw) {
      if (!kf) return null;
      kind = 'key';
      secret = kf;
    }
    var val = String(secret.value || '').trim();
    if (!val) return null;
    if (kind === 'key' && !KEY_VALUE_RE.test(val)) return null;
    var uname = user && user.value ? String(user.value).trim() : '';
    if (!uname) uname = String(secret.name || secret.id || (kind === 'key' ? 'api_key' : ''));
    return {
      origin: location.origin,
      username: uname,
      password: val,
      kind: kind,
      url: location.href,
      created_at: Date.now()
    };
  }

  function stash(entry) {
    var p = Object.assign({}, entry);
    p._t = Date.now();
    writeSS('__encre_cred_pending', JSON.stringify(p));
  }

  function confirmPending(maxAge) {
    var raw = readSS('__encre_cred_pending');
    if (!raw) return;
    removeSS('__encre_cred_pending');
    var p = null;
    try { p = JSON.parse(raw); } catch (e) {}
    if (!p) return;
    if (Date.now() - (p._t || 0) > maxAge) return;
    queue({ origin: p.origin, username: p.username, password: p.password, kind: p.kind, url: p.url, created_at: p.created_at });
  }

  // A new document that still shows a password prompt means the login failed
  // — drop the pending entry. No prompt + a recent submit means success.
  function checkNavigation() {
    if (!readSS('__encre_cred_pending')) return;
    if (document.querySelector('input[type=password]')) { removeSS('__encre_cred_pending'); return; }
    confirmPending(60000);
  }

  // SPA logins never navigate: confirm via the auth request's 2xx answer.
  function hookNetwork() {
    var origFetch = window.fetch;
    if (origFetch) {
      window.fetch = function (input, init) {
        var result = origFetch.apply(this, arguments);
        try {
          var url = typeof input === 'string' ? input : (input && input.url) || '';
          var method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
          if ((method === 'POST' || method === 'PUT') && url.indexOf(location.origin) === 0 && readSS('__encre_cred_pending')) {
            if (result && result.then) {
              result.then(function (r) { if (r && r.ok) confirmPending(20000); }, function () {});
            }
          }
        } catch (e) {}
        return result;
      };
    }
    try {
      var origOpen = XMLHttpRequest.prototype.open;
      var origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (m, u) {
        this.__encreReq = { m: String(m || '').toUpperCase(), u: String(u || '') };
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function () {
        var x = this;
        var info = x.__encreReq || {};
        if ((info.m === 'POST' || info.m === 'PUT') && info.u.indexOf(location.origin) === 0) {
          x.addEventListener('load', function () {
            if (x.status >= 200 && x.status < 300 && readSS('__encre_cred_pending')) confirmPending(20000);
          });
        }
        return origSend.apply(this, arguments);
      };
    } catch (e) {}
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    var entry = detect(form);
    if (entry) stash(entry);
  }, true);

  hookNetwork();
  checkNavigation();

  window.__encreDrainCreds = function () {
    var q = [];
    try { q = JSON.parse(readSS('__encre_cred_queue') || '[]'); } catch (e) { q = []; }
    if (q.length) writeSS('__encre_cred_queue', '[]');
    return q;
  };
})();`;

export interface SearchEngine {
  id: string;
  name: string;
  homepage: string;
  searchUrl: string;
}

export const SEARCH_ENGINES: SearchEngine[] = [
  { id: "bing", name: "Bing", homepage: "https://www.bing.com", searchUrl: "https://www.bing.com/search?q={query}" },
  { id: "google", name: "Google", homepage: "https://www.google.com", searchUrl: "https://www.google.com/search?q={query}" },
  { id: "duckduckgo", name: "DuckDuckGo", homepage: "https://duckduckgo.com", searchUrl: "https://duckduckgo.com/?q={query}" },
  { id: "baidu", name: "\u767E\u5EA6", homepage: "https://www.baidu.com", searchUrl: "https://www.baidu.com/s?wd={query}" },
  { id: "sogou", name: "\u641C\u72D7", homepage: "https://www.sogou.com", searchUrl: "https://www.sogou.com/web?query={query}" },
  { id: "yahoo", name: "Yahoo", homepage: "https://search.yahoo.com", searchUrl: "https://search.yahoo.com/search?p={query}" },
  { id: "brave", name: "Brave", homepage: "https://search.brave.com", searchUrl: "https://search.brave.com/search?q={query}" },
  { id: "qwant", name: "Qwant", homepage: "https://www.qwant.com", searchUrl: "https://www.qwant.com/?q={query}" },
];

export function getDefaultSearchEngine(): SearchEngine {
  const st = getState();
  const id = (st.settings.default_search_engine as string) || "bing";
  return SEARCH_ENGINES.find(e => e.id === id) || SEARCH_ENGINES[0];
}

export function getDefaultHomepage(): string {
  const v = getState().settings.browser_homepage;
  if (typeof v === "string") {
    const trimmed = v.trim();
    if (trimmed !== "" && trimmed !== "search") return trimmed;
  }
  return getDefaultSearchEngine().homepage;
}

/** Guest page zoom as a factor, read per call.  Chromium replays its own
 *  per-site zoom whenever a document commits, so the value still has to be
 *  re-asserted on every load -- only the number it is clobbered back to now
 *  comes from Settings instead of the constant 1. */
export function browserZoomFactor(): number {
  const v = getState().settings.browser_zoom_percent;
  const n = typeof v === "number" ? v : parseInt(String(v ?? ""), 10);
  if (!Number.isFinite(n)) return 0.75;
  return Math.min(500, Math.max(25, n)) / 100;
}

/** Whether a page may pick its own dark stylesheet inside the browser. */
export function browserForceLightPage(): boolean {
  const v = getState().settings.browser_force_light_page;
  return v === undefined ? true : isEnabled(v);
}

/** Schemes that legitimately appear WITHOUT a trailing `//` in an address bar. */
const KNOWN_NO_SLASH_SCHEMES = new Set([
  "data", "javascript", "mailto", "about", "view-source", "tel", "blob", "cid",
]);

/** True for an explicit scheme such as `http://`, `file://`, `data:`, `about:`. */
function isExplicitScheme(s: string): boolean {
  const m = /^([a-z][a-z0-9+.-]*):/i.exec(s);
  if (!m) return false;
  const scheme = m[1].toLowerCase();
  const rest = s.slice(m[0].length); // text after "scheme:"
  return rest.startsWith("//") || KNOWN_NO_SLASH_SCHEMES.has(scheme);
}

/** True for inputs that are local filesystem paths needing a `file://` prefix. */
function isLocalFilePath(s: string): boolean {
  if (s.startsWith("\\\\")) return true; // Windows UNC share \\host\share
  if (/^[a-zA-Z]:[\\/]/.test(s)) return true; // C:\ or C:/
  if (s.startsWith("/") || s.startsWith("~")) return true;
  if (s.includes("\\")) return true;
  return false;
}

/** Convert a local filesystem path into a proper, space-safe `file://` URL. */
function pathTofileUrl(s: string): string {
  // Windows network share: \\host\share\file  ->  file://host/share/file
  if (s.startsWith("\\\\")) {
    const p = s.slice(2).replace(/\\/g, "/");
    return "file://" + encodeURI(p);
  }
  let p = s.replace(/\\/g, "/").trim();
  let prefix: string;
  if (/^[a-zA-Z]:/.test(p)) {
    p = p.replace(/^\/+/, "");
    prefix = "file:///";
  } else if (p.startsWith("~")) {
    prefix = "file://";
  } else {
    prefix = "file://" + (p.startsWith("/") ? "" : "/");
  }
  return prefix + encodeURI(p);
}

/** True for a host-style target (domain, IPv4, localhost, host:port). */
function isHostLike(s: string): boolean {
  if (/^\d{1,3}(\.\d{1,3}){1,3}(:\d+)?$/.test(s)) return true; // IPv4 (>=2 octets), optional port
  if (/^localhost6?$/i.test(s)) return true;
  if (/^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?$/i.test(s)) return true; // domain w/ a dot, optional port
  return false;
}

/** Pick the protocol, mirroring Edge: HTTPS-First for bare hosts, HTTP for
 *  localhost and explicit-port hosts (dev/intranet servers are http-only). */
function hostToUrl(s: string): string {
  if (/^localhost6?$/i.test(s)) return "http://" + s;
  if (/:\d+$/.test(s)) return "http://" + s;
  return "https://" + s;
}

/**
 * Extensions Chromium can render as a document by itself. Everything else —
 * source code, configs, logs, extension-less files like `Makefile` — has no
 * usable MIME on Windows, so a webview navigation to `file:///…/build.py` is
 * treated as a download and aborts with `ERR_ABORTED (-3)`. The renderer sends
 * such targets through the privileged `encre-file://` scheme instead, which
 * the main process serves as plain text (see main.ts).
 */
const RENDERABLE_FILE_EXTS = new Set([
  "html", "htm", "xhtml", "shtml", "svg", "xml", "xsl", "xslt", "pdf",
  "png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "apng",
  "mp4", "m4v", "webm", "ogv", "mov", "mkv",
  "mp3", "wav", "m4a", "flac", "oga", "aac", "opus",
]);

/**
 * Rewrite a `file://` URL to `encre-file://` when Chromium cannot display the
 * file as a document, so the in-app browser shows its text content instead of
 * aborting the navigation. Renderable types (HTML, images, PDF, media) and
 * every non-`file://` URL are returned unchanged.
 */
function toBrowserUrl(fileUrl: string): string {
  const m = /^file:\/\/([^/?#]*)([^?#]*)/i.exec(fileUrl);
  if (!m) return fileUrl;
  const host = m[1] || "";
  const p = m[2] || "/";
  const base = p.slice(p.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  const ext = dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
  // No extension (Makefile, LICENSE, …) is still a text file.
  if (ext && RENDERABLE_FILE_EXTS.has(ext)) return fileUrl;
  return "encre-file://" + host + p;
}

/**
 * Resolve whatever the user / a caller passed into the final webview target,
 * matching Microsoft Edge's address-bar behavior:
 *   - explicit scheme (http/https/file/local/data/about/...) -> used verbatim
 *   - local path (drive / UNIX abs / home / UNC share)       -> `file://` URL
 *   - host-like (domain / IPv4 / localhost / host:port)      -> `http(s)://` URL
 *   - anything else (free text, single word)                 -> search-engine query
 * `file://` targets Chromium cannot render are upgraded to `encre-file://`.
 */
function normalizeNavigationTarget(input: string): string {
  const s = (input || "").trim();
  if (!s) return s;
  if (isExplicitScheme(s)) return toBrowserUrl(s);
  if (isLocalFilePath(s)) return toBrowserUrl(pathTofileUrl(s));
  if (isHostLike(s)) return hostToUrl(s);
  return buildSearchUrl(s);
}

export function buildSearchUrl(input: string, engine?: SearchEngine): string {
  const se = engine || getDefaultSearchEngine();
  return se.searchUrl.replace("{query}", encodeURIComponent(input));
}

export interface BrowserViewOptions {
  startUrl?: string;
  partition?: string;
  onTitleChange?: (title: string) => void;
  onUrlChange?: (url: string) => void;
  onFaviconChange?: (favicon: string) => void;
  onNewWindow?: (url: string) => void;
  onDialog?: (type: string, message: string, defaultPromptText: string, respond: (value: string | null) => void) => void;
  onPermissionRequest?: (permission: string, origin: string, details: any) => void;
  /** The page registered a beforeunload handler and tried to leave — show
   *  the in-app "leave without saving?" dialog, then respond with the user's
   *  choice (true = leave, false = stay). */
  onBeforeUnload?: (respond: (allow: boolean) => void) => void;
  cdpPort?: number;
  compact?: boolean;
}

/* ── Agent control overlay wiring ────────────────────────────────────────
 * Each renderer window runs its own copy of this module. While the Agent is
 * operating the browser, EVERY live view locks — green tint + pill + input
 * isolation on all of them, in the main window and in the ESD sub-app alike.
 * The lock follows the Agent's operating STATE, not any single webview: a
 * navigation, a new tab, or a guest swap cannot dodge it, because whichever
 * view ends up hosting the operated page is already locked by definition.
 * A per-window broadcast from main keeps every renderer in sync; views
 * created mid-operation inherit the current state at construction. */
const liveBrowserViews = new Set<BrowserView>();
let agentControlListenerBound = false;

/* ── beforeunload guard fan-out ──────────────────────────────────────────
 * One global IPC listener routes "page wants to leave" requests to the view
 * that owns the guest webContents.  The mapping is refreshed on every
 * dom-ready: a <webview> detached from the DOM (sidebar teardown, tab
 * close/recreate) gets its guest destroyed and rebuilt with a NEW
 * webContentsId, so the guard must re-bind alongside the CDP relay. */
const unloadGuardTargets = new Map<number, BrowserView>();
let unloadListenerBound = false;
function bindUnloadRequestFanout(): void {
  if (unloadListenerBound) return;
  unloadListenerBound = true;
  const api = (window as any).electronAPI;
  api?.onBrowserUnloadRequest?.((wcId: number) => {
    unloadGuardTargets.get(wcId)?.handleUnloadRequest();
  });
}
/** Whether a browser (or future computer-use) tool call is running. */
let agentControlActive = false;
/** Whether the user took over mid-operation (pill switches, input released). */
let agentControlTakenOver = false;
/** Pending Agent control-change request, if any (pill shows Agree/Decline). */
let agentControlRequest: string | null = null;

function applyAgentControlToAll(): void {
  for (const view of liveBrowserViews) view.syncAgentControl(agentControlActive, agentControlTakenOver);
  for (const view of liveBrowserViews) {
    const overlay = view.agentOverlay;
    if (overlay) overlay.showRequest(agentControlActive ? agentControlRequest : null, (granted) => {
      (window as any).electronAPI?.respondAgentControlRequest?.(granted);
    });
  }
  // Window-level chrome: while the Agent operates, the subwindow's tab strip
  // is locked too (the user must not switch or close the page being driven).
  // Taken over = the user holds the wheel, everything unlocks.
  if (document.body.classList.contains("child-mode")) {
    const tabStrip = document.getElementById("child-header-tabs");
    if (tabStrip) tabStrip.classList.toggle("agent-tabs-locked", agentControlActive && !agentControlTakenOver);
  }
}

function ensureAgentControlListener(): void {
  if (agentControlListenerBound) return;
  agentControlListenerBound = true;
  const api = (window as any).electronAPI;
  if (!api?.onAgentControl) return;
  api.onAgentControl((state: { active: boolean; webContentsId: number | null; takenOver?: boolean; request?: string | null }) => {
    agentControlActive = !!state.active;
    // Main is the source of truth for the taken-over flag (it also gates
    // the CDP relay), so adopt whatever it says instead of keeping local
    // bookkeeping authoritative.
    agentControlTakenOver = agentControlActive && !!state.takenOver;
    agentControlRequest = agentControlActive ? (state.request || null) : null;
    applyAgentControlToAll();
  });
}

/* Settings land asynchronously (fresh windows, e.g. the ESD sub-app, start
 * with an empty local snapshot). When the snapshot arrives, re-assert the
 * configured zoom on every live view — including views created before the
 * snapshot landed, whose applyZoom calls were skipped for lack of the key. */
subscribe(() => {
  for (const view of liveBrowserViews) view.refreshZoom();
}, ["settings"]);

export class BrowserView {
  readonly container: HTMLElement;
  readonly webview: any;
  private urlInput: HTMLInputElement;
  private statusEl: HTMLElement | null;
  private loadingEl: HTMLElement | null;
  private errorEl: HTMLElement | null;
  private overlayTitle: HTMLElement | null;
  private overlayDesc: HTMLElement | null;
  private loader: EALoader | null = null;
  private loadTimer: number | null = null;
  private loadingTimer: number | null = null;
  private _showedError = false;
  private explicitNav = true;
  private _destroyed = false;
  private _cdpPort: number;
  private _webContentsId: number = -1;
  private _ro: ResizeObserver | null = null;
  private _onTitleChange?: (title: string) => void;
  private _onUrlChange?: (url: string) => void;
  private _onFaviconChange?: (favicon: string) => void;
  private _onNewWindow?: (url: string) => void;
  private _onDialog?: (type: string, message: string, defaultPromptText: string, respond: (value: string | null) => void) => void;
  private _onPermissionRequest?: (permission: string, origin: string, details: any) => void;
  private _onBeforeUnload?: (respond: (allow: boolean) => void) => void;
  private _settingsBtn: HTMLButtonElement;
  private _bookmarks: BookmarksData | null = null;
  private _starBtn: HTMLButtonElement;
  private _unsubLocale: (() => void) | null = null;
  /** Key of the CSS injected into the webview via insertCSS, kept so we can
   *  remove + re-insert it when the host theme flips (re-inject) or destroy. */
  private _injectedCssKey: string | null = null;
  /** Watches the host <html data-theme> so the scrollbar thumb can re-track
   *  the project palette (an injected stylesheet can't read host CSS vars). */
  private _themeObserver: MutationObserver | null = null;
  /** Watches this panel's `.active` class and forces a compositor repaint
   *  the moment it becomes visible (webviews shown via visibility can paint
   *  blank / stale until nudged). */
  private _visObserver: MutationObserver | null = null;
  private _wasActive = false;
  /** Periodic drain of the page-side credential queue (auto-save). */
  private _credDrainTimer: number | null = null;
  private _credDraining = false;
  /** "Agent is operating" isolation overlay, when mounted. */
  private _agentOverlay: AgentControlOverlayHandle | null = null;
  /** Public accessor so the global control-state fan-out can reach the overlay. */
  get agentOverlay(): AgentControlOverlayHandle | null {
    return this._agentOverlay;
  }

  constructor(container: HTMLElement, options: BrowserViewOptions = {}) {
    this.container = container;
    this._onTitleChange = options.onTitleChange;
    this._onUrlChange = options.onUrlChange;
    this._onFaviconChange = options.onFaviconChange;
    this._onNewWindow = options.onNewWindow;
    this._onDialog = options.onDialog;
    this._onPermissionRequest = options.onPermissionRequest;
    this._onBeforeUnload = options.onBeforeUnload;
    this._cdpPort = options.cdpPort || 0;
    container.style.cssText = "display:flex;flex-direction:column;flex:1;min-height:0;";

    const startUrl = normalizeNavigationTarget(options.startUrl && options.startUrl !== "about:blank" ? options.startUrl : "");
    const partition = options.partition || "persist:encre-browser";
    const compact = options.compact ? " browser-compact" : "";

    const localePlaceholder = t("browserNav.searchOrEnter");
    container.innerHTML = `
      <div class="browser-nav-bar${compact}">
        <button class="browser-nav-btn" data-nav="back" data-i18n-title="browserNav.back">
          <svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>
        </button>
        <button class="browser-nav-btn" data-nav="forward" data-i18n-title="browserNav.forward">
          <svg viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>
        </button>
        <button class="browser-nav-btn" data-nav="reload" data-i18n-title="browserNav.reload">
          <svg viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>
        </button>
        <input type="text" class="browser-url-input" value="${startUrl}" placeholder="${localePlaceholder}" data-i18n-placeholder="browserNav.searchOrEnter" spellcheck="false" />
        <button class="browser-star-btn" data-i18n-title="browserNav.bookmark">☆</button>
        <button class="browser-settings-btn" data-i18n-title="browserNav.settings">
          <svg viewBox="0 0 24 24"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2" fill="none"/></svg>
        </button>
      </div>
      <div class="browser-webview-wrap">
        <webview class="browser-webview" src="${startUrl || "about:blank"}" partition="${partition}" allowpopups></webview>
        <div class="browser-webview-status hidden">
          <div class="browser-status-loading"></div>
          <div class="browser-status-error hidden">
            <div class="si-empty-center browser-error-state">
              <i data-lucide="cloud-off" class="lucide"></i>
              <div class="si-empty-title" data-i18n="browserNav.failedToLoad">Failed to load</div>
              <div class="si-empty-sub" data-i18n="browserNav.checkConnection">The page could not be loaded. Please check your connection and try again.</div>
            </div>
          </div>
        </div>
      </div>`;

    this.urlInput = container.querySelector(".browser-url-input") as HTMLInputElement;
    this.statusEl = container.querySelector(".browser-webview-status");
    this.loadingEl = container.querySelector(".browser-status-loading");
    this.errorEl = container.querySelector(".browser-status-error");
    this.overlayTitle = container.querySelector(".si-empty-title");
    this.overlayDesc = container.querySelector(".si-empty-sub");
    this._settingsBtn = container.querySelector(".browser-settings-btn") as HTMLButtonElement;

    this._starBtn = container.querySelector(".browser-star-btn") as HTMLButtonElement;

    applyI18n();
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: container });
    }
    this._bindSettings();
    this._bindStarButton();
    this.loadBookmarks();
    this._unsubLocale = onLocaleChange(() => applyI18n());

    const wv = container.querySelector("webview") as any;
    this.webview = wv;

    this.bindEvents();
    this.bindNavButtons();
    this.bindUrlInput();
    // Re-assert the 100% zoom lock on any layout change. The factor is a
    // constant now (see applyZoom) — this is purely a guard, so resizing the
    // side panel or the window can never rescale the page again.
    let zoomRaf = 0;
    const ro = new ResizeObserver(() => {
      if (this._destroyed || zoomRaf) return;
      zoomRaf = requestAnimationFrame(() => {
        zoomRaf = 0;
        this.applyZoom();
      });
    });
    ro.observe(this.container);
    this._ro = ro;
    const api = (window as any).electronAPI;
    if (api?.onNewWindow) {
      api.onNewWindow((url: string, wcId: number) => {
        let myId = -1;
        try { myId = this.webview.getWebContentsId(); } catch {}
        if (myId === wcId) {
          if (url && /^https?:\/\//i.test(url)) {
            this.hideStatus();
            this._onNewWindow?.(url);
          }
        }
      });
    }
    // Register CDP relay for the internal webview and send URL to the backend.
    // NOT a { once: true } listener: a <webview> that gets detached from the
    // DOM (sidebar teardown, tab close/recreate) has its guest WebContents
    // destroyed and rebuilt with a NEW webContentsId, and the relay dies with
    // it. Re-registering on every dom-ready — but skipping when the same guest
    // is merely navigating — keeps the backend endpoint always fresh.
    wv.addEventListener("dom-ready", () => {
      try {
        const wcId = wv.getWebContentsId();
        if (wcId === this._webContentsId && this._cdpPort > 0) return;
        const api = (window as any).electronAPI;
        if (api?.registerCdpWebview) {
          api.registerCdpWebview(wcId).then((port: number) => {
            this._webContentsId = wcId;
            this._cdpPort = port;
            sendSetCdpUrl(`ws://127.0.0.1:${port}`);
          }).catch((err: any) => {
            console.warn("[browser] CDP relay registration failed:", err);
          });
        } else {
          this._webContentsId = wcId;
        }
        // (Re)bind the beforeunload guard — rides the same dom-ready refresh
        // as the CDP relay, since a re-attached webview gets a new guest id.
        if (api?.browserBindUnloadGuard) {
          unloadGuardTargets.set(wcId, this);
          api.browserBindUnloadGuard(wcId).catch(() => {});
        }
        bindUnloadRequestFanout();
      } catch (err) {
        console.warn("[browser] CDP setup failed:", err);
      }
    });

    // Unified scrollbar + disable Chromium auto dark-mode inversion (so web
    // content / opened files render readable instead of black-on-black), and
    // force a repaint when this panel is shown via the shared `.active` switch.
    this._setupUnifiedWebviewStyles();
    this._setupCredentialCapture();
    this._startVisibilityWatch();
    // Join the live-instance registry and listen for the Agent-control
    // broadcast. A view created while the Agent is already operating (new
    // tab / navigated page) inherits the lock immediately at construction.
    liveBrowserViews.add(this);
    ensureAgentControlListener();
    if (agentControlActive) this.syncAgentControl(true, agentControlTakenOver);
  }

  get cdpPort(): number {
    return this._cdpPort;
  }

  set cdpPort(port: number) {
    this._cdpPort = port;
  }

  navigate(url: string): void {
    if (!url || this._destroyed) return;
    const target = normalizeNavigationTarget(url);
    this._showedError = false;
    this.hideStatus();
    this.explicitNav = true;
    this.webview.src = target;
  }

  goBack(): void {
    if (this._destroyed) return;
    this.explicitNav = true;
    this.hideStatus();
    this.webview.goBack();
  }

  goForward(): void {
    if (this._destroyed) return;
    this.explicitNav = true;
    this.hideStatus();
    this.webview.goForward();
  }

  reload(): void {
    if (this._destroyed) return;
    this.webview.reload();
  }

  getUrl(): string {
    try { return this.webview.getURL(); } catch { return ""; }
  }

  getTitle(): string {
    try { return this.webview.getTitle(); } catch { return ""; }
  }

  canGoBack(): boolean {
    try { return this.webview.canGoBack(); } catch { return false; }
  }

  canGoForward(): boolean {
    try { return this.webview.canGoForward(); } catch { return false; }
  }

  isLoading(): boolean {
    try { return this.webview.isLoading(); } catch { return false; }
  }

  executeJavaScript(code: string): Promise<any> {
    try { return this.webview.executeJavaScript(code); } catch { return Promise.resolve(null); }
  }

  /**
   * Build the project-unified stylesheet injected into the webview's OWN
   * document. Two jobs:
   *   1. `color-scheme: light` opts the page out of Chromium's automatic
   *      dark-mode inversion, so web content (and locally-opened files) render
   *      in their authored colors instead of black-text-on-black-background.
   *   2. Mirror the app's unified thin scrollbar (transparent track, rounded
   *      thumb) using the host's `--border` / `--text-muted` palette.
   * Host vars are read at injection time because an injected stylesheet cannot
   * resolve the host page's CSS custom properties.
   */
  private _buildUnifiedCss(): string {
    const cs = getComputedStyle(document.documentElement);
    const thumb = (cs.getPropertyValue("--border") || "").trim() || "rgba(128,128,128,0.5)";
    const thumbHover = (cs.getPropertyValue("--text-muted") || "").trim() || "rgba(128,128,128,0.75)";
    // NOTE: do NOT emit `scrollbar-width` / `scrollbar-color` here. In Chromium
    // the standard scrollbar properties take precedence and DISABLE the
    // ::-webkit-scrollbar pseudo-elements, which silently falls back to the
    // native OS scrollbar — the one with the up/down arrow buttons. The webkit
    // pseudo-elements are the only way to reproduce the project's 4px bar
    // inside a webview document, so they are used exclusively here.
    return `
${browserForceLightPage() ? ":root, :host { color-scheme: light !important; }" : ""}
::-webkit-scrollbar { width: 4px !important; height: 4px !important; background: transparent !important; }
::-webkit-scrollbar-button { display: none !important; width: 0 !important; height: 0 !important; }
::-webkit-scrollbar-corner { background: transparent !important; }
::-webkit-scrollbar-track { background: transparent !important; }
::-webkit-scrollbar-thumb { background: ${thumb} !important; border-radius: 10px !important; }
::-webkit-scrollbar-thumb:hover { background: ${thumbHover} !important; }
`;
  }

  private async _injectUnifiedStyles(freshDocument = false): Promise<void> {
    const wv = this.webview;
    if (!wv || this._destroyed) return;
    const css = this._buildUnifiedCss();
    try {
      // A key from a previous document is dead after navigation — the whole
      // inserted sheet was destroyed with that document, so skip the removal.
      if (!freshDocument && this._injectedCssKey && typeof wv.removeInsertedCSS === "function") {
        try { await wv.removeInsertedCSS(this._injectedCssKey); } catch {}
      }
      this._injectedCssKey = null;
      if (typeof wv.insertCSS === "function") {
        const key = await wv.insertCSS(css);
        this._injectedCssKey = key;
      }
    } catch (err) {
      console.warn("[browser] unified style injection failed:", err);
    }
  }

  /** Inject the unified stylesheet on every document load and re-inject whenever
   *  the host theme flips (so the scrollbar thumb tracks the project palette).
   *  The listener is deliberately NOT `{ once: true }`: inserted CSS lives on the
   *  current document only, so a single-shot listener would drop back to the
   *  native scrollbar as soon as the user navigates. */
  private _setupUnifiedWebviewStyles(): void {
    const wv = this.webview;
    if (!wv) return;
    wv.addEventListener("dom-ready", () => {
      void this._injectUnifiedStyles(true);
    });
    try {
      this._themeObserver = new MutationObserver(() => {
        void this._injectUnifiedStyles();
      });
      this._themeObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
    } catch {}
  }

  /** True unless the user explicitly turned credential auto-save off. The
   *  setting travels over the WS `configure` channel as a string, so only a
   *  literal "false" counts as disabled — an unset value defaults to on. */
  private static _autosaveEnabled(): boolean {
    const v = getState().settings.browser_autosave_credentials;
    return !(v === "false" || v === false);
  }

  /** Inject the credential capture script into every document while the
   *  auto-save setting is on, and drain confirmed entries every 2s. */
  private _setupCredentialCapture(): void {
    const wv = this.webview;
    if (!wv) return;
    wv.addEventListener("dom-ready", () => {
      if (this._destroyed || !BrowserView._autosaveEnabled()) return;
      try {
        wv.executeJavaScript(CRED_CAPTURE_SCRIPT).catch(() => {});
      } catch {}
    });
    this._credDrainTimer = window.setInterval(() => this._drainCredentials(), 2000);
  }

  /** Pull confirmed credentials out of the webview's sessionStorage queue
   *  and hand them to the main process for encrypted persistence. */
  private _drainCredentials(): void {
    if (this._destroyed || this._credDraining) return;
    const wv = this.webview;
    const api = (window as any).electronAPI;
    if (!wv || !api?.browserSaveCredential) return;
    this._credDraining = true;
    let p: Promise<any>;
    try {
      p = Promise.resolve(wv.executeJavaScript("window.__encreDrainCreds ? window.__encreDrainCreds() : []"));
    } catch {
      this._credDraining = false;
      return;
    }
    p.then((list: any) => {
      if (!Array.isArray(list) || list.length === 0) return undefined;
      return Promise.all(list.map((e: any) =>
        api.browserSaveCredential({
          origin: String(e?.origin || ""),
          username: String(e?.username || ""),
          password: String(e?.password || ""),
          kind: e?.kind === "key" ? "key" : "password",
          url: String(e?.url || ""),
          created_at: Number(e?.created_at) || Date.now(),
        }).catch(() => {})
      ));
    }).catch(() => {}).finally(() => { this._credDraining = false; });
  }

  /**
   * A browser panel is shown/hidden through the shared `.active` class (CSS
   * toggles `visibility` + `z-index`, never `display`). Electron webviews
   * brought back from `visibility:hidden` frequently paint blank or stale until
   * the compositor is forced to repaint — nudge it the instant we go visible.
   */
  private _startVisibilityWatch(): void {
    const container = this.container;
    try {
      this._visObserver = new MutationObserver(() => {
        const active = container.classList.contains("active");
        if (active && !this._wasActive) {
          this._wasActive = true;
          requestAnimationFrame(() => {
            if (this._destroyed || !this.webview) return;
            this.applyZoom();
            const wv = this.webview;
            wv.style.transform = "translateZ(0)";
            requestAnimationFrame(() => {
              if (wv) wv.style.transform = "";
            });
          });
        } else if (!active) {
          this._wasActive = false;
        }
      });
      this._visObserver.observe(container, { attributes: true, attributeFilter: ["class"] });
    } catch {}
  }

  /* ── Agent control overlay (host-side input isolation) ───────────────── */

  /** Reconcile this view with the module-level agent-control state. While
   *  the Agent is operating, every live view locks — navigation, new tabs
   *  and guest swaps cannot dodge the overlay because it tracks the
   *  operating STATE, not any single webview. */
  syncAgentControl(active: boolean, takenOver: boolean): void {
    if (this._destroyed) return;
    if (!active) {
      this._hideAgentOverlay();
      return;
    }
    if (!this._agentOverlay) {
      const wrap = this.container.querySelector(".browser-webview-wrap") as HTMLElement | null;
      if (!wrap) return;
      this._agentOverlay = createAgentControlOverlay({
        // User clicked "take over" — release isolation on EVERY view and
        // switch all pills to the taken-over presentation until the tool
        // call ends (takeover is a global action, not per-view). Main also
        // blocks the CDP relay while taken over, so the Agent cannot keep
        // operating and must ask the user to hand control back.
        onTakeover: () => {
          agentControlTakenOver = true;
          (window as any).electronAPI?.setAgentTakeover?.(true);
          applyAgentControlToAll();
        },
        // User handed the pages back — re-engage isolation everywhere.
        onRelease: () => {
          agentControlTakenOver = false;
          (window as any).electronAPI?.setAgentTakeover?.(false);
          applyAgentControlToAll();
        },
      });
      wrap.appendChild(this._agentOverlay.el);
    }
    this._agentOverlay.setPhase(takenOver ? "taken-over" : "operating");
    this._applyAgentIsolation(!takenOver);
  }

  /** Re-assert the configured zoom factor. Called from the module-level
   *  settings subscription when the settings snapshot lands in windows that
   *  started before it arrived. */
  refreshZoom(): void {
    this.applyZoom();
  }

  private _hideAgentOverlay(): void {
    const overlay = this._agentOverlay;
    if (!overlay) return;
    this._agentOverlay = null;
    // Reset the taken-over gate so the next operation starts un-blocked
    // (a turn that ends while taken over must not poison the next one).
    (window as any).electronAPI?.setAgentTakeover?.(false);
    this._applyAgentIsolation(false);
    overlay.destroy();
  }

  /** Engage / disengage user-input isolation. The overlay itself swallows
   *  every pointer event aimed at the page; stealing host focus plus a
   *  tabindex=-1 on the webview keeps keyboard input out of the page. The
   *  Agent's CDP-synthesized input never travels the UI input pipeline, so
   *  it is unaffected by any of this. */
  private _applyAgentIsolation(isolate: boolean): void {
    const wv = this.webview as HTMLElement | null;
    if (wv) {
      wv.classList.toggle("agent-input-blocked", isolate);
      if (isolate) {
        wv.setAttribute("tabindex", "-1");
        const overlay = this._agentOverlay;
        if (overlay) {
          overlay.el.setAttribute("tabindex", "-1");
          try { overlay.el.focus({ preventScroll: true }); } catch { /* focus is best-effort */ }
        }
      } else {
        wv.removeAttribute("tabindex");
      }
    }
    // Navigation chrome (back / forward / reload / URL bar / bookmark /
    // settings) is locked while the Agent operates — the user must not
    // navigate the page underneath the automation. Unlocked on takeover.
    this.container.classList.toggle("agent-nav-locked", isolate);
  }

  /**
   * The guest page tripped its beforeunload handler while leaving.  Hand the
   * decision to the embedder's dialog; an absent handler (or a destroyed
   * view) answers "stay" — the safe default that also keeps the parked
   * Electron event from leaking.
   */
  public handleUnloadRequest(): void {
    const api = (window as any).electronAPI;
    if (this._destroyed || !this._onBeforeUnload) {
      api?.browserRespondUnload?.(this._webContentsId, false);
      return;
    }
    this._onBeforeUnload((allow: boolean) => {
      api?.browserRespondUnload?.(this._webContentsId, allow);
    });
  }

  destroy(): void {
    this._destroyed = true;
    if (this._webContentsId) unloadGuardTargets.delete(this._webContentsId);
    this._hideAgentOverlay();
    liveBrowserViews.delete(this);
    // Recompute the window-level tab-strip lock for the remaining views.
    applyAgentControlToAll();
    this.clearLoadTimer();
    this.clearLoadingTimer();
    if (this._ro) {
      this._ro.disconnect();
      this._ro = null;
    }
    if (this._themeObserver) {
      this._themeObserver.disconnect();
      this._themeObserver = null;
    }
    if (this._visObserver) {
      this._visObserver.disconnect();
      this._visObserver = null;
    }
    if (this._credDrainTimer !== null) {
      window.clearInterval(this._credDrainTimer);
      this._credDrainTimer = null;
    }
    if (this._unsubLocale) {
      this._unsubLocale();
      this._unsubLocale = null;
    }
    if (this.loader) {
      this.loader.destroy();
      this.loader = null;
    }
    // Clean up CDP relay. Push an empty URL first so the backend drops the
    // stale endpoint immediately instead of retrying a dead port later.
    if (this._webContentsId > 0) {
      const api = (window as any).electronAPI;
      sendSetCdpUrl("");
      if (api?.unregisterCdpWebview) {
        api.unregisterCdpWebview(this._webContentsId).catch(() => {});
      }
    }
    try { this.webview.remove(); } catch {}
    this.container.innerHTML = "";
  }

  private _bindSettings(): void {
    this._settingsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isChild = document.body.classList.contains("child-mode");
      if (isChild) {
        const api = (window as any).electronAPI;
        if (api?.openSettings) {
          api.openSettings("browser");
        }
      } else {
        window.dispatchEvent(new CustomEvent("open-settings-panel", { detail: { panel: "browser" } }));
      }
    });
  }

  private _bindStarButton(): void {
    this._starBtn.addEventListener("click", () => {
      const raw = this.getUrl();
      if (!raw || raw === "about:blank" || raw.startsWith("chrome-error://")) return;
      const url = raw.replace(/\/+$/, "");
      const api = (window as any).electronAPI;
      if (!api) return;
      if (this._isBookmarked(url)) {
        api.removeBookmark(url).then(() => this.loadBookmarks());
      } else {
        const title = this.getTitle();
        api.addBookmark({ url, title }).then(() => this.loadBookmarks());
      }
    });
  }

  private _isBookmarked(url: string): boolean {
    if (!this._bookmarks) return false;
    const target = url.replace(/\/+$/, "");
    function search(children: BookmarkEntry[]): boolean {
      for (const c of children) {
        if (c.type === "url" && c.url && c.url.replace(/\/+$/, "") === target) return true;
        if (c.type === "folder" && c.children && search(c.children)) return true;
      }
      return false;
    }
    return search(this._bookmarks!.roots.bookmark_bar.children) || search(this._bookmarks!.roots.other.children);
  }

  private loadBookmarks(): void {
    const api = (window as any).electronAPI;
    if (!api?.getBookmarks) return;
    api.getBookmarks().then((data: BookmarksData) => {
      this._bookmarks = data;
      this._updateStarButton();
    }).catch(() => {});
  }

  private _updateStarButton(): void {
    const url = this.getUrl();
    if (url && this._isBookmarked(url)) {
      this._starBtn.textContent = "★";
      this._starBtn.classList.add("starred");
    } else {
      this._starBtn.textContent = "☆";
      this._starBtn.classList.remove("starred");
    }
  }

  private _addHistoryEntry(): void {
    // Read per navigation: turning history off in Settings stops the very next
    // entry, without reopening the tab.
    if (!isEnabled(getState().settings.browser_record_history)) return;
    const url = this.getUrl();
    if (!url || url === "about:blank" || url.startsWith("chrome-error://")) return;
    const api = (window as any).electronAPI;
    if (api?.addHistoryEntry) {
      api.addHistoryEntry({ url, title: this.getTitle() });
    }
  }

  private esc(str: string): string {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  private bindEvents(): void {
    const wv = this.webview;

    wv.addEventListener("page-title-updated", (e: any) => {
      this._onTitleChange?.(e.title || "");
    });

    wv.addEventListener("page-favicon-updated", (e: any) => {
      if (e.favicons && e.favicons.length > 0) {
        this._onFaviconChange?.(e.favicons[0]);
      }
    });

    // Drop the loading indicator as soon as the DOM is ready. did-finish-load
    // waits for every sub-resource (images / fonts / long-polling), while the
    // page is already visible — keeping the overlay up only makes it look like
    // "the page opened but I can't get in".
    wv.addEventListener("dom-ready", () => {
      if (!this._showedError) this.hideStatus();
      // Earliest point where the new document is live: drop any per-site zoom
      // Chromium remembered in this persistent partition from an earlier
      // Ctrl+wheel / Ctrl+± (that is what made one site open magnified while
      // the next opened normal). Resetting here avoids a visible snap later.
      this.applyZoom();
    });

    wv.addEventListener("will-navigate", (e: any) => {
      this.hideStatus();
      this.explicitNav = false;
    });

    wv.addEventListener("did-finish-load", () => this.applyZoom());
    wv.addEventListener("did-navigate", () => this.applyZoom());
    this.applyZoom();

    wv.addEventListener("did-start-loading", () => {
      // Queue the indicator instead of showing it instantly, and only when no
      // error is on screen. Fast navigations never flash the spinner.
      if (!this._showedError) this.scheduleLoading();
      this.clearLoadTimer();
      this.loadTimer = window.setTimeout(() => {
        // Timeout only retracts the indicator — it never raises an error
        // panel, because a slow page may still finish loading normally.
        if (!this._showedError) this.hideStatus();
      }, LOAD_TIMEOUT_MS);
    });

    wv.addEventListener("did-stop-loading", () => {
      this.clearLoadTimer();
      if (!this._showedError) this.hideStatus();
    });

    wv.addEventListener("did-finish-load", () => {
      this.clearLoadTimer();
      const url = this.webview.getURL();
      // Only show error overlay for real chrome-error pages, not for
      // the initial about:blank state that happens before CDP navigation
      if (this._showedError) {
        // An error was already displayed (e.g. did-fail-load fired first);
        // don't let a subsequent did-finish-load wipe it out.
      } else if (url && url.startsWith("chrome-error://")) {
        this.showError(t("browserNav.failedToLoad"), t("browserNav.checkConnection"));
      } else {
        this.hideStatus();
      }
      this._addHistoryEntry();
      this._updateStarButton();
    });

    wv.addEventListener("did-fail-load", (e: any) => {
      if (e && e.isMainFrame === false) return;
      // ERR_ABORTED (-3) fires on redirects and cancelled navigations —
      // the page will retry or the final navigation will fire did-finish-load.
      if (e && e.errorCode === -3) return;
      const desc = (e && (e.errorDescription || e.message)) || t("browserNav.checkConnection");
      // A failure can belong to a navigation that a later one already took
      // over (redirects, the about:blank hand-off, or a CDP-driven
      // re-navigation). Only surface the panel once the webContents has
      // actually settled on the internal error page — otherwise we would
      // report "failed to load" for pages that open just fine.
      window.setTimeout(() => {
        if (this._destroyed || this._showedError) return;
        const current = this.getUrl();
        if (!current || !current.startsWith("chrome-error://")) return;
        this.clearLoadTimer();
        this.showError(t("browserNav.failedToLoad"), String(desc));
      }, 0);
    });

    wv.addEventListener("did-navigate", (e: any) => {
      try {
        const url = e.url || wv.getURL() || "";
        if (url.startsWith("chrome-error://")) {
          this.showError(t("browserNav.failedToLoad"), t("browserNav.checkConnection"));
        }
        this._onUrlChange?.(url);
        this._addHistoryEntry();
        this._updateStarButton();
      } catch {}
    });

    wv.addEventListener("did-navigate-in-page", (e: any) => {
      this._onUrlChange?.(e.url);
      this._updateStarButton();
    });

    // Browser dialog events (alert, confirm, prompt, beforeunload)
    wv.addEventListener("dialog", (e: any) => {
      if (this._onDialog && e.detail) {
        e.preventDefault();
        this._onDialog(
          e.detail.type || "alert",
          e.detail.message || "",
          e.detail.defaultPromptText || "",
          (value: string | null) => { e.detail.response(value); },
        );
      }
    });

    // Permission request events
    wv.addEventListener("permission-request", (e: any) => {
      if (this._onPermissionRequest && e.request) {
        this._onPermissionRequest(
          e.request.permission || "",
          e.request.origin || "",
          e.request,
        );
      }
    });
  }

  private bindNavButtons(): void {
    this.container.querySelectorAll("[data-nav]").forEach(btn => {
      btn.addEventListener("click", () => {
        const action = btn.getAttribute("data-nav");
        this.hideStatus();
        if (action === "back") { this.explicitNav = true; this.webview.goBack(); }
        else if (action === "forward") { this.explicitNav = true; this.webview.goForward(); }
        else if (action === "reload") { this.webview.reload(); }
      });
    });
  }

  private bindUrlInput(): void {
    this.urlInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        let url = this.urlInput.value.trim();
        if (!url) return;
        this.navigate(url);
      }
    });

    this.webview.addEventListener("did-navigate", (e: any) => {
      this.urlInput.value = this._toDisplayUrl(e.url);
    });
    this.webview.addEventListener("did-navigate-in-page", (e: any) => {
      this.urlInput.value = this._toDisplayUrl(e.url);
    });
  }

  /** Show the internal `encre-file://` scheme as the plain `file://` URL the
   *  user actually asked for (it only ever appears for unrenderable local
   *  files, which the main process serves as text). */
  private _toDisplayUrl(url: string): string {
    try {
      if (url && url.startsWith("encre-file://")) {
        return decodeURI("file://" + url.slice("encre-file://".length));
      }
      return decodeURI(url);
    } catch {
      return url;
    }
  }

  /**
   * Queue the loading indicator. It only appears after a short delay, so a
   * cache hit or a local file never flashes the spinner, and it is retracted
   * as soon as the DOM is ready (see the `dom-ready` handler in bindEvents).
   */
  private scheduleLoading(): void {
    if (this.loadingTimer !== null) return;
    this.loadingTimer = window.setTimeout(() => {
      this.loadingTimer = null;
      if (this._destroyed || this._showedError) return;
      if (this.errorEl && !this.errorEl.classList.contains("hidden")) return;
      this.showLoading();
    }, LOADING_INDICATOR_DELAY_MS);
  }

  private clearLoadingTimer(): void {
    if (this.loadingTimer !== null) {
      clearTimeout(this.loadingTimer);
      this.loadingTimer = null;
    }
  }

  private showLoading(): void {
    this.statusEl?.classList.remove("hidden");
    this.loadingEl?.classList.remove("hidden");
    this.errorEl?.classList.add("hidden");
    if (!this.loader && this.loadingEl) {
      this.loader = new EALoader(this.loadingEl, { maxWidth: "96px" });
    }
  }

  private showError(title: string, desc: string): void {
    this._showedError = true;
    this.clearLoadingTimer();
    if (this.loader) { this.loader.destroy(); this.loader = null; }
    this.statusEl?.classList.remove("hidden");
    this.loadingEl?.classList.add("hidden");
    if (this.overlayTitle) this.overlayTitle.textContent = title;
    if (this.overlayDesc) this.overlayDesc.textContent = desc;
    this.errorEl?.classList.remove("hidden");
  }

  private hideStatus(): void {
    this._showedError = false;
    this.clearLoadingTimer();
    if (this.loader) { this.loader.destroy(); this.loader = null; }
    this.statusEl?.classList.add("hidden");
    this.loadingEl?.classList.add("hidden");
    this.errorEl?.classList.add("hidden");
  }

  /**
   * Pin this webview to 100% zoom — the same policy the main window and every
   * child window already enforce (main.ts locks zoom to 1 and swallows the
   * Ctrl+/-/0 hotkeys).
   *
   * The previous implementation derived the factor from the container width:
   * `clamp(clientWidth / 1280, 0.3, 1)`. That made the *same* page render at
   * ~30% inside the narrow session sidebar and at 100% in the wide Encre Agent Desktop Subwindow (ESD) sub-app
   * tab, and made the size jump whenever the panel or window was resized (the
   * ResizeObserver re-derived a new factor mid-animation). Content size must
   * never depend on how wide its container happens to be at that instant.
   */
  private applyZoom(): void {
    if (this._destroyed) return;
    const wv = this.webview;
    if (!wv) return;
    // Deliberately NOT guarded.  Chromium re-applies any per-site zoom it
    // remembered for this host when a new document commits, so every load /
    // navigation must actively clobber it back to the configured factor, not
    // just the first.
    // Skip while the setting has not arrived yet: a freshly spawned window
    // (the ESD sub-app) starts with an empty local settings snapshot, and
    // asserting the fallback factor here would clobber the correct factor
    // the main process already applied when it created this guest. Once the
    // snapshot lands, the module-level settings subscription re-applies for
    // every live view.
    if (getState().settings.browser_zoom_percent !== undefined) {
      try {
        wv.setZoomFactor(browserZoomFactor());
      } catch {}
    }
    // Belt and braces: refuse pinch / Ctrl+wheel visual zoom so a stray gesture
    // cannot rescale the guest. Idempotent and cheap, so no latch — an early
    // call before `dom-ready` throws and must be retried on the next event.
    try {
      const pending = wv.setVisualZoomLevelLimits?.(1, 1);
      if (pending && typeof pending.catch === "function") pending.catch(() => {});
    } catch {}
  }

  private clearLoadTimer(): void {
    if (this.loadTimer !== null) {
      clearTimeout(this.loadTimer);
      this.loadTimer = null;
    }
  }

  private isErrorPage(): boolean {
    try {
      const url = this.webview.getURL();
      return !!(url && (url.startsWith("chrome-error://") || url === "about:blank"));
    } catch { return false; }
  }

  }