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
 * Encre Agent Desktop application entry point (Electron main process).
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

import { app, BrowserWindow, ipcMain, shell, dialog, Tray, nativeImage, nativeTheme, session, protocol, net, webContents, clipboard, screen, powerSaveBlocker } from "electron";
import { ChildProcess, spawn, execSync, exec, execFile } from "child_process";
import { promisify } from "util";
import * as path from "path";
import * as fs from "fs";
import * as crypto from "crypto";
import { pathToFileURL, fileURLToPath } from "url";
import * as os from "os";
import * as WebSocket from "ws";
import * as http from "http";
import { Readable } from "node:stream";
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
// Set of auxiliary "child" BrowserWindows (e.g. the Encre Agent Desktop Subwindow / ESD window).
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

// Opt out of Windows' native window-occlusion detection.
//
// The window is created hidden (`show:false`) and close-to-tray hides it again,
// so for long stretches it owns no visible window. Windows' occlusion detector
// reports such a window to Chromium as occluded; Chromium then lowers the whole
// app's scheduling priority and Windows follows by putting the process into
// Efficiency Mode. Timers and input handling are then serviced seconds late:
// the boot window never paints until something forces it on screen, and a tray
// click sits unanswered for ten-plus seconds before the window appears. The app
// is a resident tray client that must come back instantly, so it opts out of
// that classification and keeps running normally while hidden.
app.commandLine.appendSwitch("disable-features", "CalculateNativeWinOcclusion");
// Tray-resident restore latency: `backgroundThrottling:false` in webPreferences
// only stops per-contents timer throttling. Chromium still (a) drops the
// renderer process to background CPU priority when every window is hidden and
// (b) backgrounds the renderer for occluded/hidden windows at the process
// level. A backgrounded renderer then re-rasters the heavy shell seconds late
// when the tray reveals the window. Disable those two process-level paths.
app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("disable-backgrounding-occluded-windows");
// NOTE: do NOT add "disable-direct-composition" here. It was added while a
// third-party AppInit hook DLL (PGPmapih.dll, Symantec Encryption Desktop)
// was burning a core and freezing paints for ~20s at startup -- the stale
// fragment artifacts were blamed on DirectComposition's oversized surface.
// With the DLL out of the picture the legacy present path is strictly worse:
// its swapchain desyncs from the window during interactive resizes (the
// renderer's viewport stays at the old size, so flex layouts overflow past
// the real window edge) and it reintroduces exactly the artifacts it claimed
// to fix. DirectComposition is the modern Windows present path and handles
// live resize damage correctly.
// Cap the HTTP disk cache. The in-app browser partition's cache grew to
// 330MB / 1766 files, and rebuilding the Simple Cache index over that at
// startup burned ~3 CPU cores for ~19s while freezing the UI thread for
// ~14s (watchdog + CPU-sample verified). The cap keeps the entry count (and
// therefore the startup index cost) bounded for every session after this.
app.commandLine.appendSwitch("disk-cache-size", "134217728");

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

// Desktop-Chrome identity for every in-app browser request and page. Electron's
// stock UA advertises `Electron/x` plus the app name, and plenty of sites (MSN,
// 微博, 一点资讯 …) sniff that as an unknown/handheld browser and answer with
// their mobile H5 build instead of the desktop page. Those H5 builds size
// everything from a phone-width design (`html { font-size: 13.33vw }` rem
// schemes), so they swell to several times the intended size in a
// desktop-width viewport — while Bing, which serves one conventional desktop
// layout regardless of UA, keeps looking right. The bundled Chromium version is
// reused verbatim so version sniffing stays truthful.
const BROWSER_CHROME_VERSION = process.versions.chrome || "120.0.0.0";
const BROWSER_CHROME_MAJOR = BROWSER_CHROME_VERSION.split(".")[0];
const BROWSER_IS_WIN = process.platform === "win32";
const BROWSER_IS_MAC = process.platform === "darwin";
const BROWSER_PLATFORM_TOKEN = BROWSER_IS_WIN
  ? "Windows NT 10.0; Win64; x64"
  : BROWSER_IS_MAC
    ? "Macintosh; Intel Mac OS X 10_15_7"
    : "X11; Linux x86_64";
const BROWSER_DESKTOP_UA =
  `Mozilla/5.0 (${BROWSER_PLATFORM_TOKEN}) AppleWebKit/537.36 (KHTML, like Gecko) ` +
  `Chrome/${BROWSER_CHROME_VERSION} Safari/537.36`;
/** `navigator.userAgentData` blob matching BROWSER_DESKTOP_UA (mobile: false). */
const BROWSER_DESKTOP_UADATA = {
  brands: [
    { brand: "Chromium", version: BROWSER_CHROME_MAJOR },
    { brand: "Google Chrome", version: BROWSER_CHROME_MAJOR },
    { brand: "Not?A_Brand", version: "24" },
  ],
  fullVersion: BROWSER_CHROME_VERSION,
  platform: BROWSER_IS_WIN ? "Windows" : BROWSER_IS_MAC ? "macOS" : "Linux",
  platformVersion: BROWSER_IS_WIN ? "10.0.0" : "",
  architecture: "x86",
  model: "",
  mobile: false,
};

/** Android Chrome identity for the "browse as phone" switch.  Both the
 *  session-level UA and the CDP override below have to agree on it, or
 *  `navigator.userAgent` and `navigator.userAgentData` contradict each other
 *  and sites sniff the mismatch straight back to the desktop build. */
const BROWSER_MOBILE_UA =
  "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) " +
  `Chrome/${BROWSER_CHROME_VERSION} Mobile Safari/537.36`;
const BROWSER_MOBILE_UADATA = {
  brands: BROWSER_DESKTOP_UADATA.brands,
  fullVersion: BROWSER_DESKTOP_UADATA.fullVersion,
  platform: "Android",
  platformVersion: "",
  architecture: "",
  model: "Pixel 7",
  mobile: true,
};

/** The one reading of the identity switch: UA string, client-hint blob and
 *  CDP platform token always move together. */
function browserIdentity(): { ua: string; uaData: typeof BROWSER_DESKTOP_UADATA; platform: string } {
  return _browserOpts.mobileIdentity
    ? { ua: BROWSER_MOBILE_UA, uaData: BROWSER_MOBILE_UADATA, platform: "Android" }
    : {
      ua: BROWSER_DESKTOP_UA,
      uaData: BROWSER_DESKTOP_UADATA,
      platform: BROWSER_IS_WIN ? "Win32" : BROWSER_IS_MAC ? "MacIntel" : "Linux x86_64",
    };
}

/**
 * Privileged scheme used to display local files Chromium refuses to render as
 * documents. On Windows a source/config file (e.g. `build.py`) has no
 * renderable MIME in the registry, so navigating a webview to
 * `file:///D:/…/build.py` is turned into a *download*, which aborts the
 * navigation and surfaces as `ERR_ABORTED (-3)`. The in-app browser rewrites
 * such paths to `encre-file://…`, served below as plain text. Registered
 * before app-ready, as `registerSchemesAsPrivileged` requires.
 */
const ENCRE_FILE_SCHEME = "encre-file";
// Plugin front-end assets (iframe HTML, scripts, icons) are served by the
// Python backend, but the renderer must not talk to it directly: its only
// backend channel is the encrypted WebSocket. This scheme is handled in the
// main process, which proxies to the loopback backend (an Electron-internal
// hop, same as the adminFetch IPC).
const PLUGIN_ASSET_SCHEME = "plugin-asset";
protocol.registerSchemesAsPrivileged([
  {
    scheme: ENCRE_FILE_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true },
  },
  {
    scheme: PLUGIN_ASSET_SCHEME,
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      stream: true,
      corsEnabled: true,
    },
  },
]);
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

/**
 * Persist a single credential captured by the in-app browser into the
 * encrypted passwords store. Entries are deduplicated by
 * (origin, username, kind): an existing match is replaced in place.
 */
