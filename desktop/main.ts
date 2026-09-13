/**
 * Copyright 婵?2025-2026 Wenze Wei. All Rights Reserved.
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
 * Encre desktop application entry point (Electron main process).
 *
 * Responsibilities of this module:
 *  - Boot a Python backend service (`encre.server.app`) as a child process and
 *    manage its lifecycle (start, restart, kill on quit, free its TCP port).
 *  - Create and manage the main BrowserWindow and auxiliary child windows.
 *  - Provide a system tray icon + popup for quick session switching.
 *  - Expose an encrypted on-disk cookie store for the in-app browser.
 *  - Register a large set of IPC handlers bridging the renderer to the OS
 *    (file system, terminal/pty, git, window controls, auto-start, docs….
 *
 * All heavy interaction with the OS happens here; the renderer only talks to
 * the main process through the `electronAPI` exposed by `preload.ts`.
 */

import { app, BrowserWindow, ipcMain, shell, dialog, Tray, nativeImage, nativeTheme, session, protocol, net, webContents, clipboard } from "electron";
import { ChildProcess, spawn, execSync, exec } from "child_process";
import * as path from "path";
import * as fs from "fs";
import * as crypto from "crypto";
import { pathToFileURL, fileURLToPath } from "url";
import * as os from "os";
import * as WebSocket from "ws";
import * as http from "http";
import { WebSocketServer, WebSocket as WS } from "ws";

// Handle to the spawned Python backend process (null when not running).
let serverProcess: ChildProcess | null = null;
// Stores the last backend startup error (stderr) for display in the splash screen.
let serverStartError: string | null = null;
// The primary application window (null when hidden/closed).
let mainWindow: BrowserWindow | null = null;
// System tray icon handle.
let tray: Tray | null = null;
// Set to true right before an explicit "Quit" so the window close handler
// does not just hide the app to the tray.
let isQuitting = false;
let winKeyCapture = false;
// WebSocket / HTTP port the Python backend listens on.
const WS_PORT = 7110;
// Root directory for all Encre user data (~/.dunimd/encre).
const DATA_DIR = getDataDir();
// Set of auxiliary "child" BrowserWindows (e.g. the ESD window).
const childWindows = new Set<BrowserWindow>();
// add-tab requests queued while a child window is still loading its page.
// The renderer cannot receive ipcRenderer events before the preload has run,
// so sends during that window would be silently dropped. They are flushed
// once the page finishes loading.
const childPendingTabs = new WeakMap<BrowserWindow, Array<[string, string]>>();
// PID file written by the Python service; used to manage its lifecycle.
const PID_FILE = path.join(DATA_DIR, "yimd.pid");
// In-memory cache for `git status` results keyed by repository path.
const GIT_STATUS_CACHE = new Map<string, { ts: number; result: any }>();
// In-memory cache for `git diff` results keyed by "repo::file" string.
const GIT_DIFF_CACHE = new Map<string, { ts: number; result: any }>();
// Time-to-live (ms) for the git caches before a fresh command is run.
const GIT_CACHE_TTL_MS = 5000;

/* 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Encrypted browser cookie store 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶?*/

// Dedicated directory for all browser data.
const BROWSER_DIR = path.join(DATA_DIR, "browser");
// On-disk path to the static AES-256 key for browser data encryption.
const BROWSER_KEY_FILE = path.join(BROWSER_DIR, "key");
// On-disk path to the encrypted cookie blob.
const BROWSER_COOKIE_FILE = path.join(BROWSER_DIR, "cookies.enc");
// On-disk path to the encrypted localStorage blob.
const BROWSER_LOCALSTORAGE_FILE = path.join(BROWSER_DIR, "localstorage.enc");
const BROWSER_BOOKMARKS_FILE = path.join(BROWSER_DIR, "bookmarks.enc");
const BROWSER_HISTORY_FILE = path.join(BROWSER_DIR, "history.enc");
const BROWSER_PASSWORDS_FILE = path.join(BROWSER_DIR, "passwords.enc");
// Partition with persist: prefix so Chromium persists IndexedDB/Cache to disk.
const BROWSER_PARTITION = "persist:encre-browser";
/**
 * Returns the AES-256 key used to encrypt/decrypt browser cookies.
 * The key is generated once and persisted to BROWSER_KEY_FILE; subsequent
 * calls reuse the stored key so cookies survive app restarts.
 * @returns A 32-byte Buffer holding the raw encryption key.
 */
function getBrowserEncryptionKey(): Buffer {
  try {
    if (fs.existsSync(BROWSER_KEY_FILE)) {
      return fs.readFileSync(BROWSER_KEY_FILE);
    }
  } catch {}
  // Generate a new 32-byte AES-256 key
  const key = crypto.randomBytes(32);
  fs.mkdirSync(BROWSER_DIR, { recursive: true });
  fs.writeFileSync(BROWSER_KEY_FILE, key);
  return key;
}

/**
 * Encrypts a JSON cookie string using AES-256-GCM.
 * Output layout: [iv(16 bytes) | authTag(16 bytes) | ciphertext].
 * @param json - The serialized cookie array to encrypt.
 * @returns A Buffer containing the IV, auth tag and ciphertext.
 */
