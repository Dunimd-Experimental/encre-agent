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
 * Electron preload script for the Encre renderer.
 *
 * Runs in an isolated context with access to Node/Electron APIs and safely
 * exposes a curated `electronAPI` object onto `window` via contextBridge.
 * Every method here is a thin wrapper around `ipcRenderer.invoke` (request/
 * response) or `ipcRenderer.send` (fire-and-forget), with strongly typed
 * signatures so the renderer never touches Electron internals directly.
 * This keeps `nodeIntegration` disabled and `contextIsolation` enabled.
 */

import { contextBridge, ipcRenderer } from "electron";

// Expose the curated, safe API surface to the renderer under `window.electronAPI`.
contextBridge.exposeInMainWorld("electronAPI", {
  getServerPort: (): Promise<number> => ipcRenderer.invoke("getServerPort"),

  pickFiles: (): Promise<string[]> => ipcRenderer.invoke("pickFiles"),
  pickFolder: (): Promise<string | null> => ipcRenderer.invoke("pickFolder"),
  pickDirectory: (): Promise<string | null> => ipcRenderer.invoke("pickDirectory"),
  readFile: (filePath: string) =>
    ipcRenderer.invoke("readFile", filePath),
  statFile: (filePath: string): Promise<{ size: number; mtime: number; isDirectory: boolean } | null> =>
    ipcRenderer.invoke("statFile", filePath),
  readDirectory: (dirPath: string) =>
    ipcRenderer.invoke("readDirectory", dirPath),

  readFileBase64: (filePath: string) =>
    ipcRenderer.invoke("readFileBase64", filePath),

  writeFile: (filePath: string, data: string): Promise<boolean> =>
    ipcRenderer.invoke("writeFile", filePath, data),

  // File clipboard (cross-application copy/paste via Windows CF_HDROP)
  copyFilesToClipboard: (filePaths: string[]): Promise<boolean> =>
    ipcRenderer.invoke("clipboard:copy-files", filePaths),
  readFilesFromClipboard: (): Promise<string[]> =>
    ipcRenderer.invoke("clipboard:read-files"),
  pasteFiles: (sourcePaths: string[], targetDir: string): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("fs:paste-files", sourcePaths, targetDir),

  // File-tree operations: create file/folder, delete (move to trash).
  createFile: (parentDir: string, name: string): Promise<{ success: boolean; error?: string; path?: string; name?: string }> =>
    ipcRenderer.invoke("fs:create-file", parentDir, name),
  createFolder: (parentDir: string, name: string): Promise<{ success: boolean; error?: string; path?: string; name?: string }> =>
    ipcRenderer.invoke("fs:create-folder", parentDir, name),
  deletePath: (targetPath: string): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("fs:delete", targetPath),

  getAppPath: (): Promise<string> => ipcRenderer.invoke("getAppPath"),

  // Crypto keyfile access for transport encryption
  readKeyfile: (): Promise<ArrayBuffer | null> =>
    ipcRenderer.invoke("readKeyfile"),

  readKeyWrapMaterial: (): Promise<{ fingerprint: string; appSecret: string }> =>
    ipcRenderer.invoke("readKeyWrapMaterial"),

  adminFetch: (urlPath: string): Promise<{ ok: boolean; status: number; body?: string; error?: string }> =>
    ipcRenderer.invoke("adminFetch", urlPath),

  // Window controls
  windowMinimize: (): Promise<void> => ipcRenderer.invoke("window-minimize"),
  windowMaximize: (): Promise<void> => ipcRenderer.invoke("window-maximize"),
  windowClose: (): Promise<void> => ipcRenderer.invoke("window-close"),
  windowIsMaximized: (): Promise<boolean> =>
    ipcRenderer.invoke("window-is-maximized"),
  onWindowMaximizeChange: (cb: (maximized: boolean) => void): void => {
    ipcRenderer.on("window-maximized-state", (_event, maximized: boolean) => cb(maximized));
  },

  toggleDevTools: (): Promise<void> =>
    ipcRenderer.invoke("toggle-devtools"),

  // Terminal
  terminalSpawn: (shell?: string, shellArgs?: string[], cwd?: string): Promise<{ id?: number; error?: string }> =>
    ipcRenderer.invoke("terminal:spawn", shell, shellArgs, cwd),
  terminalWrite: (id: number, data: string): Promise<void> =>
    ipcRenderer.invoke("terminal:write", id, data),
  terminalResize: (id: number, cols: number, rows: number): Promise<void> =>
    ipcRenderer.invoke("terminal:resize", id, cols, rows),
  terminalKill: (id: number): Promise<void> =>
    ipcRenderer.invoke("terminal:kill", id),
  terminalListShells: (): Promise<Array<{ name: string; path: string; args?: string[] }>> =>
    ipcRenderer.invoke("terminal:listShells"),
  onTerminalData: (callback: (data: { id: number; data: string }) => void) => {
    const handler = (_event: any, data: any) => callback(data);
    ipcRenderer.on("terminal:data", handler);
    return () => { ipcRenderer.removeListener("terminal:data", handler); };
  },
  onTerminalExit: (callback: (data: { id: number }) => void) => {
    const handler = (_event: any, data: any) => callback(data);
    ipcRenderer.on("terminal:exit", handler);
    return () => { ipcRenderer.removeListener("terminal:exit", handler); };
  },

  // Files
  listDirectory: (dirPath: string): Promise<Array<{ name: string; isDirectory: boolean; isFile: boolean }>> =>
    ipcRenderer.invoke("listDirectory", dirPath),
  getDrives: (): Promise<string[]> =>
    ipcRenderer.invoke("getDrives"),

  // Git
  gitStatus: (repoPath: string): Promise<{ output?: string; error?: string }> =>
    ipcRenderer.invoke("gitStatus", repoPath),
  gitDiff: (repoPath: string, filePath?: string): Promise<{ output?: string; error?: string }> =>
    ipcRenderer.invoke("gitDiff", repoPath, filePath),
  gitDiffEx: (repoPath: string, filter: string, filePath?: string): Promise<{ output?: string; error?: string }> =>
    ipcRenderer.invoke("gitDiffEx", repoPath, filter, filePath),
  gitCommit: (repoPath: string, message: string): Promise<{ output?: string; error?: string }> =>
    ipcRenderer.invoke("gitCommit", repoPath, message),
  gitPush: (repoPath: string): Promise<{ output?: string; error?: string }> =>
    ipcRenderer.invoke("gitPush", repoPath),
  gitCreatePr: (repoPath: string): Promise<{ output?: string; error?: string; compare_url?: string }> =>
    ipcRenderer.invoke("gitCreatePr", repoPath),
  gitPull: (repoPath: string): Promise<{ output?: string; error?: string }> =>
    ipcRenderer.invoke("gitPull", repoPath),
  gitBehind: (repoPath: string): Promise<{ behind: number; error?: string }> =>
    ipcRenderer.invoke("gitBehind", repoPath),

  // Service
  getServiceStatus: (): Promise<{ running: boolean; pid: number | null; port: number; error: string | null }> =>
    ipcRenderer.invoke("getServiceStatus"),
  restartService: (): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("restartService"),
  openFolder: (folderPath: string): Promise<boolean> =>
    ipcRenderer.invoke("openFolder", folderPath),

  // Auto-start
  getAutoStart: (): Promise<boolean> =>
    ipcRenderer.invoke("getAutoStart"),
  setAutoStart: (enabled: boolean): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("setAutoStart", enabled),

  // Tray
  trayLocaleUpdate: (locale: string): void => {
    ipcRenderer.send("tray-locale", locale);
  },
  // In-app browser Accept-Language
  browserLanguageUpdate: (locale: string): void => {
    ipcRenderer.send("browser-language", locale);
  },
  // In-app browser credential auto-save (passwords & API keys)
  browserAutosaveUpdate: (enabled: boolean): void => {
    ipcRenderer.send("browser-autosave", enabled);
  },
  // In-app browser switches (Settings -> Assist -> Browser)
  browserOptionsUpdate: (opts: Record<string, unknown>): void => {
    ipcRenderer.send("browser-options", opts);
  },
  // Wipe cookies and site storage only -- bookmarks and history survive.
  browserClearSessionData: (): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("browser:clear-session-data"),
  browserSaveCredential: (entry: { origin: string; username: string; password: string; kind: string; url?: string; created_at?: number }): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("browser:save-credential", entry),
  trayThemeUpdate: (themePreference: string): void => {
    ipcRenderer.send("tray-theme", themePreference);
  },
  traySkinUpdate: (skin: string): void => {
    ipcRenderer.send("tray-skin", skin);
  },
  trayPopupAction: (action: string | null, sessionId: string | null): void => {
    ipcRenderer.send("tray-popup-action", { action, sessionId });
  },
  traySessionsUpdate: (sessions: any[]): void => {
    ipcRenderer.send("tray-sessions-update", sessions);
  },
  traySessionsBothUpdate: (payload: { normal: any[]; iwork: any[] }): void => {
    ipcRenderer.send("tray-sessions-both", payload);
  },
  trayModeUpdate: (mode: string): void => {
    ipcRenderer.send("tray-mode", mode);
  },
  onTrayData: (callback: (data: {
    sessions: any[];
    sessionsNormal?: any[];
    sessionsIwork?: any[];
    activeMode?: string;
    locale: string;
    theme: string;
  }) => void): (() => void) => {
    const handler = (_event: any, data: any) => callback(data);
    ipcRenderer.on("tray-data", handler);
    return () => { ipcRenderer.removeListener("tray-data", handler); };
  },
  onSwitchSession: (callback: (sessionId: string) => void): (() => void) => {
    const handler = (_event: any, sessionId: string) => callback(sessionId);
    ipcRenderer.on("switch-session", handler);
    return () => { ipcRenderer.removeListener("switch-session", handler); };
  },
  onSwitchWorkspace: (callback: (path: string) => void): (() => void) => {
    const handler = (_event: any, path: string) => callback(path);
    ipcRenderer.on("switch-workspace", handler);
    return () => { ipcRenderer.removeListener("switch-workspace", handler); };
  },

  // Quit confirmation: main asks the renderer to check running tasks before
  // quitting; the renderer replies with the user's decision.
  onQuitRequest: (callback: () => void): (() => void) => {
    const handler = () => callback();
    ipcRenderer.on("quit-request", handler);
    return () => { ipcRenderer.removeListener("quit-request", handler); };
  },
  respondQuitConfirm: (allowed: boolean): void => {
    ipcRenderer.send("quit-confirm-response", allowed);
  },

  // Browser
  browserClearData: (): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("browser:clear-data"),

  // beforeunload guard: bind will-prevent-unload on a guest webContents and
  // route the "leave without saving?" decision through the renderer's dialog.
  browserBindUnloadGuard: (webContentsId: number): Promise<void> =>
    ipcRenderer.invoke("browser:bind-unload-guard", webContentsId),
  browserRespondUnload: (webContentsId: number, allow: boolean): Promise<void> =>
    ipcRenderer.invoke("browser:respond-unload", webContentsId, allow),
  onBrowserUnloadRequest: (callback: (webContentsId: number) => void): void => {
    ipcRenderer.on("browser:unload-request", (_e, wcId) => callback(wcId));
  },

  // Browser bookmarks
  getBookmarks: (): Promise<any> =>
    ipcRenderer.invoke("browser:get-bookmarks"),
  setBookmarks: (data: any): Promise<{ success: boolean }> =>
    ipcRenderer.invoke("browser:set-bookmarks", data),
  addBookmark: (entry: { url: string; title: string }): Promise<{ success: boolean }> =>
    ipcRenderer.invoke("browser:add-bookmark", entry),
  removeBookmark: (url: string): Promise<{ success: boolean }> =>
    ipcRenderer.invoke("browser:remove-bookmark", url),

  // Browser history
  getHistory: (): Promise<any[]> =>
    ipcRenderer.invoke("browser:get-history"),
  addHistoryEntry: (entry: { url: string; title: string }): Promise<{ success: boolean }> =>
    ipcRenderer.invoke("browser:add-history-entry", entry),
  clearHistory: (): Promise<{ success: boolean }> =>
    ipcRenderer.invoke("browser:clear-history"),

  exportFile: (options: { content: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }): Promise<{ success: boolean; canceled?: boolean; filePath?: string; error?: string }> =>
    ipcRenderer.invoke("browser:export-file", options),
  exportBinary: (options: { base64: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }): Promise<{ success: boolean; canceled?: boolean; filePath?: string; error?: string }> =>
    ipcRenderer.invoke("browser:export-binary", options),
  copyFileTo: (options: { sourcePath: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }): Promise<{ success: boolean; canceled?: boolean; filePath?: string; error?: string }> =>
    ipcRenderer.invoke("browser:copy-file-to", options),

  // Browser import/export
  detectBrowsers: (): Promise<Array<{ id: string; name: string; profilePath: string; hasBookmarks: boolean; hasCookies: boolean; hasHistory: boolean }>> =>
    ipcRenderer.invoke("browser:detect-browsers"),
  importBrowserData: (browserId: string, profilePath: string): Promise<{ success: boolean; data?: any; error?: string }> =>
    ipcRenderer.invoke("browser:import-data", browserId, profilePath),
  saveImportedBrowserData: (data: { bookmarks?: any; history?: any[]; cookies?: any[] }): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("browser:save-imported-data", data),
  exportAllBrowserData: (): Promise<{ success: boolean; error?: string }> =>
    ipcRenderer.invoke("browser:export-all"),

  // Open external URLs in system browser
  openExternal: (url: string): Promise<boolean> =>
    ipcRenderer.invoke("open-external", url),

  getAppVersions: (): Promise<{ desktop: string; agent: string }> =>
    ipcRenderer.invoke("getAppVersions"),

  getLicenseContent: (): Promise<string> =>
    ipcRenderer.invoke("getLicenseContent"),

  getDocumentContent: (docId: string, region?: string): Promise<string> =>
    ipcRenderer.invoke("getDocumentContent", docId, region),

  getDocRegion: (): Promise<string> => ipcRenderer.invoke("getDocRegion"),

  setDocRegion: (region: string): Promise<void> => ipcRenderer.invoke("setDocRegion", region),

  openChildWindow: (view: string, label: string): Promise<void> =>
    ipcRenderer.invoke("openChildWindow", view, label),

  onChildAddTab: (callback: (view: string, label: string) => void): (() => void) => {
    const handler = (_event: any, view: string, label: string) => callback(view, label);
    ipcRenderer.on("child-window:add-tab", handler);
    return () => { ipcRenderer.removeListener("child-window:add-tab", handler); };
  },

  forwardToChild: (channel: string, ...args: any[]): void => {
    ipcRenderer.send("forward-to-child", channel, ...args);
  },

  onChildEvent: (channel: string, callback: (data: any) => void): (() => void) => {
    const handler = (_event: any, data: any) => callback(data);
    ipcRenderer.on(channel, handler);
    return () => { ipcRenderer.removeListener(channel, handler); };
  },

  openSettings: (panel: string): Promise<void> =>
    ipcRenderer.invoke("open-settings", panel),

  openInfoHtml: (html: string): Promise<string | null> =>
    ipcRenderer.invoke("openInfoHtml", html),

  onNewWindow: (callback: (url: string, wcId: number) => void): (() => void) => {
    const handler = (_event: any, url: string, wcId: number) => callback(url, wcId);
    ipcRenderer.on("browser:new-window", handler);
    return () => { ipcRenderer.removeListener("browser:new-window", handler); };
  },

  onOpenRoutedUrl: (callback: (url: string) => void): (() => void) => {
    const handler = (_event: any, url: string) => callback(url);
    ipcRenderer.on("open-routed-url", handler);
    return () => { ipcRenderer.removeListener("open-routed-url", handler); };
  },

  openLogs: (): Promise<void> =>
    ipcRenderer.invoke("openLogs"),

  getDiagnostics: (): Promise<{
    versions: { desktop: string; agent: string };
    dataDir: string;
    logFile: string;
    recentLogs: string[];
  }> => ipcRenderer.invoke("getDiagnostics"),

  getLogs: (filters: {
    fromDate?: string;
    toDate?: string;
    offset?: number;
    limit?: number;
  }): Promise<{
    entries: { timestamp: string; level: string; source: string; message: string }[];
    total: number;
    fileExists: boolean;
    rawLines: number;
  }> => ipcRenderer.invoke("getLogs", filters),
  getLogFileInfo: (): Promise<{ exists: boolean; size: number; mtimeMs: number }> =>
    ipcRenderer.invoke("getLogFileInfo"),

  clearLogs: (): Promise<{ success: boolean }> => ipcRenderer.invoke("clearLogs"),

  setWinKeyCapture: (enabled: boolean): Promise<void> => ipcRenderer.invoke("setWinKeyCapture", enabled),

  onRestartProgress: (callback: (data: { progress: number }) => void): (() => void) => {
    const handler = (_event: any, data: any) => callback(data);
    ipcRenderer.on("restart-progress", handler);
    return () => { ipcRenderer.removeListener("restart-progress", handler); };
  },

  // Browser CDP
  getCdpPort: (webContentsId: number): Promise<number> =>
    ipcRenderer.invoke("browser:get-cdp-port", webContentsId),
  registerCdpWebview: (webContentsId: number): Promise<number> =>
    ipcRenderer.invoke("browser:register-cdp-webview", webContentsId),
  unregisterCdpWebview: (webContentsId: number): Promise<void> =>
    ipcRenderer.invoke("browser:unregister-cdp-webview", webContentsId),

  // Agent control overlay broadcast (shared by browser / computer tools).
  // While the lock is up every browser view shows the overlay; the
  // taken-over flag additionally gates the CDP relay in main. `request`
  // carries a pending control-change request awaiting the user's answer.
  agentControlUpdate: (active: boolean): void =>
    ipcRenderer.send("agent-control", active),
  setAgentTakeover: (taken: boolean): void =>
    ipcRenderer.send("agent-takeover", taken),
  respondAgentControlRequest: (granted: boolean): void =>
    ipcRenderer.send("agent-control-request-respond", granted),
  onAgentControl: (callback: (state: { active: boolean; webContentsId: number | null; takenOver?: boolean; request?: string | null }) => void): (() => void) => {
    const handler = (_event: any, state: { active: boolean; webContentsId: number | null; takenOver?: boolean; request?: string | null }) => callback(state);
    ipcRenderer.on("agent-control-state", handler);
    return () => { ipcRenderer.removeListener("agent-control-state", handler); };
  },

  // Site info & permissions
  getSiteInfo: (url: string): Promise<{ origin: string; isSecure: boolean; cookieCount: number; permissions: Array<{ name: string; granted: boolean }> }> =>
    ipcRenderer.invoke("browser:get-site-info", url),
  setPermission: (origin: string, permission: string, granted: boolean): Promise<{ success: boolean }> =>
    ipcRenderer.invoke("browser:set-permission", origin, permission, granted),
  listPermissionGrants: (): Promise<Array<{ origin: string; permission: string; granted: boolean }>> =>
    ipcRenderer.invoke("browser:list-permissions"),
  getCookiesForOrigin: (url: string): Promise<{ cookies: Array<{ name: string; value: string; domain: string; path: string; secure: boolean; httpOnly: boolean; sameSite: string; expirationDate?: number }> }> =>
    ipcRenderer.invoke("browser:get-cookies-for-origin", url),
});
