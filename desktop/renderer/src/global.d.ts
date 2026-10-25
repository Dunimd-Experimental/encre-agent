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
 * Global type ambient declarations for the renderer.
 *
 * Declares the shapes exposed on `window` by the Electron preload bridge
 * (`electronAPI`) and the `lucide` icon runtime, plus ambient interfaces used
 * across the renderer and a module shim for `markdown-it`.
 */

interface DirEntry {
  name: string;
  isDirectory: boolean;
  isFile: boolean;
}

/** Result of reading a single file through the Electron bridge. */
interface FileReadResult {
  content: string;
  mime_type: string;
  size: number;
  is_binary: boolean;
}

/** The surface of the Electron preload bridge exposed as `window.electronAPI`. */
interface ElectronAPI {
  getServerPort(): Promise<number>;
  pickFiles(): Promise<string[]>;
  pickDirectory(): Promise<string | null>;
  readFile(filePath: string): Promise<FileReadResult>;
  statFile(filePath: string): Promise<{ size: number; mtime: number; isDirectory: boolean } | null>;
  readFileBase64(filePath: string): Promise<{ data: string; mime_type: string } | null>;
  readDirectory(dirPath: string): Promise<{ path: string; name: string } | null>;
  writeFile(filePath: string, data: string): Promise<boolean>;
  copyFilesToClipboard(filePaths: string[]): Promise<boolean>;
  readFilesFromClipboard(): Promise<string[]>;
  pasteFiles(sourcePaths: string[], targetDir: string): Promise<{ success: boolean; error?: string }>;
  createFile(parentDir: string, name: string): Promise<{ success: boolean; error?: string; path?: string; name?: string }>;
  createFolder(parentDir: string, name: string): Promise<{ success: boolean; error?: string; path?: string; name?: string }>;
  deletePath(targetPath: string): Promise<{ success: boolean; error?: string }>;
  readKeyfile(): Promise<ArrayBuffer>;
  readKeyWrapMaterial(): Promise<{ fingerprint: string; appSecret: string }>;
  adminFetch(urlPath: string): Promise<{ ok: boolean; status: number; body?: string; error?: string }>;
  getAppPath(): Promise<string>;
  windowMinimize(): Promise<void>;
  windowMaximize(): Promise<void>;
  windowClose(): Promise<void>;
  windowIsMaximized(): Promise<boolean>;
  onWindowMaximizeChange(cb: (maximized: boolean) => void): void;
  toggleDevTools(): Promise<void>;
  terminalSpawn(shell?: string, shellArgs?: string[], cwd?: string): Promise<{ id?: number; error?: string }>;
  terminalWrite(id: number, data: string): Promise<void>;
  terminalResize(id: number, cols: number, rows: number): Promise<void>;
  terminalKill(id: number): Promise<void>;
  terminalListShells(): Promise<Array<{ name: string; path: string; args?: string[] }>>;
  onTerminalData(callback: (data: { id: number; data: string }) => void): () => void;
  onTerminalExit(callback: (data: { id: number }) => void): () => void;
  listDirectory(dirPath: string): Promise<DirEntry[]>;
  getDrives(): Promise<string[]>;
  gitStatus(repoPath: string): Promise<{ output?: string; error?: string }>;
  gitDiff(repoPath: string, filePath?: string): Promise<{ output?: string; error?: string }>;
  gitDiffEx(repoPath: string, filter: string, filePath?: string): Promise<{ output?: string; error?: string }>;
  gitCommit(repoPath: string, message: string): Promise<{ output?: string; error?: string }>;
  gitPush(repoPath: string): Promise<{ output?: string; error?: string }>;
  gitCreatePr(repoPath: string): Promise<{ output?: string; error?: string; compare_url?: string }>;
  gitPull(repoPath: string): Promise<{ output?: string; error?: string }>;
  gitBehind(repoPath: string): Promise<{ behind: number; error?: string }>;
  getServiceStatus(): Promise<{ running: boolean; pid: number | null; port: number; error: string | null }>;
  getAutoStart(): Promise<boolean>;
  setAutoStart(enabled: boolean): Promise<{ success: boolean; error?: string }>;
  trayLocaleUpdate(locale: string): void;
  browserLanguageUpdate(locale: string): void;
  browserAutosaveUpdate(enabled: boolean): void;
  browserOptionsUpdate(opts: Record<string, unknown>): void;
  browserClearSessionData(): Promise<{ success: boolean; error?: string }>;
  browserSaveCredential(entry: { origin: string; username: string; password: string; kind: string; url?: string; created_at?: number }): Promise<{ success: boolean; error?: string }>;
  trayThemeUpdate(themePreference: string): void;
  traySkinUpdate(skin: string): void;
  trayPopupAction(action: string | null, sessionId: string | null): void;
  traySessionsUpdate(sessions: any[]): void;
  traySessionsBothUpdate(payload: { normal: any[]; iwork: any[]; workspaces?: any[] }): void;
  trayModeUpdate(mode: string): void;
  onTrayData(callback: (data: {
    sessions: any[];
    sessionsNormal?: any[];
    sessionsIwork?: any[];
    activeMode?: string;
    locale: string;
    theme: string;
  }) => void): () => void;
  onSwitchSession(callback: (sessionId: string) => void): () => void;
  onSwitchWorkspace(callback: (path: string) => void): () => void;
  onQuitRequest(callback: () => void): () => void;
  respondQuitConfirm(allowed: boolean): void;
  browserClearData(): Promise<{ success: boolean; error?: string }>;
  getBookmarks(): Promise<any>;
  setBookmarks(data: any): Promise<{ success: boolean }>;
  addBookmark(entry: { url: string; title: string }): Promise<{ success: boolean }>;
  removeBookmark(url: string): Promise<{ success: boolean }>;
  getHistory(): Promise<any[]>;
  addHistoryEntry(entry: { url: string; title: string }): Promise<{ success: boolean }>;
  clearHistory(): Promise<{ success: boolean }>;
  exportFile(options: { content: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }): Promise<{ success: boolean; canceled?: boolean; filePath?: string; error?: string }>;
  exportBinary(options: { base64: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }): Promise<{ success: boolean; canceled?: boolean; filePath?: string; error?: string }>;
  copyFileTo(options: { sourcePath: string; defaultName: string; filters: Array<{ name: string; extensions: string[] }> }): Promise<{ success: boolean; canceled?: boolean; filePath?: string; error?: string }>;