function encryptCookies(json: string): Buffer {
  const key = getBrowserEncryptionKey();
  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([cipher.update(json, "utf-8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  // Format: iv(16) + tag(16) + encrypted data
  return Buffer.concat([iv, tag, encrypted]);
}

/**
 * Decrypts a cookie blob produced by {@link encryptCookies}.
 * Returns null on any failure (truncated data, wrong key, tampered payload).
 * @param data - The encrypted Buffer (iv | tag | ciphertext).
 * @returns The decrypted JSON string, or null if decryption fails.
 */
function decryptCookies(data: Buffer): string | null {
  try {
    const key = getBrowserEncryptionKey();
    if (data.length < 32) return null;
    const iv = data.subarray(0, 16);
    const tag = data.subarray(16, 32);
    const encrypted = data.subarray(32);
    const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
    decipher.setAuthTag(tag);
    return decipher.update(encrypted) + decipher.final("utf-8");
  } catch {
    return null; // Decryption failure (e.g. key changed)
  }
}

/**
 * Persists an encrypted cookie JSON blob to disk. Failures are logged but
 * swallowed so the caller is never blocked.
 * @param cookieJson - Serialized cookie array to encrypt and save.
 */
function saveBrowserCookies(cookieJson: string): void {
  try {
    fs.mkdirSync(BROWSER_DIR, { recursive: true });
    const encrypted = encryptCookies(cookieJson);
    fs.writeFileSync(BROWSER_COOKIE_FILE, encrypted);
  } catch (e) {
    console.error("[browser] failed to save cookies:", e);
  }
}

/**
 * Loads and decrypts the persisted cookie blob from disk.
 * @returns The decrypted JSON string, or null when no file exists or the
 *          decryption fails.
 */
function loadBrowserCookies(): string | null {
  try {
    if (!fs.existsSync(BROWSER_COOKIE_FILE)) return null;
    const data = fs.readFileSync(BROWSER_COOKIE_FILE);
    return decryptCookies(data);
  } catch {
    return null;
  }
}

/**
 * Encrypts and saves a localStorage JSON blob to disk.
 */
function saveBrowserLocalStorage(json: string): void {
  try {
    fs.mkdirSync(BROWSER_DIR, { recursive: true });
    const encrypted = encryptCookies(json);
    fs.writeFileSync(BROWSER_LOCALSTORAGE_FILE, encrypted);
  } catch (e) {
    console.error("[browser] failed to save localStorage:", e);
  }
}

/**
 * Loads and decrypts the persisted localStorage blob from disk.
 */


/**
 * Encrypts and saves a bookmarks JSON blob to disk.
 */
function saveBrowserBookmarks(json: string): void {
  try {
    fs.mkdirSync(BROWSER_DIR, { recursive: true });
    const encrypted = encryptCookies(json);
    fs.writeFileSync(BROWSER_BOOKMARKS_FILE, encrypted);
  } catch (e) {
    console.error("[browser] failed to save bookmarks:", e);
  }
}

/**
 * Loads and decrypts the persisted bookmarks blob from disk.
 */
function loadBrowserBookmarks(): string | null {
  try {
    if (!fs.existsSync(BROWSER_BOOKMARKS_FILE)) return null;
    const data = fs.readFileSync(BROWSER_BOOKMARKS_FILE);
    return decryptCookies(data);
  } catch {
    return null;
  }
}

/**
 * Encrypts and saves a history JSON blob to disk.
 */
function saveBrowserHistory(json: string): void {
  try {
    fs.mkdirSync(BROWSER_DIR, { recursive: true });
    const encrypted = encryptCookies(json);
    fs.writeFileSync(BROWSER_HISTORY_FILE, encrypted);
  } catch (e) {
    console.error("[browser] failed to save history:", e);
  }
}

/**
 * Loads and decrypts the persisted history blob from disk.
 */
function loadBrowserHistory(): string | null {
  try {
    if (!fs.existsSync(BROWSER_HISTORY_FILE)) return null;
    const data = fs.readFileSync(BROWSER_HISTORY_FILE);
    return decryptCookies(data);
  } catch {
    return null;
  }
}

function saveBrowserPasswords(json: string): void {
  try {
    fs.mkdirSync(BROWSER_DIR, { recursive: true });
    const encrypted = encryptCookies(json);
    fs.writeFileSync(BROWSER_PASSWORDS_FILE, encrypted);
  } catch (e) {
    console.error("[browser] failed to save passwords:", e);
  }
}

function loadBrowserPasswords(): string | null {
  try {
    if (!fs.existsSync(BROWSER_PASSWORDS_FILE)) return null;
    const data = fs.readFileSync(BROWSER_PASSWORDS_FILE);
    return decryptCookies(data);
  } catch {
    return null;
  }
}

function loadBrowserLocalStorage(): string | null {
  try {
    if (!fs.existsSync(BROWSER_LOCALSTORAGE_FILE)) return null;
    const data = fs.readFileSync(BROWSER_LOCALSTORAGE_FILE);
    return decryptCookies(data);
  } catch {
    return null;
  }
}

/**
 * Wires up the persistent, encrypted browser data store for the in-app browser
 * session (persist:encre-browser partition). Responsibilities:
 *  - Loads previously saved cookies and localStorage into the session on startup.
 *  - Watches cookie changes, debouncing writes to disk (500ms).
 *  - Saves localStorage from all webview origins on navigation/quit.
 *  - Flushes the cache synchronously on `before-quit`.
 *  - Restricts which permissions the in-app browser may request.
 */
// Current Accept-Language value for the in-app browser session.
// Updated via the "browser-language" IPC channel; used by onBeforeSendHeaders
// so multi-language websites serve content in the user's preferred language.
let _browserAcceptLanguage = "en-US,en;q=0.9";

function setupBrowserSession(): void {
  const bs = session.fromPartition(BROWSER_PARTITION);

  // Override Accept-Language on every request so multi-language websites
  // respect the app-level language setting.
  bs.webRequest.onBeforeSendHeaders((details, callback) => {
    details.requestHeaders["Accept-Language"] = _browserAcceptLanguage;
    callback({ requestHeaders: details.requestHeaders });
  });

  // In-memory cache of the latest cookie JSON …used for synchronous save on quit
  let cookieCache: string | null = null;

  // Load encrypted cookies into the session
  const raw = loadBrowserCookies();
  if (raw) {
    try {
      const cookies = JSON.parse(raw);
      if (Array.isArray(cookies)) {
        for (const c of cookies) {
          try {
            const cleanDomain = typeof c.domain === 'string' ? c.domain.replace(/^\./, '') : '';
            const url = cleanDomain
              ? `http${c.secure ? "s" : ""}://${cleanDomain}${c.path || "/"}`
              : undefined;
            if (url && c.name) {
              const isHost = c.name.startsWith("__Host-");
              const isSecure = c.name.startsWith("__Secure-");
              bs.cookies.set({
                url,
                name: c.name,
                value: c.value || "",
                domain: isHost ? undefined : c.domain,
                path: isHost ? "/" : (c.path || "/"),
                secure: isHost || isSecure || !!c.secure,
                httpOnly: !!c.httpOnly,
                sameSite: c.sameSite || "unspecified",
                expirationDate: c.expirationDate,
              }).catch(() => {});
            }
          } catch (e) {
            console.error("[browser] cookie load error:", e);
          }
        }
      }
    } catch (e) {
      console.error("[browser] cookie JSON parse error:", e);
    }
  }

  // On every cookie change: update the in-memory cache and debounce disk write
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  bs.cookies.on("changed", () => {
    bs.cookies.get({}).then((all) => {
      cookieCache = JSON.stringify(all);
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        if (cookieCache) saveBrowserCookies(cookieCache);
      }, 500);
    }).catch((e: any) => console.error("[browser] cookie save error:", e));
  });

  // Synchronous save on quit …Electron does NOT await async event handlers
  app.on("before-quit", () => {
    if (saveTimer) clearTimeout(saveTimer);
    if (cookieCache) {
      console.log("[browser] saving cookies on quit");
      saveBrowserCookies(cookieCache);
    }
  });

  // Permission handler for the in-app browser
  const originPerms = new Map<string, Map<string, boolean>>();
  const originRequestedPerms = new Map<string, Set<string>>();
  const allowed = new Set(["geolocation", "notifications", "midi", "midiSysex", "pointerLock", "fullscreen", "openExternal", "clipboard-read", "clipboard-sanitized-write", "display-capture", "media"]);
  bs.setPermissionRequestHandler((wc, permission, callback) => {
    let origin = "";
    try { origin = new URL(wc.getURL()).origin; } catch {}
    if (origin) {
      // Track that this permission was requested by the site
      let req = originRequestedPerms.get(origin);
      if (!req) { req = new Set(); originRequestedPerms.set(origin, req); }
      req.add(permission);
      // Check user override
      const perms = originPerms.get(origin);
      if (perms && perms.has(permission)) {
        callback(perms.get(permission)!);
        return;
      }
    }
    callback(allowed.has(permission));
  });
  (global as any).__browserOriginPerms = originPerms;
  (global as any).__browserRequestedPerms = originRequestedPerms;
}

// Clears the in-app browser's cookies, storage and encrypted cookie file.
ipcMain.handle("browser:clear-data", async () => {
  try {
    const bs = session.fromPartition(BROWSER_PARTITION);
    // Clear all cookies
    const all = await bs.cookies.get({});
    for (const c of all) {
      try {
        const cleanDomain = typeof c.domain === 'string' ? c.domain.replace(/^\./, '') : '';
        const url = cleanDomain
          ? `http${c.secure ? "s" : ""}://${cleanDomain}${c.path || "/"}`
          : undefined;
        if (url) await bs.cookies.remove(url, c.name);
      } catch {}
    }
    // Clear Chromium storage (localStorage, IndexedDB, cache, etc.)
    await bs.clearStorageData();
    // Delete encrypted data files
    try { fs.unlinkSync(BROWSER_COOKIE_FILE); } catch {}
    try { fs.unlinkSync(BROWSER_LOCALSTORAGE_FILE); } catch {}
    try { fs.unlinkSync(BROWSER_BOOKMARKS_FILE); } catch {}
    try { fs.unlinkSync(BROWSER_HISTORY_FILE); } catch {}
    return { success: true };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});

// ===== Site Info & Permissions =====

ipcMain.handle("browser:get-site-info", async (_event, url: string): Promise<{
  origin: string;
  isSecure: boolean;
  cookieCount: number;
  permissions: Array<{ name: string; granted: boolean }>;
}> => {
  const result = {
    origin: "",
    isSecure: false,
    cookieCount: 0,
    permissions: [] as Array<{ name: string; granted: boolean }>,
  };
  try {
    const u = new URL(url);
    result.origin = u.origin;
    result.isSecure = u.protocol === "https:";
    // Get cookie count for domain
    const bs = session.fromPartition(BROWSER_PARTITION);
    const cookies = await bs.cookies.get({ domain: u.hostname });
    result.cookieCount = cookies.length;
    // Get requested permissions for origin
    const originRequestedPerms: Map<string, Set<string>> = (global as any).__browserRequestedPerms || new Map();
    const originPerms: Map<string, Map<string, boolean>> = (global as any).__browserOriginPerms || new Map();
    const requested: Set<string> = originRequestedPerms.get(u.origin) || new Set();
    const userPerms = originPerms.get(u.origin) || new Map();
    result.permissions = [...requested].map((name) => ({
      name,
      granted: userPerms.has(name) ? userPerms.get(name)! : true,
    }));
  } catch {}
  return result;
});

ipcMain.handle("browser:get-cookies-for-origin", async (_event, url: string): Promise<{ cookies: Array<{ name: string; value: string; domain: string; path: string; secure: boolean; httpOnly: boolean; sameSite: string; expirationDate?: number }> }> => {
  const out: { cookies: any[] } = { cookies: [] };
  try {
    const u = new URL(url);
    const bs = session.fromPartition(BROWSER_PARTITION);
    const cookies = await bs.cookies.get({ domain: u.hostname });
    out.cookies = cookies.map((c: any) => ({
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      secure: c.secure,
      httpOnly: c.httpOnly,
      sameSite: c.sameSite,
      expirationDate: c.expirationDate,
    }));
  } catch {}
  return out;
});

ipcMain.handle("browser:set-permission", async (_event, origin: string, permission: string, granted: boolean): Promise<{ success: boolean }> => {
  try {
    const originPerms: Map<string, Map<string, boolean>> = (global as any).__browserOriginPerms || new Map();
    let perms = originPerms.get(origin);
    if (!perms) {
      perms = new Map();
      originPerms.set(origin, perms);
    }
    perms.set(permission, granted);
    (global as any).__browserOriginPerms = originPerms;
    return { success: true };
  } catch {
    return { success: false };
  }
});

// ===== Bookmarks IPC =====
ipcMain.handle("browser:get-bookmarks", async () => {
  const raw = loadBrowserBookmarks();
  if (raw) {
    try { return JSON.parse(raw); } catch {}
  }
  return {
    checksum: "",
    roots: {
      bookmark_bar: { children: [], date_added: "0", date_modified: "0", guid: "", id: "1", name: "Bookmarks bar", type: "folder" },
      other: { children: [], date_added: "0", date_modified: "0", guid: "", id: "2", name: "Other bookmarks", type: "folder" },
    },
    version: 1,
  };
});

ipcMain.handle("browser:set-bookmarks", async (_e, data: any) => {
  saveBrowserBookmarks(JSON.stringify(data));
  return { success: true };
});

ipcMain.handle("browser:add-bookmark", async (_e, entry: { url: string; title: string }) => {
  const raw = loadBrowserBookmarks();
  const data = raw ? JSON.parse(raw) : { checksum: "", roots: { bookmark_bar: { children: [], date_added: "0", date_modified: "0", guid: "", id: "1", name: "Bookmarks bar", type: "folder" }, other: { children: [], date_added: "0", date_modified: "0", guid: "", id: "2", name: "Other bookmarks", type: "folder" } }, version: 1 };
  const bar = data.roots.bookmark_bar;
  const guid = "bm_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8);
  const id = String(bar.children.length + 1);
  bar.children.push({
    date_added: String(Date.now() * 1000 + 11644473600000000),
    guid,
    id,
    name: entry.title || entry.url,
    type: "url",
    url: entry.url,
  });
  bar.date_modified = String(Date.now() * 1000 + 11644473600000000);
  data.checksum = guid;
  saveBrowserBookmarks(JSON.stringify(data));
  return { success: true };
});

ipcMain.handle("browser:remove-bookmark", async (_e, url: string) => {
  const raw = loadBrowserBookmarks();
  if (!raw) return { success: false };
  const data = JSON.parse(raw);
  function removeFrom(arr: any[], targetUrl: string): boolean {
    for (let i = arr.length - 1; i >= 0; i--) {
      if (arr[i].type === "url" && arr[i].url === targetUrl) { arr.splice(i, 1); return true; }
      if (arr[i].type === "folder" && arr[i].children) { if (removeFrom(arr[i].children, targetUrl)) return true; }
    }
    return false;
  }
  removeFrom(data.roots.bookmark_bar.children, url);
  removeFrom(data.roots.other.children, url);
  data.checksum = "rm_" + Date.now();
  saveBrowserBookmarks(JSON.stringify(data));
  return { success: true };
});

// ===== History IPC =====
ipcMain.handle("browser:get-history", async () => {
  const raw = loadBrowserHistory();
  if (raw) {
    try { const arr = JSON.parse(raw); if (Array.isArray(arr)) return arr; } catch {}
  }
  return [];
});

ipcMain.handle("browser:add-history-entry", async (_e, entry: { url: string; title: string }) => {
  const raw = loadBrowserHistory();
  const history = raw ? JSON.parse(raw) : [];
  const existing = history.findIndex((h: any) => h.url === entry.url);
  if (existing >= 0) {
    history[existing].visit_count = (history[existing].visit_count || 0) + 1;
    history[existing].visit_time = Date.now();
    history[existing].title = entry.title || history[existing].title;
  } else {
    history.unshift({ id: Date.now(), url: entry.url, title: entry.title || "", visit_time: Date.now(), visit_count: 1, typed_count: 0 });
  }
  if (history.length > 5000) history.length = 5000;
  saveBrowserHistory(JSON.stringify(history));
  return { success: true };
});

ipcMain.handle("browser:clear-history", async () => {
  try { fs.unlinkSync(BROWSER_HISTORY_FILE); } catch {}
  return { success: true };
});

ipcMain.handle("browser:export-file", async (_event, options: { content: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }) => {
  const result = await dialog.showSaveDialog({
    defaultPath: options.defaultName,
    filters: options.filters,
  });
  if (result.canceled || !result.filePath) return { success: false, canceled: true };
  try {
    fs.writeFileSync(result.filePath, options.content, "utf-8");
    return { success: true, filePath: result.filePath };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});

ipcMain.handle("browser:export-binary", async (_event, options: { base64: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }) => {
  const result = await dialog.showSaveDialog({
    defaultPath: options.defaultName,
    filters: options.filters,
  });
  if (result.canceled || !result.filePath) return { success: false, canceled: true };
  try {
    const buf = Buffer.from(options.base64, "base64");
    fs.writeFileSync(result.filePath, buf);
    return { success: true, filePath: result.filePath };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});

// Copies an existing on-disk file to a user-chosen destination (used to save
// full data-dir backup zips without round-tripping them over base64).
ipcMain.handle("browser:copy-file-to", async (_event, options: { sourcePath: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }) => {
  if (!options.sourcePath || !fs.existsSync(options.sourcePath)) {
    return { success: false, error: "Source file not found" };
  }
  const result = await dialog.showSaveDialog({
    defaultPath: options.defaultName,
    filters: options.filters,
  });
  if (result.canceled || !result.filePath) return { success: false, canceled: true };
  try {
    fs.copyFileSync(options.sourcePath, result.filePath);
    return { success: true, filePath: result.filePath };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});

// ===== Browser import/export IPC =====

interface DetectedBrowser {
  id: string;
  name: string;
  profilePath: string;
  hasBookmarks: boolean;
  hasCookies: boolean;
  hasHistory: boolean;
}

const BROWSER_PROFILES: Array<{ id: string; name: string; profileDir: string; executableDir: string; exeName: string }> = [
  { id: "chrome", name: "Google Chrome", profileDir: path.join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\User Data\\Default"), executableDir: path.join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\Application"), exeName: "chrome.exe" },
  { id: "edge", name: "Microsoft Edge", profileDir: path.join(process.env.LOCALAPPDATA || "", "Microsoft\\Edge\\User Data\\Default"), executableDir: path.join(process.env.LOCALAPPDATA || "", "Microsoft\\Edge\\Application"), exeName: "msedge.exe" },
  { id: "brave", name: "Brave", profileDir: path.join(process.env.LOCALAPPDATA || "", "BraveSoftware\\Brave-Browser\\User Data\\Default"), executableDir: path.join(process.env.LOCALAPPDATA || "", "BraveSoftware\\Brave-Browser\\Application"), exeName: "brave.exe" },
  { id: "opera", name: "Opera", profileDir: path.join(process.env.APPDATA || "", "Opera Software\\Opera Stable"), executableDir: path.join(process.env.PROGRAMFILES || "C:\\Program Files", "Opera"), exeName: "launcher.exe" },
  { id: "vivaldi", name: "Vivaldi", profileDir: path.join(process.env.LOCALAPPDATA || "", "Vivaldi\\User Data\\Default"), executableDir: path.join(process.env.LOCALAPPDATA || "", "Vivaldi\\Application"), exeName: "vivaldi.exe" },
  { id: "yandex", name: "Yandex Browser", profileDir: path.join(process.env.LOCALAPPDATA || "", "Yandex\\YandexBrowser\\User Data\\Default"), executableDir: path.join(process.env.LOCALAPPDATA || "", "Yandex\\YandexBrowser\\Application"), exeName: "browser.exe" },
  { id: "chromium", name: "Chromium", profileDir: path.join(process.env.LOCALAPPDATA || "", "Chromium\\User Data\\Default"), executableDir: path.join(process.env.LOCALAPPDATA || "", "Chromium\\Application"), exeName: "chrome.exe" },
  { id: "firefox", name: "Firefox", profileDir: "", executableDir: path.join(process.env.PROGRAMFILES || "C:\\Program Files", "Mozilla Firefox"), exeName: "firefox.exe" },
];

function findFirefoxProfileDir(): string | null {
  const profilesIni = path.join(process.env.APPDATA || "", "Mozilla\\Firefox\\profiles.ini");
  if (!fs.existsSync(profilesIni)) return null;
  try {
    const content = fs.readFileSync(profilesIni, "utf-8");
    const match = content.match(/Default=Profiles\/([^\r\n]+)/);
    if (match) {
      const dir = path.join(process.env.APPDATA || "", "Mozilla\\Firefox\\Profiles", match[1]);
      if (fs.existsSync(dir)) return dir;
    }
    const fallback = content.match(/Path=Profiles\/([^\r\n]+)/);
    if (fallback) {
      const dir = path.join(process.env.APPDATA || "", "Mozilla\\Firefox\\Profiles", fallback[1]);
      if (fs.existsSync(dir)) return dir;
    }
  } catch {}
  return null;
}

ipcMain.handle("browser:detect-browsers", async (): Promise<DetectedBrowser[]> => {
  const result: DetectedBrowser[] = [];
  for (const b of BROWSER_PROFILES) {
    let profilePath = b.profileDir;
    if (b.id === "firefox") {
      const ff = findFirefoxProfileDir();
      if (!ff) continue;
      profilePath = ff;
    }
    if (!fs.existsSync(profilePath)) continue;
    const hasBookmarks = b.id === "firefox"
      ? fs.existsSync(path.join(profilePath, "places.sqlite"))
      : fs.existsSync(path.join(profilePath, "Bookmarks"));
    const hasCookies = b.id === "firefox"
      ? fs.existsSync(path.join(profilePath, "cookies.sqlite"))
      : fs.existsSync(path.join(profilePath, "Cookies"));
    const hasHistory = b.id === "firefox"
      ? fs.existsSync(path.join(profilePath, "places.sqlite"))
      : fs.existsSync(path.join(profilePath, "History"));
    if (hasBookmarks || hasCookies || hasHistory) {
      result.push({ id: b.id, name: b.name, profilePath, hasBookmarks, hasCookies, hasHistory });
    }
  }
  return result;
});

function readSqliteViaPython(dbPath: string, query: string): any[] {
  const tmpFile = path.join(os.tmpdir(), "encre_sqlite_reader.py");
  const script = `
import sqlite3, json, sys
db = sys.argv[1]
q = sys.argv[2]
try:
  conn = sqlite3.connect(db)
  conn.text_factory = bytes
  cursor = conn.cursor()
  cursor.execute(q)
  rows = cursor.fetchall()
  cols = [d[0] for d in cursor.description]
  def decode(v):
    if isinstance(v, bytes): return v.decode("utf-8", errors="replace")
    return v
  result = [{cols[i]: decode(row[i]) for i in range(len(cols))} for row in rows]
  print(json.dumps(result, ensure_ascii=False))
  conn.close()
except Exception as e:
  print(json.dumps({"error": str(e)}))
  sys.exit(1)
`;
  fs.writeFileSync(tmpFile, script, "utf-8");
  const pythonCmd = process.platform === "win32" ? "python" : "python3";
  const out = execSync(`"${pythonCmd}" "${tmpFile}" "${dbPath}" "${query}"`, { encoding: "utf-8", timeout: 15000 });
  return JSON.parse(out);
}

async function readChromePasswordsViaPythonAsync(dbPath: string): Promise<any[]> {
  const tmpFile = path.join(os.tmpdir(), "encre_chrome_pw_reader.py");
  const script = `
import sqlite3, json, sys, os, ctypes, ctypes.wintypes
db = sys.argv[1]
try:
  import shutil, tempfile
  tmp_db = os.path.join(tempfile.gettempdir(), "encre_lr_tmp.db")
  shutil.copy2(db, tmp_db)
  conn = sqlite3.connect(tmp_db)
  conn.text_factory = bytes
  cursor = conn.cursor()
  cursor.execute("SELECT signon_realm, username_value, password_value FROM logins")
  rows = cursor.fetchall()
  class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
  crypt32 = ctypes.windll.crypt32
  kernel32 = ctypes.windll.kernel32
  def decrypt(d):
    if not d: return ""
    try:
      bi = DATA_BLOB(len(d), ctypes.cast(ctypes.create_string_buffer(d), ctypes.POINTER(ctypes.c_byte)))
      bo = DATA_BLOB(0, None)
      if crypt32.CryptUnprotectData(ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        raw = (ctypes.c_byte * bo.cbData).from_address(ctypes.addressof(bo.contents)) if bo.cbData else b""
        data = bytes(raw)
        kernel32.LocalFree(bo.pbData)
        return data.decode("utf-8", errors="replace")
    except: pass
    return ""
  result = [{"signon_realm": r[0].decode("utf-8", errors="replace") if isinstance(r[0], bytes) else str(r[0] or ""), "username_value": r[1].decode("utf-8", errors="replace") if isinstance(r[1], bytes) else str(r[1] or ""), "password_value": decrypt(r[2])} for r in rows]
  print(json.dumps(result, ensure_ascii=False))
  conn.close()
  try: os.remove(tmp_db)
  except: pass
except Exception as e:
  print(json.dumps({"error": str(e)}))
  sys.exit(1)
`;
  fs.writeFileSync(tmpFile, script, "utf-8");
  const pythonCmd = process.platform === "win32" ? "python" : "python3";
  return new Promise((resolve) => {
    exec(`"${pythonCmd}" "${tmpFile}" "${dbPath}"`, { encoding: "utf-8", timeout: 15000 }, (err, stdout) => {
      try { if (err) { resolve([]); return; } resolve(JSON.parse(stdout)); } catch { resolve([]); }
    });
  });
}

async function readSqliteViaPythonAsync(dbPath: string, query: string): Promise<any[]> {
  const tmpFile = path.join(os.tmpdir(), "encre_sqlite_reader.py");
  const script = `
import sqlite3, json, sys
db = sys.argv[1]
q = sys.argv[2]
try:
  conn = sqlite3.connect(db)
  conn.text_factory = bytes
  cursor = conn.cursor()
  cursor.execute(q)
  rows = cursor.fetchall()
  cols = [d[0] for d in cursor.description]
  def decode(v):
    if isinstance(v, bytes): return v.decode("utf-8", errors="replace")
    return v
  result = [{cols[i]: decode(row[i]) for i in range(len(cols))} for row in rows]
  print(json.dumps(result, ensure_ascii=False))
  conn.close()
except Exception as e:
  print(json.dumps({"error": str(e)}))
  sys.exit(1)
`;
  fs.writeFileSync(tmpFile, script, "utf-8");
  const pythonCmd = process.platform === "win32" ? "python" : "python3";
  return new Promise((resolve, reject) => {
    exec(`"${pythonCmd}" "${tmpFile}" "${dbPath}" "${query}"`, { encoding: "utf-8", timeout: 15000 }, (err, stdout) => {
      try {
        if (err) { resolve([]); return; }
        resolve(JSON.parse(stdout));
      } catch { resolve([]); }
    });
  });
}

ipcMain.handle("browser:import-data", async (_event, browserId: string, profilePath: string): Promise<{ success: boolean; data?: any; error?: string }> => {
  try {
    const data: any = { bookmarks: null, history: [], cookies: [] };

    if (browserId === "firefox") {
      // Read bookmarks & history from places.sqlite
      const placesPath = path.join(profilePath, "places.sqlite");
      if (fs.existsSync(placesPath)) {
        try {
          const bmRows = await readSqliteViaPythonAsync(placesPath, "SELECT b.title, p.url, b.dateAdded FROM moz_bookmarks b JOIN moz_places p ON b.fk = p.id WHERE b.type = 1 AND p.url LIKE 'http%' ORDER BY b.dateAdded DESC");
          data.bookmarks = { checksum: "", roots: { bookmark_bar: { children: [], date_added: "0", date_modified: "0", guid: "", id: "1", name: "Bookmarks bar", type: "folder" }, other: { children: [], date_added: "0", date_modified: "0", guid: "", id: "2", name: "Other bookmarks", type: "folder" } }, version: 1 };
          for (const row of bmRows) {
            data.bookmarks.roots.bookmark_bar.children.push({
              date_added: String(row.dateAdded || Date.now()),
              guid: "ff_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8),
              id: String(data.bookmarks.roots.bookmark_bar.children.length + 1),
              name: row.title || row.url || "",
              type: "url",
              url: row.url || "",
            });
          }
          const histRows = await readSqliteViaPythonAsync(placesPath, "SELECT p.url, p.title, p.visit_count, p.last_visit_date FROM moz_places p WHERE p.url LIKE 'http%' ORDER BY p.last_visit_date DESC LIMIT 5000");
          for (const row of histRows) {
            data.history.push({ id: Date.now(), url: row.url || "", title: row.title || "", visit_time: row.last_visit_date || Date.now(), visit_count: row.visit_count || 1, typed_count: 0 });
          }
        } catch {}
      }
      // Read cookies from cookies.sqlite
      const cookiePath = path.join(profilePath, "cookies.sqlite");
      if (fs.existsSync(cookiePath)) {
        try {
          const cookieRows = await readSqliteViaPythonAsync(cookiePath, "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies");
          for (const row of cookieRows) {
            data.cookies.push({ domain: row.host || "", name: row.name || "", value: row.value || "", path: row.path || "/", expires: row.expiry || 0, secure: !!row.isSecure, httpOnly: !!row.isHttpOnly, sameSite: row.sameSite || "unspecified" });
          }
        } catch {}
      }
    } else {
      // Chrome/Edge: read Bookmarks JSON
      const bmPath = path.join(profilePath, "Bookmarks");
      if (fs.existsSync(bmPath)) {
        try {
          data.bookmarks = JSON.parse(fs.readFileSync(bmPath, "utf-8"));
        } catch {}
      }
      // Read History SQLite
      const histPath = path.join(profilePath, "History");
      if (fs.existsSync(histPath)) {
        try {
          const histRows = await readSqliteViaPythonAsync(histPath, "SELECT u.url, u.title, u.visit_count, v.visit_time FROM urls u LEFT JOIN visits v ON u.id = v.url ORDER BY v.visit_time DESC LIMIT 5000");
          for (const row of histRows) {
            data.history.push({ id: Date.now(), url: row.url || "", title: row.title || "", visit_time: row.visit_time || Date.now(), visit_count: row.visit_count || 1, typed_count: 0 });
          }
        } catch {}
      }
      // Read Cookies SQLite
      const cookiePath = path.join(profilePath, "Cookies");
      if (fs.existsSync(cookiePath)) {
        try {
          const cookieRows = await readSqliteViaPythonAsync(cookiePath, "SELECT host_key, name, value, path, expires_utc, is_secure, is_httponly, samesite FROM cookies");
          for (const row of cookieRows) {
            data.cookies.push({ domain: row.host_key || "", name: row.name || "", value: row.value || "", path: row.path || "/", expires: row.expires_utc || 0, secure: !!row.is_secure, httpOnly: !!row.is_httponly, sameSite: row.samesite || "unspecified" });
          }
        } catch {}
      }
      // Read passwords from Login Data
      const loginPath = path.join(profilePath, "Login Data");
      if (fs.existsSync(loginPath)) {
        try {
          data.passwords = await readChromePasswordsViaPythonAsync(loginPath);
        } catch {}
      }
    }

    return { success: true, data };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});

ipcMain.handle("browser:save-imported-data", async (_event, data: { bookmarks?: any; history?: any[]; cookies?: any[]; passwords?: any[] }): Promise<{ success: boolean; error?: string }> => {
  try {
    if (data.bookmarks) {
      saveBrowserBookmarks(JSON.stringify(data.bookmarks));
    }
    if (data.history && data.history.length > 0) {
      const existing = loadBrowserHistory();
      const merged = existing ? JSON.parse(existing) : [];
      const seen = new Set(merged.map((h: any) => h.url));
      for (const entry of data.history) {
        if (!seen.has(entry.url)) {
          merged.push(entry);
          seen.add(entry.url);
        }
      }
      if (merged.length > 5000) merged.length = 5000;
      saveBrowserHistory(JSON.stringify(merged));
    }
    // Import cookies into the browser session partition
    if (data.cookies && data.cookies.length > 0) {
      const bs = session.fromPartition(BROWSER_PARTITION);
      for (const c of data.cookies) {
        try {
          const cleanDomain = typeof c.domain === 'string' ? c.domain.replace(/^\./, '') : '';
          if (!cleanDomain) continue;
          const url = `http${c.secure ? "s" : ""}://${cleanDomain}${c.path || "/"}`;
          // Convert Chrome FILETIME (100-ns since 1601-01-01) to Unix timestamp
          let expires = typeof c.expires === 'number' ? c.expires : 0;
          if (expires > 1e12) {
            expires = Math.floor((expires - 11644473600000000) / 10000000);
          }
          // Map sameSite from integer to string
          let sameSite = c.sameSite;
          if (typeof sameSite === 'number') {
            const map: Record<string, string> = { '-1': 'unspecified', '0': 'no_restriction', '1': 'lax', '2': 'strict' };
            sameSite = map[sameSite] || 'unspecified';
          }
          await bs.cookies.set({
            url,
            name: c.name,
            value: c.value,
            domain: cleanDomain,
            path: c.path || "/",
            secure: !!c.secure,
            httpOnly: !!c.httpOnly,
            sameSite: (String(sameSite || 'unspecified') as "unspecified" | "no_restriction" | "lax" | "strict"),
            expirationDate: expires > 0 ? expires : undefined,
          });
        } catch {}
      }
    }
    // Import passwords into the browser session
    if (data.passwords && data.passwords.length > 0) {
      saveBrowserPasswords(JSON.stringify(data.passwords));
    }
    return { success: true };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});

ipcMain.handle("browser:export-all", async (): Promise<{ success: boolean; error?: string }> => {
  try {
    const result = await dialog.showOpenDialog({
      properties: ["openDirectory"],
      title: "Select export directory",
    });
    if (result.canceled || !result.filePaths || !result.filePaths[0]) return { success: false, error: "canceled" };
    const dir = result.filePaths[0];

    // Export bookmarks (decrypted)
    const bmRaw = loadBrowserBookmarks();
    if (bmRaw) {
      try {
        const bmData = JSON.parse(bmRaw);
        const all: Array<{ name: string; url: string }> = [];
        function collect(children: any[]) {
          for (const c of children || []) {
            if (c.type === "url") all.push({ name: c.name, url: c.url || "" });
            if (c.type === "folder" && c.children) collect(c.children);
          }
        }
        if (bmData?.roots) {
          collect(bmData.roots.bookmark_bar?.children);
          collect(bmData.roots.other?.children);
        }
        let html = `<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n<TITLE>Bookmarks</TITLE>\n<H1>Bookmarks</H1>\n<DL><p>\n  <DT><H3>Bookmarks Bar</H3>\n  <DL><p>\n`;
        for (const bm of all) {
          const name = bm.name.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
          html += `    <DT><A HREF="${bm.url}" ADD_DATE="${Math.floor(Date.now() / 1000)}">${name}</A>\n`;
        }
        html += `  </DL><p>\n</DL><p>`;
        fs.writeFileSync(path.join(dir, "bookmarks.html"), html, "utf-8");
      } catch {}
    }

    // Export history (decrypted)
    const histRaw = loadBrowserHistory();
    if (histRaw) {
      try {
        const history = JSON.parse(histRaw);
        let csv = "url,title,visit_time,visit_count\n";
        for (const entry of history || []) {
          const url = (entry.url || "").replace(/"/g, '""');
          const title = (entry.title || "").replace(/"/g, '""');
          const time = entry.visit_time ? new Date(entry.visit_time).toISOString() : "";
          const count = entry.visit_count || 1;
          csv += `"${url}","${title}","${time}",${count}\n`;
        }
        fs.writeFileSync(path.join(dir, "history.csv"), csv, "utf-8");
      } catch {}
    }

    // Export cookies (decrypted)
    const cookieRaw = loadBrowserCookies();
    if (cookieRaw) {
      try {
        const cookies = JSON.parse(cookieRaw);
        let cookieStr = "# Netscape HTTP Cookie File\n# https://curl.se/rfc/cookie_spec.html\n";
        for (const c of cookies || []) {
          const domain = c.domain || "";
          const flag = domain.startsWith(".") ? "TRUE" : "FALSE";
          const path = c.path || "/";
          const secure = c.secure ? "TRUE" : "FALSE";
          const expires = Math.floor(c.expirationDate || c.expires || 0);
          const name = c.name || "";
          const value = (c.value || "").replace(/\n/g, "");
          cookieStr += `${domain}\t${flag}\t${path}\t${secure}\t${expires}\t${name}\t${value}\n`;
        }
        fs.writeFileSync(path.join(dir, "cookies.txt"), cookieStr, "utf-8");
      } catch {}
    }

    // Export localStorage (decrypted)
    const lsRaw = loadBrowserLocalStorage();
    if (lsRaw) {
      try {
        const lsData = JSON.parse(lsRaw);
        fs.writeFileSync(path.join(dir, "localStorage.json"), JSON.stringify(lsData, null, 2), "utf-8");
      } catch {}
    }

    return { success: true };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});


/* 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Terminal sessions 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶?*/

/* 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?CDP WebSocket Relay 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜 */

let cdpServer: http.Server | null = null;
let cdpWss: WebSocketServer | null = null;
let cdpPort = 0;
let cdpWebContentsId: number | null = null;
let cdpWsClient: WS | null = null;



// A single terminal/pty session tracked by the main process.
interface PtySession {
  pty: any;
  terminalId: number;
}
// Map of terminal id -> session.
const terminals = new Map<number, PtySession>();
// Monotonic counter used to assign terminal ids.
let terminalSeq = 0;
// Lazily loaded `node-pty` module (optional dependency).
let _nodePty: any = null;

/**
 * Lazily requires `node-pty`. Returns null when the native module is not
 * installed, so terminal features degrade gracefully.
 * @returns The node-pty module or null.
 */
function getNodePty(): any {
  if (!_nodePty) {
    try { _nodePty = require("node-pty"); } catch { return null; }
  }
  return _nodePty;
}

/**
 * Resolves the per-user data directory for Encre (`~/.dunimd/encre`).
 * Falls back to the current directory when no HOME/USERPROFILE is set.
 * @returns Absolute path to the Encre data directory.
 */
function getDataDir(): string {
  const home = process.env.HOME || process.env.USERPROFILE || ".";
  return path.join(home, ".dunimd", "encre");
}

/* 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Service management helpers 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑?*/

/**
 * Reads the PID recorded in the service PID file.
 * @returns The parsed PID, or null when the file is missing/invalid.
 */
function readPidFile(): number | null {
  try {
    if (fs.existsSync(PID_FILE)) {
      const pid = parseInt(fs.readFileSync(PID_FILE, "utf-8").trim(), 10);
      return isNaN(pid) ? null : pid;
    }
  } catch { /* ignore */ }
  return null;
}

/**
 * Tests whether a process with the given PID is currently alive using a
 * signal-less `kill(pid, 0)`.
 * @param pid - The process id to test.
 * @returns True if the process exists, false otherwise.
 */
function isProcessRunning(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/**
 * Kills a backend process (and its children) by PID.
 * On Windows uses `taskkill /F /T` (force + tree); on POSIX sends SIGKILL to
 * the process group (negative pid).
 * @param pid - The process id (or process-group id) to kill.
 */
function killServiceByPid(pid: number): void {
  try {
    if (process.platform === "win32") {
      // /F = force, /T = tree kill (children too)
      execSync(`taskkill /PID ${pid} /F /T`, { stdio: "ignore" });
    } else {
      // Kill the full process group …-pid means "process group id"
      try { process.kill(-pid, "SIGKILL"); } catch { process.kill(pid, "SIGKILL"); }
    }
  } catch { /* process already dead */ }
}

/** Force-free a TCP port by killing whatever process holds it (entire tree). */
function killProcessOnPort(port: number): void {
  try {
    if (process.platform === "win32") {
      const out = execSync(`netstat -ano | findstr "LISTENING" | findstr ":${port} "`, { encoding: "utf-8", timeout: 3000 });
      for (const line of out.split("\n")) {
        const parts = line.trim().split(/\s+/);
        const pid = parseInt(parts[parts.length - 1], 10);
        if (!isNaN(pid) && pid > 0) {
          try { execSync(`taskkill /PID ${pid} /F /T`, { stdio: "ignore" }); } catch {}
        }
      }
    } else {
      execSync(`lsof -ti:${port} | xargs kill -9 2>/dev/null`, { stdio: "ignore" });
    }
  } catch { /* nothing on that port */ }
}

/** Kills ALL processes (python.exe or encre-server.exe) whose command line contains "encre". */
function killAllEncreProcesses(): void {
  try {
    if (process.platform === "win32") {
      // Kill python.exe processes running encre (dev mode)
      execSync(
        `powershell -Command "Get-CimInstance Win32_Process -Filter \\"name='python.exe'\\" | Where-Object { $_.CommandLine -match 'encre' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"`,
        { stdio: "ignore", timeout: 5000 },
      );
      // Kill bundled encre-server.exe processes (release mode)
      execSync(`taskkill /F /IM "encre-server.exe" 2>nul`, { stdio: "ignore", timeout: 3000 });
    } else {
      execSync(`pkill -f "python.*encre" 2>/dev/null`, { stdio: "ignore" });
      execSync(`pkill -f "encre-server" 2>/dev/null`, { stdio: "ignore" });
    }
  } catch { /* no remaining encre processes */ }
}

/**
 * Spawns the backend service as a detached child process and resolves once it
 * logs "Server ready".  In release mode (PyInstaller bundle exists), launches
 * the standalone `encre-server` executable directly; in development mode,
 * falls back to spawning the system Python interpreter.
 * Rejects on timeout (30s) or early exit/error.
 * @returns A promise that resolves when the server is ready.
 */
function startPythonServer(): Promise<void> {
  return new Promise((resolve, reject) => {
    const isWin = process.platform === "win32";
    const rootDir = path.resolve(__dirname, "..", "..");

    // --- Detect bundled server executable (packaged builds only) ---
    // `npm start` runs the unpackaged app (app.isPackaged === false) and is
    // always treated as a development environment: it must run the in-repo
    // Python source backend, never a stale PyInstaller bundle that happens to
    // sit in build/server. Only a real packaged build uses the bundled exe.
    const bundledExeName = isWin ? "encre-server.exe" : "encre-server";
    const bundledExe = app.isPackaged
      ? [path.join(process.resourcesPath || "", "encre-server", bundledExeName)].find((p) => fs.existsSync(p))
      : undefined;

    let spawnCmd: string;
    let spawnArgs: string[];
    let spawnEnv: NodeJS.ProcessEnv;
    let spawnCwd: string;

    if (bundledExe) {
      // Release mode: use the PyInstaller-bundled standalone executable.
      console.log(`[server] using bundled exe: ${bundledExe}`);
      spawnCmd = bundledExe;
      spawnArgs = ["--port", String(WS_PORT), "--service", "--log-level", "DEBUG"];
      spawnEnv = { ...process.env, ENCRE_DATA_DIR: DATA_DIR };
      spawnCwd = path.dirname(bundledExe);
    } else {
      // Development mode: always run the in-repo Python source backend.
      console.log("[server] development mode (unpackaged) -> using system Python source backend");
      const pythonCmd = isWin ? "python" : "python3";
      // The "encre" package spans harness/encre (library) and core/encre
      // (server layer); both trees must be on PYTHONPATH.
      const harnessDir = path.resolve(rootDir, "harness");
      const coreDir = path.resolve(rootDir, "core");
      const pythonPath = isWin
        ? `${harnessDir};${coreDir};${rootDir};${process.env.PYTHONPATH || ""}`
        : `${harnessDir}:${coreDir}:${rootDir}:${process.env.PYTHONPATH || ""}`;
      spawnCmd = pythonCmd;
      spawnArgs = ["-m", "encre.server.app", "--port", String(WS_PORT), "--service", "--log-level", "DEBUG"];
      spawnEnv = { ...process.env, PYTHONPATH: pythonPath, ENCRE_DATA_DIR: DATA_DIR };
      spawnCwd = rootDir;
    }

    serverProcess = spawn(spawnCmd, spawnArgs, {
      cwd: spawnCwd,
      stdio: ["ignore", "pipe", "pipe"],
      env: spawnEnv,
      detached: isWin ? true : false,
      windowsHide: isWin ? true : false,
    });

    // Do NOT unref …the before-quit handler needs the reference to kill
    // this process tree on exit.
    if (isWin && serverProcess) {
      // Log the PID for debugging; Python server writes its own PID file
      console.log(`[server] spawned as PID ${serverProcess.pid}`);
    }

    let resolved = false;
    let stderrOutput = "";
    const timeout = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        const msg = stderrOutput
          ? `Server start timed out after 30s\n\n${stderrOutput}`
          : "Server start timed out after 30s";
        reject(new Error(msg));
      }
    }, 30000);

    const onData = (chunk: Buffer, src: string) => {
      const text = chunk.toString("utf-8");
      if (src === "stderr") {
        stderrOutput += text;
        console.error("[encre server]", text);
      }
      if (!resolved) {
        const match = text.match(/Server ready: ws:\/\/[\w.-]+:(\d+)\/ws/);
        if (match) {
          resolved = true;
          clearTimeout(timeout);
          resolve();
        }
      }
    };

    serverProcess.stdout?.on("data", (chunk: Buffer) => onData(chunk, "stdout"));
    serverProcess.stderr?.on("data", (chunk: Buffer) => onData(chunk, "stderr"));

    serverProcess.on("exit", (code) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timeout);
        const msg = stderrOutput
          ? `Server exited with code ${code}\n\n${stderrOutput}`
          : `Server exited with code ${code}`;
        reject(new Error(msg));
      }
    });

    serverProcess.on("error", (err) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timeout);
        reject(err);
      }
    });
  });
}

/**
 * Fully restarts the backend service: kills any existing instance (via PID
 * file and/or the tracked process), frees the port, then spawns a fresh one
 * and updates the tray status.
 */
async function restartService(event: Electron.IpcMainInvokeEvent): Promise<{ success: boolean; error?: string }> {
  const sendProgress = (progress: number) => {
    try { event.sender.send("restart-progress", { progress }); } catch {}
  };
  sendProgress(5);
  // Kill existing process via PID file (most reliable across detached processes)
  const existingPid = readPidFile();
  if (existingPid !== null && isProcessRunning(existingPid)) {
    killServiceByPid(existingPid);
  }
  // Also kill serverProcess reference if we have one
  if (serverProcess) {
    try { serverProcess.kill(); } catch {}
    serverProcess = null;
  }
  // Sweep any remaining Encre server processes
  killAllEncreProcesses();
  // Clean up PID file
  try { fs.unlinkSync(PID_FILE); } catch {}
  sendProgress(30);
  // Wait briefly for port to be released
  await new Promise((r) => setTimeout(r, 1500));
  sendProgress(60);
  // Start again
  try {
    await startPythonServer();
    serverStartError = null;
    updateTrayStatus(true);
    sendProgress(100);
    return { success: true };
  } catch (err) {
    console.error("Failed to restart service:", err);
    serverStartError = String(err);
    updateTrayStatus(false);
    return { success: false, error: String(err) };
  }
}

// Currently selected tray locale ("en" or "zh").
let currentTrayLocale = "en";
// Currently selected tray theme ("dark" or "light").
let currentTrayTheme: "dark" | "light" = "dark";
// Currently active session mode in the tray ("normal" or "iwork").
let currentTrayMode = "normal";
// Cache of all sessions shown in the tray popup.
let traySessionsCache: any[] = [];
// Cache of normal/iwork sessions shown in the tray popup.
let traySessionsBothCache: { normal: any[]; iwork: any[]; workspaces?: any[] } = { normal: [], iwork: [], workspaces: [] };
// The floating tray popup window (null when not open).
let trayPopup: BrowserWindow | null = null;


// Localized strings for the tray, keyed by locale.
const TRAY_LABELS: Record<string, { openYim: string; quit: string; tooltip: string }> = {
  en: { openYim: "Open Encre Agent", quit: "Quit", tooltip: "Encre Agent" },
  zh: { openYim: "打开 Encre Agent", quit: "退出", tooltip: "Encre Agent" },
};

/**
 * Updates the tray tooltip to reflect the backend running status.
 * @param running - Whether the backend service is currently running.
 */
function updateTrayStatus(_running: boolean): void {
  if (!tray) return;
  const labels = TRAY_LABELS[currentTrayLocale] || TRAY_LABELS.en;
  tray.setToolTip(labels.tooltip);
}

/**
 * Resolves the effective tray theme from a preference string.
 * Explicit "light"/"dark" are returned as-is; "system" (or anything else)
 * defers to the OS dark-mode setting via nativeTheme.
 * @param themePreference - "light", "dark", or "system".
 * @returns The resolved concrete theme ("light" | "dark").
 */
function resolveTrayTheme(themePreference: string): "dark" | "light" {
  if (themePreference === "light" || themePreference === "dark") return themePreference;
   return "light"; // "system" defaults to light
}

/**
 * Pushes the latest cached session/theme data to the open tray popup window.
 */
function sendTrayDataToPopup(): void {
  if (trayPopup && !trayPopup.isDestroyed()) {
    trayPopup.webContents.send("tray-data", {
      sessions: traySessionsCache,
      sessionsNormal: traySessionsBothCache.normal,
      sessionsIwork: traySessionsBothCache.iwork,
      workspaces: traySessionsBothCache.workspaces || [],
      activeMode: currentTrayMode,
      locale: currentTrayLocale,
      theme: currentTrayTheme,
    });
  }
}

/**
 * Closes and destroys the tray popup window if it is open.
 */
function closeTrayPopup(): void {
  if (trayPopup && !trayPopup.isDestroyed()) {
    trayPopup.close();
    trayPopup = null;
  }
}

/**
 * Shows or hides the tray popup window. When opening, it creates a small
 * frameless window, positions it above the tray icon, and loads the popup
 * HTML. Clicking outside (blur) closes it.
 */
function toggleTrayPopup(): void {
  if (trayPopup && !trayPopup.isDestroyed()) {
    closeTrayPopup();
    return;
  }

  if (!tray) return;
  const bounds = tray.getBounds();

  trayPopup = new BrowserWindow({
    width: 280,
    height: 440,
    frame: false,
    resizable: false,
    skipTaskbar: true,
    show: false,
    backgroundColor: currentTrayTheme === "dark" ? "#1a1a1a" : "#ffffff",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (process.platform === "win32" || process.platform === "darwin") {
    trayPopup.setHasShadow(true);
  }

  trayPopup.webContents.on("did-finish-load", () => {
    sendTrayDataToPopup();
  });

  trayPopup.loadFile(path.join(__dirname, "..", "renderer", "tray-popup.html"));

  trayPopup.once("ready-to-show", () => {
    if (!trayPopup || trayPopup.isDestroyed()) return;
    const popupBounds = trayPopup.getBounds();
    let x = Math.round(bounds.x + bounds.width / 2 - popupBounds.width / 2);
    let y = Math.round(bounds.y - popupBounds.height - 8);
    if (y < 0) {
      y = Math.round(bounds.y + bounds.height + 8);
    }
    trayPopup.setPosition(x, y);
    trayPopup.show();
  });

  trayPopup.on("blur", () => closeTrayPopup());
  trayPopup.on("closed", () => { trayPopup = null; });
}

/**
 * Returns the tray label set for the current locale (falls back to English).
 * @returns The localized open/quit/tooltip labels.
 */
function getTrayLabels(): { openYim: string; quit: string; tooltip: string } {
  return TRAY_LABELS[currentTrayLocale] || TRAY_LABELS.en;
}

/**
 * HTML-escapes a string for safe insertion into the tray popup markup.
 * @param s - The raw string to escape.
 * @returns The escaped string.
 */
function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * Truncates a string to `max` characters, appending an ellipsis when cut.
 * @param str - The string to truncate.
 * @param max - Maximum length (including the ellipsis).
 * @returns The truncated string.
 */
function truncate(str: string, max: number): string {
  return str.length > max ? str.slice(0, max - 1) + "…" : str;
}

/**
 * Returns the on-disk path to the Encre application icon (ICO) asset.
 * @returns Absolute path to the renderer's Encre.ico.
 */
function resolveAppIconPath(): string {
  return path.join(__dirname, "..", "renderer", "assets", "Encre.ico");
}

/**
 * Loads the Encre application icon as a NativeImage, falling back to an
 * inline 16x16 green briefcase data URL when the asset is missing.
 * @returns A NativeImage usable for windows and the tray.
 */
function loadAppIcon(): Electron.NativeImage {
  const iconPath = resolveAppIconPath();
  try {
    if (fs.existsSync(iconPath)) {
      const img = nativeImage.createFromPath(iconPath);
      if (!img.isEmpty()) return img;
    }
  } catch { /* fall through to fallback */ }
  // 16x16 green briefcase fallback (matches legacy tray look)
  const fallbackDataUrl =
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAI0lEQVR4nGNgoDZQOhr3HxsmyQBixIi2mWiXjBowasAINwAAHBA4uJJpecIAAAAASUVORK5CYII=";
  return nativeImage.createFromDataURL(fallbackDataUrl);
}

/**
 * Creates the system tray icon and wires its click/right-click handlers.
 * Left click shows/focuses the main window; right click toggles the popup.
 */
function createTray(): void {
  // Use the Encre app icon for the tray …works on Windows (ICO) and macOS/Linux.
  const icon = loadAppIcon();
  tray = new Tray(icon);
  const labels = getTrayLabels();
  tray.setToolTip(labels.tooltip);

  updateTrayStatus(true);

  tray.on("click", () => {
    if (mainWindow === null) {
      createWindow();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  tray.on("right-click", () => {
    toggleTrayPopup();
  });
}

/* 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Window creation 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎?*/

/**
 * Creates the main application BrowserWindow: a frameless, hidden-by-default
 * window that loads the renderer, locks zoom to 100%, opens the dev tools only
 * on demand (ENCRE_DEVTOOLS=1), and hides to the tray on close instead of quitting.
 */
function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 800,
    minHeight: 600,
    frame: false,
    titleBarStyle: "hidden",
    titleBarOverlay: false,
    backgroundColor: "#0f0f0f",
    title: "Encre Agent Desktop",
    icon: resolveAppIconPath(),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"));
  // Push maximize-state changes to the renderer so the custom maximize/restore
  // button icon follows native state changes (Win+Up snap, drag-to-top,
  // double-click on the drag region), not just in-app button clicks.
  const pushMaxState = () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send("window-maximized-state", mainWindow.isMaximized());
  };
  mainWindow.on("maximize", pushMaxState);
  mainWindow.on("unmaximize", pushMaxState);
  // Developer tools are opt-in (ENCRE_DEVTOOLS=1). Leaving them docked in a
  // frameless window desyncs the OS client-area hit region from what the
  // renderer paints, so real mouse clicks on the custom min/max/close buttons
  // silently miss the buttons (injected/synthetic clicks still "work", which
  // is why this only shows up with a real mouse). Docked DevTools also grabs
  // focus right after boot, which makes the tray popup's blur handler close it
  // again the instant it opens. Detached mode is used to avoid the same
  // hit-region desync when the tools are actually wanted.
  if (process.env.ENCRE_DEVTOOLS === "1") {
    mainWindow.webContents.openDevTools({ mode: "detach" });
  }
  // Force-reset and lock zoom to 100% to avoid accidental Ctrl+-/Ctrl+wheel shrink.
  mainWindow.webContents.setZoomFactor(1);
  mainWindow.webContents.setVisualZoomLevelLimits(1, 1).catch(() => {});
  mainWindow.webContents.on("did-finish-load", () => {
    mainWindow?.webContents.setZoomFactor(1);
  });
  mainWindow.webContents.on("before-input-event", (event, input) => {
    const key = (input.key || "").toLowerCase();
    // Block Win key when capturing shortcuts, preventing Start menu from opening
    if (winKeyCapture && key === "meta") {
      event.preventDefault();
      return;
    }
    const isZoomHotkey =
      input.control &&
      (key === "-" || key === "_" || key === "+" || key === "=" || key === "0");
    if (isZoomHotkey) {
      event.preventDefault();
      mainWindow?.webContents.setZoomFactor(1);
    }
  });

  // mainWindow.webContents.openDevTools();

  // Show the main window as soon as it can paint; the renderer's in-app
  // splash screen covers the loading phase, so there is no native splash
  // window anymore.
  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });

  // Override close: hide to tray instead of destroying, unless user clicked "Quit"
  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?IPC handlers 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?

// Returns the backend WebSocket port to the renderer.
ipcMain.handle("getServerPort", () => {
  return WS_PORT;
});

// Opens a multi-file picker dialog.
ipcMain.handle("pickFiles", async () => {
  if (!mainWindow) return [];
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openFile", "multiSelections"],
  });
  return result.canceled ? [] : result.filePaths;
});

// Opens a directory picker dialog.
ipcMain.handle("pickDirectory", async () => {
  if (!mainWindow) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"],
  });
  return result.canceled ? null : result.filePaths[0] || null;
});