function saveBrowserCredential(entry: { origin: string; username: string; password: string; kind: string; url?: string; created_at?: number }): void {
  try {
    const existing = loadBrowserPasswords();
    const list: any[] = existing ? JSON.parse(existing) : [];
    const idx = list.findIndex((p: any) =>
      p && p.origin === entry.origin && p.username === entry.username && (p.kind || "password") === entry.kind);
    if (idx >= 0) list.splice(idx, 1);
    list.push(entry);
    if (list.length > 5000) list.splice(0, list.length - 5000);
    saveBrowserPasswords(JSON.stringify(list));
  } catch (e) {
    console.error("[browser] failed to save credential:", e);
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

// Auto-save flag for credentials (passwords / API keys) captured by the
// in-app browser after a successful authentication. Toggled from the
// renderer via the "browser-autosave" IPC channel; when off, the
// "browser:save-credential" handler ignores all writes.
let _browserAutosaveCredentials = true;

// Everything the in-app browser does that a user can steer (Settings ->
// Assist -> Browser), pushed from the renderer over "browser-options" in the
// same shape as "browser-autosave".  Each field is read AT USE TIME by the
// handler that owns it -- request headers, the permission gate, the zoom lock,
// the download hook, the history cap -- so a change lands on the next
// navigation without rebuilding the session.  The defaults reproduce the
// behaviour this file had before the switches existed.
interface BrowserOpts {
  doNotTrack: boolean;
  denyPermissions: boolean;
  blockThirdPartyCookies: boolean;
  askBeforeDownload: boolean;
  downloadDir: string;
  forceLightPage: boolean;
  mobileIdentity: boolean;
  historyLimit: number;
  zoomFactor: number;
  blockPopups: boolean;
}
const _browserOpts: BrowserOpts = {
  doNotTrack: false,
  denyPermissions: false,
  blockThirdPartyCookies: false,
  askBeforeDownload: true,
  downloadDir: "",
  forceLightPage: true,
  mobileIdentity: false,
  historyLimit: 5000,
  zoomFactor: 0.75,
  blockPopups: false,
};
// Permissions a page can still get while the deny switch is on.  Everything
// outside this set is refused outright when it is enabled.
const _browserPromptablePermissions = new Set([
  "geolocation", "notifications", "media", "midi", "midiSysex",
  "clipboard-read", "display-capture",
]);

// The browser partition's storage init (Chromium opening every LevelDB under
// it and rebuilding the Simple Cache / Code Cache / Service Worker indexes)
// scales with the partition's file count -- 330MB / ~3000 files froze the
// main thread for ~20s at startup (watchdog). Startup therefore loads the
// session EAGERLY and IN PARALLEL: first a threadpool-side pre-clean shrinks
// the regenerable cache dirs (fs.promises -- never the main loop), then the
// partition is opened over a few MB instead of 330MB. Window, backend and
// tray startup all proceed simultaneously; nothing waits for this chain.
let _browserSessionReady = false;
let _browserSetupScheduled = false;

function ensureBrowserSession(): void {
  if (_browserSessionReady || _browserSetupScheduled) return;
  _browserSetupScheduled = true;
  void preCleanBrowserPartition().then(() => setupBrowserSession());
}

// Regenerable cache dirs Chromium rebuilds by itself. Cookies, site storage,
// bookmarks, history and the service-worker registration database are never
// touched, so logins survive.
const _BROWSER_CACHE_DIRS = [
  "Cache",
  "Code Cache",
  "GPUCache",
  "DawnCache",
  "DawnGraphiteCache",
  "DawnWebGPUCache",
  path.join("Service Worker", "CacheStorage"),
  path.join("Service Worker", "ScriptCache"),
];

// Runs on the libuv threadpool, so it never blocks the main loop even while
// walking hundreds of megabytes.
async function preCleanBrowserPartition(): Promise<void> {
  try {
    const partitionDir = path.join(app.getPath("userData"), "Partitions", "encre-browser");
    let total = 0;
    const walk = async (dir: string): Promise<void> => {
      let entries: fs.Dirent[];
      try { entries = await fs.promises.readdir(dir, { withFileTypes: true }); } catch { return; }
      for (const e of entries) {
        const p = path.join(dir, e.name);
        if (e.isDirectory()) await walk(p);
        else { try { total += (await fs.promises.stat(p)).size; } catch { /* raced with eviction */ } }
      }
    };
    await walk(partitionDir);
    if (total <= 150 * 1024 * 1024) return;
    console.log(`[cache] trimming in-app browser partition before session init (~${Math.round(total / 1048576)}MB)`);
    for (const rel of _BROWSER_CACHE_DIRS) {
      await fs.promises.rm(path.join(partitionDir, rel), { recursive: true, force: true }).catch(() => { /* in use or already gone */ });
    }
  } catch { /* best-effort */ }
}

function setupBrowserSession(): void {
  _browserSessionReady = true;
  const bs = session.fromPartition(BROWSER_PARTITION);

  // Present the in-app browser as plain desktop Chrome — see
  // BROWSER_DESKTOP_UA for why this matters (mobile H5 builds balloon).  The
  // identity switch re-applies this through the "browser-options" handler.
  try {
    bs.setUserAgent(browserIdentity().ua, _browserAcceptLanguage);
  } catch (e) {
    console.error("[browser] failed to set desktop user agent:", e);
  }

  // Override Accept-Language on every request so multi-language websites
  // respect the app-level language setting, and advertise a light-only colour
  // scheme so servers that pick a theme server-side never hand back a dark
  // page — browser content stays light no matter what the app theme is.
  bs.webRequest.onBeforeSendHeaders((details, callback) => {
    details.requestHeaders["Accept-Language"] = _browserAcceptLanguage;
    details.requestHeaders["Sec-CH-Prefers-Color-Scheme"] =
      _browserOpts.forceLightPage ? "light" : "light dark";
    if (_browserOpts.doNotTrack) {
      details.requestHeaders["DNT"] = "1";
      delete details.requestHeaders["Referer"];
    } else {
      delete details.requestHeaders["DNT"];
    }
    // A cross-site subresource is exactly what a tracking cookie rides in on.
    // `frame` names the document that asked for it, so a subresource whose
    // origin differs from that document's is third-party by the same
    // definition the browsers use.  Documents themselves (`mainFrame` /
    // `subFrame`) keep their cookies, so the site you actually visit still
    // signs you in.
    if (_browserOpts.blockThirdPartyCookies
      && details.resourceType !== "mainFrame" && details.resourceType !== "subFrame") {
      const frameUrl = details.frame?.url;
      if (frameUrl) {
        try {
          if (new URL(frameUrl).origin !== new URL(details.url).origin) {
            delete details.requestHeaders["Cookie"];
          }
        } catch {}
      }
    }
    callback({ requestHeaders: details.requestHeaders });
  });

  // Downloads.  With no handler Chromium already puts up its own Save-As
  // prompt, so "ask where to save" is just not interfering; the other choice
  // resolves a path here so a click never stalls on a dialog.
  bs.on("will-download", (_event, item) => {
    if (_browserOpts.askBeforeDownload) return;
    try {
      const dir = _browserOpts.downloadDir && fs.existsSync(_browserOpts.downloadDir)
        ? _browserOpts.downloadDir
        : app.getPath("downloads");
      // A server can name a file with separators in it; only the last segment
      // is ever allowed to reach the filesystem.
      const raw = path.basename(item.getFilename() || "") || `download-${Date.now()}`;
      const dot = raw.lastIndexOf(".");
      const stem = dot > 0 ? raw.slice(0, dot) : raw;
      const ext = dot > 0 ? raw.slice(dot) : "";
      let target = path.join(dir, raw);
      for (let n = 1; fs.existsSync(target); n++) target = path.join(dir, `${stem} (${n})${ext}`);
      item.setSavePath(target);
    } catch {
      // Anything unexpected: leave the prompt in place rather than lose it.
    }
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

  // ── localStorage snapshot ────────────────────────────────────────────
  // Cookies have a session API; localStorage does not -- each origin's data
  // lives in Chromium's own LevelDB, which the encrypted store cannot mirror
  // from the session layer. So it is read out of the page instead: every
  // main-frame navigation snapshots that origin, and an origin found with an
  // empty localStorage (a profile that predates the visit, or a restart) gets
  // its stored keys written back. Writing back happens at `dom-ready`, which
  // is after the page's own scripts -- a site that reads its state on first
  // paint therefore needs one reload, and that is the honest limit of doing
  // this without injecting a preload into third-party pages.
  let lsStore: Record<string, Record<string, string>> = {};
  try {
    const raw = loadBrowserLocalStorage();
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) lsStore = parsed;
    }
  } catch (e) {
    console.error("[browser] localStorage parse error:", e);
  }

  let lsTimer: ReturnType<typeof setTimeout> | null = null;
  const scheduleLocalStorageSave = (): void => {
    if (lsTimer) clearTimeout(lsTimer);
    lsTimer = setTimeout(() => saveBrowserLocalStorage(JSON.stringify(lsStore)), 800);
  };
  // The last snapshot of a session can land inside the debounce window; write
  // it synchronously on quit, exactly as the cookie cache does.
  app.on("before-quit", () => {
    if (lsTimer) clearTimeout(lsTimer);
    saveBrowserLocalStorage(JSON.stringify(lsStore));
  });

  // Per-origin byte ceiling: a site hoarding megabytes of scratch data is not
  // state worth persisting, and this whole map is encrypted as one blob.
  const LS_MAX_ORIGIN = 256 * 1024;
  const LS_READ = `(() => { try {
    const items = {}; let bytes = 0;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i); if (k === null) continue;
      const v = localStorage.getItem(k) || "";
      bytes += k.length + v.length;
      if (bytes > ${LS_MAX_ORIGIN}) break;
      items[k] = v;
    }
    return { origin: location.origin, items };
  } catch { return null; } })()`;

  // ── Credential capture ───────────────────────────────────────────────
  // Guest pages get no preload of ours, and `executeJavaScript` can only pull
  // a value back -- so the handoff between the document the user typed into
  // and the document that follows it rides on sessionStorage, which is
  // same-origin and tab-scoped: the submit hook stashes the entry, the next
  // navigation on that origin pulls it back out and clears it. A login that
  // leaves the origin (POST to a third-party IdP, then a cross-origin
  // redirect) therefore is not captured -- the alternative is a preload
  // injected into every third-party page, which is not a trade worth making.
  const CRED_STASH_KEY = "__encre_cred";
  const CRED_HOOK = `(window.__encreCredHook || (() => { try {
    window.__encreCredHook = true;
    const stash = (form) => { try {
      const pw = form.querySelector('input[type=password]');
      if (!pw) return;                       // not a credential form
      const un = form.querySelector('input[type=email],input[type=tel],input[type=text]');
      sessionStorage.setItem('${CRED_STASH_KEY}', JSON.stringify({
        origin: location.origin,
        username: un && un.value ? String(un.value) : '',
        password: String(pw.value || ''),
        kind: 'password',
        url: location.href,
        created_at: Date.now(),
      }));
    } catch {} };
    document.addEventListener('submit', (e) => {
      const f = e.target;
      if (f && f.tagName === 'FORM') stash(f);
    }, true);
  } catch {} })())`;
  const CRED_PULL = `(() => { try {
    const raw = sessionStorage.getItem('${CRED_STASH_KEY}');
    if (!raw) return null;
    sessionStorage.removeItem('${CRED_STASH_KEY}');
    const o = JSON.parse(raw);
    return (o && typeof o.origin === 'string') ? o : null;
  } catch { return null; } })()`;

  app.on("web-contents-created", (_event, wc) => {
    // `fromPartition` hands back the same Session object for the same name, so
    // identity is the check -- WebContents exposes no partition string.
    if (wc.session !== bs) return;

    wc.on("did-navigate", () => {
      if (wc.isDestroyed()) return;
      wc.executeJavaScript(LS_READ, true).then((res: { origin: string; items: Record<string, string> } | null) => {
        // about:blank and friends report the literal string "null" as their
        // origin; storing that would put a phantom site in the data center.
        if (!res || !/^https?:/i.test(res.origin)) return;
        lsStore[res.origin] = res.items;
        scheduleLocalStorageSave();
      }).catch(() => {});

      if (!_browserAutosaveCredentials) return;
      wc.executeJavaScript(CRED_PULL, true).then((entry: {
        origin: string; username?: string; password?: string; kind?: string; url?: string; created_at?: number;
      } | null) => {
        if (!entry || !/^https?:/i.test(String(entry.origin))) return;
        if (!entry.password && entry.kind !== "key") return;
        saveBrowserCredential({
          origin: entry.origin,
          username: String(entry.username || ""),
          password: String(entry.password || ""),
          kind: entry.kind === "key" ? "key" : "password",
          url: typeof entry.url === "string" ? entry.url : "",
          created_at: typeof entry.created_at === "number" ? entry.created_at : Date.now(),
        });
        console.log(`[browser] credential captured for ${entry.origin}`);
      }).catch(() => {});
    });

    wc.on("dom-ready", () => {
      if (wc.isDestroyed()) return;
      // Off means off: no listener is installed in the page at all.
      if (_browserAutosaveCredentials) {
        wc.executeJavaScript(CRED_HOOK, true).catch(() => {});
      }
      // Only this origin's own keys are ever handed to the page -- injecting
      // the whole store would expose every other site's data inside this
      // document's script text.
      let origin = "";
      try { origin = new URL(wc.getURL()).origin; } catch { return; }
      const mine = lsStore[origin];
      if (!mine || !Object.keys(mine).length) return;
      wc.executeJavaScript(`(() => { try {
        if (localStorage.length !== 0) return false;
        const mine = ${JSON.stringify(mine)};
        for (const k of Object.keys(mine)) { try { localStorage.setItem(k, mine[k]); } catch {} }
        return true;
      } catch { return false; } })()`, true).catch(() => {});
    });
  });

  // Permission handler for the in-app browser
  const originPerms = new Map<string, Map<string, boolean>>();
  const originRequestedPerms = new Map<string, Set<string>>();
  const allowed = new Set(["geolocation", "notifications", "midi", "midiSysex", "pointerLock", "fullscreen", "openExternal", "clipboard-read", "clipboard-sanitized-write", "display-capture", "media"]);
  bs.setPermissionRequestHandler((wc, permission, callback) => {
    // The global switch outranks both the default allowlist and any per-origin
    // grant, so a page cannot keep collecting access once the user opted out.
    if (_browserOpts.denyPermissions && _browserPromptablePermissions.has(permission)) {
      callback(false);
      return;
    }
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

  // Serve `encre-file://` targets as plain text. Chromium cannot render most
  // local source/config files (no usable MIME on Windows), and navigating a
  // webview straight to such a `file://` URL aborts with ERR_ABORTED because
  // the browser treats it as a download. The renderer rewrites only those
  // non-renderable paths to this scheme; verifiable local pages keep using
  // `file://` untouched, so their sibling `<script>`/`<link>` subresources are
  // unaffected. Scoped to the in-app browser session only.
  bs.protocol.handle(ENCRE_FILE_SCHEME, async (request) => {
    let filePath = "";
    try {
      const u = new URL(request.url);
      const host = u.host; // non-empty only for UNC targets (\\server\share)
      let p = decodeURIComponent(u.pathname || "");
      if (host) {
        p = "//" + host + p; // restore the UNC prefix for fs
      } else if (/^\/[a-zA-Z]:\//.test(p)) {
        p = p.slice(1); // drop the leading slash before a Windows drive letter
      }
      filePath = p;
    } catch {
      return new Response("Bad request", { status: 400 });
    }
    if (!filePath) return new Response("Bad request", { status: 400 });
    try {
      const stat = await fs.promises.stat(filePath);
      if (!stat.isFile()) return new Response("Not a file", { status: 404 });
      const data = await fs.promises.readFile(filePath);
      return new Response(data, {
        status: 200,
        headers: {
          "Content-Type": "text/plain; charset=utf-8",
          "Content-Length": String(data.length),
        },
      });
    } catch {
      return new Response("File not found", { status: 404 });
    }
  });
}

// Clears the in-app browser's cookies, storage and encrypted cookie file.
ipcMain.handle("browser:clear-data", async () => {
  try {
    ensureBrowserSession();
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

// Scoped variant of the above for the "don't keep me signed in" switch: wipes
// what the sites themselves stored (cookies and site storage, plus the two
// encrypted mirrors of them) but leaves the user's own library -- bookmarks and
// history -- alone.
ipcMain.handle("browser:clear-session-data", async () => {
  try {
    ensureBrowserSession();
    const bs = session.fromPartition(BROWSER_PARTITION);
    await bs.clearStorageData({
      storages: ["cookies", "localstorage", "indexdb", "websql", "serviceworkers", "cachestorage"],
    });
    try { fs.unlinkSync(BROWSER_COOKIE_FILE); } catch {}
    try { fs.unlinkSync(BROWSER_LOCALSTORAGE_FILE); } catch {}
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
    ensureBrowserSession();
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
    const bs = (() => { ensureBrowserSession(); return session.fromPartition(BROWSER_PARTITION); })();
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

// Every origin the user has decided on this session. Permission grants are
// in-memory only (never written to the data root), so the data center's 站点
// 权限 module reads them from here rather than from core.
ipcMain.handle("browser:list-permissions", async (): Promise<Array<{ origin: string; permission: string; granted: boolean }>> => {
  const out: Array<{ origin: string; permission: string; granted: boolean }> = [];
  try {
    const originPerms: Map<string, Map<string, boolean>> = (global as any).__browserOriginPerms || new Map();
    for (const [origin, perms] of originPerms) {
      for (const [permission, granted] of perms) {
        out.push({ origin, permission, granted });
      }
    }
    out.sort((a, b) => a.origin.localeCompare(b.origin) || a.permission.localeCompare(b.permission));
  } catch {}
  return out;
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
  // Read per write: the retention cap is a user setting now, so trimming an
  // over-long list happens on the very next visit after the change.
  const historyLimit = Math.max(100, _browserOpts.historyLimit || 5000);
  if (history.length > historyLimit) history.length = historyLimit;
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
      ensureBrowserSession();
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
      execSync(`taskkill /PID ${pid} /F /T`, { stdio: "ignore", timeout: 3000 });
    } else {
      // Kill the full process group …-pid means "process group id"
      try { process.kill(-pid, "SIGKILL"); } catch { process.kill(pid, "SIGKILL"); }
    }
  } catch { /* process already dead */ }
}

/** Force-free a TCP port by killing whatever process holds it (entire tree). */
function killProcessOnPort(port: number): void {
  if (process.platform === "win32") {
    // Detached fire-and-forget: netstat/Get-NetTCPConnection enumeration is
    // slow and must never block quit. The tracked server tree has already
    // been killed synchronously; this only sweeps a stray port holder.
    spawn(
      "powershell",
      [
        "-NoProfile",
        "-Command",
        `Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | ` +
          `Select-Object -ExpandProperty OwningProcess -Unique | ` +
          `ForEach-Object { taskkill /PID $_ /F /T }`,
      ],
      { detached: true, stdio: "ignore", windowsHide: true },
    ).unref();
  } else {
    try {
      spawn("sh", ["-c", `lsof -ti:${port} | xargs kill -9 2>/dev/null`], {
        detached: true,
        stdio: "ignore",
      }).unref();
    } catch { /* nothing on that port */ }
  }
}

/** Kills ALL processes (python.exe or encre-server.exe) whose command line contains "encre". */
function killAllEncreProcesses(): void {
  // All detached fire-and-forget: the PowerShell CIM sweep takes 2-5s and
  // must not block quit. The tracked server tree has already been killed
  // synchronously by the PID-file/serverProcess steps; this is only
  // insurance against stray processes from previous crashed runs.
  try {
    if (process.platform === "win32") {
      // Kill python.exe processes running encre (dev mode)
      spawn(
        "powershell",
        [
          "-NoProfile",
          "-Command",
          `Get-CimInstance Win32_Process -Filter "name='python.exe'" | ` +
            `Where-Object { $_.CommandLine -match 'encre' } | ` +
            `ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`,
        ],
        { detached: true, stdio: "ignore", windowsHide: true },
      ).unref();
      // Kill bundled encre-server.exe processes (release mode)
      spawn("taskkill", ["/F", "/IM", "encre-server.exe"], {
        detached: true,
        stdio: "ignore",
        windowsHide: true,
      }).unref();
    } else {
      spawn("pkill", ["-f", "python.*encre"], { detached: true, stdio: "ignore" }).unref();
      spawn("pkill", ["-f", "encre-server"], { detached: true, stdio: "ignore" }).unref();
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
      spawnArgs = ["--port", String(WS_PORT), "--service", "--log-level", "INFO"];
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
      spawnArgs = ["-m", "encre.server.app", "--port", String(WS_PORT), "--service", "--log-level", "INFO"];
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
    // The service runs at DEBUG level and emits tens of megabytes of stderr
    // while it boots (its own log file reaches hundreds of MB). Echoing every
    // chunk to the parent console made each write block on the console pipe,
    // which froze this process's event loop for seconds: the window then
    // shipped late and its custom min/max/close IPC stopped answering. Keep
    // only a bounded tail — that is all the failure message below surfaces.
    let stderrOutput = "";
    const STDERR_TAIL_MAX = 8 * 1024;
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
        stderrOutput = (stderrOutput + text).slice(-STDERR_TAIL_MAX);
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
// Currently selected tray theme ("dark" or "light"). Light matches the app's
// own default, so a popup opened before the renderer reports its theme is not
// briefly dark.
let currentTrayTheme: "dark" | "light" = "light";
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
 * resolves to light, matching the app's light default and the main process
 * pinning `nativeTheme.themeSource` to light.
 * @param themePreference - "light", "dark", or "system".
 * @returns The resolved concrete theme ("light" | "dark").
 */
function resolveTrayTheme(themePreference: string): "dark" | "light" {
  if (themePreference === "light" || themePreference === "dark") return themePreference;
  return "light"; // "system" defaults to light
}

// File caching the last renderer-reported theme so the next cold start can
// pre-color the hidden main window before its first paint.
const THEME_CACHE_FILE = path.join(DATA_DIR, "ui-theme.txt");

/**
 * Persists the resolved theme for the next cold start.
 * Best-effort: a failed write only means the next start falls back to light.
 * @param theme - The resolved concrete theme ("light" | "dark").
 */
function persistResolvedTheme(theme: "dark" | "light"): void {
  try {
    fs.mkdirSync(path.dirname(THEME_CACHE_FILE), { recursive: true });
    fs.writeFileSync(THEME_CACHE_FILE, theme, "utf8");
  } catch {
    // Best effort only; startup background just stays light.
  }
}

/**
 * Reads the persisted theme to pre-color the main window background.
 * The window is revealed on a fallback timer that can beat the renderer's
 * first paint; when that happens the raw `backgroundColor` is what the user
 * sees, so it must match the splash background (light #ffffff / dark
 * `--bg-primary` #000000) instead of an off-palette dark gray.
 * @returns The resolved concrete theme ("light" | "dark"), light on first run.
 */
function resolveStartupTheme(): "dark" | "light" {
  try {
    const stored = fs.readFileSync(THEME_CACHE_FILE, "utf8").trim();
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    // First run or unreadable cache: fall through to the light default.
  }
  return "light";
}

// File caching the last renderer-reported colour skin so the tray popup
// follows the app's skin across cold starts. Mirrors THEME_CACHE_FILE.
const SKIN_CACHE_FILE = path.join(DATA_DIR, "ui-skin.txt");

/**
 * The authoritative skin list is the set of palette files in
 * renderer/themes (tokens.css is the shared import point, not a skin).
 * Reading it here keeps main.ts from carrying a second copy of the list
 * that can drift from src/core/state.ts.
 */
function discoverSkins(): string[] {
  try {
    return fs
      .readdirSync(path.join(__dirname, "..", "renderer", "themes"))
      .filter((f) => f.endsWith(".css") && f !== "tokens.css")
      .map((f) => f.slice(0, -".css".length))
      .sort();
  } catch {
    // Packaging edge case: themes dir unreadable — accept the default only.
    return ["ink"];
  }
}
const TRAY_SKINS = discoverSkins();
let currentTraySkin = resolveStartupSkin();

/**
 * Persists the resolved skin for the next cold start. Best-effort.
 * @param skin - The active colour skin id ("ink" | "spring" | ...).
 */
function persistTraySkin(skin: string): void {
  try {
    fs.mkdirSync(path.dirname(SKIN_CACHE_FILE), { recursive: true });
    fs.writeFileSync(SKIN_CACHE_FILE, skin, "utf8");
  } catch {
    // Best effort only; the tray just keeps the previous skin.
  }
}

/**
 * Reads the persisted skin for the tray popup. Unknown/missing values
 * keep the ink default.
 */
function resolveStartupSkin(): string {
  try {
    const stored = fs.readFileSync(SKIN_CACHE_FILE, "utf8").trim();
    if (TRAY_SKINS.includes(stored)) return stored;
  } catch {
    // First run or unreadable cache: fall through to the ink default.
  }
  return "ink";
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
      skin: currentTraySkin,
    });
  }
}

/**
 * Closes and destroys the tray popup window if it is open.
 */
function closeTrayPopup(): void {
  if (pendingTrayBlurClose) {
    clearTimeout(pendingTrayBlurClose);
    pendingTrayBlurClose = null;
  }
  if (trayPopup && !trayPopup.isDestroyed()) {
    trayPopup.close();
    trayPopup = null;
  }
}

/* Tray popup timing marks. The tray right-click is a plain toggle, but the
   popup needs a few hundred milliseconds to load before it can be shown, and
   its own `blur` fires from the very click that toggled it. Without these the
   two handlers race and the popup either cancels its own opening or reopens
   right after that blur closed it — which is why the tray sometimes needed
   several right-clicks. */
let trayPopupShownAt = 0; // when the popup last became visible
let trayPopupClosedAt = 0; // when its `blur` last closed it
let trayPopupCreatedAt = 0; // when the current popup started loading
/** Events arriving within this window belong to the same click. */
const TRAY_POPUP_GUARD_MS = 350;
/** A popup that never reaches `ready-to-show` must not wedge the tray. */
const TRAY_POPUP_LOAD_MAX_MS = 4000;
/* On Windows a right-click on the tray icon deactivates the popup *before*
   the `right-click` event fires, so `blur` closes the popup and stamps
   trayPopupClosedAt; the right-click then lands inside the 350ms guard and
   is suppressed — looks like the click did nothing. Deferring the close
   when the cursor is on the tray icon lets the matching right-click cancel
   the timer and run the toggle itself. */
let pendingTrayBlurClose: NodeJS.Timeout | null = null;
/** Generous: a held right button can take this long to release. */
const TRAY_POPUP_BLUR_DEFER_MS = 300;
/** Pixels of slack around tray.getBounds() so a press near the icon edge
   still counts as "cursor on tray". */
const TRAY_POPUP_HIT_SLACK_PX = 6;

/**
 * Reports whether the mouse cursor is currently over the tray icon, with a
 * few pixels of slack so a press near the icon's edge still counts. Used by
 * the tray popup's `blur` handler to tell apart "blur caused by the right-
 * click the user just threw at the tray" (defer the close so the right-click
 * can toggle) from any other focus loss (close immediately).
 */
function cursorOnTrayIcon(): boolean {
  if (!tray) return false;
  try {
    const tb = tray.getBounds();
    const c = screen.getCursorScreenPoint();
    return (
      c.x >= tb.x - TRAY_POPUP_HIT_SLACK_PX &&
      c.x <= tb.x + tb.width + TRAY_POPUP_HIT_SLACK_PX &&
      c.y >= tb.y - TRAY_POPUP_HIT_SLACK_PX &&
      c.y <= tb.y + tb.height + TRAY_POPUP_HIT_SLACK_PX
    );
  } catch {
    return false;
  }
}

/**
 * Shows or hides the tray popup window. When opening, it creates a small
 * frameless window, positions it above the tray icon, and loads the popup
 * HTML. Clicking outside (blur) closes it.
 */
function toggleTrayPopup(): void {
  // A right-click arriving now is the user's intent; cancel any deferred
  // blur-close so the same click doesn't both close (via blur) and toggle.
  if (pendingTrayBlurClose) {
    clearTimeout(pendingTrayBlurClose);
    pendingTrayBlurClose = null;
  }
  const now = Date.now();
  if (trayPopup && !trayPopup.isDestroyed()) {
    // Still loading: never destroy a popup the user is waiting to see, or a
    // second right-click cancels the opening it just started. Only a popup
    // that never reached `ready-to-show` is replaced.
    if (!trayPopup.isVisible() && now - trayPopupCreatedAt < TRAY_POPUP_LOAD_MAX_MS) {
      return;
    }
    closeTrayPopup();
    return;
  }
  // The popup's own `blur` already closed it a moment ago: that blur was this
  // same right-click landing on the tray, so reopening here would make the
  // click look like it did nothing (or as if it could never close the popup).
  if (now - trayPopupClosedAt < TRAY_POPUP_GUARD_MS) return;

  if (!tray) return;
  const bounds = tray.getBounds();

  trayPopup = new BrowserWindow({
    width: 280,
    height: 440,
    frame: false,
    resizable: false,
    skipTaskbar: true,
    show: false,
    // A tray popup must never end up behind the window the click came from.
    alwaysOnTop: true,
    backgroundColor: currentTrayTheme === "dark" ? "#1a1a1a" : "#ffffff",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  trayPopupCreatedAt = now;

  if (process.platform === "win32" || process.platform === "darwin") {
    trayPopup.setHasShadow(true);
  }

  // Keep tray popup content zoom in sync with the main window (85%).
  trayPopup.webContents.setZoomFactor(0.85);

  trayPopup.webContents.on("did-finish-load", () => {
    if (trayPopup && !trayPopup.isDestroyed()) trayPopup.webContents.setZoomFactor(0.85);
    sendTrayDataToPopup();
  });

  trayPopup.loadFile(path.join(__dirname, "..", "renderer", "tray-popup.html"));

  trayPopup.once("ready-to-show", () => {
    if (!trayPopup || trayPopup.isDestroyed()) return;
    const popupBounds = trayPopup.getBounds();
    // Anchor above the icon, flipping below when there is no room, then clamp
    // to the display's work area — centered on a tray icon near a screen edge
    // the popup would otherwise hang off it.
    const area = screen.getDisplayNearestPoint({ x: bounds.x, y: bounds.y }).workArea;
    let x = Math.round(bounds.x + bounds.width / 2 - popupBounds.width / 2);
    let y = Math.round(bounds.y - popupBounds.height - 8);
    if (y < 0) {
      y = Math.round(bounds.y + bounds.height + 8);
    }
    x = Math.max(area.x + 4, Math.min(x, area.x + area.width - popupBounds.width - 4));
    y = Math.max(area.y + 4, Math.min(y, area.y + area.height - popupBounds.height - 4));
    trayPopup.setPosition(x, y);
    trayPopupShownAt = Date.now();
    trayPopup.show();
    // The popup moved before showing: clear any stale surface content the
    // reposition left behind (same present-path class as the main windows).
    trayPopup.webContents.invalidate();
    // Take focus explicitly: show() alone can leave focus with whatever the
    // click had focused, and the popup then blurs — and closes — on the spot.
    trayPopup.focus();
  });

  trayPopup.on("blur", () => {
    // Ignore the blur caused by the opening click itself.
    if (Date.now() - trayPopupShownAt < TRAY_POPUP_GUARD_MS) return;
    // A spurious blur on a popup that never reached `ready-to-show` would
    // strand the load (no right-click can recover it); ignore it.
    if (!trayPopup || trayPopup.isDestroyed() || !trayPopup.isVisible()) return;
    // If the cursor is on the tray icon, this blur was caused by the very
    // right-click the user just threw at the tray — Windows deactivates the
    // popup before dispatching `right-click`. Close it now and the matching
    // right-click lands inside the 350ms closedAt guard and is suppressed,
    // so the click appears to do nothing. Defer the close so the right-click
    // can cancel it and run the toggle itself.
    if (cursorOnTrayIcon()) {
      if (pendingTrayBlurClose) clearTimeout(pendingTrayBlurClose);
      pendingTrayBlurClose = setTimeout(() => {
        pendingTrayBlurClose = null;
        trayPopupClosedAt = Date.now();
        closeTrayPopup();
      }, TRAY_POPUP_BLUR_DEFER_MS);
      return;
    }
    trayPopupClosedAt = Date.now();
    closeTrayPopup();
  });
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

// Park / raise the main window without ever unmapping it.
//
// BrowserWindow.hide() calls ShowWindow(SW_HIDE).  The renderer keeps painting
// while hidden (measured: rAF still runs at 60fps), but the native window is
// unmapped, so the compositor releases the window's presentation surface and
// the process stops touching the bulk of its private working set — which is
// exactly what Windows trims on a tray-resident app (a live tray instance
// measured 5MB resident against 800MB private).  The next show() has to
// re-create that surface and fault the trimmed pages back off the pagefile:
// that is the "window freezes for a moment after I raise it, then it's fine
// again".  Parking the window off the desktop keeps it mapped, so the surface
// and the resident pages both survive the trip and a raise presents
// immediately (measured: park and raise each cost ~4ms of main thread).
const PARK_OFFSCREEN_XY = -32000;
let parkedBounds: { x: number; y: number; width: number; height: number; maximized: boolean } | null = null;

function parkMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed() || parkedBounds) return;
  const maximized = mainWindow.isMaximized();
  // A maximized window resizes itself when moved, so capture normal bounds first.
  if (maximized) mainWindow.unmaximize();
  const b = mainWindow.getBounds();
  parkedBounds = { ...b, maximized };
  // Skip-taskbar also drops the window from Alt+Tab, which a hidden window
  // never appeared in; raise clears it again.
  mainWindow.setSkipTaskbar(true);
  mainWindow.setPosition(PARK_OFFSCREEN_XY, PARK_OFFSCREEN_XY);
}

function raiseMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  const parked = parkedBounds;
  if (parked) {
    parkedBounds = null;
    mainWindow.setBounds({ x: parked.x, y: parked.y, width: parked.width, height: parked.height });
    mainWindow.setSkipTaskbar(false);
    if (parked.maximized) mainWindow.maximize();
  }
  mainWindow.show();
  mainWindow.focus();
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
    // Left-click raises the main window; if the tray popup is open it must
    // close now, otherwise its blur (cursor still on tray) would defer the
    // close by TRAY_POPUP_BLUR_DEFER_MS and visibly linger over the main
    // window. closeTrayPopup() cancels any pending deferred blur-close.
    closeTrayPopup();
    if (mainWindow === null) {
      // Window was destroyed (e.g. crash or quit path): recreate it, but the
      // reveal must be synchronous — no ready-to-show gating on a user click.
      createWindow({ immediate: true });
    } else {
      // show() alone leaves a minimized window minimized, so a window that was
      // minimized to the tray would never come back — raiseMainWindow() undoes
      // both the minimize and the park.  No invalidate() pass: the window was
      // never unmapped, so its presented surface is still the current one and a
      // forced full re-raster is just wasted work on the raise.
      raiseMainWindow();
    }
  });

  tray.on("right-click", () => {
    toggleTrayPopup();
  });
}

/* 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛?Window creation 闂傚倸鍊风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎规洘鍨块獮妯肩磼濡厧甯楅梻浣侯焾缁绘劙藝椤栨稓顩插Δ锝呭暞閳锋垿鏌涢幇顓炵祷閻㈩垬鍔戦弻娑氣偓锝庡亝瀹曞矂鏌＄仦鐣屝х€规洘顨嗗鍕節娴ｅ壊妫滈梻鍌氬€风粈渚€宕崸妤€鍌ㄦ繝濠傜墕绾惧鏌熼崜褏甯涢柣鎾冲暣閺屾稖绠涢幙鍐┬︽繛瀛樼矒缁犳牕顫忓ú顏勭闁圭粯甯掓潏鍛存⒑缁嬫鍎愰柟鐟版喘瀵顓兼径濠勵槯婵犮垼娉涢敃锝嗙珶閺囥垺鈷掑ù锝囶焾閺嗛亶鏌涘Ο鑽ょ煉鐎?*/

/**
 * Creates the main application BrowserWindow: a frameless, hidden-by-default
 * window that loads the renderer, locks zoom to 85%, opens the dev tools only
 * on demand (ENCRE_DEVTOOLS=1), and hides to the tray on close instead of quitting.
 */
function createWindow(opts?: { immediate?: boolean }): void {
  // Pre-paint color must match the splash background (`#splash-screen` uses
  // `var(--bg-primary)`: light #ffffff, dark #000000). The window can be
  // revealed by the fallback timer before the renderer's first paint, and the
  // raw background is all the user sees until then — a dark gray here read as
  // a black app before the light shell appeared.
  const startupTheme = resolveStartupTheme();
  // A freshly created window starts on-screen; never inherit a park (or the
  // maximize state) recorded for a window that no longer exists.
  parkedBounds = null;
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 800,
    minHeight: 600,
    frame: false,
    titleBarStyle: "hidden",
    titleBarOverlay: false,
    backgroundColor: startupTheme === "dark" ? "#000000" : "#ffffff",
    title: "Encre Agent Desktop",
    icon: resolveAppIconPath(),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
      // The main window lives in the tray most of its life. Letting Chromium
      // throttle it while hidden freezes the renderer, so the first paint is
      // withheld (the window then never appears until something forces it on
      // screen) and restoring from the tray has to wait for a full unfreeze.
      backgroundThrottling: false,
      spellcheck: false,
    },
  });

  // Hold an execution request for the app's lifetime. Windows throttles
  // (EcoQoS) processes whose windows are hidden, and a throttled renderer is
  // what turns a tray click into a multi-second wait for the shell to redraw.
  // This is a resident tray client that must come back instantly, so keep it
  // unthrottled even while hidden. Electron cleans the request up on quit.
  powerSaveBlocker.start("prevent-app-suspension");

  // Reveal only on the first paint, which is what carries the pre-painted
  // splash logo in index.html (see the splash markup there). Showing the window
  // any earlier puts an empty window on screen — just the raw
  // `backgroundColor`, with no splash at all — and that reads as a black app.
  // The renderer's first paint used to be withheld for ten-plus seconds while
  // the window was hidden, so this used to force a show on a short timer; the
  // occlusion opt-out plus `backgroundThrottling: false` below keep the hidden
  // renderer painting, so `ready-to-show` now arrives on its own within a
  // couple of seconds. The timeout stays only as a last-resort net so the
  // window can never remain hidden.
  //
  // Wired BEFORE loadFile(): the event fires once and never repeats, so
  // attaching it after the load could miss it on a fast start.
  //
  // Tray-driven recreation (`immediate`) must put a window on screen
  // SYNCHRONOUSLY — the user clicked the tray icon and a visible window is
  // non-negotiable. The background matches the splash color, so an early show
  // is seamless: white (dark: black) until the splash paints over it.
  //
  // Cold start uses the same logic with a short net: if `ready-to-show` has
  // not arrived within 800ms (slow disk, cold cache, big stylesheet), reveal
  // anyway. The pre-paint color equals the splash background, so the worst
  // case is a brief solid-color loading window — always better than a
  // multi-second invisible launch.
  const REVEAL_MAX_MS = 800;
  const reveal = () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isVisible() && !parkedBounds) return;
    raiseMainWindow();
  };
  if (opts?.immediate) {
    reveal();
  } else {
    mainWindow.once("ready-to-show", reveal);
    setTimeout(reveal, REVEAL_MAX_MS);
  }
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
  // Force-reset and lock zoom to 85% to avoid accidental Ctrl+-/Ctrl+wheel shrink.
  mainWindow.webContents.setZoomFactor(0.85);
  mainWindow.webContents.setVisualZoomLevelLimits(1, 1).catch(() => {});
  mainWindow.webContents.on("did-finish-load", () => {
    mainWindow?.webContents.setZoomFactor(0.85);
  });
  // Re-pin the zoom on geometry changes: resizing, and dragging the window
  // onto a monitor with a different DPI scale, both make Chromium re-derive
  // the effective zoom factor — without re-pinning, content then overflows
  // the window.  Debounced because resize/move fire continuously in a drag.
  // NOTE: no 1px setBounds "viewport re-derive" nudge here. The nudge raced
  // every live drag (the second setBounds restored bounds captured before the
  // user's last movement, visibly reverting the resize) and it also fired on
  // `show`, moving a freshly revealed window under the cursor. The stale
  // viewport it papered over was a legacy-present-path artifact; with
  // DirectComposition (the default, see the NOTE at the top of this file)
  // Chromium re-derives the viewport on every real resize on its own.
  let mainZoomPinTimer: ReturnType<typeof setTimeout> | null = null;
  const pinMainZoom = () => {
    if (mainZoomPinTimer) clearTimeout(mainZoomPinTimer);
    mainZoomPinTimer = setTimeout(() => {
      mainZoomPinTimer = null;
      if (!mainWindow || mainWindow.isDestroyed()) return;
      mainWindow.webContents.setZoomFactor(0.85);
    }, 120);
  };
  mainWindow.on("resize", pinMainZoom);
  mainWindow.on("move", pinMainZoom);
  // Tray reveal / un-minimize can land on a monitor with a different DPI
  // scale than the one the window was pinned on — re-pin there too.
  mainWindow.on("show", pinMainZoom);
  mainWindow.on("restore", pinMainZoom);
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
      mainWindow?.webContents.setZoomFactor(0.85);
    }
  });

  // mainWindow.webContents.openDevTools();

  // Override close: hide to tray instead of destroying, unless user clicked "Quit"
  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      parkMainWindow();
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
    // Async read: a synchronous whole-file read blocks the process that owns
    // window input, so opening a large preview froze the entire app.
    const buf = await fs.promises.readFile(filePath);
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