  // Browser import/export
  detectBrowsers(): Promise<Array<{ id: string; name: string; profilePath: string; hasBookmarks: boolean; hasCookies: boolean; hasHistory: boolean }>>;
  importBrowserData(browserId: string, profilePath: string): Promise<{ success: boolean; data?: any; error?: string }>;
  saveImportedBrowserData(data: { bookmarks?: any; history?: any[]; cookies?: any[] }): Promise<{ success: boolean; error?: string }>;
  exportAllBrowserData(): Promise<{ success: boolean; error?: string }>;
  openExternal(url: string): Promise<boolean>;
  getAppVersions(): Promise<{ desktop: string; agent: string }>;
  getLicenseContent(): Promise<string>;
  getDocumentContent(docId: string, region?: string): Promise<string>;
  getDocRegion(): Promise<string>;
  setDocRegion(region: string): Promise<void>;
  openChildWindow(view: string, label: string): Promise<void>;
  openInfoHtml(html: string): Promise<string | null>;
  onChildAddTab(callback: (view: string, label: string) => void): () => void;
  forwardToChild(channel: string, ...args: any[]): void;
  onChildEvent(channel: string, callback: (data: any) => void): () => void;
  openSettings(panel: string): Promise<void>;
  onNewWindow(callback: (url: string, wcId: number) => void): () => void;
  onOpenRoutedUrl(callback: (url: string) => void): () => void;
  openLogs(): Promise<void>;
  getDiagnostics(): Promise<{
    versions: { desktop: string; agent: string };
    dataDir: string;
    logFile: string;
    recentLogs: string[];
  }>;
  getLogs(filters: {
    fromDate?: string;
    toDate?: string;
    offset?: number;
    limit?: number;
  }): Promise<{
    entries: { timestamp: string; level: string; source: string; message: string }[];
    total: number;
    fileExists: boolean;
    rawLines: number;
  }>;
  getLogFileInfo(): Promise<{ exists: boolean; size: number; mtimeMs: number }>;
  clearLogs(): Promise<{ success: boolean }>;
  setWinKeyCapture(enabled: boolean): Promise<void>;
  onRestartProgress(callback: (data: { progress: number }) => void): () => void;
  restartService(): Promise<{ success: boolean; error?: string }>;
  openFolder(folderPath: string): Promise<boolean>;

  // Browser CDP
  getCdpPort(webContentsId: number): Promise<number>;
  registerCdpWebview(webContentsId: number): Promise<number>;
  unregisterCdpWebview(webContentsId: number): Promise<void>;

  // Agent control overlay broadcast (shared by browser / computer tools)
  agentControlUpdate(active: boolean): void;
  setAgentTakeover(taken: boolean): void;
  respondAgentControlRequest(granted: boolean): void;
  onAgentControl(callback: (state: { active: boolean; webContentsId: number | null; takenOver?: boolean; request?: string | null }) => void): () => void;

  // Site info & permissions
  getSiteInfo(url: string): Promise<{ origin: string; isSecure: boolean; cookieCount: number; permissions: Array<{ name: string; granted: boolean }> }>;
  setPermission(origin: string, permission: string, granted: boolean): Promise<{ success: boolean }>;
  /** Every origin/permission decision held in the main process this session. */
  listPermissionGrants(): Promise<Array<{ origin: string; permission: string; granted: boolean }>>;
  getCookiesForOrigin(url: string): Promise<{ cookies: Array<{ name: string; value: string; domain: string; path: string; secure: boolean; httpOnly: boolean; sameSite: string; expirationDate?: number }> }>;
}

/** Global `Window` augmentation exposing the Electron bridge and icon runtime. */
interface Window {
  electronAPI?: ElectronAPI;
  lucide?: any;
  __initMediaCards?: (scope?: HTMLElement) => void;
  __initInfoCardMedia?: (elOrId: HTMLElement | string) => void;
  __openInfoHtmlCards?: (scope?: HTMLElement) => void;
  __stopAllMedia?: () => void;
}

declare var monaco: any;
declare class EditorView {}

/** Ambient module shim so `import markdown-it` type-checks without bundled types. */
declare module "markdown-it" {
  const MarkdownIt: any;
  export default MarkdownIt;
}