// Reads a file from disk, returning its UTF-8 content (for text files) or empty content (for binary files).
ipcMain.handle("readFile", async (_event, filePath: string) => {
  try {
    const buf = fs.readFileSync(filePath);
    const ext = path.extname(filePath).toLowerCase();
    const mimeMap: Record<string, string> = {
      ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
      ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
      ".ico": "image/x-icon", ".svg": "image/svg+xml",
      ".mp4": "video/mp4", ".webm": "video/webm",
      ".pdf": "application/pdf",
      ".zip": "application/zip",
    };
    const mime = mimeMap[ext] || "";
    const binaryExts = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".mp4", ".webm", ".pdf", ".zip", ".gz", ".tar", ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".ttf", ".otf", ".woff", ".woff2"]);
    const isBinary = binaryExts.has(ext) || buf.indexOf(0) !== -1;
    return {
      content: isBinary ? "" : buf.toString("utf-8"),
      size: buf.length,
      mime_type: mime,
      is_binary: isBinary,
    };
  } catch {
    return null;
  }
});

// Reads filesystem metadata for a single path — used by the tab hover
// tooltip (size + last-modified) so the renderer never has to read the file
// contents just to stat it.
ipcMain.handle("statFile", async (_event, filePath: string) => {
  try {
    const st = fs.statSync(filePath);
    return {
      size: st.size,
      mtime: st.mtimeMs,
      isDirectory: st.isDirectory(),
    };
  } catch {
    return null;
  }
});