// Returns the key-wrap material for transport encryption (scheme v3),
// mirroring harness/encre/crypto.py. The fingerprint mixes EVERY source the
// Python side uses, framed as "source=value" lines joined by "\n"; the app
// secret follows the documented env/registry fallback chain (the native
// build-time injection is not reachable from Electron, so the system-wide
// ENCRE_EA_SECRET channel is the contract).

// Process-lifetime cache for immutable machine values (MachineGuid, the app
// secret). The app secret can appear at runtime (an admin reg add while the
// app is open), so only POSITIVE results are cached; misses are re-queried.
const _regValueCache = new Map<string, string>();

async function _regQueryValue(hive: string, valueName: string): Promise<string | null> {
  const cacheKey = hive + "::" + valueName;
  const hit = _regValueCache.get(cacheKey);
  if (hit !== undefined) return hit;
  try {
    // Async on purpose: a synchronous reg.exe spawn here sat directly on the
    // startup critical path (the renderer asks for the wrap material the
    // moment its WS transport boots), and wherever endpoint-security software
    // hooks child-process creation each spawn can stall for seconds -- the
    // watchdog measured multi-second main-thread freezes from exactly this.
    const { stdout } = await promisify(execFile)(
      "reg", ["query", hive, "/v", valueName],
      { encoding: "utf-8", timeout: 5000, maxBuffer: 1024 * 256 }
    );
    const m = new RegExp(valueName + "\\s+REG_(?:SZ|EXPAND_SZ)\\s+(.*)").exec(stdout);
    const value = m && m[1].trim() ? m[1].trim() : null;
    if (value) _regValueCache.set(cacheKey, value);
    return value;
  } catch {
    return null;
  }
}