// Writes a UTF-8 file to disk, creating parent directories as needed.
ipcMain.handle("writeFile", async (_event, filePath: string, data: string) => {
  try {
    const dir = path.dirname(filePath);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(filePath, data, "utf-8");
    return true;
  } catch (err) {
    console.error("writeFile error:", err);
    return false;
  }
});

// Copies files/directories to the system clipboard so they can be pasted into
// the OS file manager (cross-application copy). Platform-specific formats:
//   - Windows : CF_HDROP ('FileName') with a DROPFILES struct.
//   - macOS   : pasteboard type 'public.file-url' holding a file:// URL.
//   - Linux   : 'x-special/gnome-copied-files' (GNOME/KDE) + 'text/uri-list'.
ipcMain.handle("clipboard:copy-files", (_event, filePaths: string[]) => {
  try {
    if (!Array.isArray(filePaths) || filePaths.length === 0) return false;
    const paths = filePaths
      .filter((p) => typeof p === "string" && p.length > 0)
      .map((p) => p.replace(/[\\/]+$/, ""));
    if (paths.length === 0) return false;

    if (process.platform === "win32") {
      // DROPFILES struct: 20-byte header + UTF-16LE double-null-terminated paths.
      const header = Buffer.alloc(20);
      header.writeUInt32LE(20, 0); // pFiles: offset of the file list
      header.writeUInt32LE(1, 16); // fWide: TRUE (UTF-16 paths)
      const list = paths.map((p) => p.replace(/\//g, "\\") + "\0").join("") + "\0";
      clipboard.writeBuffer("FileName", Buffer.concat([header, Buffer.from(list, "utf16le")]));
      return true;
    }

    if (process.platform === "darwin") {
      // 'public.file-url' is the modern UTType Finder and most apps use for a
      // single copied file (multiple files need the NSKeyedArchiver-encoded
      // 'NSFilenamesPboardType', not constructible without a native module).
      clipboard.writeBuffer("public.file-url", Buffer.from(pathToFileURL(paths[0]).href, "utf8"));
      return true;
    }

    // Linux: GNOME/KDE copy format is 'x-special/gnome-copied-files'
    // ("copy\n<uris>"); 'text/uri-list' is the generic fallback.
    const uris = paths.map((p) => pathToFileURL(p).href);
    clipboard.writeBuffer("x-special/gnome-copied-files", Buffer.from("copy\n" + uris.join("\n"), "utf8"));
    clipboard.writeBuffer("text/uri-list", Buffer.from(uris.join("\r\n") + "\r\n", "utf8"));
    return true;
  } catch (err) {
    console.error("clipboard:copy-files error:", err);
    return false;
  }
});

// Reads file paths from the system clipboard, e.g. files copied in the OS file
// manager. Returns [] when the clipboard holds no files.
ipcMain.handle("clipboard:read-files", () => {
  try {
    if (process.platform === "win32") {
      const buffer = clipboard.readBuffer("FileName");
      if (!buffer || buffer.length === 0) return [];
      const list = buffer.subarray(Math.min(20, buffer.length)).toString("utf16le");
      const paths = list
        .split("\0")
        .map((p) => p.trim())
        .filter((p) => p.length > 0)
        .map((p) => p.replace(/\//g, "\\"));
      return [...new Set(paths)];
    }

    if (process.platform === "darwin") {
      const buffer = clipboard.readBuffer("public.file-url");
      if (!buffer || buffer.length === 0) return [];
      const url = buffer.toString("utf8").trim();
      if (!url) return [];
      try {
        return [fileURLToPath(url)];
      } catch {
        return [url];
      }
    }

    // Linux: try GNOME/KDE format first, then the generic text/uri-list.
    for (const fmt of ["x-special/gnome-copied-files", "text/uri-list"]) {
      const buffer = clipboard.readBuffer(fmt);
      if (!buffer || buffer.length === 0) continue;
      const lines = buffer
        .toString("utf8")
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter((l) => l.length > 0 && !/^(copy|cut)$/.test(l));
      const paths: string[] = [];
      for (const uri of lines) {
        try {
          paths.push(fileURLToPath(uri));
        } catch {
          if (uri.startsWith("file://")) {
            paths.push(decodeURIComponent(uri.slice(7)));
          }
        }
      }
      if (paths.length > 0) return [...new Set(paths)];
    }
    return [];
  } catch (err) {
    console.error("clipboard:read-files error:", err);
    return [];
  }
});

// Copies the given files/directories into targetDir. Used by the file-tree
// "Paste" action; auto-renames on name conflicts (Explorer-style "(1)").
ipcMain.handle("fs:paste-files", async (_event, sourcePaths: string[], targetDir: string) => {
  try {
    if (!Array.isArray(sourcePaths) || sourcePaths.length === 0 || !targetDir) {
      return { success: false, error: "No source paths or target directory" };
    }
    fs.mkdirSync(targetDir, { recursive: true });
    const uniqueDest = (name: string): string => {
      const dest = path.join(targetDir, name);
      if (!fs.existsSync(dest)) return dest;
      const dot = name.lastIndexOf(".");
      const base = dot > 0 ? name.slice(0, dot) : name;
      const ext = dot > 0 ? name.slice(dot) : "";
      for (let i = 1; ; i++) {
        const candidate = path.join(targetDir, `${base} (${i})${ext}`);
        if (!fs.existsSync(candidate)) return candidate;
      }
    };
    const copyRec = (src: string, dest: string): void => {
      const st = fs.statSync(src);
      if (st.isDirectory()) {
        fs.mkdirSync(dest, { recursive: true });
        for (const child of fs.readdirSync(src)) {
          copyRec(path.join(src, child), path.join(dest, child));
        }
      } else {
        fs.copyFileSync(src, dest);
      }
    };
    const results: Array<{ source: string; target: string }> = [];
    for (const src of sourcePaths) {
      if (!fs.existsSync(src)) continue;
      const name = path.basename(src.replace(/[\\/]+$/, ""));
      const dest = uniqueDest(name);
      copyRec(src, dest);
      results.push({ source: src, target: dest });
    }
    return { success: results.length > 0, results };
  } catch (err) {
    console.error("fs:paste-files error:", err);
    return { success: false, error: String(err) };
  }
});

// Creates a new empty file in parentDir. Auto-renames on name conflicts the
// same way Explorer does ("name (1).txt").
ipcMain.handle("fs:create-file", async (_event, parentDir: string, name: string) => {
  try {
    if (!parentDir || !name) return { success: false, error: "Missing parent directory or name" };
    const clean = name.replace(/[/\\]/g, "");
    if (!clean) return { success: false, error: "Invalid file name" };
    fs.mkdirSync(parentDir, { recursive: true });
    const uniqueName = (base: string): string => {
      const dot = base.lastIndexOf(".");
      const stem = dot > 0 ? base.slice(0, dot) : base;
      const ext = dot > 0 ? base.slice(dot) : "";
      if (!fs.existsSync(path.join(parentDir, base))) return base;
      for (let i = 1; ; i++) {
        const candidate = `${stem} (${i})${ext}`;
        if (!fs.existsSync(path.join(parentDir, candidate))) return candidate;
      }
    };
    const finalName = uniqueName(clean);
    fs.writeFileSync(path.join(parentDir, finalName), "");
    return { success: true, path: path.join(parentDir, finalName), name: finalName };
  } catch (err) {
    console.error("fs:create-file error:", err);
    return { success: false, error: String(err) };
  }
});

// Creates a new folder in parentDir. Auto-renames on name conflicts.
ipcMain.handle("fs:create-folder", async (_event, parentDir: string, name: string) => {
  try {
    if (!parentDir || !name) return { success: false, error: "Missing parent directory or name" };
    const clean = name.replace(/[/\\]/g, "");
    if (!clean) return { success: false, error: "Invalid folder name" };
    fs.mkdirSync(parentDir, { recursive: true });
    if (!fs.existsSync(path.join(parentDir, clean))) {
      fs.mkdirSync(path.join(parentDir, clean));
      return { success: true, path: path.join(parentDir, clean), name: clean };
    }
    for (let i = 1; ; i++) {
      const candidate = `${clean} (${i})`;
      if (!fs.existsSync(path.join(parentDir, candidate))) {
        fs.mkdirSync(path.join(parentDir, candidate));
        return { success: true, path: path.join(parentDir, candidate), name: candidate };
      }
    }
  } catch (err) {
    console.error("fs:create-folder error:", err);
    return { success: false, error: String(err) };
  }
});

// Deletes a file/folder by moving it to the OS trash (recoverable), matching
// the behaviour of native file managers.
ipcMain.handle("fs:delete", async (_event, targetPath: string) => {
  try {
    if (!targetPath) return { success: false, error: "Missing path" };
    await shell.trashItem(targetPath);
    return { success: true };
  } catch (err) {
    console.error("fs:delete error:", err);
    return { success: false, error: String(err) };
  }
});

// Returns the Encre data directory path.
ipcMain.handle("getAppPath", () => {
  return getDataDir();
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Crypto keyfile access 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫?

// Reads the user's transport-encryption keyfile. The canonical location is
// <data_dir>/keys/keyfile, matching the Python side's `keyfile_path()`; the
// legacy ~/.encre/keyfile is still tried so un-migrated installs keep working.
// Candidates are tried in that order so a stale legacy file can never shadow
// the live key (the two would derive different master keys).
ipcMain.handle("readKeyfile", () => {
  try {
    const legacyDir = path.join(
      process.env.HOME || process.env.USERPROFILE || ".",
      ".encre"
    );
    const candidates = [
      path.join(DATA_DIR, "keys", "keyfile"),
      path.join(legacyDir, "keyfile"),
    ];
    for (const keyfilePath of candidates) {
      if (!fs.existsSync(keyfilePath)) continue;
      const buf = fs.readFileSync(keyfilePath);
      if (buf.byteLength === 0) continue;
      return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    }
    return null;
  } catch {
    return null;
  }
});

// Returns a stable machine identifier (from /etc/machine-id or hostname).
ipcMain.handle("readMachineId", () => {
  try {
    const mid = fs.readFileSync("/etc/machine-id", "utf-8").trim();
    if (mid && mid !== "uninitialized") return mid;
  } catch {
    /* fall through */
  }
  return require("os").hostname();
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Service IPC 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?

// Reports whether the backend service is running (via PID file).
ipcMain.handle("getServiceStatus", () => {
  const pid = readPidFile();
  let running = false;
  if (pid !== null && isProcessRunning(pid)) {
    running = true;
  }
  return { running, pid, port: WS_PORT, error: running ? null : serverStartError };
});

// Restarts the backend service on demand.
ipcMain.handle("restartService", async (event) => {
  return await restartService(event);
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Logs & Diagnostics IPC 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?

// Opens the active log file (or its folder) in the system file manager.
ipcMain.handle("openLogs", async () => {
  const dataDir = getDataDir();
  if (!fs.existsSync(dataDir)) {
    fs.mkdirSync(dataDir, { recursive: true });
  }
  // Reveal the active log file in the system file manager; fall back to
  // opening the data directory root when the log file does not exist yet.
  const logFile = path.join(dataDir, "yimd.log");
  if (fs.existsSync(logFile)) {
    shell.showItemInFolder(logFile);
  } else {
    await shell.openPath(dataDir);
  }
});

// Collects app/agent versions, data dir, log path and the last 200 log lines.
ipcMain.handle("getDiagnostics", async () => {
  const pkgPath = path.join(__dirname, "..", "package.json");
  const desktopVersion = JSON.parse(fs.readFileSync(pkgPath, "utf-8")).version || "0.0.0";
  const rootDir = path.resolve(__dirname, "..", "..");
  const pyprojectPath = path.join(rootDir, "harness", "pyproject.toml");
  let agentVersion = "0.0.0";
  try {
    const pyContent = fs.readFileSync(pyprojectPath, "utf-8");
    const match = pyContent.match(/^version\s*=\s*"([^"]+)"/m);
    if (match) agentVersion = match[1];
  } catch {}
  const versions = { desktop: desktopVersion, agent: agentVersion };
  const dataDir = getDataDir();
  // The live log lives at the root of the data directory as ``yimd.log``
  // (created by ``encre.server.service``). The legacy ``logs/encre.log``
  // path is no longer used.
  const logFile = path.join(dataDir, "yimd.log");
  let recentLogs: string[] = [];
  if (fs.existsSync(logFile)) {
    try {
      const stat = fs.statSync(logFile);
      // Tail the last 200 lines efficiently without slurping the whole file
      // (the live log can exceed 50 MB on long-running installations).
      const target = 200;
      const chunkSize = 256 * 1024;
      let position = stat.size;
      let buffer = Buffer.alloc(0);
      let collected: string[] = [];
      while (position > 0 && collected.length <= target + 1) {
        const readSize = Math.min(chunkSize, position);
        position -= readSize;
        const fd = fs.openSync(logFile, "r");
        const chunk = Buffer.alloc(readSize);
        fs.readSync(fd, chunk, 0, readSize, position);
        fs.closeSync(fd);
        buffer = Buffer.concat([chunk, buffer]);
        collected = buffer.toString("utf-8").split("\n");
      }
      recentLogs = collected.slice(-target);
    } catch { /* best-effort */ }
  }
  return {
    versions,
    dataDir,
    logFile,
    recentLogs,
  };
});

// 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?Structured Log Reader 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟鏇㈡⒒娴ｇ鏆遍柛妯荤矒瀹曟垿骞樼紒妯煎帗闂佺绻愰ˇ顖涚妤ｅ啯鈷戦柛鎰絻鐢劑鏌涚€ｎ偅宕岄柡灞界Ч瀹曟寰勬繝浣割棜闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞存粓绠栧娲礃閹绘帒杈呴梺绋款儐閹瑰洭寮诲澶婄濠㈣泛锕ｆ竟?

interface LogEntry {
  timestamp: string;
  level: string;
  source: string;
  message: string;
}

/**
 * Reads and filters log entries from the active log file.
 * Supports date-range filtering and pagination (newest-first).
 */
ipcMain.handle("getLogs", async (_event, filters: {
  fromDate?: string;
  toDate?: string;
  offset?: number;
  limit?: number;
}) => {
  const logFile = path.join(getDataDir(), "yimd.log");
  if (!fs.existsSync(logFile)) return { entries: [], total: 0, fileExists: false, rawLines: 0 };

  const fromMs = filters.fromDate ? new Date(filters.fromDate + "T00:00:00").getTime() : 0;
  const toMs = filters.toDate ? new Date(filters.toDate + "T23:59:59").getTime() : Infinity;
  const offset = filters.offset ?? 0;
  const limit = filters.limit ?? 500;

  const logLineRe = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)\s*\[(\w+)\]\s*([^:]+?):\s*(.*)$/;

  try {
    const content = fs.readFileSync(logFile, "utf-8");
    const rawLines = content.replace(/\r\n/g, "\n").split("\n");
    const rawCount = rawLines.length;

    // Parse into entries (handle multi-line messages)
    const allEntries: LogEntry[] = [];
    let current: LogEntry | null = null;

    for (const line of rawLines) {
      const m = line.match(logLineRe);
      if (m) {
        if (current) allEntries.push(current);
        current = {
          timestamp: m[1].replace(",", "."),
          level: m[2],
          source: m[3].trim(),
          message: m[4],
        };
      } else if (current && line.trim()) {
        current.message += "\n" + line;
      }
    }
    if (current) allEntries.push(current);

    // If no entries were parsed but raw lines exist, use raw lines as fallback
    const entries = allEntries.length === 0 && rawCount > 0
      ? rawLines.filter(l => l.trim()).map((line, i) => ({
          timestamp: "",
          level: "",
          source: "",
          message: line,
        }))
      : allEntries;

    // Filter by date range
    const filtered = entries.filter(e => {
      if (!e.timestamp) return true;
      const ts = new Date(e.timestamp).getTime();
      return ts >= fromMs && ts <= toMs;
    });

    // Paginate (newest first)
    const reversed = filtered.reverse();
    const total = reversed.length;
    const page = reversed.slice(offset, offset + limit);

    return { entries: page, total, fileExists: true, rawLines: rawCount };
  } catch {
    return null;
  }
});

/** Lightweight stat check — does NOT read file content. */
ipcMain.handle("getLogFileInfo", async () => {
  const logFile = path.join(getDataDir(), "yimd.log");
  try {
    const stat = fs.statSync(logFile);
    return { exists: true, size: stat.size, mtimeMs: stat.mtimeMs };
  } catch {
    return { exists: false, size: 0, mtimeMs: 0 };
  }
});

// Reads a file and returns its content as base64 with mime type (for images, etc.).
ipcMain.handle("readFileBase64", async (_event, filePath: string) => {
  try {
    const buf = fs.readFileSync(filePath);
    const ext = path.extname(filePath).toLowerCase();
    const mimeMap: Record<string, string> = {
      ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
      ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
      ".ico": "image/x-icon", ".svg": "image/svg+xml",
      ".mp4": "video/mp4", ".webm": "video/webm",
    };
    return {
      data: buf.toString("base64"),
      mime_type: mimeMap[ext] || "application/octet-stream",
    };
  } catch {
    return null;
  }
});

// Clear/truncate the active log file.
ipcMain.handle("clearLogs", async () => {
  const logFile = path.join(getDataDir(), "yimd.log");
  try {
    fs.writeFileSync(logFile, "");
    return { success: true };
  } catch {
    return { success: false };
  }
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Auto-start IPC 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩?

// Path to the persisted auto-start preference file.
const AUTOSTART_FILE = path.join(DATA_DIR, "autostart.json");

/**
 * Reads the persisted auto-start preference.
 * @returns True when the app should launch at login.
 */
function readAutoStartFile(): boolean {
  try {
    if (fs.existsSync(AUTOSTART_FILE)) {
      const data = JSON.parse(fs.readFileSync(AUTOSTART_FILE, "utf-8"));
      return data.openAtLogin === true;
    }
  } catch { /* ignore */ }
  return false;
}

/**
 * Persists the auto-start preference to disk.
 * @param enabled - Whether to enable launch-at-login.
 */
function writeAutoStartFile(enabled: boolean): void {
  try {
    fs.mkdirSync(BROWSER_DIR, { recursive: true });
    fs.writeFileSync(AUTOSTART_FILE, JSON.stringify({ openAtLogin: enabled }), "utf-8");
  } catch (err) {
    console.error("Failed to write autostart.json:", err);
  }
}

// Returns the persisted auto-start setting, coercing the OS login item if needed.
ipcMain.handle("getAutoStart", () => {
  const fileSetting = readAutoStartFile();
  const loginItemSettings = app.getLoginItemSettings();
  if (fileSetting && !loginItemSettings.openAtLogin) {
    app.setLoginItemSettings({ openAtLogin: true });
  }
  return fileSetting;
});

// Sets the auto-start (launch-at-login) preference in the OS and on disk.
ipcMain.handle("setAutoStart", async (_event, enabled: boolean) => {
  try {
    app.setLoginItemSettings({ openAtLogin: enabled });
    writeAutoStartFile(enabled);
    return { success: true };
  } catch (err: any) {
    console.error("Failed to set auto-start:", err);
    return { success: false, error: err.message };
  }
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Tray popup IPC 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?

// Updates the tray locale from the renderer.
ipcMain.on("tray-locale", (_event, locale: string) => {
  currentTrayLocale = locale;
  updateTrayStatus(true);
  sendTrayDataToPopup();
});

// Updates the in-app browser Accept-Language header so multi-language
// websites serve content in the user's preferred language.
ipcMain.on("browser-language", (_event, locale: string) => {
  _browserAcceptLanguage = locale === "zh"
    ? "zh-CN,zh;q=0.9,en;q=0.5"
    : "en-US,en;q=0.9";
});

// Updates the tray theme (and popup background) from the renderer.
ipcMain.on("tray-theme", (_event, themePreference: string) => {
  currentTrayTheme = resolveTrayTheme(themePreference);
  // Sync native theme so DevTools follows the app's theme
  nativeTheme.themeSource = currentTrayTheme;
  // Update existing popup background color
  if (trayPopup && !trayPopup.isDestroyed()) {
    trayPopup.setBackgroundColor(
      currentTrayTheme === "dark"
        ? "#1a1a1a"
        : "#ffffff"
    );
  }
  sendTrayDataToPopup();
});

// Handles a tray popup action: open a session (switching to it) or quit.
ipcMain.on("tray-popup-action", (_event, payload: { action?: string; sessionId?: string }) => {
  closeTrayPopup();
  const { action, sessionId } = payload;
  if (action === "open" || action === "open_workspace") {
    if (mainWindow === null) {
      createWindow();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
    if (sessionId && mainWindow && !mainWindow.isDestroyed()) {
      // Wait for the renderer to finish loading before sending the switch.
      // This avoids the race where switch-session arrives before the
      // renderer's IPC listeners are registered (esp. on a fresh window).
      const sendSwitch = () => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send(
            action === "open_workspace" ? "switch-workspace" : "switch-session",
            sessionId,
          );
        }
      };
      if (mainWindow.webContents.isLoading()) {
        mainWindow.webContents.once("did-finish-load", () => {
          // give the renderer a tick to register listeners + WS connect
          setTimeout(sendSwitch, 200);
        });
      } else {
        setTimeout(sendSwitch, 100);
      }
    }
  } else if (action === "settings") {
    if (mainWindow === null) {
      createWindow();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
    if (mainWindow && !mainWindow.isDestroyed()) {
      const openSettings = () => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send("open-settings-panel");
        }
      };
      if (mainWindow.webContents.isLoading()) {
        mainWindow.webContents.once("did-finish-load", () => setTimeout(openSettings, 200));
      } else {
        setTimeout(openSettings, 100);
      }
    }
  } else if (action === "quit") {
    isQuitting = true;
    app.quit();
  }
});

// Receives the full session list from the renderer for the tray popup.
ipcMain.on("tray-sessions-update", (_event, sessions: any[]) => {
  traySessionsCache = sessions;
  sendTrayDataToPopup();
});

// Receives the split normal/iwork session lists for the tray popup.
ipcMain.on("tray-sessions-both", (_event, payload: { normal: any[]; iwork: any[]; workspaces?: any[] }) => {
  traySessionsBothCache = {
    normal: payload.normal || [],
    iwork: payload.iwork || [],
    workspaces: payload.workspaces || [],
  };
  sendTrayDataToPopup();
});

// Updates the active session mode ("normal"/"iwork") for the tray popup.
ipcMain.on("tray-mode", (_event, mode: string) => {
  currentTrayMode = mode === "iwork" ? "iwork" : "normal";
  sendTrayDataToPopup();
});

// Keep tray theme in sync when OS-level dark mode changes
nativeTheme.on("updated", () => {
  // We don't know the preference here ("system"/explicit), so read from
  // localStorage of the main window if possible …otherwise just resolve
  // with whatever currentTrayTheme already is.
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.executeJavaScript(
      `localStorage.getItem("encre-theme") || "system"`
    ).then((pref: string) => {
      const resolved = resolveTrayTheme(pref);
      if (resolved !== currentTrayTheme) {
        currentTrayTheme = resolved;
        if (trayPopup && !trayPopup.isDestroyed()) {
          trayPopup.setBackgroundColor(
            currentTrayTheme === "dark" ? "#1a1a1a" : "#ffffff"
          );
        }
        sendTrayDataToPopup();
      }
    }).catch(() => {});
  }
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Terminal IPC 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?

// Spawns a new pseudo-terminal (pty) session for the in-app terminal.
ipcMain.handle("terminal:spawn", async (_event, shell?: string, shellArgs?: string[]) => {
  const pty = getNodePty();
  if (!pty) return { error: "node-pty not available" };
  try {
    const sh = shell || (
      process.platform === "win32"
        ? process.env.COMSPEC || "cmd.exe"
        : process.env.SHELL || "bash"
    );
    const args = shellArgs || [];
    const id = ++terminalSeq;
    const term = pty.spawn(sh, args, {
      name: "xterm-256color",
      cols: 80,
      rows: 24,
      cwd: process.env.HOME || process.env.USERPROFILE || ".",
      env: process.env as Record<string, string>,
    });
    terminals.set(id, { pty: term, terminalId: id });
    term.onData((data: string) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("terminal:data", { id, data });
      }
    });
    term.onExit(() => {
      terminals.delete(id);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("terminal:exit", { id });
      }
    });
    return { id };
  } catch (e: any) {
    return { error: e.message || "Failed to spawn terminal" };
  }
});

// Enumerates available shells (Windows: cmd/PowerShell/Git Bash/WSL; else POSIX).
ipcMain.handle("terminal:listShells", async () => {
  const shells: Array<{ name: string; path: string; args?: string[] }> = [];
  if (process.platform === "win32") {
    const comspec = process.env.COMSPEC || "C:\\Windows\\System32\\cmd.exe";
    shells.push({ name: "Command Prompt", path: comspec });

    const psPaths = [
      "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
      "C:\\Windows\\System32\\powershell.exe",
    ];
    for (const p of psPaths) {
      try { if (fs.existsSync(p)) { shells.push({ name: "Windows PowerShell", path: p }); break; } } catch {}
    }

    for (const pwsh of ["pwsh", "pwsh.exe"]) {
      try {
        const found = execSync(`where ${pwsh} 2>nul`, { encoding: "utf-8" }).trim().split("\n")[0];
        if (found) {
          shells.push({ name: "PowerShell Core", path: found.trim() });
          break;
        }
      } catch {}
    }

    for (const bash of ["bash", "bash.exe"]) {
      try {
        const found = execSync(`where ${bash} 2>nul`, { encoding: "utf-8" }).trim().split("\n")[0];
        if (found) {
          shells.push({ name: "Git Bash", path: found.trim(), args: ["--login"] });
          break;
        }
      } catch {}
    }

    try {
      const out = execSync("wsl --list 2>nul || wsl -l 2>nul", { encoding: "utf-8" });
      if (out.trim()) {
        shells.push({ name: "WSL", path: "wsl.exe" });
      }
    } catch {}
  } else {
    try { if (fs.existsSync("/bin/bash")) shells.push({ name: "Bash", path: "/bin/bash" }); } catch {}
    try { if (fs.existsSync("/bin/zsh")) shells.push({ name: "Zsh", path: "/bin/zsh" }); } catch {}
    try { if (fs.existsSync("/bin/fish")) shells.push({ name: "Fish", path: "/bin/fish" }); } catch {}
    try { if (fs.existsSync("/bin/sh")) shells.push({ name: "sh", path: "/bin/sh" }); } catch {}
  }
  return shells;
});

// Writes input data to a running terminal session.
ipcMain.handle("terminal:write", (_event, id: number, data: string) => {
  const t = terminals.get(id);
  if (t) t.pty.write(data);
});

// Resizes a running terminal session to the given columns/rows.
ipcMain.handle("terminal:resize", (_event, id: number, cols: number, rows: number) => {
  const t = terminals.get(id);
  if (t && cols > 0 && rows > 0) t.pty.resize(cols, rows);
});

// Kills a running terminal session and removes it from the registry.
ipcMain.handle("terminal:kill", (_event, id: number) => {
  const t = terminals.get(id);
  if (t) { t.pty.kill(); terminals.delete(id); }
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Files IPC 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎?

// Lists directory entries (directories first, then alphabetical).
ipcMain.handle("listDirectory", async (_event, dirPath: string) => {
  try {
    const entries = fs.readdirSync(dirPath, { withFileTypes: true });
    return entries.map((d) => ({
      name: d.name,
      isDirectory: d.isDirectory(),
      isFile: d.isFile(),
    })).sort((a, b) => {
      if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  } catch {
    return [];
  }
});

// Returns metadata for a single directory path (or null if not a directory).
ipcMain.handle("readDirectory", async (_event, dirPath: string) => {
  try {
    const stats = fs.statSync(dirPath);
    if (!stats.isDirectory()) return null;
    return { path: dirPath, name: path.basename(dirPath) };
  } catch {
    return null;
  }
});

// Returns the list of available drives/roots (Windows letters, else "/").
ipcMain.handle("getDrives", async () => {
  if (process.platform === "win32") {
    try {
      const out = execSync("wmic logicaldisk get name", { encoding: "utf-8" });
      return out.split("\n").map((s: string) => s.trim()).filter((s: string) => /^[A-Z]:$/.test(s));
    } catch { return ["C:"]; }
  }
  return ["/"];
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Git IPC (proxied to Python backend) 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶?

ipcMain.handle("gitStatus", async (_event, repoPath: string) => {
  const now = Date.now();
  const cached = GIT_STATUS_CACHE.get(repoPath);
  if (cached && now - cached.ts < GIT_CACHE_TTL_MS) {
    return cached.result;
  }
  try {
    const res = await fetch(`http://localhost:${WS_PORT}/git/status?workspace=${encodeURIComponent(repoPath)}`);
    const data: any = await res.json();
    const result = data.error ? { error: data.error } : { output: data.output || "" };
    GIT_STATUS_CACHE.set(repoPath, { ts: Date.now(), result });
    return result;
  } catch (e) {
    return { error: (e as Error).message };
  }
});

ipcMain.handle("gitDiff", async (_event, repoPath: string, filePath?: string) => {
  const cacheKey = `${repoPath}::${filePath || "__summary__"}`;
  const now = Date.now();
  const cached = GIT_DIFF_CACHE.get(cacheKey);
  if (cached && now - cached.ts < GIT_CACHE_TTL_MS) {
    return cached.result;
  }
  try {
    const params = new URLSearchParams({ workspace: repoPath, filter: "all" });
    if (filePath) params.set("file", filePath);
    const res = await fetch(`http://localhost:${WS_PORT}/git/diff?${params}`);
    const data: any = await res.json();
    const result = data.error ? { error: data.error } : { output: data.output || "" };
    GIT_DIFF_CACHE.set(cacheKey, { ts: Date.now(), result });
    return result;
  } catch (e) {
    return { error: (e as Error).message };
  }
});

ipcMain.handle("gitDiffEx", async (_event, repoPath: string, filter: string, filePath?: string) => {
  const cacheKey = `${repoPath}::${filter}::${filePath || "__summary__"}`;
  const now = Date.now();
  const cached = GIT_DIFF_CACHE.get(cacheKey);
  if (cached && now - cached.ts < GIT_CACHE_TTL_MS) {
    return cached.result;
  }
  try {
    const params = new URLSearchParams({ workspace: repoPath, filter });
    if (filePath) params.set("file", filePath);
    const res = await fetch(`http://localhost:${WS_PORT}/git/diff?${params}`);
    const data: any = await res.json();
    const result = data.error ? { error: data.error } : { output: data.output || "" };
    GIT_DIFF_CACHE.set(cacheKey, { ts: Date.now(), result });
    return result;
  } catch (e) {
    return { error: (e as Error).message };
  }
});

ipcMain.handle("gitCommit", async (_event, repoPath: string, message: string) => {
  try {
    const params = new URLSearchParams({ workspace: repoPath, message });
    const res = await fetch(`http://localhost:${WS_PORT}/git/commit?${params}`);
    const data: any = await res.json();
    return data.error ? { error: data.error } : { output: data.output || "" };
  } catch (e) {
    return { error: (e as Error).message };
  }
});

ipcMain.handle("gitPush", async (_event, repoPath: string) => {
  try {
    const params = new URLSearchParams({ workspace: repoPath });
    const res = await fetch(`http://localhost:${WS_PORT}/git/push?${params}`);
    const data: any = await res.json();
    return data.error ? { error: data.error } : { output: data.output || "" };
  } catch (e) {
    return { error: (e as Error).message };
  }
});

ipcMain.handle("gitCreatePr", async (_event, repoPath: string) => {
  try {
    const params = new URLSearchParams({ workspace: repoPath });
    const res = await fetch(`http://localhost:${WS_PORT}/git/pr?${params}`);
    const data: any = await res.json();
    return {
      error: data.error || "",
      output: data.output || "",
      compare_url: data.compare_url || "",
    };
  } catch (e) {
    return { error: (e as Error).message, output: "", compare_url: "" };
  }
});

ipcMain.handle("gitBehind", async (_event, repoPath: string) => {
  try {
    const params = new URLSearchParams({ workspace: repoPath });
    const res = await fetch(`http://localhost:${WS_PORT}/git/behind?${params}`);
    const data: any = await res.json();
    return { behind: data.behind ?? -1, error: data.error || "" };
  } catch (e) {
    return { behind: -1, error: (e as Error).message };
  }
});

ipcMain.handle("gitPull", async (_event, repoPath: string) => {
  try {
    const params = new URLSearchParams({ workspace: repoPath });
    const res = await fetch(`http://localhost:${WS_PORT}/git/pull?${params}`);
    const data: any = await res.json();
    return data.error ? { error: data.error } : { output: data.output || "" };
  } catch (e) {
    return { error: (e as Error).message };
  }
});

// Win key capture flag …set by renderer when shortcuts panel is open
ipcMain.handle("setWinKeyCapture", (_event, enabled: boolean) => {
  winKeyCapture = enabled;
});

// Window controls
// Minimizes the window that sent the request.
ipcMain.handle("window-minimize", (event) => {
  BrowserWindow.fromWebContents(event.sender)?.minimize();
});

// Toggles maximize state of the requesting window.
ipcMain.handle("window-maximize", (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (win?.isMaximized()) {
    win.unmaximize();
  } else {
    win?.maximize();
  }
});

// Closes the requesting window.
ipcMain.handle("window-close", (event) => {
  BrowserWindow.fromWebContents(event.sender)?.close();
});

// Reports whether the requesting window is currently maximized.
ipcMain.handle("window-is-maximized", (event) => {
  return BrowserWindow.fromWebContents(event.sender)?.isMaximized() ?? false;
});

// Toggles the dev tools for the requesting window.
ipcMain.handle("toggle-devtools", (event) => {
  BrowserWindow.fromWebContents(event.sender)?.webContents.toggleDevTools();
});

// Opens a folder in the OS file explorer.
ipcMain.handle("openFolder", async (_event, folderPath: string) => {
  if (typeof folderPath !== "string") return false;
  try {
    await shell.openPath(folderPath);
    return true;
  } catch {
    return false;
  }
});

// Opens an external URL in the OS browser (http/https/mailto only).
ipcMain.handle("open-external", async (_event, url: string) => {
  // Only allow http/https URLs for security
  if (typeof url !== "string") return false;
  let parsed: URL;
  try {
    parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:" && parsed.protocol !== "mailto:") return false;
  } catch {
    return false;
  }
  try {
    await shell.openExternal(url);
    return true;
  } catch {}
  // Fallback: if shell.openExternal fails (e.g. on some Windows configs),
  // try the platform-native open command
  try {
    const { exec } = require("child_process");
    const cmd = process.platform === "win32"
      ? `start "" "${url}"`
      : process.platform === "darwin"
        ? `open "${url}"`
        : `xdg-open "${url}"`;
    exec(cmd);
    return true;
  } catch {
    return false;
  }
});

// Writes HTML to a temp file and returns the file:// URL (renderer opens in internal browser).
ipcMain.handle("openInfoHtml", async (_event, html: string) => {
  try {
    const tmpDir = path.join(os.tmpdir(), "encre-info");
    const fileName = `info-${Date.now()}.html`;
    const filePath = path.join(tmpDir, fileName);
    fs.mkdirSync(tmpDir, { recursive: true });
    fs.writeFileSync(filePath, html, "utf-8");
    // Convert to file:// URL (Windows: file:///C:/...)
    return "file://" + (process.platform === "win32" ? "/" + filePath.replace(/\\/g, "/") : filePath);
  } catch {
    return null;
  }
});

// Returns desktop (npm) and agent (pyproject) version strings.
ipcMain.handle("getAppVersions", () => {
  const rootDir = path.resolve(__dirname, "..", "..");
  const pkgPath = path.join(__dirname, "..", "package.json");
  const pyprojectPath = path.join(rootDir, "pyproject.toml");
  const desktopVersion = JSON.parse(fs.readFileSync(pkgPath, "utf-8")).version || "0.0.0";
  let agentVersion = "0.0.0";
  try {
    const pyContent = fs.readFileSync(pyprojectPath, "utf-8");
    const match = pyContent.match(/^version\s*=\s*"([^"]+)"/m);
    if (match) agentVersion = match[1];
  } catch {}
  return { desktop: desktopVersion, agent: agentVersion };
});

// Directory that holds license/legal docs.  In dev this is the repo root;
// in a packaged build the docs are bundled via extraResources under the
// Electron resources directory.
function docsRepoRoot(): string {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..", "..");
}

// Returns the license text from the repository docs.
ipcMain.handle("getLicenseContent", async () => {
  const rootDir = docsRepoRoot();
  const candidates = ["docs/LICENSE", "docs/LICENSE.txt", "docs/LICENSE.md", "LICENSE", "LICENSE.txt", "LICENSE.md"];
  for (const name of candidates) {
    const p = path.join(rootDir, name);
    if (fs.existsSync(p)) {
      return fs.readFileSync(p, "utf-8");
    }
  }
  return "License file not found.";
});

const LEGAL_DOC_FILES: Record<string, string> = {
  privacy: "PRIVACY.md",
  terms: "TERMS.md",
  agreement: "USER_AGREEMENT.md",
  "content-rules": "CONTENT_GUIDELINES.md",
  minors: "MINORS_PRIVACY.md",
  "data-rules": "DATA_PROCESSING.md",
};

const LEGAL_REGIONS: Record<string, string> = {
  // Americas
  us: "docs/legal/americas/us",
  ca: "docs/legal/americas/ca",
  mx: "docs/legal/americas/mx",
  br: "docs/legal/americas/br",
  // Europe (eu = unified set for EU member states + EEA)
  eu: "docs/legal/europe/eu",
  uk: "docs/legal/europe/uk",
  ch: "docs/legal/europe/ch",
  tr: "docs/legal/europe/tr",
  // Asia
  cn: "docs/legal/asia/cn",
  jp: "docs/legal/asia/jp",
  kr: "docs/legal/asia/kr",
  tw: "docs/legal/asia/tw",
  hk: "docs/legal/asia/hk",
  mo: "docs/legal/asia/mo",
  sg: "docs/legal/asia/sg",
  in: "docs/legal/asia/in",
  ae: "docs/legal/asia/ae",
  sa: "docs/legal/asia/sa",
  il: "docs/legal/asia/il",
  // Africa
  za: "docs/legal/africa/za",
  ng: "docs/legal/africa/ng",
  ke: "docs/legal/africa/ke",
  eg: "docs/legal/africa/eg",
  // Oceania
  au: "docs/legal/oceania/au",
  nz: "docs/legal/oceania/nz",
};

// Maps an ISO 3166-1 alpha-2 country code (as returned by
// `app.getLocaleCountryCode()`) to the matching legal region folder key in
// `LEGAL_REGIONS`. Countries without a dedicated jurisdiction fall back to
// their regional grouping (EU member states -> "eu"), and any unknown code
// falls back to "us". This makes the default legal jurisdiction track the
// real OS-reported country instead of being guessed from the UI language.
const COUNTRY_TO_LEGAL_REGION: Record<string, string> = {
  // Americas
  US: "us", CA: "ca", MX: "mx", BR: "br",
  AG: "us", AR: "us", BB: "us", BO: "us", CL: "us", CO: "us", CR: "us",
  CU: "us", DO: "us", EC: "us", GT: "us", HN: "us", JM: "us", NI: "us",
  PA: "us", PE: "us", PY: "us", SV: "us", TT: "us", UY: "us", VE: "us",
  // Europe (non-EU dedicated jurisdictions)
  GB: "uk", UK: "uk", CH: "ch", TR: "tr",
  // Asia
  CN: "cn", JP: "jp", KR: "kr", HK: "hk", MO: "mo", SG: "sg", IN: "in",
  AE: "ae", SA: "sa", IL: "il",
  // Africa
  ZA: "za", NG: "ng", KE: "ke", EG: "eg",
  // Oceania
  AU: "au", NZ: "nz",
  // Taiwan is exposed by some systems as "TW"
  TW: "tw",
};

// EU member states (+ EEA) whose legal set is covered by the unified "eu"
// folder. Anything in this list maps to "eu"; excluded from stay dedicated.
const EU_COUNTRY_CODES: ReadonlySet<string> = new Set([
  "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
  "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
  "SI", "ES", "SE", "IS", "LI", "NO",
]);

/** Returns the legal region key matching the OS-reported country, or "us". */
function systemLegalRegion(): string {
  let cc = "";
  try {
    cc = (app.getLocaleCountryCode() || "").toUpperCase();
  } catch {
    cc = "";
  }
  if (!cc) {
    // Fall back to the system locale tag (e.g. "en-US", "zh-CN") and take
    // the region subtag, then try the language subtag as a weak fallback.
    let tag = "";
    try {
      tag = app.getSystemLocale() || "";
    } catch {
      tag = "";
    }
    const parts = tag.toUpperCase().split(/[-_]/);
    if (parts.length >= 2) cc = parts[1] || "";
    if (!cc && parts.length >= 1) cc = parts[0] || "";
  }
  if (COUNTRY_TO_LEGAL_REGION[cc]) return COUNTRY_TO_LEGAL_REGION[cc];
  if (EU_COUNTRY_CODES.has(cc)) return "eu";
  return "us";
}

function getDocRegionFilePath(): string {
  return path.join(app.getPath("userData"), "doc-region.json");
}

function readDocRegion(): string {
  try {
    const raw = fs.readFileSync(getDocRegionFilePath(), "utf-8");
    const parsed = JSON.parse(raw);
    const region = typeof parsed === "string" ? parsed : parsed.region;
    if (typeof region === "string" && LEGAL_REGIONS[region]) return region;
  } catch {}
  return systemLegalRegion();
}

function writeDocRegion(region: string): void {
  if (!LEGAL_REGIONS[region]) return;
  try {
    fs.writeFileSync(getDocRegionFilePath(), JSON.stringify({ region }), "utf-8");
  } catch {}
}

// Returns the text of a policy/legal document for a jurisdiction.
ipcMain.handle("getDocumentContent", async (_event, docId: string, region: string = "us") => {
  const rootDir = docsRepoRoot();

  if (docId === "thanks") {
    const variants: Record<string, string> = {
      default: "docs/THANKS.md",
      cn: "docs/THANKS_CN.md",
    };
    const fileName = region === "cn" ? variants.cn : variants.default;
    const p = path.join(rootDir, fileName);
    try {
      return fs.readFileSync(p, "utf-8");
    } catch {
      return `Document "${fileName}" not found.`;
    }
  }

  const fileName = LEGAL_DOC_FILES[docId];
  if (!fileName) return `Document "${docId}" not found.`;

  const candidates: string[] = [];
  if (region && LEGAL_REGIONS[region]) {
    candidates.push(path.join(rootDir, LEGAL_REGIONS[region], fileName));
  }
  // Fall back to the OS-reported country jurisdiction so the document always
  // matches the user's real region rather than being inferred from the UI
  // language (which previously produced mismatched language/country combos).
  const systemRegion = systemLegalRegion();
  if (systemRegion && !candidates.some((c) => c.includes(LEGAL_REGIONS[systemRegion]))) {
    candidates.push(path.join(rootDir, LEGAL_REGIONS[systemRegion], fileName));
  }

  for (const p of candidates) {
    try {
      return fs.readFileSync(p, "utf-8");
    } catch {}
  }
  return `Document "${fileName}" not found.`;
});

ipcMain.handle("getDocRegion", async () => readDocRegion());

ipcMain.handle("setDocRegion", async (_event, region: string) => {
  if (typeof region === "string" && LEGAL_REGIONS[region]) {
    writeDocRegion(region);
  }
});

// Opens (or reuses and tabs into) a child window for a given view/label.
ipcMain.handle("openChildWindow", (_event, view: string, label: string) => {
  // Find existing child window, or create one
  let child: BrowserWindow | null = null;
  for (const w of childWindows) {
    if (!w.isDestroyed()) { child = w; break; }
  }
  if (!child) {
    child = new BrowserWindow({
      width: 960,
      height: 700,
      minWidth: 600,
      minHeight: 400,
      frame: false,
      backgroundColor: "#0f0f0f",
      title: "ESD",
      icon: resolveAppIconPath(),
      show: false,
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
        webviewTag: true,
      },
    });
    childWindows.add(child);
    child.on("closed", () => childWindows.delete(child!));
    child.loadFile(path.join(__dirname, "..", "renderer", "index.html"), {
      query: { child: view, label },
    });
    // Flush tabs that were queued while this window was still loading
    // (see childPendingTabs below).
    child.webContents.on("did-finish-load", () => {
      const pending = childPendingTabs.get(child!) || [];
      childPendingTabs.delete(child!);
      for (const [v, l] of pending) {
        if (!child!.isDestroyed()) {
          child!.webContents.send("child-window:add-tab", v, l);
        }
      }
    });
    // Force-reset and lock zoom to 100% to avoid accidental Ctrl+-/Ctrl+wheel shrink.
    child.webContents.setZoomFactor(1);
    child.webContents.setVisualZoomLevelLimits(1, 1).catch(() => {});
    child.webContents.on("did-finish-load", () => {
      child?.webContents.setZoomFactor(1);
    });
    child.webContents.on("before-input-event", (event, input) => {
      const key = (input.key || "").toLowerCase();
      const isZoomHotkey =
        input.control &&
        (key === "-" || key === "_" || key === "+" || key === "=" || key === "0");
      if (isZoomHotkey) {
        event.preventDefault();
      }
    });
    const createdChild = child;
    child.webContents.openDevTools();
    child.once("ready-to-show", () => createdChild.show());
  } else {
    // If the page is still loading (first paint not done), the preload has
    // not registered the add-tab listener yet — queue the tab and flush it
    // after did-finish-load instead of dropping it silently.
    if (child.webContents.isLoading() || !child.webContents.getURL()) {
      const pending = childPendingTabs.get(child) || [];
      pending.push([view, label]);
      childPendingTabs.set(child, pending);
    } else {
      child.webContents.send("child-window:add-tab", view, label);
    }
    child.focus();
  }
});

// Forward arbitrary events from main renderer to all child windows
// (used for streaming to the ESD child window).
// Forwards an arbitrary event from the main renderer to all child windows.
ipcMain.on("forward-to-child", (_event, channel: string, ...args: any[]) => {
  for (const child of childWindows) {
    if (!child.isDestroyed()) {
      child.webContents.send(channel, ...args);
    }
  }
});

ipcMain.handle("open-settings", (_event, panel: string) => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("open-settings-panel", panel);
    mainWindow.show();
    mainWindow.focus();
  }
});

/* 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?CDP WebSocket Relay for BrowserView 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?*/
const cdpRelays = new Map<number, { wsc: WebSocket.Server; clients: Set<WebSocket.WebSocket>; debuggerAttached: boolean }>();

function startCdpRelay(webContentsId: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const wsc = new WebSocket.Server({ port: 0, host: "127.0.0.1" });
    const clients = new Set<WebSocket.WebSocket>();
    let debuggerAttached = false;

    wsc.on("listening", () => {
      const addr = wsc.address();
      if (!addr) {
        reject(new Error("WebSocket server address is null"));
        return;
      }
      const port = (addr as any).port;
      cdpRelays.set(webContentsId, { wsc, clients, debuggerAttached });

      const wc = webContents.fromId(webContentsId);
      if (wc && !wc.isDestroyed()) {
        try {
          wc.debugger.attach("1.3");
          const relay = cdpRelays.get(webContentsId);
          if (relay) relay.debuggerAttached = true;
          wc.debugger.on("message", (_event: any, method: string, params: any) => {
            for (const c of clients) {
              if (c.readyState === WebSocket.OPEN) c.send(JSON.stringify({ method, params }));
            }
          });
        } catch (e) { console.error("[cdp-relay] Failed to attach debugger:", e); }
      }
      resolve(port);
    });

    wsc.on("error", (err) => {
      reject(err);
    });

    wsc.on("connection", (ws) => {
      clients.add(ws);
      const wc = webContents.fromId(webContentsId);
      ws.on("message", (data) => {
        if (!wc || wc.isDestroyed()) return;
        try {
          const msg = JSON.parse(data.toString());
          wc.debugger.sendCommand(msg.method, msg.params || {})
            .then((result: any) => {
              if (msg.id !== undefined) ws.send(JSON.stringify({ id: msg.id, result }));
            })
            .catch((err: any) => {
              if (msg.id !== undefined) ws.send(JSON.stringify({ id: msg.id, error: { code: -32000, message: err.message || String(err) } }));
            });
        } catch {}
      });
      ws.on("close", () => clients.delete(ws));
    });
  });
}

function stopCdpRelay(webContentsId: number): void {
  const relay = cdpRelays.get(webContentsId);
  if (!relay) return;
  for (const c of relay.clients) { try { c.close(); } catch {} }
  relay.clients.clear();
  try { relay.wsc.close(); } catch {}
  const wc = webContents.fromId(webContentsId);
  if (wc && !wc.isDestroyed() && relay.debuggerAttached) { try { wc.debugger.detach(); } catch {} }
  cdpRelays.delete(webContentsId);
}

ipcMain.handle("browser:get-cdp-port", async (_event, webContentsId: number): Promise<number> => {
  const existing = cdpRelays.get(webContentsId);
  if (existing) {
    const addr = existing.wsc.address();
    if (addr) return (addr as any).port;
  }
  return await startCdpRelay(webContentsId);
});

ipcMain.handle("browser:register-cdp-webview", async (_event, webContentsId: number): Promise<number> => startCdpRelay(webContentsId));
ipcMain.handle("browser:unregister-cdp-webview", (_event, webContentsId: number): void => stopCdpRelay(webContentsId));

app.on("web-contents-created", (_event, wc) => {
  wc.on("destroyed", () => { if (cdpRelays.has(wc.id)) stopCdpRelay(wc.id); });
  if (wc.getType() === "window") {
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
  }
  if (wc.getType() === "webview") {
    wc.setWindowOpenHandler((details) => {
      let url = details.url;
      if (url && !/^https?:\/\//i.test(url)) {
        try { url = new URL(url, wc.getURL()).href; } catch {}
      }
      if (url && /^https?:\/\//i.test(url)) {
        const hostWc = wc.hostWebContents;
        if (hostWc) hostWc.send("browser:new-window", url, wc.id);
      }
      return { action: "deny" };
    });
  }
});

// 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?App lifecycle 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾?

/**
 * Pings the backend `/health` endpoint to verify it is responsive.
 * @returns True when the service answers with an OK status.
 */
async function healthCheck(): Promise<boolean> {
  try {
    const response = await fetch(`http://localhost:${WS_PORT}/health`);
    return response.ok;
  } catch {
    return false;
  }
}

// --- Single-instance lock -----------------------------------------------
// Prevents multiple app instances from fighting over port 7110 / the backend
// (which produced a stuck splash: the old instance owned the WebSocket while
// the new window could never establish its own connection). The lock must be
// requested before whenReady(). If another instance is already running we quit
// immediately and ask it to bring its main window to the foreground instead.
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
}

// Application entry: runs once Electron is ready.
app.whenReady().then(async () => {
  // Force the app's theme to dark so native browser widgets (e.g. the
  // <input type="date"> calendar popup) render with dark colors regardless
  // of the OS-level light/dark setting.
   nativeTheme.themeSource = "light";

  // Pin the AppUserModelID so Windows taskbar/notification icons attach to Encre
  // (and not the generic electron.exe icon).
  app.setAppUserModelId("com.encre.desktop");

  // Register local:// protocol to serve local files (for notification media, etc.)
  protocol.handle("local", (request) => {
    const rawPath = decodeURIComponent(request.url.slice("local://".length)).replace(/^\//, "");
    const filePath = path.resolve(rawPath);

    try {
      const data = fs.readFileSync(filePath);

      const ext = path.extname(filePath).toLowerCase();
      const mime: Record<string, string> = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
        ".mp4": "video/mp4", ".webm": "video/webm", ".ogg": "video/ogg",
      };
      const contentType = mime[ext] || "application/octet-stream";

      return new Response(data, {
        status: 200,
        headers: {
          "Content-Type": contentType,
          "Content-Length": String(data.length),
        },
      });
    } catch {
      return new Response("File not found", { status: 404 });
    }
  });

  // Set up encrypted browser cookie store
  setupBrowserSession();

  // Create the main window directly — no native splash window. The renderer's
  // in-app splash screen covers the loading phase, so the main window is
  // shown as soon as it can paint (ready-to-show in createWindow). The
  // Python backend boots in parallel further down instead of blocking it.
  createWindow();
  if (!process.env.ENCRE_NOTRAY) {
    createTray();
  }

  // Async background boot: free the port, then start or attach to the
  // Python service. The renderer's WebSocket connect() retries until the
  // backend is reachable, so the window never waits on this.
  void (async () => {
    if (process.env.ENCRE_NOBACKEND) return;
    // Fast path first: if the service from a previous run is still alive and
    // healthy, attach instead of restarting. This skips the slow port-free
    // step entirely on the common warm-start case.
    const existingPid = readPidFile();
    if (existingPid !== null && isProcessRunning(existingPid)) {
      const healthy = await healthCheck();
      if (healthy) {
        console.log(`Background service already running (PID ${existingPid}), connecting`);
        updateTrayStatus(true);
        return;
      }
      // PID is stale/zombie …kill it and restart fresh
      console.log(`Server PID ${existingPid} is unresponsive, restarting`);
      killServiceByPid(existingPid);
      await new Promise(r => setTimeout(r, 1500));
    }

    // Force-free the port before boot (kills orphaned processes from dead terminals)
    killProcessOnPort(WS_PORT);

    try {
      await startPythonServer();
      serverStartError = null;
      console.log(`Background service started on port ${WS_PORT}`);
    } catch (err) {
      console.error("Failed to start background service:", err);
      serverStartError = String(err);
      updateTrayStatus(false);
    }
  })();

  // Apply persisted auto-start setting on each launch
  const autoStart = readAutoStartFile();
  if (autoStart) {
    app.setLoginItemSettings({ openAtLogin: true });
  }
});

// Keep the app alive (tray) when all windows close; do not quit.
app.on("window-all-closed", () => {
  // Do NOT quit …service continues running in background.
  // The system tray keeps the app alive on Windows/Linux.
  // On macOS, window hiding is the default behavior.
  if (process.platform === "darwin") {
    // macOS: standard behavior …app stays alive without windows
  } else {
    // Windows/Linux: window reference cleared, tray keeps app alive
    mainWindow = null;
  }
});

// macOS dock re-activation: recreate/show the main window.
app.on("activate", () => {
  if (mainWindow === null) {
    createWindow();
  } else {
    mainWindow.show();
  }
});

// Tear down everything (terminals, backend, port) on quit.
function cleanupOnQuit(): void {
  console.log("[app] before-quit …cleaning up all child processes");
  // Kill all terminal sessions
  for (const [, t] of terminals) {
    try { t.pty.kill(); } catch {}
  }
  terminals.clear();

  // 1. Kill by PID file
  const pid = readPidFile();
  if (pid !== null) {
    killServiceByPid(pid);
  }

  // 2. Direct kill the serverProcess reference
  if (serverProcess) {
    if (serverProcess.pid) killServiceByPid(serverProcess.pid);
    try { serverProcess.kill("SIGKILL"); } catch {}
    try { serverProcess.kill(); } catch {}
    serverProcess = null;
  }

  // 3. Kill any Python process running encre (works for both bundled exe and dev mode)
  killAllEncreProcesses();

  // 4. Force-free the port
  killProcessOnPort(WS_PORT);

  // 5. Delete PID file
  try { fs.unlinkSync(PID_FILE); } catch {}
}
app.on("before-quit", cleanupOnQuit);

// Last-resort cleanup: kill any stray encre-related processes.
process.on("exit", () => {
  if (process.platform === "win32") {
    try { execSync(`taskkill /F /IM "encre-server.exe" 2>nul`, { stdio: "ignore", timeout: 3000 }); } catch {}
    try { execSync(`taskkill /F /FI "PID ne ${process.pid}" /IM "Encre.exe" 2>nul`, { stdio: "ignore", timeout: 3000 }); } catch {}
  } else {
    try { execSync(`pkill -f "encre-server" 2>/dev/null`, { stdio: "ignore", timeout: 3000 }); } catch {}
    try { execSync(`pkill -f "Encre" 2>/dev/null`, { stdio: "ignore", timeout: 3000 }); } catch {}
  }
});