async function _readMachineFingerprint(): Promise<string> {
  const parts: string[] = [];
  const add = (source: string, value: string | null | undefined) => {
    if (!value) return;
    const stripped = value.trim();
    if (stripped) parts.push(source + "=" + stripped);
  };

  // Windows: registry machine GUID (stable across reboots and renames)
  if (process.platform === "win32") {
    const guid = await _regQueryValue("HKLM\\SOFTWARE\\Microsoft\\Cryptography", "MachineGuid");
    if (guid) add("win-machineguid", guid);
  }

  for (const entry of [
    ["/etc/machine-id", "machine-id"],
    ["/sys/class/dmi/id/product_uuid", "dmi-product-uuid"],
  ] as const) {
    try {
      const mid = fs.readFileSync(entry[0], "utf-8");
      if (mid && mid.trim() !== "uninitialized") add(entry[1], mid);
    } catch {
      /* source not present on this host */
    }
  }

  add("hostname", require("os").hostname());

  // Mirror Python getpass.getuser(): pwd database on POSIX, env fallback
  // (LOGNAME/USER/LNAME/USERNAME) everywhere else.
  let user: string | null = null;
  if (process.platform !== "win32") {
    try { user = require("os").userInfo().username; } catch { /* fall through */ }
  }
  if (!user) {
    for (const name of ["LOGNAME", "USER", "LNAME", "USERNAME"]) {
      const v = process.env[name];
      if (v) { user = v; break; }
    }
  }
  add("username", user);

  return parts.join("\n");
}

const _DEV_APP_SECRET = "encre-dev-app-secret-public-dev-fallback";

async function _readAppSecret(): Promise<string> {
  const env = process.env.ENCRE_EA_SECRET;
  if (env && env.trim()) return env.trim();
  if (process.platform === "win32") {
    for (const hive of [
      "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment",
      "HKCU\\Environment",
    ]) {
      const v = await _regQueryValue(hive, "ENCRE_EA_SECRET");
      if (v) return v;
    }
  }
  return _DEV_APP_SECRET;
}

// Memoized for the process lifetime: the material is immutable once resolved,
// and the renderer asks for it on every WS boot.
let _keyWrapMaterialPromise: Promise<{ fingerprint: string; appSecret: string }> | null = null;

ipcMain.handle("readKeyWrapMaterial", () => {
  if (!_keyWrapMaterialPromise) {
    _keyWrapMaterialPromise = (async () => ({
      fingerprint: await _readMachineFingerprint(),
      appSecret: await _readAppSecret(),
    }))();
  }
  return _keyWrapMaterialPromise;
});

// Proxies a same-origin GET to the backend's local admin channel for the
// renderer. The renderer cannot fetch http://127.0.0.1:<WS_PORT> itself:
// pages loaded from file:// are an opaque origin, and the admin channel
// deliberately sends no CORS headers (anti drive-by, see admin.py), so
// Chromium blocks the response even though the server answered 200. The
// main process has no such restriction and sends no Origin header, which
// the channel treats as a local request.
ipcMain.handle("adminFetch", async (_event, urlPath: string) => {
  if (typeof urlPath !== "string" || !urlPath.startsWith("/") || urlPath.startsWith("//")) {
    return { ok: false, status: 0, error: "bad admin path" };
  }
  try {
    const res = await fetch(`http://127.0.0.1:${WS_PORT}${urlPath}`, {
      signal: AbortSignal.timeout(10_000),
    });
    const body = await res.text();
    return { ok: res.ok, status: res.status, body };
  } catch (err) {
    return { ok: false, status: 0, error: String(err) };
  }
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

  // Read only a bounded tail of the file. The backend logs at DEBUG level and
  // the file can reach hundreds of MB; reading it whole synchronously froze
  // the main event loop for several seconds (the watchdog measured ~9s). The
  // UI paginates newest-first in small batches, so a fixed tail window is
  // always sufficient.
  const LOG_TAIL_BYTES = 2 * 1024 * 1024;
  let rawLines: string[];
  let truncatedHead = false;
  try {
    const st = fs.statSync(logFile);
    const start = Math.max(0, st.size - LOG_TAIL_BYTES);
    truncatedHead = start > 0;
    const len = st.size - start;
    const fd = fs.openSync(logFile, "r");
    try {
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, start);
      rawLines = buf.toString("utf-8").replace(/\r\n/g, "\n").split("\n");
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return null;
  }
  // The tail window may begin mid-line: drop the partial first line so the
  // parser never sees a torn entry.
  if (truncatedHead && rawLines.length > 0) rawLines = rawLines.slice(1);
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
    const buf = await fs.promises.readFile(filePath);
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

// XDG autostart entry for Linux.  Electron's `app.setLoginItemSettings` is a
// documented no-op on Linux, so auto-start there is implemented with a
// standard `~/.config/autostart/*.desktop` file instead — the same mechanism
// desktop environments use for their own "startup applications" list.
const LINUX_AUTOSTART_DIR = path.join(os.homedir(), ".config", "autostart");
const LINUX_DESKTOP_FILE = path.join(LINUX_AUTOSTART_DIR, "encre-agent.desktop");

/**
 * Reads the OS-level login-item state for the current platform.  On Linux
 * that is the presence of the XDG autostart entry; on Windows/macOS it is
 * Electron's native login item registry / service.
 */
function isOsAutoStartEnabled(): boolean {
  if (process.platform === "linux") {
    try {
      return fs.existsSync(LINUX_DESKTOP_FILE);
    } catch { return false; }
  }
  return app.getLoginItemSettings().openAtLogin;
}

/**
 * Applies the OS-level login item for the current platform.
 * @param enabled - Whether the app should launch at login.
 */
function applyOsAutoStart(enabled: boolean): void {
  if (process.platform === "linux") {
    try {
      if (enabled) {
        fs.mkdirSync(LINUX_AUTOSTART_DIR, { recursive: true });
        // AppImage builds expose the mounted image path via env; using it
        // keeps the entry valid when the image moves between boots.
        const execPath = process.env.APPIMAGE || process.execPath;
        const desktop = [
          "[Desktop Entry]",
          "Type=Application",
          "Name=Encre Agent Desktop",
          `Exec=${execPath}`,
          "Hidden=false",
          "NoDisplay=false",
          "X-GNOME-Autostart-enabled=true",
          "",
        ].join("\n");
        fs.writeFileSync(LINUX_DESKTOP_FILE, desktop, "utf-8");
      } else {
        fs.rmSync(LINUX_DESKTOP_FILE, { force: true });
      }
    } catch (err) {
      console.error("Failed to update XDG autostart entry:", err);
      throw err;
    }
  } else {
    // Windows (HKCU Run key) and macOS (login service) are handled natively.
    app.setLoginItemSettings({ openAtLogin: enabled });
  }
}

// Returns the persisted auto-start setting, coercing the OS login item if needed.
ipcMain.handle("getAutoStart", () => {
  const fileSetting = readAutoStartFile();
  if (fileSetting && !isOsAutoStartEnabled()) {
    applyOsAutoStart(true);
  }
  return fileSetting;
});

// Sets the auto-start (launch-at-login) preference in the OS and on disk.
ipcMain.handle("setAutoStart", async (_event, enabled: boolean) => {
  try {
    applyOsAutoStart(enabled);
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

// Toggle auto-saving of credentials captured by the in-app browser.
ipcMain.on("browser-autosave", (_event, enabled: boolean) => {
  _browserAutosaveCredentials = !!enabled;
});

// Browser switches (Settings -> Assist -> Browser).  The flags themselves are
// read at use time; only the three that Chromium caches per-guest need a push,
// so those are re-applied to the tabs that are already open.
ipcMain.on("browser-options", (_event, opts: Partial<BrowserOpts>) => {
  if (!opts || typeof opts !== "object") return;
  const prevUa = browserIdentity().ua;
  const prevForceLight = _browserOpts.forceLightPage;
  const prevZoom = _browserOpts.zoomFactor;
  (Object.keys(opts) as Array<keyof BrowserOpts>).forEach((k) => {
    const v = opts[k];
    if (v !== undefined) (_browserOpts[k] as unknown) = v;
  });
  const guests = webContents.getAllWebContents().filter((wc) => wc.getType() === "webview");
  // Two separate surfaces, and both have to move: the session UA is what goes
  // out on the wire (what a server sees), the CDP override below is what the
  // page's own `navigator.userAgent` reports.  Touching only one leaves them
  // disagreeing, which is exactly what sites sniff.
  if (browserIdentity().ua !== prevUa) {
    // Session-level UA write only when the browser session is initialized:
    // this handler fires at every renderer boot (settings snapshot) and must
    // NOT trigger the partition's expensive storage init. When setup runs
    // later (first webview attach) it applies the identity itself.
    if (_browserSessionReady) {
      try {
        session.fromPartition(BROWSER_PARTITION).setUserAgent(browserIdentity().ua, _browserAcceptLanguage);
      } catch {}
    }
  }
  if (browserIdentity().ua !== prevUa || _browserOpts.forceLightPage !== prevForceLight) {
    guests.forEach((wc) => applyBrowserGuestEmulation(wc));
  }
  // A UA override is a per-document thing: the override command above only
  // takes hold on the NEXT commit, so an already-loaded page keeps reporting
  // the old identity to itself and to any script it runs.  Reloading is what
  // makes the switch actually visible; the guest is the same object and keeps
  // its session, history position and scroll-free state, so the flash is a
  // single frame rather than a lost tab.
  if (browserIdentity().ua !== prevUa) {
    guests.forEach((wc) => { try { wc.reloadIgnoringCache(); } catch {} });
  }
  if (_browserOpts.zoomFactor !== prevZoom) {
    const factor = _browserOpts.zoomFactor > 0 ? _browserOpts.zoomFactor : 0.75;
    guests.forEach((wc) => {
      try {
        wc.setZoomFactor(factor);
        // A zoom change re-lays the guest out at the (possibly stale) guest
        // viewport; end the sequence with a full repaint (same ordering
        // policy as the window heal sequences).
        wc.invalidate();
      } catch {}
    });
  }
});

// Persist one credential (password or API key) captured after a successful
// authentication in the in-app browser into the encrypted store.
ipcMain.handle("browser:save-credential", async (_event, entry: { origin?: string; username?: string; password?: string; kind?: string; url?: string; created_at?: number }): Promise<{ success: boolean; error?: string }> => {
  if (!_browserAutosaveCredentials) return { success: false, error: "autosave-disabled" };
  try {
    if (!entry || typeof entry.origin !== "string" || !entry.origin) {
      return { success: false, error: "invalid credential entry" };
    }
    saveBrowserCredential({
      origin: entry.origin,
      username: String(entry.username || ""),
      password: String(entry.password || ""),
      kind: entry.kind === "key" ? "key" : "password",
      url: typeof entry.url === "string" ? entry.url : "",
      created_at: typeof entry.created_at === "number" ? entry.created_at : Date.now(),
    });
    return { success: true };
  } catch (e: any) {
    return { success: false, error: e.message };
  }
});

// Updates the tray colour skin from the renderer: cached for cold starts
// and pushed to the open popup so it repaints at once.
ipcMain.on("tray-skin", (_event, skin: string) => {
  if (typeof skin !== "string" || !TRAY_SKINS.includes(skin)) return;
  currentTraySkin = skin;
  persistTraySkin(skin);
  sendTrayDataToPopup();
});

// Updates the tray theme (and popup background) from the renderer.
ipcMain.on("tray-theme", (_event, themePreference: string) => {
  currentTrayTheme = resolveTrayTheme(themePreference);
  // Cache the choice so the next cold start pre-colors the window background
  // with the matching splash color instead of an off-palette dark gray.
  persistResolvedTheme(currentTrayTheme);
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
      createWindow({ immediate: true });
    } else {
      raiseMainWindow();
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
      createWindow({ immediate: true });
    } else {
      raiseMainWindow();
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
    requestQuitWithTaskCheck();
  }
});

// Pending fallback timer for the quit confirmation flow.
let quitFallbackTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Asks the BACKEND (not the renderer) whether any task is still running
 * before quitting.  The backend owns the authoritative per-session state,
 * so this check works even when the renderer's websocket is disconnected —
 * closing the blind spot where an automation keeps running invisibly.
 *
 * - Backend unreachable or `running_sessions == 0`: quit silently, the main
 *   window is never raised.
 * - `running_sessions > 0`: raise the main window, send `quit-request` and
 *   let the renderer show the in-app confirm dialog.  The renderer replies
 *   on `quit-confirm-response`: `true` proceeds with the quit, `false`
 *   aborts it and keeps the running tasks alive.  A fallback timer
 *   guarantees the quit still happens if the renderer never answers
 *   (crashed page, blocked JS).
 */
async function requestQuitWithTaskCheck(): Promise<void> {
  if (!mainWindow || mainWindow.isDestroyed()) {
    isQuitting = true;
    app.quit();
    return;
  }

  let runningTasks = false;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 2000);
    const res = await fetch(`http://localhost:${WS_PORT}/stats`, { signal: ctrl.signal });
    clearTimeout(timer);
    runningTasks = res.ok && ((await res.json() as { running_sessions?: number }).running_sessions || 0) > 0;
  } catch {
    // Backend unreachable: nothing can be running there; quit directly.
    isQuitting = true;
    app.quit();
    return;
  }

  if (!runningTasks) {
    isQuitting = true;
    app.quit();
    return;
  }

  // Running tasks confirmed by the backend: raise the window so the user
  // can actually see the confirm dialog, then ask the renderer to show it.
  raiseMainWindow();
  mainWindow.webContents.send("quit-request");
  if (quitFallbackTimer) clearTimeout(quitFallbackTimer);
  quitFallbackTimer = setTimeout(() => {
    quitFallbackTimer = null;
    if (!isQuitting) {
      isQuitting = true;
      app.quit();
    }
  }, 15000);
}

// Renderer's answer to the quit confirmation: true = end running tasks and
// quit, false = cancel the quit and keep everything running.
ipcMain.on("quit-confirm-response", (_event, allowed: boolean) => {
  if (quitFallbackTimer) {
    clearTimeout(quitFallbackTimer);
    quitFallbackTimer = null;
  }
  if (allowed) {
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
ipcMain.handle("terminal:spawn", async (_event, shell?: string, shellArgs?: string[], cwd?: string) => {
  const pty = getNodePty();
  if (!pty) return { error: "node-pty not available" };
  try {
    const sh = shell || (
      process.platform === "win32"
        ? process.env.COMSPEC || "cmd.exe"
        : process.env.SHELL || "bash"
    );
    const args = shellArgs || [];
    // The settings panel can start terminals in the workspace root; a stale or
    // typo'd path would make node-pty throw, so only honour an existing dir.
    const home = process.env.HOME || process.env.USERPROFILE || ".";
    let dir = home;
    if (cwd) {
      try { if (fs.statSync(cwd).isDirectory()) dir = cwd; } catch { /* keep home */ }
    }
    const id = ++terminalSeq;
    const term = pty.spawn(sh, args, {
      name: "xterm-256color",
      cols: 80,
      rows: 24,
      cwd: dir,
      env: process.env as Record<string, string>,
    });
    terminals.set(id, { pty: term, terminalId: id });
    // PTY output arrives in tiny chunks; one IPC frame per chunk floods the
    // renderer during `ls`, builds, or `cat`. Coalesce per terminal so the
    // renderer writes to xterm at most once per frame.
    let pendingOutput = "";
    let flushTimer: NodeJS.Timeout | null = null;
    const flushOutput = () => {
      flushTimer = null;
      if (!pendingOutput) return;
      const data = pendingOutput;
      pendingOutput = "";
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("terminal:data", { id, data });
      }
    };
    term.onData((data: string) => {
      pendingOutput += data;
      if (!flushTimer) flushTimer = setTimeout(flushOutput, 16);
    });
    term.onExit(() => {
      if (flushTimer) clearTimeout(flushTimer);
      pendingOutput = "";
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

// Promisified exec that never throws: resolves to stdout on success, null on
// any failure/timeout. All main-process probes must run through this -- the
// browser process owns window dragging/input for every frameless window, so
// a synchronous exec here freezes the whole app (a missing WSL alone blocks
// `wsl --list` for 5-15s).
function execP(cmd: string, timeoutMs = 4000): Promise<string | null> {
  return new Promise((resolve) => {
    try {
      exec(cmd, { encoding: "utf-8", timeout: timeoutMs, windowsHide: true }, (err, stdout) =>
        resolve(err ? null : String(stdout ?? "")),
      );
    } catch {
      resolve(null);
    }
  });
}

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

    // All probes run in parallel and asynchronously -- the main event loop
    // keeps pumping (window dragging stays live) while they run.
    const [pwshFound, bashFound, wslOut] = await Promise.all([
      execP("where pwsh 2>nul").then((out) => (out ? out.trim().split("\n")[0].trim() : null)),
      execP("where bash 2>nul").then((out) => (out ? out.trim().split("\n")[0].trim() : null)),
      execP("wsl --list 2>nul || wsl -l 2>nul", 6000),
    ]);
    if (pwshFound) shells.push({ name: "PowerShell Core", path: pwshFound });
    if (bashFound) shells.push({ name: "Git Bash", path: bashFound, args: ["--login"] });
    if (wslOut && wslOut.trim()) shells.push({ name: "WSL", path: "wsl.exe" });
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
    // Async probe: a sync `wmic` call here blocks the whole app (and wmic is
    // removed on recent Win11, where the call fails fast and we fall back).
    const out = await execP("wmic logicaldisk get name", 5000);
    if (out) {
      const drives = out.split("\n").map((s: string) => s.trim()).filter((s: string) => /^[A-Z]:$/.test(s));
      if (drives.length) return drives;
    }
    return ["C:"];
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
// The agent version lives in the SPLIT source trees: the repo root
// pyproject.toml is no longer an installable package (only shared tool
// config), so read harness/pyproject.toml (encre-harness, the agent loop)
// first and core/pyproject.toml (encre-core) second.  In a packaged build
// the pyproject files are not bundled -- fall back to the desktop version
// (both trees ship in lockstep) instead of a bogus 0.0.0.
ipcMain.handle("getAppVersions", () => {
  const rootDir = path.resolve(__dirname, "..", "..");
  const pkgPath = path.join(__dirname, "..", "package.json");
  const desktopVersion = JSON.parse(fs.readFileSync(pkgPath, "utf-8")).version || "0.0.0";
  let agentVersion = "";
  for (const rel of ["harness/pyproject.toml", "core/pyproject.toml"]) {
    try {
      const pyContent = fs.readFileSync(path.join(rootDir, rel), "utf-8");
      const match = pyContent.match(/^version\s*=\s*"([^"]+)"/m);
      if (match) {
        agentVersion = match[1];
        break;
      }
    } catch {}
  }
  return { desktop: desktopVersion, agent: agentVersion || desktopVersion };
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

// Maps an IANA timezone (as reported by the OS clock) to a legal region key.
// The OS timezone tracks the machine's REAL physical location -- users set it
// when they move or when the region was configured -- which makes it a far
// stronger geo signal than locale/display-language preferences: a PC in China
// with an English Windows display language must still resolve to "cn", not
// "us".  Checked before the country code for exactly that reason.
const TIMEZONE_TO_LEGAL_REGION: ReadonlyArray<readonly [RegExp, string]> = [
  // Asia
  [/^Asia\/(Shanghai|Urumqi|Chongqing|Harbin|Kashgar)/, "cn"],
  [/^Asia\/Tokyo$/, "jp"],
  [/^Asia\/(Seoul|Pyongyang)$/, "kr"],
  [/^Asia\/Taipei$/, "tw"],
  [/^Asia\/Hong_Kong$/, "hk"],
  [/^Asia\/Macau$/, "mo"],
  [/^Asia\/Singapore$/, "sg"],
  [/^Asia\/(Kolkata|Calcutta|Colombo)$/, "in"],
  [/^Asia\/(Dubai|Muscat|Abu_Dhabi)$/, "ae"],
  [/^Asia\/(Riyadh|Qatar|Kuwait|Bahrain)$/, "sa"],
  [/^Asia\/(Jerusalem|Tel_Aviv|Gaza|Hebron)$/, "il"],
  // Europe
  [/^Europe\/(London|Belfast)$/, "uk"],
  [/^Europe\/(Zurich|Geneva)$/, "ch"],
  [/^Europe\/Istanbul$/, "tr"],
  // Americas
  [/^America\/(New_York|Chicago|Denver|Los_Angeles|Anchorage|Phoenix|Detroit|Honolulu|Boise|Indianapolis|Juneau)$/, "us"],
  [/^America\/(Toronto|Vancouver|Winnipeg|Halifax|St_Johns|Edmonton|Regina|Iqaluit)$/, "ca"],
  [/^America\/(Mexico_City|Monterrey|Tijuana|Cancun|Merida)$/, "mx"],
  [/^America\/(Sao_Paulo|Bahia|Fortaleza|Manaus|Rio_Branco|Recife|Araguaina)$/, "br"],
  // Oceania
  [/^Australia\/(Sydney|Melbourne|Brisbane|Perth|Adelaide|Hobart|Darwin)$/, "au"],
  [/^Pacific\/(Auckland|Fiji)$/, "nz"],
  // Africa
  [/^Africa\/(Johannesburg|Cape_Town)$/, "za"],
  [/^Africa\/(Lagos|Abuja)$/, "ng"],
  [/^Africa\/Nairobi$/, "ke"],
  [/^Africa\/Cairo$/, "eg"],
];

/** Returns the legal region matching the OS timezone, or "" when unknown. */
function timezoneLegalRegion(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    for (const [re, region] of TIMEZONE_TO_LEGAL_REGION) {
      if (re.test(tz)) return region;
    }
  } catch {}
  return "";
}

/**
 * OS-level home-country detection using each platform's own system setting
 * (NO network, NO IP lookup, NO permission prompt):
 *  - Windows: `HKCU\Control Panel\International\Geo\Nation` -- the "Country
 *    or region" home location the OS itself manages in Settings > Region.
 *  - macOS:   the global `AppleLocale` preference carries the home region
 *    subtag (e.g. "zh_CN", "en_US").
 *  - Linux:   no uniform system geo service from a plain process -- return ""
 *    and let the timezone fallback decide.
 * Returns an uppercase ISO-3166 alpha-2 code, or "" when unavailable.
 */
function osHomeCountry(): string {
  try {
    if (process.platform === "win32") {
      // `Name` is the ISO-3166 alpha-2 home country (e.g. "CN"); `Nation`
      // is a numeric GeoID (e.g. 45 = China) on many systems -- parse both.
      const out = execSync(
        'reg query "HKCU\\Control Panel\\International\\Geo" /v Name && reg query "HKCU\\Control Panel\\International\\Geo" /v Nation',
        {
          encoding: "utf-8",
          timeout: 2000,
          windowsHide: true,
        }
      ) as string;
      const nameM = out.match(/Name\s+REG_SZ\s+([A-Za-z]{2})/);
      if (nameM) return nameM[1].toUpperCase();
      const nationM = out.match(/Nation\s+REG_SZ\s+(\d+)/);
      if (nationM) return GEOLID_TO_ISO[nationM[1]] || "";
    } else if (process.platform === "darwin") {
      const out = execSync("defaults read -g AppleLocale", {
        encoding: "utf-8",
        timeout: 2000,
      }) as string;
      // e.g. "zh_CN" / "en_US@rg=uszzzz" / "zh-Hans-CN"
      const s = out.trim().replace(/@.*$/, "");
      const m = s.match(/[a-z]{2,3}[-_]([A-Z]{2})/) || s.match(/^[a-z]{2,3}[-_]([A-Z]{2})/);
      if (m) return m[1];
    }
  } catch {}
  return "";
}

// Windows GeoID (numeric home location) -> ISO-3166 alpha-2, high-confidence
// common values only; anything unknown falls through to the timezone fallback.
const GEOLID_TO_ISO: Record<string, string> = {
  "12": "AU", "26": "BR", "39": "CA", "45": "CN", "84": "FR", "94": "DE",
  "97": "ES", "105": "IT", "113": "IN", "122": "JP", "134": "KR",
  "139": "MX", "188": "SG", "203": "RU", "215": "HK", "224": "MO",
  "236": "TW", "242": "GB", "244": "US",
};

/** Returns the legal region key matching the OS-reported country, or "us". */
function systemLegalRegion(): string {
  // 1. The OS's own home-country setting (Windows Region / macOS locale) --
  //    the strongest "system-level geography" signal a user configures.
  const home = osHomeCountry();
  if (home) {
    if (COUNTRY_TO_LEGAL_REGION[home]) return COUNTRY_TO_LEGAL_REGION[home];
    if (EU_COUNTRY_CODES.has(home)) return "eu";
  }
  // 2. OS timezone -- the machine's real physical environment.
  const tzRegion = timezoneLegalRegion();
  if (tzRegion) return tzRegion;
  // 3. Electron-reported country code / locale subtag.
  let cc = home;
  if (!cc) {
    try {
      cc = (app.getLocaleCountryCode() || "").toUpperCase();
    } catch {
      cc = "";
    }
    if (!cc) {
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
    // Only an EXPLICIT user pick is honored.  Legacy files written by older
    // builds (plain string, or {region} without the "user" source stamp)
    // were auto-detection snapshots, not choices -- if those are stale the
    // user gets pinned to a wrong jurisdiction forever.  Ignore them and
    // recompute from the system instead.
    if (parsed && typeof parsed === "object" && parsed.source === "user") {
      const region = parsed.region;
      if (typeof region === "string" && LEGAL_REGIONS[region]) return region;
    }
  } catch {}
  return systemLegalRegion();
}

function writeDocRegion(region: string, source: "user" = "user"): void {
  if (!LEGAL_REGIONS[region]) return;
  try {
    fs.writeFileSync(getDocRegionFilePath(), JSON.stringify({ region, source }), "utf-8");
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
      // Mirror the main window's title-bar options so the two windows share the
      // same drag/hit-region behaviour instead of diverging.
      titleBarStyle: "hidden",
      titleBarOverlay: false,
      backgroundColor: "#0f0f0f",
      title: "ESD",
      icon: resolveAppIconPath(),
      show: false,
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
        webviewTag: true,
        backgroundThrottling: true,
        spellcheck: false,
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
    // Force-reset and lock zoom to 85% to avoid accidental Ctrl+-/Ctrl+wheel shrink.
    child.webContents.setZoomFactor(0.85);
    child.webContents.setVisualZoomLevelLimits(1, 1).catch(() => {});
    child.webContents.on("did-finish-load", () => {
      child?.webContents.setZoomFactor(0.85);
    });
    // Same re-pin policy as the main window: keep ESD at 85% across resizes
    // and monitor DPI changes (debounced, see pinMainZoom in createWindow).
    // Mirrors the main-window policy exactly: zoom re-pin only — no 1px
    // setBounds nudge (it raced live drags) and no invalidate() passes (they
    // compensated for the legacy present path; DirectComposition repaints
    // resize damage on its own).
    let childZoomPinTimer: ReturnType<typeof setTimeout> | null = null;
    const pinChildZoom = () => {
      if (childZoomPinTimer) clearTimeout(childZoomPinTimer);
      childZoomPinTimer = setTimeout(() => {
        childZoomPinTimer = null;
        if (!child || child.isDestroyed()) return;
        child.webContents.setZoomFactor(0.85);
      }, 120);
    };
    child.on("resize", pinChildZoom);
    child.on("move", pinChildZoom);
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
    // Docked DevTools grab focus right after boot and desync the OS client-area
    // hit region from what a frameless window paints (see createWindow). For
    // the child window that makes its own drag region and buttons miss real
    // mouse clicks, so the click falls through to the main window behind it and
    // the main window looks permanently stuck on top. Keep DevTools opt-in and
    // detached, exactly like the main window.
    if (process.env.ENCRE_DEVTOOLS === "1") {
      child.webContents.openDevTools({ mode: "detach" });
    }
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

ipcMain.handle("open-settings", (event, panel: string) => {
  // Target the window that asked (main or ESD child) instead of always
  // raising the main window, which stole focus from the child and pulled the
  // main window back to the foreground.
  const win = BrowserWindow.fromWebContents(event.sender);
  if (!win || win.isDestroyed()) return;
  win.webContents.send("open-settings-panel", panel);
  win.show();
  win.focus();
});

/* 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?CDP WebSocket Relay for BrowserView 闂傚倷绀侀崯鍧楀储濠婂牆纾婚柟鍓х帛閻撳啴鏌涜箛鎿冩Ц濞?*/
const cdpRelays = new Map<number, { wsc: WebSocket.Server; clients: Set<WebSocket.WebSocket>; debuggerAttached: boolean }>();
const cdpRelayPending = new Map<number, Promise<number>>();

function startCdpRelay(webContentsId: number): Promise<number> {
  // Synchronous de-dup: overlapping dom-ready registrations (guest rebuild,
  // rapid navigation) must never spawn two WebSocket servers for the same
  // guest. The relay map is only populated after the async "listening" event,
  // so an in-flight promise table is needed to close that race window.
  const existing = cdpRelays.get(webContentsId);
  if (existing) {
    const addr = existing.wsc.address();
    if (addr) return Promise.resolve((addr as any).port);
  }
  const pending = cdpRelayPending.get(webContentsId);
  if (pending) return pending;
  const p = createCdpRelay(webContentsId).finally(() => cdpRelayPending.delete(webContentsId));
  cdpRelayPending.set(webContentsId, p);
  return p;
}

function createCdpRelay(webContentsId: number): Promise<number> {
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
          // A browser guest is usually attached already by
          // applyBrowserGuestEmulation. Attaching twice throws, and the message
          // forwarder below would never be registered — silently cutting the
          // agent off from this target's CDP.
          if (!wc.debugger.isAttached()) wc.debugger.attach("1.3");
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
      // The backend browser tool is the sole WebSocket client of a relay, so
      // a fresh client means the Agent is now operating this guest. If a tool
      // call is already announced, retarget the overlay to this guest right
      // away (covers reconnects after a guest rebuild mid-call).
      agentControlTargetWcId = webContentsId;
      if (agentControlActive) broadcastAgentControl();
      const wc = webContents.fromId(webContentsId);
      ws.on("message", (data) => {
        if (!wc || wc.isDestroyed()) return;
        try {
          const msg = JSON.parse(data.toString());
          // Control-change request from the Agent tool: intercepted here,
          // surfaced on the pill, answered after the user decides.
          if (msg.method === "Encre.agentControlRequest") {
            handleAgentControlRequest(ws, msg);
            return;
          }
          // User has taken over: reject every backend CDP command right away
          // (no buffering, no timeout) so the tool call fails fast with an
          // explicit message — the model then asks the user to hand control
          // back. The user's own input is unaffected (it never travels this
          // relay).
          if (agentControlTakenOver) {
            if (msg.id !== undefined) {
              ws.send(JSON.stringify({
                id: msg.id,
                error: {
                  code: -32000,
                  message: "The user has taken over manual control of the browser. Ask the user to hand control back, then retry.",
                },
              }));
            }
            return;
          }
          wc.debugger.sendCommand(msg.method, msg.params || {})
            .then((result: any) => {
              if (msg.id !== undefined) ws.send(JSON.stringify({ id: msg.id, result }));
            })
            .catch((err: any) => {
              if (msg.id !== undefined) ws.send(JSON.stringify({ id: msg.id, error: { code: -32000, message: err.message || String(err) } }));
            });
        } catch {}
      });
      ws.on("close", () => {
        // If the closing connection carried a pending control request,
        // settle it as denied so nothing hangs on a dead socket, and
        // re-broadcast so every pill leaves the request view.
        if (agentControlRequestRespond) {
          settleAgentControlRequest(false, "disconnected");
          broadcastAgentControl();
        }
        clients.delete(ws);
      });
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

/* ── beforeunload guard (will-prevent-unload) ────────────────────────────
 * A page that registers a beforeunload handler ("leave without saving?")
 * must surface the SAME in-app dialog as the page's own alert/confirm
 * prompts — not a native OS message box, and not a silent block.  The
 * renderer binds one guard per guest webContents; when the event fires,
 * main parks it and asks the renderer, and the renderer's answer decides:
 * allow → preventDefault (Electron inverts the usual semantics: calling it
 * on will-prevent-unload LETS the page unload); deny / no answer → the
 * page simply stays, which is the safe default. */
const unloadGuardEvents = new Map<number, Electron.Event>();
ipcMain.handle("browser:bind-unload-guard", (event, wcId: number): void => {
  const wc = webContents.fromId(wcId);
  if (!wc || unloadGuardEvents.has(wcId)) return;
  wc.on("will-prevent-unload", (ev: Electron.Event) => {
    unloadGuardEvents.set(wcId, ev);
    event.sender.send("browser:unload-request", wcId);
  });
  wc.once("destroyed", () => unloadGuardEvents.delete(wcId));
});
ipcMain.handle("browser:respond-unload", (_event, wcId: number, allow: boolean): void => {
  const ev = unloadGuardEvents.get(wcId);
  unloadGuardEvents.delete(wcId);
  if (allow && ev) {
    try { ev.preventDefault(); } catch { /* event already settled */ }
  }
});

/* ── Agent control broadcast ────────────────────────────────────────────
 * When a browser (or, in the future, computer-use) tool call is running,
 * the renderer announces it here; main fans the state out to every window
 * so every browser view can show the takeover overlay. While the lock is
 * up, ALL views are isolated; the lock engages once per turn and is
 * released at the turn boundary. */
let agentControlActive = false;
let agentControlTargetWcId: number | null = null;
/** True while the user has taken over mid-operation: the overlay keeps its
 *  status pill (no green tint) and every CDP command from the backend is
 *  rejected, so the Agent must ask the user to hand control back. */
let agentControlTakenOver = false;
/** Control-change request surfaced on the pill, awaiting the user's answer.
 *  The Agent can ASK (never take): "user_takeover" = hand the wheel to the
 *  user (login / CAPTCHA), "agent_control" = ask for control back. */
let agentControlRequestType: string | null = null;
/** Resolves the pending request's JSON-RPC response towards the backend. */
let agentControlRequestRespond: ((granted: boolean, reason: string) => void) | null = null;
let agentControlRequestTimer: NodeJS.Timeout | null = null;

function broadcastAgentControl(): void {
  const payload = {
    active: agentControlActive,
    webContentsId: agentControlTargetWcId,
    takenOver: agentControlTakenOver,
    request: agentControlRequestType,
  };
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("agent-control-state", payload);
  for (const win of childWindows) {
    if (!win.isDestroyed()) win.webContents.send("agent-control-state", payload);
  }
}

function settleAgentControlRequest(granted: boolean, reason: string): void {
  const respond = agentControlRequestRespond;
  agentControlRequestRespond = null;
  agentControlRequestType = null;
  if (agentControlRequestTimer) {
    clearTimeout(agentControlRequestTimer);
    agentControlRequestTimer = null;
  }
  if (respond) respond(granted, reason);
}

ipcMain.on("agent-control-request-respond", (_event, granted: boolean) => {
  const req = agentControlRequestType;
  settleAgentControlRequest(!!granted, granted ? "" : "declined");
  // The USER'S answer is the only thing that changes control.
  if (granted && req === "user_takeover") agentControlTakenOver = true;
  else if (granted && req === "agent_control") agentControlTakenOver = false;
  broadcastAgentControl();
});

/** Intercept an Encre.agentControlRequest from the backend tool: surface it
 *  on the pill of every window and answer the tool once the user responds
 *  (60s silence = denied, the tool must never hang forever). */
function handleAgentControlRequest(ws: WS, msg: { id?: number; params?: { request?: string } }): void {
  const request = String(msg?.params?.request || "");
  if (request !== "user_takeover" && request !== "agent_control") {
    ws.send(JSON.stringify({ id: msg.id, error: { code: -32000, message: "request must be 'user_takeover' or 'agent_control'" } }));
    return;
  }
  if (!agentControlActive || agentControlRequestRespond) {
    ws.send(JSON.stringify({ id: msg.id, result: { granted: false, reason: agentControlRequestRespond ? "busy" : "not-operating" } }));
    return;
  }
  agentControlRequestType = request;
  agentControlRequestRespond = (granted: boolean, reason: string) => {
    ws.send(JSON.stringify({ id: msg.id, result: { granted, reason } }));
  };
  broadcastAgentControl();
  agentControlRequestTimer = setTimeout(() => {
    settleAgentControlRequest(false, "timeout");
    broadcastAgentControl();
  }, 60_000);
}

ipcMain.on("agent-control", (_event, active: boolean) => {
  const next = !!active;
  // A fresh operation (false -> true transition) always starts in the
  // operating phase. A re-sent "true" while already active is NOT a fresh
  // operation — it must never strip a user takeover that happened in
  // between (the "it steals control back" bug).
  if (next && !agentControlActive) agentControlTakenOver = false;
  agentControlActive = next;
  if (!agentControlActive) {
    agentControlTargetWcId = null;
    agentControlTakenOver = false;
  }
  broadcastAgentControl();
});

// The user clicked "take over" (or "hand back") in one of the windows.
ipcMain.on("agent-takeover", (_event, taken: boolean) => {
  agentControlTakenOver = !!taken;
  broadcastAgentControl();
});

/**
 * Force a browser guest to behave like a plain, light-mode desktop browser,
 * independently of the app's own theme and of Electron's stock identity.
 *
 * Light mode: `ipcMain.on("tray-theme")` drives `nativeTheme.themeSource`,
 * which flips the OS-level `prefers-color-scheme` for every renderer — browser
 * guests included. Sites then select their *dark* stylesheet, and any whose
 * dark branch is half-finished render black text on a black page; once our
 * injected `color-scheme: light` flips the UA canvas the other way, other
 * elements come out white-on-white instead. Overriding the media feature for
 * the guest alone keeps the app dark while every page (and every
 * locally-opened file preview) stays a genuinely light page.
 *
 * Desktop identity: the session-level UA already covers request headers, but
 * `navigator.userAgentData` is built from Electron's own brand list, so sites
 * that use the modern API would still sniff a non-desktop client and could hand
 * back their mobile build.
 *
 * Both need CDP — Electron exposes no per-WebContents API for either.
 */
function applyBrowserGuestEmulation(wc: Electron.WebContents): void {
  if (!wc || wc.isDestroyed()) return;
  try {
    if (!wc.debugger.isAttached()) wc.debugger.attach("1.3");
  } catch {
    return; // Debugger unavailable for this guest — nothing else we can do.
  }
  const send = (method: string, params: Record<string, unknown>) => {
    try { wc.debugger.sendCommand(method, params).catch(() => {}); } catch {}
  };
  // 1. Colour scheme the page is allowed to see.  "light" is the historical
  //    pin (no site may pick its dark stylesheet); switching it off clears the
  //    override so the guest follows the app theme like any other tab.
  send("Emulation.setEmulatedMedia", _browserOpts.forceLightPage
    ? { media: "", features: [{ name: "prefers-color-scheme", value: "light" }] }
    : { media: "", features: [] });
  // 2. Chromium's own auto dark-mode inversion would wreck legibility as well.
  send("Emulation.setAutoDarkModeOverride", { enabled: false });
  // 3. Browsing identity for scripts, client hints and server-side sniffing.
  const id = browserIdentity();
  send("Emulation.setUserAgentOverride", {
    userAgent: id.ua,
    acceptLanguage: _browserAcceptLanguage,
    platform: id.platform,
    userAgentMetadata: id.uaData,
  });
}

app.on("web-contents-created", (_event, wc) => {
  wc.on("destroyed", () => { if (cdpRelays.has(wc.id)) stopCdpRelay(wc.id); });
  if (wc.getType() === "window") {
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
    // Any in-page navigation to a web URL (programmatic location change, etc.)
    // is bounced back to the renderer's shared URL router so the user's
    // default_link_behavior (ESD sub-app vs. system browser) always applies.
    // file:// and internal schemes keep their native behavior.
    wc.on("will-navigate", (details) => {
      const url = details.url || "";
      if (!/^https?:\/\//i.test(url)) return;
      details.preventDefault();
      wc.send("open-routed-url", url);
    });
  }
  if (wc.getType() === "webview") {
    // Fallback for attach paths that skipped `will-attach-webview` (e.g. a
    // guest restored before the host hook ran): make sure the session hooks
    // exist before this guest's navigation commits.
    ensureBrowserSession();
    wc.setWindowOpenHandler((details) => {
      // Blocking popups means never offering the tab: the renderer only learns
      // about a window request if this handler forwards it.
      if (_browserOpts.blockPopups) return { action: "deny" };
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

    // Lock every browser guest (the ESD sub-app tab and the session sidebar
    // browser tab) to 100% zoom and swallow its zoom hotkeys — the same policy
    // the main window and every child window already apply. A webview is its
    // own webContents, so the main window's lock does NOT cover it: without
    // this a Ctrl+- / Ctrl+wheel inside a page permanently rescales just that
    // guest, which is why the very same page showed up huge in one view and
    // tiny in another (on top of the width-derived factor the renderer used).
    const lockZoom = () => {
      // The configured factor, not the constant 1: pages still cannot rescale
      // the guest by themselves (visual zoom stays pinned below), the user's
      // setting is simply the one number everything re-asserts.
      const factor = _browserOpts.zoomFactor > 0 ? _browserOpts.zoomFactor : 0.75;
      try { wc.setZoomFactor(factor); } catch {}
      try { wc.setVisualZoomLevelLimits(1, 1).catch(() => {}); } catch {}
    };
    // Proximity-revealed scrollbars for guest documents.  A webview is its
    // own document: the host renderer's reveal listener (ui/scrollbar-reveal.ts)
    // cannot reach it, so the same behaviour is injected here.  The scrollbar
    // CSS goes through `insertCSS` with user origin (survives page CSP); the
    // pointer-proximity logic comes from the standalone guest script.
    const GUEST_REVEAL_CSS = `
      ::-webkit-scrollbar { width: 4px; height: 4px; background: transparent; }
      ::-webkit-scrollbar-button { display: none; }
      ::-webkit-scrollbar-corner { background: transparent; }
      ::-webkit-scrollbar-track { background: transparent; }
      ::-webkit-scrollbar-thumb { background: transparent; border-radius: 10px; }
      [data-scroll-reveal]::-webkit-scrollbar-thumb { background: rgba(136, 140, 152, 0.55); }
      [data-scroll-reveal]::-webkit-scrollbar-thumb:hover { background: rgba(110, 114, 126, 0.85); }
    `;
    let guestRevealJs = "";
    try {
      guestRevealJs = fs.readFileSync(
        path.join(__dirname, "..", "renderer", "guest-scrollbar-reveal.js"),
        "utf8"
      );
    } catch {}
    const injectGuestReveal = () => {
      try {
        wc.insertCSS(GUEST_REVEAL_CSS, { cssOrigin: "user" });
      } catch {}
      if (guestRevealJs) {
        try { wc.executeJavaScript(guestRevealJs, true); } catch {}
      }
    };

    // Both invariants need re-asserting per document: Chromium replays any
    // per-site zoom it remembered on commit, and the light-mode override has to
    // survive a CDP relay detach.
    const enforceBrowserView = () => {
      lockZoom();
      applyBrowserGuestEmulation(wc);
    };
    enforceBrowserView();
    wc.on("dom-ready", () => {
      enforceBrowserView();
      // Inject once per document: dom-ready fires exactly once per page load,
      // so the reveal CSS never stacks across the load/navigate events below.
      injectGuestReveal();
    });
    wc.on("did-finish-load", enforceBrowserView);
    wc.on("did-navigate", enforceBrowserView);
    wc.on("before-input-event", (event, input) => {
      const key = (input.key || "").toLowerCase();
      const isZoomHotkey =
        input.control &&
        (key === "-" || key === "_" || key === "+" || key === "=" || key === "0");
      if (isZoomHotkey) {
        event.preventDefault();
        lockZoom();
      }
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
    raiseMainWindow();
  });
}

// Disable the OS spell checker (Windows / macOS / Linux dictionaries and
// their red squiggles) for EVERY webContents in the app: all BrowserWindows
// get `spellcheck: false` in their webPreferences, and <webview> guests get
// it forced off at attach time.  Encre renders its own editor chrome — no
// native spellcheck anywhere.
app.on("web-contents-created", (_event, contents) => {
  contents.on("will-attach-webview", (_event, webPreferences) => {
    webPreferences.spellcheck = false;
    // Fires before the guest exists: initializing the browser session here
    // guarantees the webRequest/permission/cookie hooks are installed before
    // the guest's first navigation, while keeping the expensive partition
    // init off the startup path entirely.
    if (webPreferences.partition === BROWSER_PARTITION) ensureBrowserSession();
  });
});

// Application entry: runs once Electron is ready.
app.whenReady().then(async () => {
  // Pin the app to light so native browser widgets (e.g. the <input type="date">
  // calendar popup) and any `prefers-color-scheme` query in the UI render light
  // regardless of the OS-level light/dark setting. Encre's default appearance is
  // light; an explicitly chosen theme is applied by the renderer on top.
  nativeTheme.themeSource = "light";

  // Main-process event-loop stall watchdog: a frameless window's dragging
  // and input go through the browser process, so any main-thread block shows
  // up as "the window cannot be dragged / not responding" -- and the queued
  // burst of geometry changes after the block is exactly when Chromium's
  // surface desync (stale UI fragments) manifests. Log any block over
  // ~700ms; if these correlate with the freeze episodes the blocker is in
  // the main process, if the loop never stalls while the window still
  // freezes the blocker is the renderer or the GPU process.
  let healBeat = Date.now();
  setInterval(() => {
    const now = Date.now();
    const stalled = now - healBeat;
    healBeat = now;
    if (stalled > 700) {
      console.warn(`[watchdog] main event loop blocked ~${stalled}ms`);
    }
  }, 500).unref();

  // Pin the AppUserModelID so Windows taskbar/notification icons attach to Encre
  // (and not the generic electron.exe icon).
  app.setAppUserModelId("com.encre.desktop");
  if (process.platform === "win32") {
    // Register the AUMID with Explorer: without a registered display name/icon
    // the taskbar right-click flyout falls back to electron.exe's version info
    // and shows the generic "Electron" entry.  Unpackaged dev runs have no
    // installer shortcut to do this registration, so write it to HKCU directly
    // (same mechanism Chrome uses for PWA windows).  Packaged installs get
    // shortcut-based registration from the NSIS installer; these values are
    // idempotent and harmless there.  HKCU only -- no elevation required.
    // Fire-and-forget async: a synchronous reg.exe spawn at whenReady delayed
    // the first window paint wherever endpoint-security software hooks
    // process creation. The registration is cosmetic and idempotent, so it
    // never needs to block startup.
    void (async () => {
      try {
        const aumidKey = "HKCU\\Software\\Classes\\AppUserModelId\\com.encre.desktop";
        await promisify(execFile)(
          "reg", ["add", aumidKey, "/v", "DisplayName", "/t", "REG_SZ", "/d", "Encre Agent Desktop", "/f"],
          { timeout: 5000 },
        );
        // IconUri must be a plain-disk file Explorer can read; paths inside
        // app.asar are virtual and unreadable to Explorer, so only register it
        // for unpackaged runs (packaged builds use the exe's embedded icon).
        const aumidIconPath = resolveAppIconPath();
        if (!app.isPackaged && fs.existsSync(aumidIconPath)) {
          await promisify(execFile)(
            "reg", ["add", aumidKey, "/v", "IconUri", "/t", "REG_SZ", "/d", aumidIconPath, "/f"],
            { timeout: 5000 },
          );
        }
      } catch { /* best-effort: Explorer falls back to exe metadata */ }
    })();
  }

  // Plugin assets: renderer → main → loopback backend. Keeping this off the
  // direct HTTP path means every renderer↔backend byte besides this internal
  // proxy travels over the encrypted WebSocket.
  protocol.handle(PLUGIN_ASSET_SCHEME, async (request) => {
    try {
      const url = new URL(request.url);
      if (url.host !== "assets" || !url.pathname.startsWith("/plugin-assets/")) {
        return new Response("Not found", { status: 404 });
      }
      const res = await net.fetch(
        `http://127.0.0.1:${WS_PORT}${url.pathname}${url.search}`,
        { method: "GET" },
      );
      const headers = new Headers(res.headers);
      // The renderer document is file:// (opaque origin); the proxied
      // response must be readable by it.
      headers.set("Access-Control-Allow-Origin", "*");
      return new Response(res.body, { status: res.status, headers });
    } catch {
      return new Response("Not found", { status: 404 });
    }
  });

  // Register local:// protocol to serve local files (for notification media, etc.)
  // Streams instead of reading the whole file: chat thumbnails, screenshots and
  // videos used to be fully buffered on the main thread, freezing every window
  // until the copy finished. Range support also lets <video> seek.
  protocol.handle("local", async (request) => {
    const rawPath = decodeURIComponent(request.url.slice("local://".length)).replace(/^\//, "");
    const filePath = path.resolve(rawPath);

    try {
      const stat = await fs.promises.stat(filePath);
      if (!stat.isFile()) {
        return new Response("File not found", { status: 404 });
      }

      const ext = path.extname(filePath).toLowerCase();
      const mime: Record<string, string> = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
        ".mp4": "video/mp4", ".webm": "video/webm", ".ogg": "video/ogg",
      };
      const contentType = mime[ext] || "application/octet-stream";
      const headers: Record<string, string> = {
        "Content-Type": contentType,
        "Accept-Ranges": "bytes",
        "Content-Length": String(stat.size),
      };

      const range = request.headers.get("range");
      const match = range ? /^bytes=(\d*)-(\d*)$/.exec(range.trim()) : null;
      if (match) {
        const size = stat.size;
        let start: number;
        let end: number;
        if (!match[1] && match[2]) {
          // Suffix range: last N bytes.
          start = Math.max(0, size - Number(match[2]));
          end = size - 1;
        } else {
          start = Number(match[1]);
          end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
        }
        if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= size) {
          return new Response(null, {
            status: 416,
            headers: { "Content-Range": `bytes */${size}` },
          });
        }
        headers["Content-Range"] = `bytes ${start}-${end}/${size}`;
        headers["Content-Length"] = String(end - start + 1);
        return new Response(Readable.toWeb(fs.createReadStream(filePath, { start, end })) as any, {
          status: 206,
          headers,
        });
      }

      return new Response(Readable.toWeb(fs.createReadStream(filePath)) as any, {
        status: 200,
        headers,
      });
    } catch {
      return new Response("File not found", { status: 404 });
    }
  });

  // Set up encrypted browser cookie store.
  // Eager + parallel: the browser session loads right at startup, alongside
  // window/backend/tray creation. A threadpool-side pre-clean first shrinks
  // oversized cache dirs so Chromium's partition init rebuilds its indexes
  // over a few MB instead of the 330MB that froze startup for ~20s.
  ensureBrowserSession();

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
    applyOsAutoStart(true);
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
    createWindow({ immediate: true });
  } else {
    raiseMainWindow();
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
// Detached fire-and-forget only — an "exit" handler must never block final
// teardown with synchronous taskkill/exec sweeps.
process.on("exit", () => {
  try {
    if (process.platform === "win32") {
      spawn("taskkill", ["/F", "/IM", "encre-server.exe"], {
        detached: true, stdio: "ignore", windowsHide: true,
      }).unref();
      spawn("taskkill", ["/F", "/FI", `PID ne ${process.pid}`, "/IM", "Encre.exe"], {
        detached: true, stdio: "ignore", windowsHide: true,
      }).unref();
    } else {
      spawn("pkill", ["-f", "encre-server"], { detached: true, stdio: "ignore" }).unref();
      spawn("pkill", ["-f", "Encre"], { detached: true, stdio: "ignore" }).unref();
    }
  } catch { /* best effort */ }
});


