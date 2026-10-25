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
 * Session-inner sidebar.
 *
 * The resizable right-hand sidebar shown inside a session: a draggable tab bar
 * hosting an info/summary panel plus dynamically-opened panels (terminal,
 * code editor, file review, agent, �?, each with its own embedded xterm
 * terminal instances. Width is persisted to `localStorage`.
 */

import { getState, subscribe, addAttachments, showToast, openRoutedLink, isEnabled } from "../core/state.js";
import { scheduleRender } from "../core/render_scheduler.js";
import { guardHtml, safeCreateIcons } from "../core/ui_boundary.js";
import { runScheduledRenderNow } from "../core/render_scheduler.js";
import { registerSurface, unregisterSurface } from "../core/scope.js";
import type { ArtifactItem, AttachmentMeta, ReferenceItem } from "../core/types.js";
import { getFileIcon, getFolderIcon, getTerminalIcon } from "../features/files.js";
import { applyTabTooltip, baseName } from "../ui/tab-tooltip.js";
import { t, onLocaleChange, getIntlLocale } from "../features/i18n.js";
import { EALoader } from "../features/ealoader.js";
import { send } from "../core/ws.js";
import { getMonaco, monacoLang, registerEditor, unregisterEditor } from "../features/monaco.js";
import { Dialog } from "../ui/dialog.js";

// Heavy on-demand libraries (xterm, xlsx, mammoth, pptx-to-html) are loaded
// dynamically at first use so they never block startup parsing of bundle.js:
//   - xterm            -> setupTerminalPanel()  (terminal tabs)
//   - mammoth / xlsx   -> _renderOfficeDocx() / _renderOfficeXlsx()
//   - pptx-to-html     -> _renderOfficePptx()
import { renderDiffHtml, setupSplitViewScrollSync, expandDiffToFullFile, type DiffRenderOptions } from "../chat/diff_render.js";
import { showAnchoredDropdown, showContextMenu, hideContextMenu, renderContextMenuItems, type ContextMenuItemSpec } from "../ui/context-menu.js";
import { pluginMenuSpecs } from "../features/plugin_contributions.js";
import { BrowserView } from "../features/browser.js";
import { getDefaultHomepage } from "../features/browser.js";
import { MarkdownPreviewView } from "../chat/markdown_preview.js";
import { MediaViewer } from "../chat/media-viewer.js";
import { isSubappView, mountSubappView, subappTabIcon } from "../features/subapp.js";
import { hideAnimated, showAnimated } from "../ui/animate.js";
import { showSelectionActionBarFor } from "../ui/selection-action-bar.js";

/** True in the Encre Agent Desktop Subwindow (ESD). That window has no session
 *  and no chat, so everything here that is scoped to a session (tab-state
 *  swapping, sidebar visibility) must stay out of its way. */
function inChildWindow(): boolean {
  return document.body.classList.contains("child-mode");
}

/** Definition of a sidebar tab. */
export interface TabDef {
  id: string;            /** unique instance ID: "terminal-1", "browser-1", "editor", "review" */
  type: string;          /** panel type: "terminal" | "editor" | "review" | "browser" | "markdown" */
  label: string;         /** display label shown in the tab bar */
  closable: boolean;
  shellPath?: string;    /** for terminal tabs */
  shellArgs?: string[];  /** for terminal tabs */
  startUrl?: string;     /** for browser tabs */
  favicon?: string;      /** for browser tabs: favicon URL from the page */
  content?: string;      /** for markdown tabs: initial markdown content */
  title?: string;        /** for markdown tabs: preview title */
  filePath?: string;     /** for editor/markdown tabs: file path */
  mediaType?: string;    /** for media tabs: "image" | "video" (when path has no known ext) */
  state?: string;        /** breathing light: "running" | "awaiting_approval" | "permission" | "" */
}

/** One user-configurable tile of the home board.  The whole board (content,
 *  position, per-tile size, visibility) is edited on the drag-and-drop
 *  preview board in Settings → Assist Panels and persisted as the
 *  `assist_tiles` setting. */
export interface AssistTileCfg {
  id: string;            // "terminal" | "editor" | "review" | "browser" | "custom-*"
  icon: string;          // lucide icon name
  label: string;         // display name
  size: "s" | "w" | "h" | "m"; // legacy bucket: "s" = 1×1, "w" = 2×1, "h" = 1×2, "m" = 2×2
  w?: number;            // explicit column span (1..3) — wins over `size` when set
  h?: number;            // explicit row span (1..3) — wins over `size` when set
  cell?: number;         // explicit top-left cell (0..8) on page 0; absent = auto-packed
  hidden?: boolean;      // hidden tiles stay configured but leave the board
}

/** The four built-in launcher ids.  They keep their real panel actions; any
 *  other id is a custom (plugin-shaped) tile that has no panel behind it yet. */
export const DEFAULT_TILE_IDS: readonly string[] = ["terminal", "editor", "review", "browser"];

/** Icon palette offered by the tile editor.  Lucide names only — every entry
 *  must exist in the bundled icon set. */
export const ASSIST_TILE_ICONS: readonly string[] = [
  "terminal", "files", "git-compare-arrows", "globe", "puzzle", "layout-dashboard",
  "folder", "settings", "cpu", "search", "star", "music", "video", "image",
  "calendar", "clock", "calculator", "book-open", "message-circle", "mail",
  "camera", "gamepad-2",
];

/** The shipped board: four standard launchers in a 2×2 square. */
export function defaultHomeTiles(): AssistTileCfg[] {
  return [
    { id: "terminal", icon: "terminal", label: t("sessionInner.tabTerminal"), size: "s" },
    { id: "editor", icon: "folder", label: t("sessionInner.tabEditor"), size: "s" },
    { id: "review", icon: "git-compare-arrows", label: t("sessionInner.tabReview"), size: "s" },
    { id: "browser", icon: "globe", label: t("sessionInner.tabBrowser"), size: "s" },
  ];
}

/** Parse the user-configured board from the `assist_tiles` setting (a JSON
 *  array).  Any malformed entry is dropped; an empty / broken value falls
 *  back to the defaults — the board must never render blank because of one
 *  bad write. */
export function parseAssistTiles(raw: unknown): AssistTileCfg[] {
  if (typeof raw !== "string" || raw.trim() === "") return defaultHomeTiles();
  try {
    const arr: unknown = JSON.parse(raw);
    if (!Array.isArray(arr)) return defaultHomeTiles();
    const out: AssistTileCfg[] = [];
    // Saved name / icon always win — every tile stays manually editable.
    // Built-in defaults only fill values that were never saved, so fresh
    // boards pick up the shipped template (renames included).
    const defaults = defaultHomeTiles();
    for (const it of arr) {
      if (!it || typeof it !== "object") continue;
      const o = it as Record<string, unknown>;
      const id = typeof o.id === "string" ? o.id.trim() : "";
      if (id === "") continue;
      const builtin = defaults.find((d) => d.id === id);
      out.push({
        id,
        icon: typeof o.icon === "string" && o.icon.trim() !== "" ? o.icon.trim() : builtin ? builtin.icon : "files",
        label: typeof o.label === "string" && o.label.trim() !== "" ? o.label : builtin ? builtin.label : "",
        size: o.size === "w" || o.size === "h" || o.size === "m" ? o.size : "s",
        w: typeof o.w === "number" && Number.isInteger(o.w) && o.w >= 1 && o.w <= 3 ? o.w : undefined,
        h: typeof o.h === "number" && Number.isInteger(o.h) && o.h >= 1 && o.h <= 3 ? o.h : undefined,
        cell: typeof o.cell === "number" && Number.isInteger(o.cell) && o.cell >= 0 && o.cell <= 8 ? o.cell : undefined,
        hidden: o.hidden === true,
      });
    }
    return out.length > 0 ? out : defaultHomeTiles();
  } catch {
    return defaultHomeTiles();
  }
}

/** Geometric placement of one tile on the home board.  `w` / `h` are the
 *  column / row spans (1..3), `page` the board page the tile sits on. */
export interface PlacedTile {
  tile: AssistTileCfg;
  r: number;
  c: number;
  w: number;
  h: number;
  page: number;
}

/** Lay the visible tiles out onto free 3×3 board pages.
 *
 *  On page 0 a tile's saved `cell` wins when it is still free, the rest fill
 *  top-left first (each tile claims its whole shape — any rectangle up to the
 *  full 3×3 page), and whatever does not fit opens the next page.
 *  `includeHidden` (the settings preview board) treats hidden tiles as visible
 *  so the manager can show them greyed out on the slot they will retake when
 *  re-enabled. */
export function layoutHomeTiles(tiles: AssistTileCfg[], opts?: { includeHidden?: boolean }): { placed: PlacedTile[] } {
  const visible = opts?.includeHidden ? tiles.slice() : tiles.filter((tl) => !tl.hidden);
  const shape = (tl: AssistTileCfg): { w: number; h: number } => {
    // Explicit spans win — every shape from 1×1 up to 3×3 is allowed.  The
    // legacy `size` bucket only answers when no explicit span was saved.
    if (tl.w != null && tl.h != null) return { w: Math.min(3, Math.max(1, tl.w)), h: Math.min(3, Math.max(1, tl.h)) };
    return tl.size === "m" ? { w: 2, h: 2 } : tl.size === "w" ? { w: 2, h: 1 } : tl.size === "h" ? { w: 1, h: 2 } : { w: 1, h: 1 };
  };
  const placed: PlacedTile[] = [];
  let page = 0;
  let cells = new Array<boolean>(9).fill(false);
  const fitsAt = (start: number, w: number, h: number, grid: boolean[]): boolean => {
    if (Math.floor(start / 3) + h > 3 || (start % 3) + w > 3) return false;
    for (let i = 0; i < h; i++) {
      for (let j = 0; j < w; j++) {
        if (grid[start + i * 3 + j]) return false;
      }
    }
    return true;
  };
  const greedy = (w: number, h: number, grid: boolean[]): number => {
    for (let i = 0; i < 9; i++) {
      if (fitsAt(i, w, h, grid)) return i;
    }
    return -1;
  };
  for (const tl of visible) {
    const sh = shape(tl);
    let at = -1;
    if (page === 0 && tl.cell != null && fitsAt(tl.cell, sh.w, sh.h, cells)) at = tl.cell;
    if (at === -1) at = greedy(sh.w, sh.h, cells);
    if (at === -1) {
      page++;
      cells = new Array<boolean>(9).fill(false);
      at = greedy(sh.w, sh.h, cells);
    }
    if (at === -1) continue; // unreachable: every shape fits an empty 3×3 page
    for (let i = 0; i < sh.h; i++) {
      for (let j = 0; j < sh.w; j++) cells[at + i * 3 + j] = true;
    }
    placed.push({ tile: tl, r: Math.floor(at / 3), c: at % 3, w: sh.w, h: sh.h, page });
  }
  return { placed };
}

const TABS_STORAGE_KEY = "session-sidebar-tabs";

function _loadTabs(): TabDef[] {
  try {
    const raw = localStorage.getItem(TABS_STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return [];
}
function _saveTabs(tabs: TabDef[]): void {
  try { localStorage.setItem(TABS_STORAGE_KEY, JSON.stringify(tabs.map(t => ({id: t.id, type: t.type, label: t.label, closable: t.closable})))); } catch {}
}

// ── Settings → panel option helpers (see Settings "Assist" panel) ────────────
function _settingBool(key: string, fallback: boolean): boolean {
  const v = getState().settings[key];
  if (v === undefined || v === null) return fallback;
  return isEnabled(v);
}
function _settingInt(key: string, fallback: number, min: number, max: number): number {
  const v = getState().settings[key];
  const n = typeof v === "number" ? v : parseInt(String(v ?? ""), 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}
function _settingNum(key: string, fallback: number, min: number, max: number): number {
  const v = getState().settings[key];
  const n = typeof v === "number" ? v : parseFloat(String(v ?? ""));
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}
function _settingStr(key: string, fallback: string): string {
  const v = getState().settings[key];
  return typeof v === "string" && v.trim() !== "" ? v : fallback;
}

/** Terminal font stacks the settings panel can pin the grid to.  Every entry
 *  ends in the generic `monospace`, so an uninstalled family degrades to the
 *  system monospace rather than to the proportional UI font. */
const TERMINAL_FONT_STACKS: Record<string, string> = {
  default: '"Cascadia Code", "JetBrains Mono", "Fira Code", "Cascadia Mono", Consolas, monospace',
  cascadia: '"Cascadia Code", "Cascadia Mono", Consolas, monospace',
  jetbrains: '"JetBrains Mono", "Cascadia Mono", Consolas, monospace',
  firacode: '"Fira Code", "Cascadia Mono", Consolas, monospace',
  sourcecode: '"Source Code Pro", "Cascadia Mono", Consolas, monospace',
  consolas: "Consolas, monospace",
  menlo: "Menlo, Monaco, monospace",
  courier: '"Courier New", monospace',
};
function terminalFontFamily(): string {
  return TERMINAL_FONT_STACKS[_settingStr("terminal_font_family", "default")] || TERMINAL_FONT_STACKS.default;
}
/** xterm's cursor enums are closed sets: a stale or hand-edited config value
 *  would be ignored by the renderer, so each one falls back to the documented
 *  default instead of reaching `term.options`. */
function terminalCursorStyle(): "block" | "underline" | "bar" {
  const v = _settingStr("terminal_cursor_style", "bar");
  return v === "block" || v === "underline" ? v : "bar";
}
function terminalCursorInactiveStyle(): "outline" | "block" | "bar" | "underline" | "none" {
  const v = _settingStr("terminal_cursor_inactive", "outline");
  return v === "outline" || v === "block" || v === "bar" || v === "underline" || v === "none" ? v : "outline";
}

/** The one place Settings → Assist → Editor is turned into Monaco options.
 *  Both view creation and the live re-apply go through it, so no option can
 *  end up read-at-creation-only. */
function editorViewOptions(): Record<string, unknown> {
  return {
    fontSize: _settingInt("editor_font_size", 13, 9, 32),
    wordWrap: _settingBool("editor_word_wrap", false) ? "on" : "off",
    minimap: { enabled: _settingBool("editor_minimap", false) },
    lineNumbers: _settingBool("editor_line_numbers", true) ? "on" : "off",
    tabSize: _settingInt("editor_tab_size", 4, 2, 8),
    detectIndentation: _settingBool("editor_detect_indentation", true),
    renderWhitespace: editorRenderWhitespace(),
    cursorBlink: _settingBool("editor_cursor_blink", true) ? "smooth" : "solid",
    fontLigatures: _settingBool("editor_font_ligatures", false),
    stickyScroll: { enabled: _settingBool("editor_sticky_scroll", true) },
    tabIndent: editorTabIndent(),
    insertSpaces: _settingBool("editor_use_spaces", true),
    wordWrapColumn: _settingInt("editor_word_wrap_column", 80, 20, 200),
    folding: _settingBool("editor_folding", true),
    glyphMargin: _settingBool("editor_glyph_margin", false),
    formatOnPaste: _settingBool("editor_format_on_paste", false),
    // Inline code suggestions were a test-phase experiment and have been
    // removed from the product; never auto-popup completion candidates.
    quickSuggestions: false,
    rulers: _settingBool("editor_rulers", true) ? [120] : [],
    cursorStyle: editorCursorStyle(),
    cursorBlinking: editorCursorBlinking(),
    // Slim, app-consistent scrollbars: 4px like the global ::-webkit-scrollbar,
    // no overlays/shadows; the thumb colours come from the encre-* theme
    // definitions in monaco.ts.  The overview ruler strip is dropped so the
    // gutter edge matches the rest of the app.
    scrollbar: {
      verticalScrollbarSize: 4,
      horizontalScrollbarSize: 4,
      useShadows: false,
    },
    overviewRulerLanes: 0,
  };
}

function editorTabIndent(): "none" | "indent" | "indentOutdent" | "outdent" {
  const v = _settingStr("editor_tab_indent", "indent");
  return v === "none" || v === "indentOutdent" || v === "outdent" ? v : "indent";
}

function editorCursorStyle(): "line" | "underline" | "block" | "line-thin" | "block-outline" | "underline-thin" {
  const v = _settingStr("editor_cursor_style", "line");
  return v === "underline" || v === "block" || v === "line-thin" || v === "block-outline" || v === "underline-thin"
    ? v : "line";
}

function editorCursorBlinking(): "blink" | "smooth" | "phase" | "expand" | "solid" {
  const v = _settingStr("editor_cursor_blinking", "smooth");
  return v === "blink" || v === "phase" || v === "expand" || v === "solid" ? v : "smooth";
}

/** xterm only accepts the two CSS keyword weights; anything else in the
 *  config is a hand-edit and falls back rather than reaching `term.options`. */
function terminalFontWeight(): "normal" | "bold" {
  return _settingStr("terminal_font_weight", "normal") === "bold" ? "bold" : "normal";
}

/** Monaco rejects unknown enum members here, so the stored id is validated
 *  rather than passed through. */
function editorRenderWhitespace(): "none" | "boundary" | "selection" | "all" {
  const v = _settingStr("editor_render_whitespace", "selection");
  return v === "none" || v === "boundary" || v === "all" ? v : "selection";
}

/** Fingerprint of the six Settings-owned review toggles.  The panel compares
 *  it on every settings push so an unrelated write cannot wipe a toggle the
 *  user just flipped in the review action menu. */
function reviewSettingsSig(): string {
  return [
    _settingBool("review_split_view", false),
    _settingBool("review_collapse_unified", false),
    _settingBool("review_wrap", false),
    _settingBool("review_word_diff", false),
    _settingBool("review_hide_whitespace", false),
    _settingBool("review_full_file", true),
  ].join("|");
}

interface NewTabOption {
  id: string;
  icon: string;
}

const NEW_TAB_OPTIONS: NewTabOption[] = [
  { id: "terminal", icon: "terminal" },
  { id: "editor", icon: "files" },
  { id: "review", icon: "git-compare-arrows" },
  { id: "browser", icon: "globe" },
];

function tabLabel(id: string): string {
  switch (id) {
    case "terminal": return t("sessionInner.tabTerminal");
    case "editor": return t("sessionInner.tabEditor");
    case "review": return t("sessionInner.tabReview");
    case "browser": return t("sessionInner.tabBrowser");
    case "markdown": return t("sessionInner.tabMarkdown");
    case "code": return t("sessionInner.tabCode");
    case "media": return t("sessionInner.tabMedia");
    default: return id;
  }
}

/** Every terminal — regardless of the shell behind it (PowerShell, cmd,
 *  WSL, bash) — shares the same Material terminal glyph so tabs, cards
 *  and menus render one consistent icon. */
function termIcon(_shellPath?: string): string {
  return getTerminalIcon();
}

/** Non-file, application-level tab icons (lucide). File-backed tabs
 *  resolve through `getFileIcon()` and are rendered as `<img>` instead. */
function tabIcon(type: string, shellPath?: string): string {
  if (type === "terminal") return termIcon(shellPath);
  if (isSubappView(type)) return subappTabIcon(type);
  switch (type) {
    case "review": return "git-compare-arrows";
    case "browser": return "globe";
    case "editor": return "files";
    default: return "square";
  }
}

function getTerminalAllText(term: any): string {
  try {
    const buffer = term.buffer?.active;
    if (!buffer || typeof buffer.length !== "number") return "";
    const lines: string[] = [];
    for (let y = 0; y < buffer.length; y++) {
      const line = buffer.getLine(y);
      if (line && typeof line.translateToString === "function") {
        lines.push(line.translateToString());
      }
    }
    return lines.join("\n");
  } catch { return ""; }
}

/**
 * Manages the session-inner sidebar tabs, panels and embedded terminals.
 */
export class SessionInner {
  private el: HTMLElement;
  private tabBar: HTMLElement;
  private tabList: HTMLElement;
  /** Where the tab list renders: the sidebar's own bar in the main window, the
   *  title-row slot `mountTabList` hands over in the subwindow. */
  private tabStrip: HTMLElement;
  private tabBody: HTMLElement;
  private infoBody!: HTMLElement;
  private tabAddBtn: HTMLButtonElement | null = null;
  private tabAddDropdown: HTMLDivElement | null = null;
  private tabAddDocClickHandler: ((e: MouseEvent) => void) | null = null;
  private tabs: TabDef[] = [];
  private activeTab: string = "";
  /** Short-lived cache for "size · last modified" strings, keyed by path. */
  private _fileMetaCache = new Map<string, { size: number; mtime: number; at: number } | null>();
  /** Origin → permissions the page asked for, refreshed on a short TTL. */
  private _permCache = new Map<string, { list: Array<{ name: string; granted: boolean }>; at: number }>();
  private _sessionTabs = new Map<string, TabDef[]>();
  private _sessionTerminals = new Map<string, Map<string, Array<{ ptyId: number; term: any; cleanup: () => void; resizeObs: ResizeObserver; applySizing: () => void }>>>();
  private _sessionTermActiveIdx = new Map<string, Map<string, number>>();
  private _sessionSidebarVisible = new Map<string, boolean>();
  private _sessionBrowsers = new Map<string, Map<string, BrowserView>>();

  private _sessionReviewState = new Map<string, { filter: string; mode: "git" | "artifact"; artifact: ArtifactItem | null; filePending: string | undefined }>();
  /** Key used for storing sidebar tabs: "session:<sid>" or "workspace:<path>". */
  private _tabKey = "";

  /** Build the storage key: session-level in normal mode, workspace-level in ws mode. */
  private _tabStorageKey(): string {
    const s = getState();
    if (s.activeWorkspace) return "ws:" + s.activeWorkspace;
    return "session:" + (s.sessionId || "");
  }

  /** React to state changes: swap tab + terminal state on session/workspace
   * switches, and refresh the live info panels when the same session mutates
   * (plan items, artifacts, references, etc.). */
  private _onStateChange(changed?: ReadonlySet<unknown>): void {
    // The subwindow owns no session, so a session/workspace change it happens
    // to hear about must not swap its tab set out from under the open views.
    if (inChildWindow()) return;
    const newKey = this._tabStorageKey();
    if (newKey === this._tabKey) {
      // Same session/workspace but state mutated (e.g. plan progress, new
      // artifacts, references). Keep the info panels live without rebuilding
      // the whole tab skeleton.
      if (this.activeTab === "info") {
        // `messages` mutates on every streamed token, but the info panel only
        // derives the tool-call and turn counts from it. Rebuilding six panel
        // strings per token was pure churn (and any real HTML change would
        // swap the DOM and reset its scroll).
        const onlyMessages = !!changed && changed.size === 1 && changed.has("messages");
        if (onlyMessages) {
          const st = getState();
          const last = st.messages[st.messages.length - 1];
          const sig = `${st.messages.length}:${last?.toolCalls?.length ?? 0}`;
          if (sig === this._panelMsgSig) return;
          this._panelMsgSig = sig;
        }
        this.requestRender();
      }
      return;
    }

    // Save current state to old key.
    if (this._tabKey) {
      this._sessionTabs.set(this._tabKey, [...this.tabs]);
      // Only save visibility if sidebar is currently visible.
      // If hidden, the correct state was already saved by whoever hid it
      // (cleanupContentArea, closeSessionInnerSidebar, or session menu button).
      const _panel = document.getElementById("session-inner-sidebar");
      if (_panel && !_panel.classList.contains("hidden")) {
        this.saveSidebarVisibility();
      }
      // Deep-copy terminals map so each session gets its own Map instance.
      const termCopy = new Map<string, Array<{ ptyId: number; term: any; cleanup: () => void; resizeObs: ResizeObserver; applySizing: () => void }>>();
      for (const [k, arr] of this.panelTerminals) {
        termCopy.set(k, [...arr]);
      }
      this._sessionTerminals.set(this._tabKey, termCopy);
      this._sessionTermActiveIdx.set(this._tabKey, new Map(this.panelActiveTermIdx));
      const browserCopy = new Map<string, BrowserView>();
      for (const [k, bv] of this.panelBrowsers) {
        browserCopy.set(k, bv);
      }
      this._sessionBrowsers.set(this._tabKey, browserCopy);

      this._sessionReviewState.set(this._tabKey, {
        filter: this._reviewFilter,
        mode: this._reviewMode,
        artifact: this._reviewArtifact,
        filePending: this._reviewFilePending,
      });
    }
    this._tabKey = newKey;

    // Restore terminals for the new key, or create fresh.
    const restoredTerminals = this._sessionTerminals.get(newKey);
    const restoredActiveIdx = this._sessionTermActiveIdx.get(newKey);
    this.panelTerminals = restoredTerminals ?? new Map();
    this.panelActiveTermIdx = restoredActiveIdx ?? new Map();
    const restoredBrowsers = this._sessionBrowsers.get(newKey);
    this.panelBrowsers = restoredBrowsers ?? new Map();

    const savedReview = this._sessionReviewState.get(newKey);
    if (savedReview) {
      this._reviewFilter = savedReview.filter;
      this._reviewMode = savedReview.mode;
      this._reviewArtifact = savedReview.artifact;
      this._reviewFilePending = savedReview.filePending;
    } else {
      const hasWs = !!getState().activeWorkspace;
      this._reviewFilter = hasWs ? "all" : "lastRound";
      this._reviewMode = hasWs ? "git" : "artifact";
      this._reviewArtifact = null;
      this._reviewFilePending = undefined;
    }

    const restored = this._sessionTabs.get(newKey);
    if (restored) {
      this.tabs = restored;
    } else {
      this.tabs = [];
    }
    this.activeTab = this.tabs.length > 0 ? this.tabs[0].id : "";
    // Force panels to re-render so terminal panel reflects restored state.
    this.tabBody.querySelectorAll(".tab-panel").forEach((p) => p.remove());
    // Switching sessions must rebuild the info panel even if its markup
    // coincidentally matches the previous session's cached HTML.
    this._lastInfoHtml = "";
    this._panelMsgSig = "";
    this.renderTabs();
    // Structural session switch: immediate paint through the scheduler's
    // immediate mode, so there is one paint path and one fault boundary.
    runScheduledRenderNow("right-panel", () => this.render(), 30);
    this.restoreSidebarVisibility();
  }

  /* tab drag */
  private dragEl: HTMLElement | null = null;
  private dragIdx: number = -1;
  private dragStartX: number = 0;
  /* Untransformed geometry of the dragged tab, sampled once at drag start.
     The clamp must be relative to these: deriving them per-frame from
     getBoundingClientRect() minus the raw dx drifts as soon as the transform
     is clamped (rect already includes the clamped translateX). */
  private dragBaseLeft: number = 0;
  private dragBaseWidth: number = 0;
  private dragSlot: number = -1;
  /** Orange permission-light pulse timers per browser tab id. */
  private _permLightTimers = new Map<string, number>();
  private dragBarEl: HTMLElement | null = null;
  private dragBound = false;
  private wasDragged = false;

  /* width persistence */
  private sidebarWidth = 280;

  /* resize */
  private resizing = false;
  private resizeStartX = 0;
  private resizeStartW = 0;

  /* terminals �� each panel can have multiple sub-terminals */
  private panelTerminals = new Map<string, Array<{ ptyId: number; term: any; cleanup: () => void; resizeObs: ResizeObserver; applySizing: () => void }>>();
  private panelActiveTermIdx = new Map<string, number>();
  private panelShellPath = new Map<string, string>();
  private panelShellArgs = new Map<string, string[]>();
  private panelBrowsers = new Map<string, BrowserView>();
  private panelMarkdownPreviews = new Map<string, MarkdownPreviewView>();
  /** Teardowns for the subwindow-only views (legal doc / license / data
   *  center / nebula) — they own listeners, polls and animation frames. */
  private panelSubapps = new Map<string, () => void>();
  private panelCodeEditors = new Map<string, any>();
  /** Tracks which code tabs have unsaved edits (true = dirty). */
  private _dirtyTabs = new Map<string, boolean>();

  /** VSCode-style per-root expansion state for the explorer tree: which
   *  directories each root had expanded, surviving tab close/reopen and
   *  panel rebuilds.  Keyed by root path so workspaces/sessions keep and
   *  restore their own fold state independently. */
  private static _editorExpandState = new Map<string, Set<string>>();
  /** Last rendered info-panel HTML. Lets renderContent() skip DOM rebuilds
   *  when state mutated but the info panel content is unchanged (e.g. every
   *  streaming/status emit while the model answers), so the sidebar does not
   *  visibly flash on every update. */
  private _lastInfoHtml = "";
  /** Coarse message fingerprint for the info panel. `messages` mutates on
   *  every streamed token, but the panel only derives two counts from it, so
   *  a token that changes neither must not rebuild the six panel strings. */
  private _panelMsgSig = "";
  private panelMediaViewers = new Map<string, MediaViewer>();
  private panelOfficeControllers = new Map<string, HTMLElement>();
  private _terminalCounter = 0;
  private _browserCounter = 0;
  private _markdownCounter = 0;
  private _codeCounter = 0;
  private _mediaCounter = 0;
  private _officeCounter = 0;
  private _editorCounter = 0;
  private _availableShells: Array<{ name: string; path: string; args?: string[] }> = [];
  private _shellsLoaded = false;
  /** Current page of the home tile board (pagination kicks in above 9 tiles). */
  private _homePage = 0;
  /** Wheel accumulator for the home board so trackpad micro-scrolls do not
   *  flip one page per event. */
  private _homeWheelAccum = 0;
  /** Last seen `assist_tiles` JSON, used to repaint the board only when the
   *  user actually edits it (Settings → Assist Panels). */
  private _lastTilesRaw: string | null = null;
/**
   * Constructor: resolves DOM nodes, restores width and wires tabs/resize.
   */
  constructor() {
    this.el = document.getElementById("session-inner-sidebar")!;
    this.tabBar = this.el.querySelector(".tab-bar")!;
    this.tabList = document.createElement("div");
    this.tabList.className = "header-tabs";
    this.tabStrip = this.tabBar;
    this.tabBody = this.el.querySelector(".tab-body")!;

    // Start empty �?the home page (terminal/editor/review cards) shows.
    this.tabs = [];
    this.activeTab = "";

    // Width is a user-draggable px value; the hard cap of half the main
    // content area is enforced live by CSS max-width:50% (see styles.css),
    // so window resizes re-clamp automatically without JS.
    try {
      const saved = localStorage.getItem("session-sidebar-width");
      if (saved) this.sidebarWidth = parseInt(saved, 10) || 280;
    } catch {}
    this.el.style.setProperty("--sidebar-w", this.sidebarWidth + "px");
    document.getElementById("main-body")?.style.setProperty("--sidebar-w", this.sidebarWidth + "px");

    subscribe((changed) => this._onStateChange(changed), ["sessionId", "activeWorkspace", "messages", "telemetry", "planItems", "planModeActive", "planProposals", "artifacts", "references", "branches", "agentState", "planReview", "compactEvents"]);
    // Settings → Assist rows save instantly, so the panels that consume them
    // re-read on the same push.  Terminal binds its own; the code editor and
    // the review panel are pushed from here (see the two `_apply*Settings`).
    subscribe(() => {
      this._applyEditorSettings();
      this._applyReviewSettings();
      this._applyHomeTiles();
    }, ["settings"]);
    onLocaleChange(() => this.requestRender());
    this.renderTabs();
    this.bindAddButton();
    this.bindResize();

    // Auto-open sidebar + browser tab when the model uses the browser tool.
    // A manual user collapse is respected: the webview keeps running while
    // the sidebar is transform-hidden, so the tool works without reopening
    // the panel — only the user restores it by hand.
    window.addEventListener("browser-tool-call", () => {
      const panel = document.getElementById("session-inner-sidebar");
      const mainBody = document.getElementById("main-body");
      if (!this.isUserSidebarCollapsed() && panel && mainBody && panel.classList.contains("hidden")) {
        panel.classList.remove("hidden");
        mainBody.classList.remove("sidebar-hidden");
        document.getElementById("app")?.classList.add("sidebar-collapsed");
        this.saveSidebarVisibility();
      }
      // Only create a browser tab if none exists yet, so the model
      // operates the same webview across multiple tool calls
      const existingBrowser = this.tabs.find(t => t.type === "browser");
      if (!existingBrowser) {
        this.createTab("browser");
      }
    });

    window.addEventListener("info-html-open", ((e: CustomEvent) => {
      const { url } = e.detail;
      if (!url) return;
      // Ensure sidebar is visible
      const panel = document.getElementById("session-inner-sidebar");
      const mainBody = document.getElementById("main-body");
      if (panel && mainBody && panel.classList.contains("hidden")) {
        panel.classList.remove("hidden");
        mainBody.classList.remove("sidebar-hidden");
        document.getElementById("app")?.classList.add("sidebar-collapsed");
        this.saveSidebarVisibility();
      }
      // Reuse existing browser tab or create one
      const existing = this.tabs.find(t => t.type === "browser");
      if (existing) {
        this.activateTab(existing.id);
        const bv = this.panelBrowsers.get(existing.id);
        if (bv) bv.navigate(url);
      } else {
        this.createTab("browser", { startUrl: url });
      }
    }) as EventListener);

    // Open a media file from a chat media viewer (info tool card) as a
    // sidebar media preview tab — same primitive the file tree uses.
    window.addEventListener("media-open-preview", ((e: CustomEvent) => {
      const { type, src } = e.detail as { type?: string; src?: string };
      if (!src) return;
      const filePath = src.replace(/^local:\/\/\//, "").replace(/^local:\/\//, "").replace(/^file:\/\/\//, "").replace(/^file:\/\//, "");
      if (/^https?:/i.test(filePath)) return;
      const panel = document.getElementById("session-inner-sidebar");
      const mainBody = document.getElementById("main-body");
      if (panel && mainBody && panel.classList.contains("hidden")) {
        panel.classList.remove("hidden");
        mainBody.classList.remove("sidebar-hidden");
        document.getElementById("app")?.classList.add("sidebar-collapsed");
        this.saveSidebarVisibility();
      }
      const existing = this.tabs.find(t => t.type === "media" && t.filePath === filePath);
      if (existing) {
        this.activateTab(existing.id);
        return;
      }
      const name = filePath.split(/[/\\]/).pop() || filePath;
      this.createTab("media", { title: name, filePath, mediaType: type === "video" ? "video" : "image" });
    }) as EventListener);
  }

  /** Re-renders the active tab's content (reactive to state/locale). */
  /** Coalesced paint through the shared scheduler (one per frame). */
  public requestRender(): void {
    scheduleRender("right-panel", () => this.render(), 30);
  }

  render(): void {
    if (this.tabs.length === 0) {
      this.renderHomePage();
    }
    if (this.activeTab === "info") this.renderContent();
    const agentPanel = this.tabBody.querySelector('.tab-panel[data-panel="agent"]') as HTMLElement | null;
    if (agentPanel) {
      this.renderAgentPanel(agentPanel);
    }
  }

  renderForce(): void {
    // When re-opening the sidebar, the .tab-panel elements were removed from
    // DOM by resetToDefaultTabs. Re-render tab state so panels come back.
    if (this.tabs.length === 0) {
      this.renderHomePage();
    } else {
      // Re-bind events and let renderTabs recreate panels.
      this.renderTabs();
    }
    if (this.activeTab === "info") this.renderContent();
    const agentPanel = this.tabBody.querySelector('.tab-panel[data-panel="agent"]') as HTMLElement | null;
    if (agentPanel) {
      this.renderAgentPanel(agentPanel);
    }
  }

  /* ── Tab Management ─────────────────────────────────────────────── */

  /** Serialised tab rendering: concurrent triggers (state subscription,
   *  poller, user actions, createTab) used to interleave panel builds —
   *  double setup for the same panel and late `.active` writes left the
   *  visible panel out of sync with the tab highlight (the "sometimes stuck
   *  on the wrong container" symptom).  Calls are now chained: each
   *  renderTabs() waits for the previous one to finish. */
  private _tabsGate: Promise<void> = Promise.resolve();
  private async renderTabs(): Promise<void> {
    const run = this._tabsGate.then(() => this._doRenderTabs());
    this._tabsGate = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  private async _doRenderTabs(): Promise<void> {
    this.tabList.innerHTML = this.tabs.map((tab, i) => {
      const activeCls = tab.id === this.activeTab ? " active" : "";
      const isDirty = tab.type === "code" && !!this._dirtyTabs.get(tab.id);
      let iconHtml: string;
      if (tab.type === "browser" && tab.favicon) {
        iconHtml = `<img class="tab-favicon" src="${this.esc(tab.favicon)}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display=''"/><i data-lucide="globe" class="lucide lucide-sm" style="flex-shrink:0;display:none"></i>`;
      } else if (tab.type === "terminal") {
        iconHtml = `<img src="${termIcon(tab.shellPath)}" class="icon" style="width:14px;height:14px;flex-shrink:0">`;
      } else if (tab.filePath) {
        iconHtml = `<img src="${getFileIcon(baseName(tab.filePath) || tab.label)}" class="icon" style="width:14px;height:14px;flex-shrink:0">`;
      } else {
        iconHtml = `<i data-lucide="${tabIcon(tab.type, tab.shellPath)}" class="lucide lucide-xs" style="flex-shrink:0"></i>`;
      }
      const stateCls = tab.state === "running" ? "session-running" : tab.state === "awaiting_approval" ? "session-waiting" : tab.state === "permission" ? "session-orange" : "";
      /* mode-chip pattern: NO close element in the DOM.  `closable` arms the
         × pseudo-element over the icon slot; `dirty` swaps the icon for a
         pre-rendered `circle` marker in the same slot (CSS gates which one
         shows).  The icon element itself carries the close click. */
      const flagCls = `${tab.closable ? " closable" : ""}${isDirty ? " dirty" : ""}`;
      return `<button class="tab${activeCls}${flagCls}" data-tab="${tab.id}" data-idx="${i}" draggable="true">
        <span class="tab-icon-wrap">${stateCls ? `<span class="${stateCls} tab-indicator-pos"></span>` : ""}${iconHtml}<i data-lucide="circle" class="lucide tab-dirty-dot"></i></span>
        <span class="tab-label">${this.esc(tab.label)}</span>
      </button>`;
    }).join("");

    if (!this.tabStrip.contains(this.tabList)) {
      this.tabStrip.prepend(this.tabList);
    }

    this.bindTabEvents();
    if (this.tabs.length === 0) {
      this.renderHomePage();
    } else {
      const homeEl = this.tabBody.querySelector(".si-home");
      if (homeEl) homeEl.remove();
      await this.renderPanels();
    }
    this.refreshLucide();
    void this.refreshTabTooltips();
  }

  /**
   * Sync a code tab's dirty state without rebuilding the whole tab bar /
   * panels.  The dirty marker is a pre-rendered `circle` icon inside the
   * icon slot, so dirty state is just a class on the tab button: the icon
   * dot replaces the file icon at rest and the × takes over on hover.
   */
  private _refreshTabClose(id: string): void {
    const tabEl = this.tabList.querySelector(`.tab[data-tab="${id}"]`) as HTMLElement | null;
    if (!tabEl) return;
    tabEl.classList.toggle("dirty", !!this._dirtyTabs.get(id));
  }

  private refreshLucide(): void {
    if (typeof (window as any).lucide === "undefined") return;
    // Two roots because the subwindow renders its tab list on the title row:
    // the icons there are no longer inside the sidebar's bar.
    (window as any).lucide.createIcons({ root: this.tabStrip });
    if (this.tabStrip !== this.tabBar) {
      (window as any).lucide.createIcons({ root: this.tabBar });
    }
  }

  /** Render the tab list into `host` instead of the sidebar's own bar. The
   *  Encre Agent Desktop Subwindow calls this to put its strip on the title
   *  row; the tab engine, its data and its styling stay the single one. */
  public mountTabList(host: HTMLElement): void {
    this.tabStrip = host;
    if (!host.contains(this.tabList)) host.appendChild(this.tabList);
    void this.renderTabs();
  }

  private bindAddButton(): void {
    this.tabAddBtn?.remove();
    this.tabAddDropdown?.remove();
    if (this.tabAddDocClickHandler) {
      document.removeEventListener("click", this.tabAddDocClickHandler);
      this.tabAddDocClickHandler = null;
    }

    const btn = document.createElement("button");
    btn.className = "tab-add-btn";
    btn.dataset.tooltip = t("sessionInner.newTab");
    btn.innerHTML = `<i data-lucide="plus" class="lucide lucide-sm"></i>`;
    this.tabBar.appendChild(btn);
    this.tabAddBtn = btn;

    const dropdown = document.createElement("div");
    dropdown.className = "tab-add-dropdown hidden";
    document.body.appendChild(dropdown);
    this.tabAddDropdown = dropdown;

    const rebuildDropdown = () => {
      const openTypes = new Set(this.tabs.map((tab) => tab.type));
      const available = NEW_TAB_OPTIONS.filter((opt) => {
        if (opt.id === "terminal" || opt.id === "browser") return true;
        return !openTypes.has(opt.id);
      });
      if (available.length === 0) {
        dropdown.innerHTML = `<div class="tab-add-empty">${t("sessionInner.newTabEmpty")}</div>`;
      } else {
        dropdown.innerHTML = available.map((opt) => {
          return `<div class="tab-add-item" data-add="${opt.id}">
            <i data-lucide="${opt.icon}" class="lucide lucide-sm"></i>
            <span>${this.esc(tabLabel(opt.id))}</span>
          </div>`;
        }).join("");
        dropdown.querySelectorAll(".tab-add-item").forEach((item) => {
          item.addEventListener("click", async (e) => {
            e.stopPropagation();
            const type = (item as HTMLElement).dataset.add!;
            if (type === "terminal") {
              await this._showShellSelector(btn);
            } else {
              await this.createTab(type);
            }
            hideAnimated(dropdown);
          });
        });
      }
    };

    const toggle = () => {
      // Show/hide goes through the unified motion helpers: an exit still in
      // flight (closed <120ms ago) has to be cancelled, not raced — the
      // toggle alone would leave `.closing` on and the dropdown invisible.
      const willShow = dropdown.classList.contains("hidden");
      if (willShow) {
        showAnimated(dropdown);
        registerSurface(dropdown, { scope: "ephemeral", onClose: () => hideAnimated(dropdown) });
      } else {
        hideAnimated(dropdown);
        unregisterSurface(dropdown);
      }
      const isHidden = !willShow;
      if (!isHidden) {
        rebuildDropdown();
        showAnchoredDropdown(dropdown, btn);
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: dropdown });
        }
      }
    };

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggle();
    });

    this.tabAddDocClickHandler = (e: MouseEvent) => {
      if (!btn.contains(e.target as Node) && !dropdown.contains(e.target as Node)) {
        hideAnimated(dropdown);
      }
    };
    document.addEventListener("click", this.tabAddDocClickHandler);
  }

  private async _showShellSelector(anchor: HTMLElement): Promise<void> {
    const api = (window as any).electronAPI;
    if (!api) {
      await this.createTab("terminal");
      return;
    }
    if (!this._shellsLoaded) {
      try {
        this._availableShells = await api.terminalListShells();
        this._shellsLoaded = true;
      } catch {
        this._availableShells = [];
      }
    }
    if (this._availableShells.length === 0) {
      this._availableShells.push({ name: "Default", path: "" });
    }
    if (this._availableShells.length === 1) {
      const s = this._availableShells[0];
      await this.createTab("terminal", { shellPath: s.path, shellArgs: s.args });
      return;
    }

    const shellDropdown = document.createElement("div");
    shellDropdown.className = "tab-add-dropdown hidden";
    document.body.appendChild(shellDropdown);

    shellDropdown.innerHTML = this._availableShells.map((s) => {
      return `<div class="tab-add-item" data-path="${this.esc(s.path)}" data-args="${this.esc(JSON.stringify(s.args || []))}" style="flex-direction:column;align-items:flex-start;gap:2px">
        <span>${this.esc(s.name)}</span>
        ${s.path ? `<span style="font-size:10px;color:var(--text-muted);font-family:var(--font-mono,monospace)">${this.esc(s.path)}</span>` : ""}
      </div>`;
    }).join("");
    let dismissed = false;
    function dismissShellDropdown(): void {
      if (dismissed) return;
      dismissed = true;
      document.removeEventListener("click", closeHandler);
      // Play the shared exit motion first so this menu fades out like every
      // other floating surface instead of snapping away.
      shellDropdown.classList.add("closing");
      const onEnd = (ev: AnimationEvent): void => {
        if (ev.target !== shellDropdown) return;
        shellDropdown.removeEventListener("animationend", onEnd);
        shellDropdown.remove();
      };
      shellDropdown.addEventListener("animationend", onEnd);
      setTimeout(() => shellDropdown.remove(), 300);
    }
    shellDropdown.querySelectorAll(".tab-add-item").forEach((item) => {
      item.addEventListener("click", async () => {
        const el = item as HTMLElement;
        const sp = el.dataset.path || "";
        let sa: string[] = [];
        try { sa = JSON.parse(el.dataset.args || "[]"); } catch {}
        dismissShellDropdown();
        await this.createTab("terminal", { shellPath: sp, shellArgs: sa });
      });
    });
    showAnimated(shellDropdown);
    registerSurface(shellDropdown, { scope: "ephemeral", onClose: () => hideAnimated(shellDropdown) });
    showAnchoredDropdown(shellDropdown, anchor);

    const closeHandler = (e: MouseEvent) => {
      if (!shellDropdown.contains(e.target as Node)) {
        dismissShellDropdown();
      }
    };
    setTimeout(() => document.addEventListener("click", closeHandler), 0);
  }

  public getTabs(): TabDef[] {
    return this.tabs.slice();
  }

  public getActiveTab(): string {
    return this.activeTab;
  }

  /**
   * Tear down every dynamic tab/panel/terminal in the sidebar and restore
   * the default "info" tab.  Called from app.ts on session switch and
   * mode switch so terminals and editors from a previous session do not
   * leak into the next.
   */
  public async resetToDefaultTabs(): Promise<void> {
    // Hide panels from the DOM so the sidebar shows the home page.
    // Keep this.tabs intact so re-opening restores everything.
    this.tabBody.querySelectorAll(".tab-panel").forEach((p) => p.remove());
    this.tabBody.innerHTML = "";
    this.activeTab = "";
    await this.renderTabs();
    this.bindAddButton();
  }

  /** The home board: user-configurable tiles (Settings → Assist Panels),
   *  falling back to the four default launchers. */
  private renderHomePage(): void {
    // Remove any leftover panels
    this.tabBody.querySelectorAll(".tab-panel").forEach((p) => p.remove());

    const raw = String((getState().settings as Record<string, unknown>)["assist_tiles"] ?? "");
    this._lastTilesRaw = raw;
    const tiles = parseAssistTiles(raw);

    // Shared packing (identical to the drag-and-drop preview in Settings):
    // every board is the free 3×3 grid, honoring each tile's saved cell
    // before greedy fill; overflow opens the next page.
    const { placed } = layoutHomeTiles(tiles);
    const pageCount = Math.max(1, ...placed.map((p) => p.page + 1));
    if (this._homePage >= pageCount) this._homePage = pageCount - 1;
    const pageTiles = placed.filter((p) => p.page === this._homePage);

    const tileHtml = (tl: AssistTileCfg, style = "") =>
      `<button class="si-grid-tile${DEFAULT_TILE_IDS.includes(tl.id) ? "" : " si-grid-tile--custom"}"${style ? ` style="${style}"` : ""} data-tab="${this.esc(tl.id)}">
        <i data-lucide="${this.esc(tl.icon)}" class="lucide si-grid-icon"></i>
        <span class="si-grid-label">${this.esc(tl.label || tl.id)}</span>
      </button>`;
    // Free 3×3 grid with explicit grid areas for every tile.
    const cellsHtml = pageTiles
      .map((p) => tileHtml(p.tile, `grid-area:${p.r + 1} / ${p.c + 1} / span ${p.h} / span ${p.w}`))
      .join("");
    const used = pageTiles.reduce((acc, p) => acc + p.w * p.h, 0);
    const empties = new Array(9 - used).fill(`<div class="si-grid-empty"></div>`).join("");
    const boardHtml = `<div class="si-grid si-grid--free">${cellsHtml}${empties}</div>`;

    // Pagination dots — pinned to the bottom of the sidebar, one per page.
    const dotsHtml = pageCount > 1
      ? `<div class="si-grid-dots">${Array.from({ length: pageCount }, (_, i) =>
          `<button class="si-grid-dot${i === this._homePage ? " active" : ""}" data-page="${i}" aria-label="${i + 1}"></button>`).join("")}</div>`
      : "";

    this.tabBody.innerHTML = `<div class="si-home">
      <div class="si-grid-wrap">${boardHtml}</div>
      ${dotsHtml}
    </div>`;

    // Built-in launcher tiles open their panel; custom tiles have no panel
    // behind them yet (the plugin admission point) and say so on click.
    this.tabBody.querySelectorAll(".si-grid-tile").forEach((el) => {
      el.addEventListener("click", () => {
        const type = (el as HTMLElement).dataset.tab!;
        if (DEFAULT_TILE_IDS.includes(type)) {
          if (type === "terminal") {
            this._showShellSelector(this.tabAddBtn || this.el);
          } else {
            void this.createTab(type);
          }
        } else {
          showToast(t("sessionInner.tileNotReady"), "", "info");
        }
      });
    });

    // Dots switch pages on click. The wheel flips pages ONLY while hovering
    // the dots strip at the bottom — scrolling over the board must never
    // flip pages — with an accumulator so a single trackpad glide does not
    // skip through every page at once.
    this.tabBody.querySelectorAll(".si-grid-dot").forEach((el) => {
      el.addEventListener("click", () => {
        this._homePage = parseInt((el as HTMLElement).dataset.page || "0", 10) || 0;
        this.renderHomePage();
      });
    });
    if (pageCount > 1) {
      const dots = this.tabBody.querySelector(".si-grid-dots") as HTMLElement | null;
      dots?.addEventListener("wheel", (e: WheelEvent) => {
        e.preventDefault();
        this._homeWheelAccum += e.deltaY || e.deltaX;
        if (Math.abs(this._homeWheelAccum) < 50) return;
        const dir = this._homeWheelAccum > 0 ? 1 : -1;
        this._homeWheelAccum = 0;
        const next = this._homePage + dir;
        if (next < 0 || next >= pageCount) return;
        this._homePage = next;
        this.renderHomePage();
      }, { passive: false });
    }
    this._homeWheelAccum = 0;

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: this.tabBody });
    }
  }

  /** Repaint the home board when the user edits the tile configuration in
   *  Settings → Assist Panels.  Only fires while the board is the active
   *  view (no panels open); open panels keep their own render paths. */
  private _applyHomeTiles(): void {
    const raw = String((getState().settings as Record<string, unknown>)["assist_tiles"] ?? "");
    const changed = this._lastTilesRaw !== null && raw !== this._lastTilesRaw;
    this._lastTilesRaw = raw;
    if (changed && this.tabs.length === 0) this.renderHomePage();
  }

  public async createTab(type: string, opts?: { shellPath?: string; shellArgs?: string[]; startUrl?: string; content?: string; title?: string; filePath?: string; mediaType?: string; label?: string }): Promise<void> {
    this.revealPanel();
    // Subwindow views are one-per-key: re-opening "privacy" from About focuses
    // the document that is already open instead of stacking a second copy.
    if (isSubappView(type)) {
      const open = this.tabs.find((tab) => tab.id === type);
      if (open) {
        if (opts?.label !== undefined) open.label = opts.label;
        this.activeTab = open.id;
        await this.renderTabs();
        return;
      }
    }
    let id: string;
    let label: string;
    if (type === "terminal") {
      this._terminalCounter++;
      id = `terminal-${this._terminalCounter}`;
      const shellName = opts?.shellPath ? opts.shellPath.split(/[/\\]/).pop()?.replace(/\.(exe|cmd|bat)$/i, "") || t("sessionInner.tabTerminal") : t("sessionInner.tabTerminal");
      label = `${shellName} ${this._terminalCounter}`;
    } else if (type === "browser") {
      this._browserCounter++;
      id = `browser-${this._browserCounter}`;
      label = t("sessionInner.tabBrowser");
    } else if (type === "markdown") {
      this._markdownCounter++;
      id = `markdown-${this._markdownCounter}`;
      label = opts?.title || t("sessionInner.tabMarkdown");
    } else if (type === "editor") {
      this._editorCounter = (this._editorCounter || 0) + 1;
      id = `editor-${this._editorCounter}`;
      const fileName = opts?.filePath ? opts.filePath.split(/[/\\]/).pop() || opts.filePath : t("sessionInner.tabEditor");
      label = fileName;
    } else if (type === "code") {
      this._codeCounter++;
      id = `code-${this._codeCounter}`;
      label = opts?.title || t("sessionInner.tabCode");
    } else if (type === "media") {
      this._mediaCounter++;
      id = `media-${this._mediaCounter}`;
      label = opts?.title || t("sessionInner.tabMedia");
    } else if (type === "office") {
      this._officeCounter++;
      id = `office-${this._officeCounter}`;
      label = opts?.title || t("sessionInner.tabOffice");
    } else {
      id = type;
      label = tabLabel(type);
    }
    // Explicit label wins over the per-type default (routed child-view tabs).
    if (opts?.label !== undefined) label = opts.label;
    const tab: TabDef = { id, type, label, closable: true };
    if (opts?.shellPath !== undefined) tab.shellPath = opts.shellPath;
    if (opts?.shellArgs !== undefined) tab.shellArgs = opts.shellArgs;
    if (opts?.startUrl !== undefined) tab.startUrl = opts.startUrl;
    if (opts?.content !== undefined) tab.content = opts.content;
    if (opts?.title !== undefined) tab.title = opts.title;
    if (opts?.filePath !== undefined) tab.filePath = opts.filePath;
    if (opts?.mediaType !== undefined) tab.mediaType = opts.mediaType;
    this.tabs.push(tab);
    _saveTabs(this.tabs);
    this.activeTab = id;
    await this.renderTabs();
  }

  private async renderPanels(): Promise<void> {
    const existingIds = new Set<string>();
    this.tabBody.querySelectorAll(".tab-panel").forEach((p) => {
      existingIds.add((p as HTMLElement).dataset.panel || "");
    });

    for (const t of this.tabs) {
      if (existingIds.has(t.id)) continue;
      const panel = document.createElement("div");
      panel.className = "tab-panel" + (t.id === this.activeTab ? " active" : "");
      panel.dataset.panel = t.id;
      panel.dataset.type = t.type;

      if (t.type === "terminal") {
        await this.setupTerminalPanel(panel, t.id, t.shellPath, t.shellArgs);
      } else if (t.type === "editor") {
        this.setupEditorPanel(panel);
      } else if (t.type === "review") {
        this.setupReviewPanel(panel);
      } else if (t.type === "browser") {
        this.setupBrowserPanel(panel, t.id, t.startUrl);
      } else if (t.type === "markdown") {
        this.setupMarkdownPanel(panel, t.id, t.content, t.title, t.filePath);
      } else if (t.type === "binary") {
        this.setupBinaryPanel(panel);
      } else if (t.type === "code") {
        await this.setupCodePanel(panel, t.id, t.content || "", t.filePath || "");
      } else if (t.type === "media") {
        this.setupMediaPanel(panel, t.id, t.filePath || "", t.mediaType);
      } else if (t.type === "office") {
        this.setupOfficePanel(panel, t.id, t.filePath || "");
      } else if (isSubappView(t.type)) {
        this.panelSubapps.set(t.id, mountSubappView(t.type, panel));
      }

      this.tabBody.appendChild(panel);
    }

    this.tabBody.querySelectorAll(".tab-panel").forEach((p) => {
      const pid = (p as HTMLElement).dataset.panel || "";
      const ptype = (p as HTMLElement).dataset.type || "";
      const exists = this.tabs.some((t) => t.id === pid);
      if (!exists) {
        if (ptype === "terminal") {
          const terms = this.panelTerminals.get(pid);
          if (terms) {
            for (const trm of terms) {
              trm.cleanup();
              trm.resizeObs.disconnect();
              trm.term.dispose();
              const api = (window as any).electronAPI;
              if (api) api.terminalKill(trm.ptyId);
            }
            this.panelTerminals.delete(pid);
            this.panelActiveTermIdx.delete(pid);
            this._themeUnsubs.get(pid)?.();
            this._themeUnsubs.delete(pid);
          }
          const dd = document.querySelector(".si-term-shell-dropdown");
          if (dd) dd.remove();
        }
        if (ptype === "browser") {
          const bv = this.panelBrowsers.get(pid);
          if (bv) {
            bv.destroy();
            this.panelBrowsers.delete(pid);
          }
        }
        if (ptype === "markdown") {
          const mp = this.panelMarkdownPreviews.get(pid);
          if (mp) {
            mp.destroy();
            this.panelMarkdownPreviews.delete(pid);
          }
        }
        if (ptype === "code") {
          const editor = this.panelCodeEditors.get(pid);
          if (editor) {
            if (typeof (editor as any)._ctxMenuCleanup === "function") {
              (editor as any)._ctxMenuCleanup();
            }
            unregisterEditor(editor);
            editor.dispose();
            this.panelCodeEditors.delete(pid);
          }
          this._dirtyTabs.delete(pid);
        }
        if (ptype === "media") {
          const mv = this.panelMediaViewers.get(pid);
          if (mv) {
            mv.destroy();
            this.panelMediaViewers.delete(pid);
          }
        }
        if (ptype === "office") {
          const c = this.panelOfficeControllers.get(pid);
          if (c && typeof (c as any)._officeCleanup === "function") (c as any)._officeCleanup();
          this.panelOfficeControllers.delete(pid);
        }
        if (ptype === "editor" && typeof (p as any)._siTreeTimer === "number") {
          clearInterval((p as any)._siTreeTimer);
          (p as any)._siTreeTimer = null;
        }
        const subapp = this.panelSubapps.get(pid);
        if (subapp) {
          subapp();
          this.panelSubapps.delete(pid);
        }
        p.remove();
      } else {
        p.classList.toggle("active", pid === this.activeTab);
      }
    });
  }

  /* ── Terminal ───────────────────────────────────────────────────── */

  private async setupTerminalPanel(panel: HTMLElement, tabId: string, shellPath?: string, shellArgs?: string[]): Promise<void> {
    panel.innerHTML = `<div class="si-terminal-wrap">
      <div class="si-terminal-body"></div>
    </div>`;

    const body = panel.querySelector(".si-terminal-body") as HTMLElement;
    const panelId = tabId;

    if (this.panelTerminals.has(panelId) && (this.panelTerminals.get(panelId)?.length || 0) > 0) {
      const terms = this.panelTerminals.get(panelId)!;
      for (const trm of terms) {
        if (trm.term?.element) {
          body.appendChild(trm.term.element);
        }
      }
      if (terms[0]) {
        try { terms[0].term.focus(); } catch {}
      }
      this._bindTerminalSettings(panelId);
      return;
    }

    if (!this.panelTerminals.has(panelId)) {
      this.panelTerminals.set(panelId, []);
      this.panelActiveTermIdx.set(panelId, 0);
    }

    const api = (window as any).electronAPI;
    if (!api) {
      body.innerHTML = `<div class="si-empty-center"><i data-lucide="terminal" class="lucide"></i><span class="si-empty-title">${t("sessionInner.termEmpty")}</span></div>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: body });
      }
      return;
    }

    let shells: Array<{ name: string; path: string; args?: string[] }>;
    try {
      shells = await api.terminalListShells();
    } catch {
      shells = [];
    }
    if (shells.length === 0) {
      shells.push({ name: "Default", path: "" });
    }

    // Shell precedence: an explicit caller choice (the shell picker menu) >
    // the settings panel's default shell > the first detected shell.
    const settingsShell = () => {
      const want = _settingStr("terminal_shell", "auto");
      return want === "auto" ? undefined : shells.find((x) => x.path === want);
    };
    const getShellPath = () => shellPath || settingsShell()?.path || shells[0]?.path || "";
    const getShellArgs = () => shellArgs || settingsShell()?.args || shells[0]?.args || [];
    /** New terminals start in the user home (the spawn default) or in the
     *  workspace root when the settings panel asks for it. */
    const getSpawnCwd = (): string | undefined =>
      _settingStr("terminal_start_dir", "home") === "workspace"
        ? (getState().activeWorkspace || undefined)
        : undefined;

    const sh = getShellPath();
    const args = getShellArgs();
    // Lazy-load the terminal engine (xterm) on first terminal tab open.
    const [{ Terminal }, { FitAddon }, { WebglAddon }] = await Promise.all([
      import("@xterm/xterm"),
      import("@xterm/addon-fit"),
      import("@xterm/addon-webgl"),
    ]);
    let ptyId: number;
    try {
      const result = await api.terminalSpawn(sh || undefined, args.length ? args : undefined, getSpawnCwd());
      if (result.error) {
        body.innerHTML = `<div class="si-empty-center"><i data-lucide="alert-triangle" class="lucide"></i><span class="si-empty-title">${t("sessionInner.termError")}: ${this.esc(result.error)}</span></div>`;
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: body });
        }
        return;
      }
      ptyId = result.id as number;
    } catch (e: any) {
      body.innerHTML = `<div class="si-empty-center"><i data-lucide="alert-triangle" class="lucide"></i><span class="si-empty-title">${t("sessionInner.termError")}: ${this.esc(e?.message || String(e))}</span></div>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: body });
      }
      return;
    }

    const term = new Terminal({
      cursorBlink: _settingBool("terminal_cursor_blink", true),
      cursorStyle: terminalCursorStyle(),
      cursorInactiveStyle: terminalCursorInactiveStyle(),
      fontSize: _settingInt("terminal_font_size", 13, 9, 32),
      fontFamily: terminalFontFamily(),
      lineHeight: _settingNum("terminal_line_height", 1.3, 0.8, 2),
      drawBoldTextInBrightColors: _settingBool("terminal_bold_bright", true),
      rightClickSelectsWord: _settingStr("terminal_right_click", "menu") === "word",
      theme: this._terminalTheme(),
      cols: 60,
      rows: 16,
      allowProposedApi: true,
      allowTransparency: false,
      scrollback: _settingInt("terminal_scrollback", 10000, 200, 200000),
      smoothScrollDuration: 120,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);

    try {
      const webglAddon = new WebglAddon();
      term.loadAddon(webglAddon);
    } catch {}

    term.open(body);

    term.attachCustomKeyEventHandler((e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && (e.key === "c" || e.key === "C")) {
        if (term.hasSelection()) {
          e.preventDefault();
          navigator.clipboard.writeText(term.getSelection()).catch(() => {});
          return false;
        }
        return true;
      }
      if (mod && (e.key === "v" || e.key === "V")) {
        e.preventDefault();
        navigator.clipboard.readText().then((text) => term.paste(text)).catch(() => {});
        return false;
      }
      if (e.key === "Insert") {
        if (e.shiftKey) {
          e.preventDefault();
          navigator.clipboard.readText().then((text) => term.paste(text)).catch(() => {});
          return false;
        }
        if (e.ctrlKey && term.hasSelection()) {
          e.preventDefault();
          navigator.clipboard.writeText(term.getSelection()).catch(() => {});
          return false;
        }
      }
      return true;
    });

    /* context menu */
    const ctxMenu = document.createElement("div");
    ctxMenu.className = "context-menu hidden";
    ctxMenu.setAttribute("id", `term-ctx-menu-${tabId}`);
    document.body.appendChild(ctxMenu);
    let ctxTermTarget: any = null;

    // Selection strip for the terminal. xterm paints cells onto a canvas and
    // xterm.css sets `user-select: none`, so a DOM range can never exist here;
    // the selection lives in xterm's own model. A mouseup ending the drag is
    // the trigger, and the rect is derived from the selected cell rows.
    term.element?.addEventListener("mouseup", (e: MouseEvent) => {
      // Left button only: a right-press is the terminal's own context menu, and
      // the strip must not ride along with it.
      if (e.button !== 0) return;
      const sel = term.getSelection();
      if (!sel.trim()) return;
      const el = term.element;
      const screenEl = el?.querySelector(".xterm-screen") as HTMLElement | null;
      if (!screenEl) return;
      const sRect = screenEl.getBoundingClientRect();
      const cellH = sRect.height / Math.max(1, term.rows);
      const p = term.getSelectionPosition() as unknown as {
        startRow?: number;
        endRow?: number;
        start?: { y?: number };
        end?: { y?: number };
      } | undefined;
      const startRow = Number(p?.startRow ?? p?.start?.y ?? 0);
      const endRow = Number(p?.endRow ?? p?.end?.y ?? startRow);
      const top = sRect.top + startRow * cellH;
      const bottom = sRect.top + (endRow + 1) * cellH;
      showSelectionActionBarFor({
        text: sel,
        kind: "code",
        rect: { left: sRect.left, top, right: sRect.right, bottom },
      });
    });

    term.element?.addEventListener("contextmenu", (ev: MouseEvent) => {
      // The setting owns the gesture: "word" leaves it to xterm (which then
      // selects a word under the pointer), "paste" drops straight into the
      // shell, "menu" opens the copy/paste menu below.
      const rightClick = _settingStr("terminal_right_click", "menu");
      if (rightClick === "word") return;
      ev.preventDefault();
      if (rightClick === "paste") {
        navigator.clipboard.readText().then((text) => term.paste(text)).catch(() => {});
        return;
      }
      ctxTermTarget = term;
      renderContextMenuItems(
        [
          {
            action: "copy",
            icon: "copy",
            label: t("sessionInner.termCopy"),
            onClick: () => {
              const sel = ctxTermTarget?.getSelection();
              if (sel) navigator.clipboard.writeText(sel).catch(() => {});
            },
          },
          {
            action: "paste",
            icon: "clipboard",
            label: t("sessionInner.termPaste"),
            onClick: () => {
              navigator.clipboard.readText().then((text) => ctxTermTarget?.paste(text)).catch(() => {});
            },
          },
          { divider: true },
          {
            action: "send",
            icon: "send",
            label: t("sessionInner.termSendToChat"),
            onClick: () => {
              const content = getTerminalAllText(ctxTermTarget);
              if (content) {
                const shellName = sh.split(/[/\\]/).pop()?.replace(/\.(exe|cmd|bat)$/i, "") || "Terminal";
                const att: AttachmentMeta = {
                  name: shellName,
                  path: `terminal:${Date.now()}`,
                  content,
                  mime_type: "text/x-terminal",
                  size: content.split("\n").length,
                  is_binary: false,
                };
                addAttachments([att]);
              }
            },
          },
          ...pluginMenuSpecs("terminal"),
        ],
        { container: ctxMenu },
      );
      showContextMenu(ctxMenu, ev.clientX, ev.clientY, { target: term.element });
    });

    term.onData((data: string) => {
      api.terminalWrite(ptyId, data);
    });

    const dataCleanup = api.onTerminalData((d: { id: number; data: string }) => {
      if (d.id === ptyId) term.write(d.data);
    });
    const exitCleanup = api.onTerminalExit((d: { id: number }) => {
      if (d.id === ptyId) {
        term.write(`\r\n\x1b[33m${t("sessionInner.processExited")}\x1b[0m\r\n`);
      }
    });

    // Keep the PTY size in sync with the rendered grid. xterm v5's
    // `_renderService.dimensions` carries pixel metrics only (no cols/rows),
    // so the resize call below must derive the grid from the fit addon —
    // sending the PTY a wrong (or no) size makes conpty repaint prompts at
    // stale coordinates, which visually overlaps the command output.
    const syncPtySize = () => {
      try {
        const p = fitAddon.proposeDimensions();
        if (p && p.cols > 0 && p.rows > 0) {
          api.terminalResize(ptyId, p.cols, p.rows);
        }
      } catch {}
    };
    term.onResize(({ cols, rows }: { cols: number; rows: number }) => {
      if (cols > 0 && rows > 0) api.terminalResize(ptyId, cols, rows);
    });

    /** Fit the grid to the panel, or pin the column count when the settings
     *  panel asks for a fixed width.  xterm's own `onResize` forwards the new
     *  grid to the PTY, so both branches stay in sync without a second call. */
    const applySizing = () => {
      const mode = _settingStr("terminal_cols_mode", "auto");
      const fixed = mode === "80" || mode === "120" ? parseInt(mode, 10) : 0;
      if (fixed) {
        if (term.cols !== fixed) term.resize(fixed, term.rows);
        return;
      }
      try {
        fitAddon.fit();
        syncPtySize();
      } catch {}
    };

    let resizeTimer: any = null;
    const obs = new ResizeObserver(() => {
      if (resizeTimer) return;
      resizeTimer = setTimeout(() => {
        resizeTimer = null;
        applySizing();
      }, 50);
    });
    obs.observe(body);
    requestAnimationFrame(() => applySizing());

    // xterm 6 has no `bellStyle` option -- the bell is only an event -- so the
    // behaviour lives here and reads its setting at ring time: flipping the
    // switch in the settings panel takes effect on the next bell.
    term.onBell(() => {
      if (_settingStr("terminal_bell", "visual") !== "visual") return;
      body.classList.remove("si-term-bell");
      void body.offsetWidth;
      body.classList.add("si-term-bell");
    });

    term.onSelectionChange(() => {
      if (!_settingBool("terminal_copy_on_select", false)) return;
      const sel = term.getSelection();
      if (sel) navigator.clipboard.writeText(sel).catch(() => {});
    });

    term.focus();

    const cleanup = () => { dataCleanup(); exitCleanup(); ctxMenu.remove(); };
    const tms = this.panelTerminals.get(panelId) || [];
    tms.push({ ptyId, term, cleanup, resizeObs: obs, applySizing });
    this.panelTerminals.set(panelId, tms);

    document.addEventListener("click", (e) => {
      if (!ctxMenu.contains(e.target as Node)) {
        hideAnimated(ctxMenu);
      }
    });

    this._bindTerminalSettings(panelId);

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: panel });
    }
  }

  private killSubTerminal: ((pid: string, idx: number, body: HTMLElement) => void) | undefined;

  /** The terminal palette, derived from the app theme only.  There is no
   *  per-terminal override: a terminal that could be dark while the app is
   *  light (or the reverse) is exactly the mismatch that made the panel look
   *  broken, so the palette follows whatever theme is active right now. */
  private _terminalTheme(): Record<string, string> {
    const isLight = getState().theme === "light";
    return isLight ? {
      background: "#ffffff", foreground: "#1a1a1a", cursor: "#1a1a1a",
      selectionBackground: "rgba(150, 240, 200, 0.25)",
      black: "#000000", red: "#cd3131", green: "#00bc00",
      yellow: "#949800", blue: "#0451a5", magenta: "#bc05bc",
      cyan: "#0598bc", white: "#555555",
      brightBlack: "#666666", brightRed: "#cd3131", brightGreen: "#14ce14",
      brightYellow: "#b5ba00", brightBlue: "#0451a5", brightMagenta: "#bc05bc",
      brightCyan: "#0598bc", brightWhite: "#a5a5a5",
    } : {
      background: "#000000", foreground: "#f5f5f7", cursor: "#f5f5f7",
      selectionBackground: "rgba(150, 240, 200, 0.25)",
      black: "#000000", red: "#cd3131", green: "#0dbc79",
      yellow: "#e5e510", blue: "#2472c8", magenta: "#bc3fbc",
      cyan: "#11a8cd", white: "#e5e5e5",
      brightBlack: "#666666", brightRed: "#f14c4c", brightGreen: "#23d18b",
      brightYellow: "#f5f543", brightBlue: "#3b8eea", brightMagenta: "#d670d6",
      brightCyan: "#29b8db", brightWhite: "#ffffff",
    };
  }

  // One live-settings subscription per terminal panel.  Each bind replaces the
  // previous one, so rebuilding a panel can't pile up state subscriptions.
  private _themeUnsubs = new Map<string, () => void>();

  /** Push the current terminal settings into every live grid of a panel.
   *  xterm takes all of these at runtime, so a change in the settings panel
   *  lands on the open terminals immediately -- no re-spawn, no lost
   *  scrollback, no half-applied state. */
  private _applyTerminalSettings(panelId: string): void {
    const terms = this.panelTerminals.get(panelId);
    if (!terms || terms.length === 0) return;
    const theme = this._terminalTheme();
    const rightClick = _settingStr("terminal_right_click", "menu");
    for (const tt of terms) {
      const o = tt.term?.options;
      if (!o) continue;
      o.fontSize = _settingInt("terminal_font_size", 13, 9, 32);
      o.fontFamily = terminalFontFamily();
      o.lineHeight = _settingNum("terminal_line_height", 1.3, 0.8, 2);
      o.scrollback = _settingInt("terminal_scrollback", 10000, 200, 200000);
      o.cursorBlink = _settingBool("terminal_cursor_blink", true);
      o.cursorStyle = terminalCursorStyle();
      o.cursorInactiveStyle = terminalCursorInactiveStyle();
      o.drawBoldTextInBrightColors = _settingBool("terminal_bold_bright", true);
      o.rightClickSelectsWord = rightClick === "word";
      o.theme = theme;
      o.cursorWidth = _settingInt("terminal_cursor_width", 1, 1, 2);
      o.fontWeight = terminalFontWeight();
      o.fontWeightBold = terminalFontWeight();
      o.letterSpacing = _settingInt("terminal_letter_spacing", 0, -5, 10);
      o.minimumContrastRatio = _settingNum("terminal_min_contrast", 1, 1, 21);
      o.convertEol = _settingBool("terminal_convert_eol", false);
      o.wordSeparator = _settingStr("terminal_word_separator", "  ()[]{}\",'`");
      // Column mode is a layout decision, not an xterm option: the terminal's
      // own sizing helper re-fits or pins the grid and syncs the PTY.
      tt.applySizing?.();
    }
  }

  private _bindTerminalSettings(panelId: string): void {
    this._themeUnsubs.get(panelId)?.();
    this._themeUnsubs.delete(panelId);
    this._themeUnsubs.set(
      panelId,
      subscribe(() => this._applyTerminalSettings(panelId), ["theme", "themePreference", "settings"]),
    );
    // Re-attaching an existing panel can happen long after the settings were
    // last touched -- apply once so the reopened grid is never stale.
    this._applyTerminalSettings(panelId);
  }

  /** Monaco options owned by Settings → Assist → Editor.  Views are created
   *  once and then cached per tab, so nothing pushes these on a later change:
   *  without this the new value would only show up in an editor opened after
   *  it. */
  private _applyEditorSettings(): void {
    if (this.panelCodeEditors.size === 0) return;
    const opts = editorViewOptions();
    this.panelCodeEditors.forEach((view) => {
      view.updateOptions?.(opts);
    });
  }

  /** Content lines a diff may render before the renderer summarises it.  The
   *  "show full diff" switch only decides whether the ceiling is the full-file
   *  budget or the compact one; both numbers come from Settings now. */
  private _reviewMaxLines(): number {
    const full = _settingInt("review_max_lines", 4000, 200, 100000);
    if (this._reviewFullFile) return full;
    return Math.max(100, Math.round(full / 10));
  }

  /** The change set the panel opens on.  Without a workspace there is no diff
   *  to show at all, so the round-based view is the only meaningful default. */
  private _reviewSeedFilter(): void {
    const ws = getState().activeWorkspace;
    if (!ws) {
      this._reviewFilter = "lastRound";
      return;
    }
    const v = _settingStr("review_default_filter", "all");
    this._reviewFilter = ["all", "unstaged", "staged", "branch", "commit"].includes(v) ? v : "all";
  }

  /** Read the six Settings-owned review toggles into the panel's state.  Used
   *  when the panel mounts, when the reviewed context changes, and on a
   *  settings push -- so the same six values are the only source of truth. */
  private _seedReviewToggles(): boolean {
    const nextSplit = _settingBool("review_split_view", false);
    const nextCollapsed = _settingBool("review_collapse_unified", false);
    const nextWrap = _settingBool("review_wrap", false);
    const nextWordDiff = _settingBool("review_word_diff", false);
    const nextHideWs = _settingBool("review_hide_whitespace", false);
    const nextFullFile = _settingBool("review_full_file", true);
    const changed = nextSplit !== this._reviewSplitView || nextCollapsed !== this._reviewCollapsed
      || nextWrap !== this._reviewWrap || nextWordDiff !== this._reviewWordDiff
      || nextHideWs !== this._reviewHideWs || nextFullFile !== this._reviewFullFile;
    this._reviewSplitView = nextSplit;
    this._reviewCollapsed = nextCollapsed;
    this._reviewWrap = nextWrap;
    this._reviewWordDiff = nextWordDiff;
    this._reviewHideWs = nextHideWs;
    this._reviewFullFile = nextFullFile;
    // Split and word diff are mutually exclusive in the renderer; split wins,
    // matching what the toolbar's own buttons do.
    if (this._reviewSplitView) this._reviewWordDiff = false;
    this._reviewSettingsSig = reviewSettingsSig();
    return changed;
  }

  /** Same push for the review panel's Settings-owned toggles.  The signature
   *  guard keeps an in-panel menu toggle from being clobbered by an unrelated
   *  settings write: only a real change of the stored values re-applies. */
  private _applyReviewSettings(): void {
    const apply = this._reviewApply;
    if (!apply || !this._reviewDiffEl?.isConnected) return;
    if (reviewSettingsSig() === this._reviewSettingsSig) return;
    if (!this._seedReviewToggles()) return;
    apply();
  }

  /* ── Review (Git Diff) ─────────────────────────────────────────── */

  private async setupReviewPanel(panel: HTMLElement): Promise<void> {
    // Seed the panel defaults from the Settings "Assist" panel.
    this._seedReviewToggles();
    this._reviewSeedFilter();
    for (const o of this._reviewObservers) o.disconnect();
    this._reviewObservers = [];
    panel.innerHTML = `<div class="si-review-toolbar">
      <div class="settings-dropdown-wrap">
        <button class="settings-dropdown-trigger" type="button">
          <span>${this.esc(t(getState().activeWorkspace ? "sessionInner.reviewAllChanges" : "sessionInner.reviewLastRound"))}</span>
          <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
        </button>
        <div class="settings-dropdown"></div>
      </div>
      <div class="settings-dropdown-wrap si-review-actions" style="margin-inline-start:auto">
        <button class="settings-dropdown-trigger si-review-action-trigger" type="button">
          <i data-lucide="more-horizontal" class="lucide lucide-sm"></i>
        </button>
        <div class="settings-dropdown si-review-action-dropdown" style="inset-inline-end:0;inset-inline-start:auto;min-width:210px"></div>
      </div>
      <button class="settings-dropdown-trigger si-review-action-trigger si-review-collapse-btn" type="button" data-tooltip="${t("sessionInner.reviewCollapse")}">
        <i data-lucide="minus" class="lucide lucide-sm"></i>
      </button>
      <button class="settings-dropdown-trigger si-review-action-trigger si-review-split-btn" type="button" data-tooltip="${t("sessionInner.reviewSplitView")}">
        <i data-lucide="list" class="lucide lucide-sm"></i>
      </button>
      <div class="settings-dropdown-wrap si-review-git-wrap">
        <button class="settings-dropdown-trigger si-review-git-trigger" type="button">
          <i data-lucide="git-commit-horizontal" class="lucide lucide-sm si-review-git-icon"></i>
          <i data-lucide="chevron-down" class="lucide settings-dropdown-chevron"></i>
        </button>
        <div class="settings-dropdown si-review-git-dropdown" style="inset-inline-end:0;inset-inline-start:auto;min-width:170px"></div>
      </div>
</div>
<div class="si-review-commit-overlay hidden">
  <div class="search-palette">
    <div class="search-overlay-inner" style="align-items:flex-start">
      <i data-lucide="git-commit-horizontal" class="search-overlay-icon" style="margin-top:10px"></i>
      <textarea class="si-review-commit-input" placeholder="${this.esc(t("sessionInner.reviewCommitPrompt"))}" rows="2" style="flex:1;font-size:14px"></textarea>
      <kbd class="search-esc-hint" style="flex-shrink:0;margin-top:10px">${this.esc(t("sessionInner.reviewCommitBtn"))}</kbd>
    </div>
  </div>
</div>
<div class="si-empty-center" style="display:none"></div>
<div class="si-review-wrap" style="display:none">
      <div class="si-review-body">
        <div class="si-review-diff"></div>
        <div class="si-review-divider"></div>
        <div class="si-review-tree"></div>
      </div>
    </div>`;

    // Initialize toolbar icons immediately (before load() completes)
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: panel });
    }

    const diffEl = panel.querySelector(".si-review-diff") as HTMLElement;
    const treeEl = panel.querySelector(".si-review-tree") as HTMLElement;
    this._reviewDiffEl = diffEl;
    const modeWrap = panel.querySelector(".settings-dropdown-wrap") as HTMLElement;
    const toolbar = panel.querySelector(".si-review-toolbar") as HTMLElement;
    const modeTrigger = modeWrap.querySelector(".settings-dropdown-trigger") as HTMLElement;
    const modeLabel = modeTrigger.querySelector("span") as HTMLElement;
    const modeDropdown = modeWrap.querySelector(".settings-dropdown") as HTMLElement;
    const allDropdownWraps = panel.querySelectorAll(".settings-dropdown-wrap");
    const actionWrap = allDropdownWraps[1] as HTMLElement;
    const actionTrigger = actionWrap.querySelector(".settings-dropdown-trigger") as HTMLButtonElement;
    const actionDropdown = actionWrap.querySelector(".settings-dropdown") as HTMLElement;
    // Collapse / split / git-action buttons (added after the �?menu).
    const collapseBtn = panel.querySelector(".si-review-collapse-btn") as HTMLButtonElement;
    const splitBtn = panel.querySelector(".si-review-split-btn") as HTMLButtonElement;
    const gitWrap = panel.querySelector(".si-review-git-wrap") as HTMLElement;
    const gitTrigger = gitWrap.querySelector(".settings-dropdown-trigger") as HTMLButtonElement;
    const gitDropdown = gitWrap.querySelector(".settings-dropdown") as HTMLElement;
    const gitIcon = gitWrap.querySelector(".si-review-git-icon") as HTMLElement;
    // Inline commit input area.
    const commitWrap = document.getElementById("review-commit-overlay") as HTMLElement;
    const commitInput = document.getElementById("review-commit-input") as HTMLInputElement;
    // The toolbar stats element was removed (it duplicated the +/- counts
    // already shown in the diff header). Keep a no-op stub so the legacy
    // statsEl.innerHTML = ... assignments don't throw if re-added.
    const statsEl: { innerHTML: string } = (panel.querySelector(".si-review-stats") as HTMLElement | null)
      || { innerHTML: "" };
    const emptyEl = panel.querySelector(".si-empty-center") as HTMLElement;
    const wrapEl = panel.querySelector(".si-review-wrap") as HTMLElement;

    const api = (window as any).electronAPI;
    const workspace = getState().activeWorkspace;

    interface StatusEntry {
      path: string;
      staged: string;
      unstaged: string;
    }

    let currentReviewPath: string | undefined;
    let cachedBranch = "";
    let cachedEntries: StatusEntry[] | null = null;

    const parseStatus = (output: string): { branch: string; entries: StatusEntry[] } => {
      const lines = output.split("\n").filter((l) => l.trim());
      let branch = "";
      const entries: StatusEntry[] = [];
      for (const line of lines) {
        if (line.startsWith("## ")) {
          branch = line.slice(3).split("...")[0].trim();
          continue;
        }
        if (line.length >= 3) {
          const staged = line[0];
          const unstaged = line[1];
          const path = line.slice(3).trim();
          // Skip directory entries (trailing / from git status)
          if (path && !path.endsWith("/")) entries.push({ path, staged, unstaged });
        }
      }
      return { branch, entries };
    };

    const buildTree = (entries: StatusEntry[], activePath?: string): string => {
      const treeCacheKey = `${activePath || ""}\n` + entries.map((e) => `${e.staged}${e.unstaged}:${e.path}`).join("\n");
      const cachedTree = this._reviewTreeCache.get(treeCacheKey);
      if (cachedTree) return cachedTree;
      if (entries.length === 0) return this.reviewEmptyMarkup();
      const groups = new Map<string, StatusEntry[]>();
      for (const e of entries) {
        const dir = e.path.includes("/") ? e.path.split("/")[0] : "(root)";
        if (!groups.has(dir)) groups.set(dir, []);
        groups.get(dir)!.push(e);
      }
      let html = "";
      for (const [dir, items] of groups) {
        html += `<div class="si-review-tree-group">
          <div class="si-review-tree-folder">
            <img src="${getFolderIcon(dir, true)}" class="icon" style="width:14px;height:14px">
            <span>${this.esc(dir)}</span>
          </div>`;
        for (const item of items) {
          // Porcelain X column: "?" => untracked, non-space => staged, else unstaged.
          const isUntracked = item.staged === "?";
          const isStaged = !isUntracked && item.staged !== " ";
          const statusClass = isUntracked
            ? "si-review-untracked"
            : isStaged ? "si-review-staged" : "si-review-unstaged";
          // Single-letter badge (locale-independent): U/M/S.
          const statusLabel = isUntracked ? "U" : isStaged ? "S" : "M";
          const active = item.path === activePath ? " si-review-tree-file--active" : "";
          html += `<div class="si-review-tree-file${active}" data-path="${this.esc(item.path)}">
            <img src="${getFileIcon(item.path)}" class="icon" style="width:14px;height:14px">
            <span class="si-review-tree-name">${this.esc(item.path.split("/").pop() || item.path)}</span>
            <span class="si-review-tree-status ${statusClass}">${statusLabel}</span>
          </div>`;
        }
        html += `</div>`;
      }
      this.cachePut(this._reviewTreeCache, treeCacheKey, html);
      return html;
    };

    const parseDiffFn = (cacheKey: string, output: string, cacheSalt = "", overrides?: Partial<DiffRenderOptions>): string => {
      this._reviewDiffCache ??= new Map<string, string>();
      const saltedKey = cacheKey + cacheSalt;
      const cached = this._reviewDiffCache.get(saltedKey);
      if (cached) return cached;
      const parsed = this.parseDiff(output, overrides);
      this.cachePut(this._reviewDiffCache, saltedKey, parsed);
      return parsed;
    };

    const computeStats = (entries: StatusEntry[], fileDiff?: string): { adds: number; dels: number; label: string } => {
      if (fileDiff) {
        let adds = 0;
        let dels = 0;
        for (const l of fileDiff.split("\n")) {
          if (l.startsWith("+") && !l.startsWith("+++")) adds++;
          if (l.startsWith("-") && !l.startsWith("---")) dels++;
        }
        return { adds, dels, label: "" };
      }
      const staged = entries.filter((e) => e.staged !== " " && e.staged !== "?").length;
      const untracked = entries.filter((e) => e.staged === "?").length;
      // Unstaged excludes untracked (X="?") so new files are not double-counted.
      const unstaged = entries.filter((e) => e.staged !== "?" && e.unstaged !== " ").length;
      const parts: string[] = [];
      if (staged) parts.push(`${t("sessionInner.reviewStaged")}: ${staged}`);
      if (unstaged) parts.push(`${t("sessionInner.reviewUnstaged")}: ${unstaged}`);
      if (untracked) parts.push(`${t("sessionInner.reviewUntracked")}: ${untracked}`);
      return {
        adds: staged,
        dels: unstaged,
        label: parts.join("  ") || t("sessionInner.reviewNoChanges"),
      };
    };

    const load = async (filePath?: string, forceRefresh = false) => {
      const requestSeq = ++this._reviewRequestSeq;
      const ws = getState().activeWorkspace;
      // Reset per-panel view toggles when the underlying conversation, workspace
      // or mode changes, so one session's collapse/split state isn't carried into
      // another. Key = workspace + mode + session id.
      const st = getState();
      const ctxKey = `${ws || ""}|${st.workspaceMode || ""}|${st.sessionId || ""}`;
      if (ctxKey !== this._reviewContextKey) {
        this._reviewContextKey = ctxKey;
        // Re-seed from Settings rather than forcing `false`: the stored values
        // are the default this panel opens with, and resetting to `false` here
        // silently discarded the user's choice on the very first load.
        const changed = this._seedReviewToggles();
        if (changed) {
          if (this._reviewDiffEl) {
            this._reviewDiffEl.classList.toggle("review-collapsed", false);
            this._reviewDiffEl.classList.toggle("review-split", false);
          }
          applyContainerClasses();
          updateToolIcons();
        }
      }
      const showEmptyState = (html: string) => {
        if (requestSeq !== this._reviewRequestSeq) return;
        wrapEl.style.display = "none";
        emptyEl.style.display = "flex";
        emptyEl.innerHTML = html;
        // In normal mode (no workspace) the toolbar holds no controls; hide it
        // entirely so its border-bottom divider doesn't render as a stray line.
        toolbar.style.display = ws ? "" : "none";
        toolbar.style.borderBottom = "none";
        EALoader.destroyMounted(statsEl as HTMLElement);
        statsEl.innerHTML = "";
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: panel });
        }
      };

      // Update mode trigger label
      const labelMap: Record<string, string> = {
        all: t("sessionInner.reviewAllChanges"),
        unstaged: t("sessionInner.reviewUnstaged"),
        staged: t("sessionInner.reviewStaged"),
        branch: t("sessionInner.reviewBranch"),
        commit: t("sessionInner.reviewCommitChanges"),
        lastRound: t("sessionInner.reviewLastRound"),
      };
      modeLabel.textContent = labelMap[this._reviewFilter] || t("sessionInner.reviewLastRound");
      // Sync dropdown selections
      modeDropdown.querySelectorAll(".settings-dropdown-item").forEach((el) => {
        el.classList.toggle("selected", el.getAttribute("data-filter") === this._reviewFilter);
      });

      // "Changes" filter: show every artifact recorded in this conversation
      // (any round), so the user can inspect each recorded change's diff
      // regardless of which AI round first produced it.
      if (this._reviewFilter === "lastRound") {
        const arts = getState().artifacts;
        if (arts.length === 0) {
          showEmptyState(this.reviewEmptyInner());
          return;
        }
        // Every recorded change is shown, most recent first.
        const roundArts = [...arts].reverse();
        // An artifact pinned via the summary-panel "eye" button may belong to
        // an older round (or was already committed); always include it so the
        // review shows exactly the change that was clicked.
        if (this._reviewArtifact && !roundArts.some((a) => a.path === this._reviewArtifact!.path)) {
          roundArts.push(this._reviewArtifact);
        }
        if (requestSeq !== this._reviewRequestSeq) return;
        wrapEl.style.display = "flex";
        emptyEl.style.display = "none";
        toolbar.style.display = ws ? "" : "none";
        toolbar.style.borderBottom = ws ? "" : "none";
        const targetPath = filePath || roundArts[0]?.path || "";
        const currentArtifact = roundArts.find((a: ArtifactItem) => a.path === targetPath) || null;
        // Workspace-parity rendering: artifacts flow through the exact same
        // tree + diff pipeline as git changes (buildTree + parseDiffFn), so
        // both modes look and behave identically. Artifacts without a captured
        // diff fall back to their real on-disk content rendered as a new-file
        // diff instead of a bare info card.
        const artifactEntries: StatusEntry[] = roundArts.map((a: ArtifactItem) => ({
          path: a.path,
          staged: a.tool === "file_write" || a.tool === "write_file" || a.tool === "writeFile" ? "?" : " ",
          unstaged: "M",
        }));
        treeEl.innerHTML = buildTree(artifactEntries, targetPath);
        if (currentArtifact) {
          diffEl.innerHTML = "";
          EALoader.mountCentered(diffEl, "96px");
          const diffText = await this.artifactDiffText(currentArtifact);
          if (requestSeq !== this._reviewRequestSeq) return;
          EALoader.destroyMounted(diffEl);
          diffEl.innerHTML = diffText
            ? parseDiffFn(`artifact:${currentArtifact.path}`, diffText, [this._reviewSplitView, this._reviewWordDiff, this._reviewFullFile, this._reviewHideWs, this._reviewWrap].join("|"), { fileName: currentArtifact.path })
            : this.reviewEmptyMarkup();
          setupSplitViewScrollSync(diffEl);
          const { adds, dels } = computeStats(artifactEntries, diffText);
          EALoader.destroyMounted(statsEl as HTMLElement);
          statsEl.innerHTML = `<span class="si-review-stat-add">+${adds}</span> <span class="si-review-stat-del">-${dels}</span>`;
        } else {
          EALoader.destroyMounted(diffEl);
          diffEl.innerHTML = this.reviewEmptyMarkup();
          EALoader.destroyMounted(statsEl as HTMLElement);
          statsEl.innerHTML = "";
        }
        treeEl.querySelectorAll(".si-review-tree-file").forEach((el) => {
          el.addEventListener("click", () => {
            const path = (el as HTMLElement).dataset.path;
            if (path) load(path);
          });
        });
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: panel });
        }
        return;
      }

      // (artifact/lastRound already handled above �?only git modes reach here)
      

      if (!ws) {
        showEmptyState(`<i data-lucide="git-pull-request" class="lucide"></i>
          <span class="si-empty-title">${t("sessionInner.reviewNoChanges")}</span>`);
        return;
      }
      if (!api) {
        showEmptyState(`<i data-lucide="alert-circle" class="lucide"></i>
          <span class="si-empty-title">${t("sessionInner.apiNotAvailable")}</span>`);
        return;
      }

      // Show loading state before potentially slow git operations
      wrapEl.style.display = "flex";
      toolbar.style.borderBottom = "";
      emptyEl.style.display = "none";
      diffEl.innerHTML = "";
      EALoader.mountCentered(diffEl, "96px");
      if (!cachedEntries || forceRefresh) {
        treeEl.innerHTML = "";
      }
      statsEl.innerHTML = "";
      // statsEl may be the no-op stub (the toolbar stats element was
      // removed); only mount the loader into a real DOM element.
      if (statsEl instanceof HTMLElement) {
        EALoader.mountCentered(statsEl, "96px");
      }

      if (!cachedEntries || forceRefresh) {
        const statusRes = await api.gitStatus(ws);
        if (requestSeq !== this._reviewRequestSeq) return;
        if (statusRes.error) {
            showEmptyState(`<i data-lucide="alert-triangle" class="lucide"></i>
              <span class="si-empty-title">${this.esc(statusRes.error)}</span>`);
          return;
        }

        const { branch, entries } = parseStatus(statusRes.output);
        if (!branch && entries.length === 0) {
          showEmptyState(`<i data-lucide="folder-git-2" class="lucide"></i>
            <span class="si-empty-title">${t("sessionInner.reviewNoGit")}</span>`);
          return;
        }
        cachedBranch = branch;
        cachedEntries = entries;
      }
      const entries = cachedEntries || [];
      const branch = cachedBranch;

      // Apply filter to entries
      let filteredEntries = entries;
      if (this._reviewFilter === "unstaged") {
        filteredEntries = entries.filter((e: StatusEntry) => e.unstaged !== " ");
      } else if (this._reviewFilter === "staged") {
        filteredEntries = entries.filter((e: StatusEntry) => e.staged !== " " && e.staged !== "?");
      }

      // When the current filter matches no entries, show an empty state
      // instead of closing the panel. Closing it (closeReviewTab) made the
      // panel "kick out" whenever the status cache lagged behind a fresh
      // `git add` (e.g. a newly staged file still read as unstaged), which
      // looked like staged files disappeared for no reason.
      if (filteredEntries.length === 0 && !filePath && this._reviewFilter !== "lastRound") {
        showEmptyState(this.reviewEmptyInner());
        return;
      }

      // Show file tree
      treeEl.innerHTML = buildTree(filteredEntries, filePath);
      let adds = 0;
      let dels = 0;
      let statsLabel = "";

      if (filePath) {
        currentReviewPath = filePath;
        diffEl.innerHTML = "";
        EALoader.mountCentered(diffEl, "96px");
        // Use gitDiffEx for staged/unstaged/branch/commit filters
        const isEx = this._reviewFilter === "all" || this._reviewFilter === "staged" || this._reviewFilter === "unstaged" || this._reviewFilter === "branch" || this._reviewFilter === "commit";
        const diffRes = isEx ? await api.gitDiffEx(ws, this._reviewFilter, filePath) : await api.gitDiff(ws, filePath);
        if (requestSeq !== this._reviewRequestSeq) return;
        if (diffRes.error) {
          EALoader.destroyMounted(diffEl);
          diffEl.innerHTML = `<div class="si-empty-center"><i data-lucide="alert-triangle" class="lucide"></i><span class="si-empty-title">${this.esc(diffRes.error)}</span></div>`;
          if (typeof (window as any).lucide !== "undefined") {
            (window as any).lucide.createIcons({ root: diffEl });
          }
        } else {
          // "加载完整文件" (default): merge the on-disk file content into the
          // compact diff so unchanged lines are shown as context too.
          // "不加载完整文件": keep the compact hunk-only view.
          let diffOut = diffRes.output;
          if (this._reviewFullFile && diffOut && !/^(Binary files |GIT binary patch)/.test(diffOut)) {
            try {
              // git status/diff report repo-relative paths; readFile needs an
              // absolute one resolved against the workspace root.
              const absPath = /^[a-zA-Z]:[\\/]/.test(filePath) || filePath.startsWith("/")
                ? filePath.replace(/\\/g, "/")
                : `${ws.replace(/[/\\]+$/, "")}/${filePath.replace(/\\/g, "/")}`;
              const fr = await api.readFile(absPath);
              if (fr && !fr.is_binary && typeof fr.content === "string" && fr.content) {
                diffOut = expandDiffToFullFile(diffOut, fr.content);
              }
            } catch { /* fall back to compact diff */ }
          }
          EALoader.destroyMounted(diffEl);
          diffEl.innerHTML = parseDiffFn(`${ws}:${filePath}`, diffOut, [this._reviewSplitView, this._reviewWordDiff, this._reviewFullFile, this._reviewHideWs, this._reviewWrap].join("|"));
          setupSplitViewScrollSync(diffEl);
          ({ adds, dels } = computeStats(filteredEntries, diffRes.output));
        }
      } else {
        currentReviewPath = undefined;
        ({ adds, dels, label: statsLabel } = computeStats(filteredEntries));
        EALoader.destroyMounted(diffEl);
        diffEl.innerHTML = this.reviewEmptyMarkup();
      }

      if (requestSeq !== this._reviewRequestSeq) return;
      // Summary view (no file selected) hides the staged/unstaged/untracked
      // counts - they were noisy and read like line stats. Per-file view still
      // shows +/- line counts.
      EALoader.destroyMounted(statsEl as HTMLElement);
      statsEl.innerHTML = statsLabel
        ? ""
        : `<span class="si-review-stat-add">+${adds}</span> <span class="si-review-stat-del">-${dels}</span>`;

      treeEl.querySelectorAll(".si-review-tree-file").forEach((el) => {
        el.addEventListener("click", () => {
          const path = (el as HTMLElement).dataset.path;
          if (path) load(path, false);
        });
      });

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: panel });
      }
    };

    this._reviewLoad = load;

    // Build action dropdown items (right dropdown - functional actions).
    // refresh is a plain action; the rest are toggles with a checkmark.
    // Merged overflow buttons (collapse/split/git) are prepended at the top.
    const buildActionItems = () => {
      type Item = { action: string; icon: string; labelKey: string; toggle?: boolean; divider?: boolean };
      const items: Item[] = [];
      // Overflow: collapse(0), split(1), git(2) merged when _reviewOverflow > idx.
      const overflowDefs: Array<{ action: string; icon: string; labelKey: string }> = [
        { action: "_overflow_collapse", icon: "minus", labelKey: "reviewCollapse" },
        { action: "_overflow_split", icon: "list", labelKey: "reviewSplitView" },
        { action: "_overflow_git", icon: "git-commit-horizontal", labelKey: "reviewActionCommit" },
      ];
      for (let i = 0; i < this._reviewOverflow && i < overflowDefs.length; i++) {
        items.push(overflowDefs[i]);
      }
      if (this._reviewOverflow > 0) {
        items.push({ action: "_ov_div", icon: "", labelKey: "", divider: true });
      }
      items.push(
        { action: "refresh", icon: "refresh-cw", labelKey: "reviewRefresh" },
        { action: "wrap", icon: "wrap-text", labelKey: "reviewAutoWrap", toggle: true },
        { action: "_divider1", icon: "", labelKey: "", divider: true },
        { action: "fullFile", icon: "file-output", labelKey: "reviewNotFullFile", toggle: true },
        { action: "wordDiff", icon: "diff", labelKey: "reviewWordDiff", toggle: true },
        { action: "hideWs", icon: "eraser", labelKey: "reviewHideWhitespace", toggle: true },
      );
      const isChecked = (a: string): boolean => {
        if (a === "wrap") return this._reviewWrap;
        if (a === "fullFile") return !this._reviewFullFile;
        if (a === "wordDiff") return this._reviewWordDiff;
        if (a === "hideWs") return this._reviewHideWs;
        return false;
      };
      actionDropdown.innerHTML = items.map((item) => {
        if (item.divider) {
          return `<div class="settings-dropdown-divider"></div>`;
        }
        const checked = item.toggle && isChecked(item.action) ? " checked" : "";
        return `<div class="settings-dropdown-item review-action-item${checked}" data-action="${item.action}">
          <i data-lucide="${item.icon}" class="lucide lucide-xs"></i>
          <span>${this.esc(t("sessionInner." + item.labelKey))}</span>
          <i data-lucide="check" class="lucide lucide-xs review-check"></i>
        </div>`;
      }).join("");
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: actionDropdown });
      }
    };
    buildActionItems();

    // Responsive overflow: as the sidebar narrows, merge buttons into the �?
    // menu from left to right (collapse -> split -> git module). The �?menu
    // and the mode selector are always kept. Button sizes never change.
    const setOverflow = (count: number) => {
      const n = Math.max(0, Math.min(3, count));
      if (n === this._reviewOverflow) return;
      this._reviewOverflow = n;
      // In normal mode (no workspace) all buttons stay hidden.
      const hasWs = !!getState().activeWorkspace;
      collapseBtn.style.display = hasWs && n <= 0 ? "" : "none";
      splitBtn.style.display = hasWs && n <= 1 ? "" : "none";
      gitWrap.style.display = hasWs && n <= 2 ? "" : "none";
      buildActionItems();
    };
    // Measure by the sidebar panel width, which is the real constraint.
    // Sidebar default is 280px; mode selector + �?need ~170px.
    const measureOverflow = () => {
      // Normal mode (no workspace): no buttons to merge, all hidden.
      if (!getState().activeWorkspace) { setOverflow(0); return; }
      const w = panel.clientWidth;
      let need = 0;
      if (w < 230) need = 3;       // everything merged, only mode + �?left
      else if (w < 270) need = 2;  // git + split merged
      else if (w < 310) need = 1; // collapse merged
      else need = 0;
      setOverflow(need);
    };
    const ro = new ResizeObserver(() => measureOverflow());
    this._reviewObservers.push(ro);
    ro.observe(panel);
    measureOverflow();

    // Collapse the right-side file tree when the panel gets narrow so the
    // diff area can use the full width.
    const reviewBody = panel.querySelector(".si-review-body") as HTMLElement;
    const reviewTree = panel.querySelector(".si-review-tree") as HTMLElement;
    const reviewDivider = panel.querySelector(".si-review-divider") as HTMLElement;
    const measureTree = () => {
      if (!reviewBody) return;
      reviewBody.classList.toggle("tree-collapsed", panel.clientWidth < 340);
    };
    const treeRo = new ResizeObserver(() => measureTree());
    this._reviewObservers.push(treeRo);
    treeRo.observe(panel);
    measureTree();

    // Draggable divider between diff and tree (mirrors editor divider).
    if (reviewDivider && reviewTree) {
      let rResizing = false, rStartX = 0, rStartW = 0;
      reviewDivider.addEventListener("mousedown", (e) => {
        rResizing = true;
        rStartX = e.clientX;
        rStartW = reviewTree.offsetWidth;
        reviewDivider.classList.add("resizing");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
        e.preventDefault();
      });
      document.addEventListener("mousemove", (e) => {
        if (!rResizing) return;
        const newW = Math.max(120, Math.min(480, rStartW - (e.clientX - rStartX)));
        reviewTree.style.flex = "0 0 " + newW + "px";
      });
      document.addEventListener("mouseup", () => {
        if (!rResizing) return;
        rResizing = false;
        reviewDivider.classList.remove("resizing");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        // Persist tree width
        const w = reviewTree.offsetWidth;
        try { localStorage.setItem("review-tree-width", String(w)); } catch {}
      });

      // Restore saved width
      try {
        const saved = localStorage.getItem("review-tree-width");
        if (saved) {
          const w = parseInt(saved, 10);
          if (w >= 120 && w <= 480) reviewTree.style.flex = "0 0 " + w + "px";
        }
      } catch {}
    }

    // Apply CSS-class-driven toggles to the diff container.
    const applyContainerClasses = () => {
      if (!this._reviewDiffEl) return;
      this._reviewDiffEl.classList.toggle("review-wrap", this._reviewWrap);
      this._reviewDiffEl.classList.toggle("review-collapsed", this._reviewCollapsed);
      this._reviewDiffEl.classList.toggle("review-split", this._reviewSplitView);
    };
    applyContainerClasses();

    // Update the collapse/split button icons to reflect the current state.
    // Lucide's createIcons() replaces <i> with <svg>, so the icon element
    // no longer has data-lucide on the next call �?rebuild the inner HTML.
    const updateToolIcons = () => {
      collapseBtn.innerHTML = `<i data-lucide="${this._reviewCollapsed ? "plus" : "minus"}" class="lucide lucide-sm"></i>`;
      splitBtn.innerHTML = `<i data-lucide="${this._reviewSplitView ? "columns" : "list"}" class="lucide lucide-sm"></i>`;
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: collapseBtn });
        (window as any).lucide.createIcons({ root: splitBtn });
      }
    };
    updateToolIcons();

    // Settings → Assist → Review must land on the mounted panel, not only on
    // the next one: expose the re-apply closure the settings push calls.
    this._reviewApply = () => {
      applyContainerClasses();
      updateToolIcons();
      buildActionItems();
      if (currentReviewPath) load(currentReviewPath, true);
      else load(undefined, true);
    };

    // Collapse / expand all diff bodies.
    collapseBtn.addEventListener("click", () => {
      this._reviewCollapsed = !this._reviewCollapsed;
      applyContainerClasses();
      updateToolIcons();
    });

    // Toggle split (two-column) view. Mutually exclusive with word diff.
    splitBtn.addEventListener("click", () => {
      this._reviewSplitView = !this._reviewSplitView;
      if (this._reviewSplitView) {
        this._reviewWordDiff = false;
      }
      applyContainerClasses();
      updateToolIcons();
      buildActionItems();
      if (currentReviewPath) load(currentReviewPath, true);
      else load(undefined, true);
    });

    // Git action dropdown: commit / push / pull.
    const gitActionItems: Array<{ action: "commit" | "push" | "pull"; icon: string; labelKey: string }> = [
      { action: "commit", icon: "git-commit-horizontal", labelKey: "reviewActionCommit" },
      { action: "push", icon: "upload", labelKey: "reviewActionPush" },
      { action: "pull", icon: "download", labelKey: "reviewActionPull" },
    ];
    const buildGitActionItems = () => {
      gitDropdown.innerHTML = gitActionItems.map((item) =>
        `<div class="settings-dropdown-item review-action-item${item.action === this._reviewGitAction ? " checked" : ""}${item.action === "push" ? " si-review-git-disabled" : ""}" data-git-action="${item.action}">
          <i data-lucide="${item.icon}" class="lucide lucide-xs"></i>
          <span>${this.esc(t("sessionInner." + item.labelKey))}</span>
        </div>`
      ).join("");
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: gitDropdown });
      }
      // Update the trigger icon to the currently-selected action.
      const sel = gitActionItems.find((i) => i.action === this._reviewGitAction);
      gitIcon.setAttribute("data-lucide", sel?.icon || "git-commit-horizontal");
      gitTrigger.dataset.tooltip = sel ? t("sessionInner." + sel.labelKey) : "";
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: gitTrigger });
      }
    };
    buildGitActionItems();
    // Move dropdowns to document.body so they escape the sidebar's stacking
    // context (transform) and overflow:clip. Position with fixed coords.
    const moveToBody = (dd: HTMLElement) => {
      if (dd.parentElement !== document.body) document.body.appendChild(dd);
      // A body-level portal outlives the panel that opened it, so the scope
      // host has to know to dismiss it when the conversation or view changes.
      registerSurface(dd, { scope: "ephemeral", onClose: () => dd.classList.remove("open") });
    };
    moveToBody(actionDropdown);
    moveToBody(gitDropdown);
    // The git dropdown lives under document.body, so the generic
    // `.settings-dropdown-wrap:has(.settings-dropdown.open)` chevron-rotate
    // rule never matches. Mirror the open state onto the wrap so the
    // trigger's chevron flips like every other dropdown.
    const gitOpenObs = new MutationObserver(() => {
      gitWrap.classList.toggle("dd-open", gitDropdown.classList.contains("open"));
    });
    this._reviewObservers.push(gitOpenObs);
    gitOpenObs.observe(gitDropdown, { attributes: true, attributeFilter: ["class"] });
    const positionDropdown = (dd: HTMLElement, trigger: HTMLElement) => {
      moveToBody(dd);
      showAnchoredDropdown(dd, trigger);
    };
    // Git trigger: clicking anywhere on the trigger opens/closes the action
    // dropdown (same interaction as the filter dropdown). The action only
    // runs when an item inside the dropdown is clicked, so the trigger can
    // no longer fire commit/push by accident.
    gitTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = gitDropdown.classList.contains("open");
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      if (!isOpen) {
        buildGitActionItems();
        gitDropdown.classList.add("open");
        positionDropdown(gitDropdown, gitTrigger);
      }
    });
    gitDropdown.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      const item = target.closest(".settings-dropdown-item") as HTMLElement;
      if (!item) return;
      e.stopPropagation();
      const action = (item.getAttribute("data-git-action") || "commit") as "commit" | "push" | "pull";
      this._reviewGitAction = action;
      buildGitActionItems();
      gitDropdown.classList.remove("open");
      // Selecting an item runs it immediately (command-menu style).
      void runGitAction();
    });

    // Trigger the selected git action when the trigger button is clicked directly
    // (not the chevron area). For commit, open the inline input; push/pr run directly.
    gitTrigger.addEventListener("dblclick", (e) => e.preventDefault());
    const runGitAction = async () => {
      const ws = getState().activeWorkspace;
      if (!ws) return;
      if (this._reviewGitAction === "commit") {
        showAnimated(commitWrap);
        registerSurface(commitWrap, { scope: "ephemeral", onClose: () => hideAnimated(commitWrap) });
        commitInput.value = "";
        commitInput.focus();
        return;
      }
      const api = (window as any).electronAPI;
      if (!api) return;
      if (this._reviewGitAction === "push") {
        const res = await api.gitPush(ws);
        if (res.error) { showToast(res.error, "", "error", "Review"); }
      } else if (this._reviewGitAction === "pull") {
        const res = await api.gitPull(ws);
        if (res.error) { showToast(res.error, "", "error", "Review"); }
      }
      if (currentReviewPath) load(currentReviewPath, true);
      else load(undefined, true);
    };

    // Commit input area handlers.
    const commitCommit = () => {
      const ws = getState().activeWorkspace;
      const message = commitInput.value.trim();
      if (!ws || !message) return;
      const api = (window as any).electronAPI;
      if (!api) return;
      api.gitCommit(ws, message).then((res: any) => {
        if (res.error) { showToast(res.error, "", "error", "Review"); return; }
        hideAnimated(commitWrap);
        currentReviewPath = undefined;
        cachedEntries = null;
        cachedBranch = "";
        load(undefined, true);
      });
    };
    commitInput.addEventListener("keydown", (e: KeyboardEvent) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        commitCommit();
      } else if (e.key === "Escape") {
        hideAnimated(commitWrap);
        commitInput.value = "";
      }
    });
    // Click outside the palette (on the dimmed overlay) closes it, matching
    // the search overlay's behavior in bindSearchOverlay().
    commitWrap.addEventListener("click", (e: MouseEvent) => {
      if (e.target === commitWrap) {
        hideAnimated(commitWrap);
        commitInput.value = "";
      }
    });

    // Action menu dropdown - use delegation for dynamically built items.
    actionTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = actionDropdown.classList.contains("open");
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      if (!isOpen) {
        buildActionItems();
        actionDropdown.classList.add("open");
        positionDropdown(actionDropdown, actionTrigger);
      }
    });
    actionDropdown.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      const item = target.closest(".settings-dropdown-item") as HTMLElement;
      if (!item) return;
      e.stopPropagation();
      const action = item.getAttribute("data-action") || "";
      if (action === "refresh") {
        currentReviewPath = undefined;
        cachedEntries = null;
        cachedBranch = "";
        load(undefined, true);
        actionDropdown.classList.remove("open");
        return;
      }
      // Overflow buttons merged from the toolbar when narrow.
      if (action === "_overflow_collapse") { collapseBtn.click(); actionDropdown.classList.remove("open"); return; }
      if (action === "_overflow_split") { splitBtn.click(); actionDropdown.classList.remove("open"); return; }
      if (action === "_overflow_git") { gitTrigger.click(); actionDropdown.classList.remove("open"); return; }
      // Toggle handlers: flip state, re-render, refresh checkmarks.
      let changed = false;
      if (action === "wrap") { this._reviewWrap = !this._reviewWrap; changed = true; }
      else if (action === "fullFile") { this._reviewFullFile = !this._reviewFullFile; changed = true; }
      else if (action === "wordDiff") {
        this._reviewWordDiff = !this._reviewWordDiff;
        if (this._reviewWordDiff) this._reviewSplitView = false;
        changed = true;
      }
      else if (action === "hideWs") { this._reviewHideWs = !this._reviewHideWs; changed = true; }
      if (changed) {
        applyContainerClasses();
        updateToolIcons();
        if (currentReviewPath) {
          load(currentReviewPath, true);
        } else {
          load(undefined, true);
        }
        buildActionItems();
      }
      actionDropdown.classList.remove("open");
    });

    // Build filter dropdown items (left dropdown �?mode/filter selection)
    const buildFilterItems = () => {
      const ws = getState().activeWorkspace;
      const items: Array<{ filter: string; labelKey: string }> = [];
      if (ws) {
        items.push(
          { filter: "all", labelKey: "reviewAllChanges" },
          { filter: "unstaged", labelKey: "reviewUnstaged" },
          { filter: "staged", labelKey: "reviewStaged" },
          { filter: "branch", labelKey: "reviewBranch" },
          { filter: "commit", labelKey: "reviewCommitChanges" },
          { filter: "lastRound", labelKey: "reviewLastRound" },
        );
      } else {
        items.push({ filter: "lastRound", labelKey: "reviewLastRound" });
      }
      // Without a workspace there is no git to review: force the artifact
      // view and hide both dropdowns so the panel just shows all session changes.
      if (!ws && this._reviewFilter !== "lastRound") {
        this._reviewFilter = "lastRound";
        this._reviewMode = "artifact";
      }
      modeWrap.style.display = ws ? "" : "none";
      actionWrap.style.display = ws ? "" : "none";
      // In normal mode (no workspace) all review buttons are disabled -
      // only the modified-file artifact list is shown.
      collapseBtn.style.display = ws ? (this._reviewOverflow <= 0 ? "" : "none") : "none";
      splitBtn.style.display = ws ? (this._reviewOverflow <= 1 ? "" : "none") : "none";
      gitWrap.style.display = ws ? (this._reviewOverflow <= 2 ? "" : "none") : "none";
      modeDropdown.innerHTML = items.map((item) =>
        `<div class="settings-dropdown-item${item.filter === this._reviewFilter ? " selected" : ""}" data-filter="${item.filter}">${this.esc(t("sessionInner." + item.labelKey))}</div>`
      ).join("");
      // Update trigger label to match current filter
      const labelMap: Record<string, string> = {
        all: t("sessionInner.reviewAllChanges"),
        unstaged: t("sessionInner.reviewUnstaged"),
        staged: t("sessionInner.reviewStaged"),
        branch: t("sessionInner.reviewBranch"),
        commit: t("sessionInner.reviewCommitChanges"),
        lastRound: t("sessionInner.reviewLastRound"),
      };
      modeLabel.textContent = labelMap[this._reviewFilter] || t("sessionInner.reviewLastRound");
    };
    buildFilterItems();

    // Filter dropdown �?delegation
    modeTrigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = modeDropdown.classList.contains("open");
      document.querySelectorAll(".settings-dropdown.open").forEach((dd) => dd.classList.remove("open"));
      if (!isOpen) {
        buildFilterItems();
        modeDropdown.classList.add("open");
      }
    });
    modeDropdown.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      const item = target.closest(".settings-dropdown-item") as HTMLElement;
      if (!item) return;
      e.stopPropagation();
      const filter = item.getAttribute("data-filter") || "all";
      if (filter === this._reviewFilter) { modeDropdown.classList.remove("open"); return; }
      this._reviewFilter = filter;
      this._reviewMode = filter === "lastRound" ? "artifact" : "git";
      modeLabel.textContent = item.textContent || "";
      buildFilterItems();
      currentReviewPath = undefined;
      cachedEntries = null;
      cachedBranch = "";
      modeDropdown.classList.remove("open");
      load(undefined, true);
    });
    document.addEventListener("click", (e) => {
      if (!modeWrap.contains(e.target as Node)) {
        modeDropdown.classList.remove("open");
      }
    });

    // Always load the review panel state when opened, even without a pending file
    if (this._reviewFilePending !== undefined) {
      await load(this._reviewFilePending, false);
      this._reviewFilePending = undefined;
    } else {
      // Opening the review tab directly (not via "view changes" with an
      // artifact): default to "all changes" so the workspace's full diff is
      // shown, never a stale "last round".
      if (getState().activeWorkspace) {
        this._reviewFilter = "all";
        this._reviewMode = "git";
        this._reviewArtifact = null;
      }
      await load(undefined, false);
    }
  }

  private setupBrowserPanel(panel: HTMLElement, tabId: string, startUrl?: string): void {
    panel.style.overflow = "hidden";

    const existing = this.panelBrowsers.get(tabId);
    if (existing) {
      panel.appendChild(existing.container);
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: panel });
      }
      return;
    }

    const container = document.createElement("div");
    container.style.cssText = "display:flex;flex-direction:column;flex:1;min-height:0;height:100%;";
    panel.appendChild(container);
    const url = startUrl || getDefaultHomepage();
    const bv = new BrowserView(container, {
      startUrl: url,
      // The compact chrome is sized for a 280px side panel. In the subwindow
      // the page owns the whole window, so it gets the full nav bar.
      compact: !inChildWindow(),
      onTitleChange: (title: string) => {
        const tab = this.tabs.find(t => t.id === tabId);
        if (tab && title) {
          tab.label = title;
          this.renderTabLabels();
        }
      },
      onFaviconChange: (faviconUrl: string) => {
        const tab = this.tabs.find(t => t.id === tabId);
        if (tab) {
          tab.favicon = faviconUrl;
          this.renderTabIcons();
        }
      },
      onNewWindow: (newUrl: string) => {
        this.createTab("browser", { startUrl: newUrl });
      },
      onDialog: (type, message, _defaultPromptText, respond) => {
        this.setTabState(tabId, "awaiting_approval");
        if (type === "alert") {
          Dialog.alert(message, "", { container: this.el }).then(() => { respond(null); this.setTabState(tabId, ""); });
        } else if (type === "confirm") {
          Dialog.confirm(message, "", { container: this.el }).then((ok) => { respond(ok ? "true" : "false"); this.setTabState(tabId, ""); });
        } else if (type === "prompt") {
          Dialog.prompt(message, "", { defaultValue: _defaultPromptText, container: this.el }).then((val) => { respond(val); this.setTabState(tabId, ""); });
        } else {
          respond(null);
          this.setTabState(tabId, "");
        }
      },
      onPermissionRequest: (_permission, _origin, _request) => {
        // Session-level permission answers resolve instantly (allowlist /
        // origin grants), so the orange light is a pulse, not a permanent
        // stain — clear it shortly after unless a dialog took the tab over.
        this.setTabState(tabId, "permission");
        const prev = this._permLightTimers.get(tabId);
        if (prev) window.clearTimeout(prev);
        this._permLightTimers.set(tabId, window.setTimeout(() => {
          this._permLightTimers.delete(tabId);
          const tab = this.tabs.find(t => t.id === tabId);
          if (tab && tab.state === "permission") this.setTabState(tabId, "");
        }, 3500));
      },
      onBeforeUnload: (respond) => {
        // "Leave without saving?" — same yellow light + same in-app dialog
        // family as the page's own alert/confirm prompts above.
        this.setTabState(tabId, "awaiting_approval");
        Dialog.confirm(t("browserNav.unloadConfirm"), "", { container: this.el }).then((ok) => {
          respond(ok);
          const tab = this.tabs.find(t => t.id === tabId);
          if (tab && tab.state === "awaiting_approval") this.setTabState(tabId, "");
        });
      },
    });
    this.panelBrowsers.set(tabId, bv);
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: panel });
    }
  }

  private setupMarkdownPanel(panel: HTMLElement, tabId: string, content?: string, title?: string, filePath?: string): void {
    panel.style.overflow = "hidden";

    const existing = this.panelMarkdownPreviews.get(tabId);
    if (existing) {
      panel.appendChild(existing.container);
      return;
    }

    const basePath = filePath ? filePath.replace(/[/\\][^/\\]+$/, "") : "";
    const container = document.createElement("div");
    container.style.cssText = "display:flex;flex-direction:column;flex:1;min-height:0;height:100%;";
    panel.appendChild(container);
    const mp = new MarkdownPreviewView(container, title || "", {
      onOpenLink: (href) => this._handleMarkdownLink(href, tabId),
    });
    if (content) mp.setContent(content, basePath || undefined);
    this.panelMarkdownPreviews.set(tabId, mp);
  }

  public async openMarkdownPreview(content: string, title?: string, filePath?: string): Promise<void> {
    const existing = filePath
      ? this.tabs.find(t => t.type === "markdown" && t.filePath === filePath)
      : this.tabs.find(t => t.type === "markdown");
    if (existing) {
      this.activeTab = existing.id;
      existing.content = content;
      if (title) existing.title = title;
      existing.filePath = filePath || existing.filePath;
      const mp = this.panelMarkdownPreviews.get(existing.id);
      const basePath = filePath ? filePath.replace(/[/\\][^/\\]+$/, "") : "";
      if (mp) mp.setContent(content, basePath || undefined);
      this.revealPanel();
      await this.renderTabs();
      return;
    }
    await this.createTab("markdown", { content, title, filePath });
  }

  private _handleMarkdownLink(href: string, tabId: string): void {
    // file:// targets (explicit local file links) open in the in-app viewer
    if (/^file:\/\//i.test(href)) {
      let p = href.replace(/^file:\/\/\/?/i, "");
      try { p = decodeURIComponent(p); } catch { /* keep encoded */ }
      p = p.replace(/^\/([A-Za-z]:)/, "$1"); // /D:/... -> D:/...
      if (p) void this.openFileInEditor(p);
      return;
    }
    if (/^(https?:\/\/|www\.|mailto:)/i.test(href)) {
      openRoutedLink(href);
      return;
    }
    // Resolve relative paths against the markdown file's directory
    const tab = this.tabs.find(t => t.id === tabId);
    const filePath = tab?.filePath;
    if (!filePath) return;
    const basePath = filePath.replace(/[/\\][^/\\]+$/, "");

    let fullPath: string;
    if (/^[a-zA-Z]:[\\/]/.test(href)) {
      // Absolute Windows path -- a full location on its own; joining it onto
      // the document directory would produce garbage like "D:/base/D:/dir".
      fullPath = href.replace(/\\/g, "/");
    } else if (href.startsWith("/")) {
      fullPath = href;
    } else {
      const dir = basePath.replace(/\\/g, "/").replace(/\/?$/, "/");
      const parts = dir.split("/").filter(Boolean);
      for (const seg of href.replace(/\\/g, "/").split("/")) {
        if (seg === "..") { if (parts.length > 1) parts.pop(); }
        else if (seg !== "." && seg) parts.push(seg);
      }
      fullPath = parts.join("/");
    }
    if (/\.md$/i.test(fullPath)) {
      this.openMarkdownPreviewFile(fullPath);
    } else {
      this.openFileInEditor(fullPath);
    }
  }

  /** Reads a markdown file from disk and opens (or focuses) its preview tab. */
  public async openMarkdownPreviewFile(filePath: string): Promise<void> {
    const api = (window as any).electronAPI;
    if (!api) return;
    const result = await api.readFile(filePath);
    if (!result) return;
    const name = filePath.split(/[/\\]/).pop() || "Markdown";
    this.openMarkdownPreview(result.content, name, filePath);
  }

  private renderTabLabels(): void {
    this.tabList.querySelectorAll(".tab").forEach((el) => {
      const id = (el as HTMLElement).dataset.tab;
      if (!id) return;
      const tab = this.tabs.find(t => t.id === id);
      if (!tab) return;
      const labelEl = el.querySelector(".tab-label");
      if (labelEl) labelEl.textContent = tab.label;
    });

    void this.refreshTabTooltips();
  }

  private renderTabIcons(): void {
    this.tabList.querySelectorAll(".tab").forEach((el) => {
      const id = (el as HTMLElement).dataset.tab;
      if (!id) return;
      const tab = this.tabs.find(t => t.id === id);
      if (!tab || tab.type !== "browser") return;
      const existingImg = el.querySelector(".tab-favicon");
      const existingIcon = el.querySelector(".lucide");
      if (tab.favicon && !existingImg) {
        if (existingIcon) {
          const img = document.createElement("img");
          img.className = "tab-favicon";
          img.src = tab.favicon;
          img.alt = "";
          // Size / spacing / alignment all come from `.tab-favicon` now — the
          // old inline box here hard-coded 14px and a physical margin-right
          // (wrong direction under RTL), and fought the baseline alignment.
          img.onerror = () => { img.style.display = "none"; if (existingIcon) (existingIcon as HTMLElement).style.display = ""; };
          existingIcon.replaceWith(img);
          if (typeof (window as any).lucide !== "undefined") {
            (window as any).lucide.createIcons({ root: el });
          }
        }
      }
    });

    void this.refreshTabTooltips();
  }

  /**
   * Refresh the hover tooltip of every tab.
   *
   * Browser tabs resolve their live title/URL from the BrowserView; file tabs
   * resolve "size · last modified" through the `statFile` bridge (cached for a
   * few seconds so hovering never hammers the main process).
   */
  private async refreshTabTooltips(): Promise<void> {
    await Promise.all(this.tabs.map((tab) => this.refreshTabTooltip(tab)));
  }

  private async refreshTabTooltip(tab: TabDef): Promise<void> {
    const el = this.tabList.querySelector<HTMLElement>(`.tab[data-tab="${tab.id}"]`);
    if (!el) return;
    if (tab.type === "browser") {
      const bv = this.panelBrowsers.get(tab.id);
      const url = (bv?.getUrl() || tab.startUrl || "").trim();
      const title = (bv?.getTitle() || tab.title || tab.label || "").trim();
      applyTabTooltip(el, {
        title: title || url || tab.label,
        icon: tab.favicon,
        location: url,
        isUrl: true,
        permissions: await this.pagePermissions(url),
      }, getIntlLocale());
      return;
    }
    if ((tab.type === "code" || tab.type === "markdown") && tab.filePath) {
      const meta = await this.fileMeta(tab.filePath);
      // The file name, never the tab's generic label — hovering an editor tab
      // and being told it is "Editor" is useless. The path line carries the
      // rest of the location.
      const name = baseName(tab.filePath) || tab.label;
      applyTabTooltip(el, {
        title: name,
        icon: getFileIcon(name),
        location: tab.filePath,
        size: meta?.size,
        mtime: meta?.mtime,
      }, getIntlLocale());
      return;
    }
    applyTabTooltip(el, { title: tab.label, icon: tab.favicon });
  }

  /** Size + mtime for a path; null when it cannot be stat'ed. TTL 5 s. */
  private async fileMeta(filePath: string): Promise<{ size: number; mtime: number } | null> {
    const hit = this._fileMetaCache.get(filePath);
    if (hit !== undefined && Date.now() - hit!.at < 5000) {
      return hit ? { size: hit.size, mtime: hit.mtime } : null;
    }
    let meta: { size: number; mtime: number } | null = null;
    try {
      const st = await window.electronAPI?.statFile(filePath);
      if (st && !st.isDirectory) meta = { size: st.size, mtime: st.mtime };
    } catch {
      meta = null;
    }
    this._fileMetaCache.set(filePath, meta ? { ...meta, at: Date.now() } : null);
    return meta;
  }

  /**
   * Permissions the page actually asked for. The main process records every
   * `setPermissionRequestHandler` call per origin, so this is the real list —
   * camera / microphone / location / … — not a static guess. TTL 5 s.
   */
  private async pagePermissions(url: string): Promise<Array<{ name: string; granted: boolean }>> {
    if (!url || !/^https?:/i.test(url)) return [];
    let origin = "";
    try { origin = new URL(url).origin; } catch { return []; }
    const hit = this._permCache.get(origin);
    if (hit && Date.now() - hit.at < 5000) return hit.list;
    let list: Array<{ name: string; granted: boolean }> = [];
    try {
      const info = await window.electronAPI?.getSiteInfo(url);
      list = info?.permissions || [];
    } catch {
      list = [];
    }
    this._permCache.set(origin, { list, at: Date.now() });
    return list;
  }

  private setupAgentPanel(panel: HTMLElement): void {
    panel.innerHTML = `<div class="session-inner-sidebar-body"><div class="si-agent-root"></div></div>`;
    this.renderAgentPanel(panel);
  }

  private renderAgentPanel(panel: HTMLElement): void {
    const root = panel.querySelector(".si-agent-root") as HTMLElement | null;
    if (!root) return;

    const agentState = getState().agentState;
    if (!agentState) {
      root.innerHTML = `<div class="si-empty-center"><i data-lucide="bot" class="lucide"></i><span class="si-empty-title">${this.esc(t("sessionInner.agentNoState"))}</span></div>`;
      if (typeof (window as any).lucide !== "undefined") (window as any).lucide.createIcons({ root });
      return;
    }

    const workingSet = (agentState.working_set || {}) as Record<string, unknown>;
    const tools = this.asStringList(workingSet.tools);
    const artifacts = this.asStringList(workingSet.artifacts);
    const references = this.asStringList(workingSet.references);
    const planItems = this.asStringList(workingSet.plan_items);
    const delegates = Array.isArray(agentState.delegate_history) ? agentState.delegate_history.slice(-6).reverse() : [];
    const stuckEvents = Array.isArray(agentState.stuck_events) ? agentState.stuck_events.slice(-6).reverse() : [];
    const stages = Array.isArray(agentState.task_stage_history) ? agentState.task_stage_history.slice(-8).reverse() : [];

    root.innerHTML = `<div class="si-panels">
      <div class="si-panel">
        ${this.panelHeader("agent-overview", t("sessionInner.agentOverview"), "bot")}
        <div class="si-panel-body${this.collapsedPanels.has("agent-overview") ? " hidden" : ""}">
          <div class="si-panel-inner">
            <div class="si-agent-stage-row">
              <span class="si-agent-label">${this.esc(t("sessionInner.agentCurrentStage"))}</span>
              <span class="si-agent-stage-badge">${this.esc(agentState.task_stage || t("sessionInner.agentUnknown"))}</span>
            </div>
            <div class="si-agent-kv-grid">
              <div class="si-agent-kv"><span class="si-agent-k">${this.esc(t("sessionInner.agentDelegations"))}</span><span class="si-agent-v">${delegates.length}</span></div>
              <div class="si-agent-kv"><span class="si-agent-k">${this.esc(t("sessionInner.agentStuckEvents"))}</span><span class="si-agent-v">${stuckEvents.length}</span></div>
              <div class="si-agent-kv"><span class="si-agent-k">${this.esc(t("sessionInner.tools"))}</span><span class="si-agent-v">${tools.length}</span></div>
              <div class="si-agent-kv"><span class="si-agent-k">${this.esc(t("sessionInner.agentPlanItems"))}</span><span class="si-agent-v">${planItems.length}</span></div>
            </div>
          </div>
        </div>
      </div>
      <div class="si-panel">
        ${this.panelHeader("agent-working-set", t("sessionInner.agentWorkingSet"), "layers")}
        <div class="si-panel-body${this.collapsedPanels.has("agent-working-set") ? " hidden" : ""}">
          <div class="si-panel-inner">
            ${this.renderAgentListSection(t("sessionInner.agentSectionTools"), tools)}
            ${this.renderAgentListSection(t("sessionInner.agentSectionArtifacts"), artifacts)}
            ${this.renderAgentListSection(t("sessionInner.agentSectionReferences"), references)}
            ${this.renderAgentListSection(t("sessionInner.agentSectionPlan"), planItems)}
          </div>
        </div>
      </div>
      <div class="si-panel">
        ${this.panelHeader("agent-delegates", t("sessionInner.agentDelegation"), "git-branch", delegates.length > 0 ? String(delegates.length) : undefined)}
        <div class="si-panel-body${this.collapsedPanels.has("agent-delegates") ? " hidden" : ""}">
          <div class="si-panel-inner">
            ${this.renderAgentEventList(delegates, t("sessionInner.agentNoDelegation"))}
          </div>
        </div>
      </div>
      <div class="si-panel">
        ${this.panelHeader("agent-stuck", t("sessionInner.agentRecovery"), "siren", stuckEvents.length > 0 ? String(stuckEvents.length) : undefined)}
        <div class="si-panel-body${this.collapsedPanels.has("agent-stuck") ? " hidden" : ""}">
          <div class="si-panel-inner">
            ${this.renderAgentEventList(stuckEvents, t("sessionInner.agentNoRecovery"))}
          </div>
        </div>
      </div>
      <div class="si-panel">
        ${this.panelHeader("agent-stages", t("sessionInner.agentStageHistory"), "route", stages.length > 0 ? String(stages.length) : undefined)}
        <div class="si-panel-body${this.collapsedPanels.has("agent-stages") ? " hidden" : ""}">
          <div class="si-panel-inner">
            ${this.renderAgentEventList(stages, t("sessionInner.agentNoStages"))}
          </div>
        </div>
      </div>
    </div>`;

    this.bindAgentPanel(panel);
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: panel });
    }
  }

  private bindAgentPanel(panel: HTMLElement): void {
    panel.querySelectorAll(".si-panel-header").forEach((header) => {
      header.addEventListener("click", () => {
        const panelId = (header as HTMLElement).dataset.panel!;
        this.togglePanel(panelId);
        this.renderAgentPanel(panel);
      });
    });
  }

  private renderAgentListSection(label: string, items: string[]): string {
    return `<div class="si-agent-section">
      <div class="si-agent-section-title">${this.esc(label)}</div>
      ${items.length > 0
        ? `<div class="si-agent-chip-list">${items.map((item) => `<span class="si-agent-chip">${this.esc(item)}</span>`).join("")}</div>`
        : `<div class="si-agent-empty-inline">${this.esc(t("sessionInner.agentEmptyInline"))}</div>`}
    </div>`;
  }

  private renderAgentEventList(items: Array<Record<string, unknown>>, emptyText: string): string {
    if (!items.length) {
      return `<div class="si-agent-empty-inline">${this.esc(emptyText)}</div>`;
    }
    return `<div class="si-agent-event-list">${items.map((item) => {
      const entries = Object.entries(item)
        .filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== "")
        .slice(0, 4);
      const title = this.esc(this.getAgentEventTitle(item, entries));
      const meta = entries.slice(1).map(([key, value]) =>
        `<span class="si-agent-event-meta"><span class="si-agent-event-key">${this.esc(key)}</span>${this.esc(this.stringifyAgentValue(value))}</span>`
      ).join("");
      return `<div class="si-agent-event">
        <div class="si-agent-event-title">${title}</div>
        ${meta ? `<div class="si-agent-event-row">${meta}</div>` : ""}
      </div>`;
    }).join("")}</div>`;
  }

  private asStringList(value: unknown): string[] {
    if (!Array.isArray(value)) return [];
    return value
      .map((item) => this.stringifyAgentValue(item))
      .filter((item) => item.length > 0);
  }

  private getAgentEventTitle(
    item: Record<string, unknown>,
    entries: Array<[string, unknown]>,
  ): string {
    const preferredKeys = ["summary", "message", "stage", "agent", "name", "reason", "status", "tool"];
    for (const key of preferredKeys) {
      const text = this.stringifyAgentValue(item[key]);
      if (text) return text;
    }
    if (entries.length > 0) {
      return this.stringifyAgentValue(entries[0][1]);
    }
    return t("sessionInner.agentEvent");
  }

  private stringifyAgentValue(value: unknown): string {
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.map((item) => this.stringifyAgentValue(item)).filter(Boolean).join(", ");
    if (value && typeof value === "object") {
      const pairs = Object.entries(value as Record<string, unknown>)
        .slice(0, 3)
        .map(([key, inner]) => `${key}: ${this.stringifyAgentValue(inner)}`);
      return pairs.join(" | ");
    }
    return "";
  }

  /** Inner content (icon + title) shared by every "no changes" empty state. */
  private reviewEmptyInner(): string {
    return `<i data-lucide="check-circle-2" class="lucide"></i>
      <span class="si-empty-title">${t("sessionInner.reviewNoChanges")}</span>`;
  }

  /** Full .si-empty-center block for embedding inside the diff/tree area. */
  private reviewEmptyMarkup(): string {
    return `<div class="si-empty-center">${this.reviewEmptyInner().replace(/\n\s*/g, " ")}</div>`;
  }

  private parseDiff(output: string, overrides?: Partial<DiffRenderOptions>): string {
    if (!output.trim()) return this.reviewEmptyMarkup();

    // Check if this is a binary/media file — use same extension sets as editor
    const fnMatch = output.match(/diff --git a\/\S+ b\/(.+?)(?:\n|$)/) || output.match(/^--- (?:[ab]\/)?(.+)$/m);
    const diffFileName = fnMatch ? fnMatch[1] : "";
    const ext = diffFileName.split(".").pop()?.toLowerCase() || "";
    const isBinary = !!ext && (
      SessionInner._mediaExts.has(ext) || SessionInner._binaryExts.has(ext)
    );
    if (isBinary || /Binary files |GIT binary patch/.test(output)) {
      return `<div class="si-empty-center">
        <i data-lucide="file-x-2" class="lucide"></i>
        <span class="si-empty-title">${t("sessionInner.binaryFileNotShown")}</span>
      </div>`;
    }

    return renderDiffHtml(output, {
      fileNameFallback: t("sessionInner.reviewUnknownFile"),
      maxLines: this._reviewMaxLines(),
      wordDiff: this._reviewWordDiff,
      hideWhitespace: this._reviewHideWs,
      splitView: this._reviewSplitView,
      truncatedNotice: (n: number) => t("sessionInner.reviewTruncated", { n }),
      ...overrides,
    });
  }

  /** Git-style diff text for one artifact — the single source the artifact
   *  review pane renders (through the same parseDiff pipeline as git diffs).
   *  Uses the diff captured by the backend when present; otherwise renders
   *  the real on-disk content as a new-file diff so the pane always shows
   *  actual content instead of an info card. */
  private async artifactDiffText(artifact: ArtifactItem): Promise<string> {
    if (artifact.diff_text && artifact.diff_text.trim()) return artifact.diff_text;
    const api = (window as any).electronAPI;
    if (!api?.readFile) return "";
    try {
      const fr = await api.readFile(artifact.path);
      if (!fr || fr.is_binary || typeof fr.content !== "string" || !fr.content) return "";
      const lines = fr.content.replace(/\r\n/g, "\n").split("\n");
      while (lines.length && lines[lines.length - 1] === "") lines.pop();
      const body = lines.map((l: string) => `+${l}`).join("\n");
      if (!body) return "";
      return [
        `diff --git a/${artifact.path} b/${artifact.path}`,
        "new file mode 100644",
        "--- /dev/null",
        `+++ b/${artifact.path}`,
        `@@ -0,0 +1,${lines.length} @@`,
        body,
      ].join("\n");
    } catch {
      return "";
    }
  }





  private async setupEditorPanel(panel: HTMLElement): Promise<void> {
    const api = (window as any).electronAPI;

    // Pre-warm Monaco in the background as soon as the Explorer opens: the
    // editor AMD bundle (~3MB) otherwise loads cold on the FIRST file click,
    // which makes open look stuck for seconds. While the user browses the
    // tree, this idle load finishes and clicking a file opens instantly.
    // Preheat the Monaco engine in the background while the user browses
    // the tree; failures surface later when a code panel awaits getMonaco.
    void getMonaco().catch(() => {});

    // Always render the tree skeleton. The "no active workspace" empty state
    // is produced by renderTree below (and kept refreshed by the poller), so
    // the tree appears automatically once a workspace becomes active instead
    // of stalling forever until the tab is reopened.
    panel.innerHTML = `<div class="si-editor-wrap" style="display:flex;flex-direction:column;height:100%">
      <span class="si-editor-head-actions">
        <button class="si-editor-head-btn" data-act="new-file" data-tooltip="${this.esc(t("sessionInner.newFile"))}"><i data-lucide="file-plus" class="lucide lucide-xs"></i></button>
        <button class="si-editor-head-btn" data-act="new-folder" data-tooltip="${this.esc(t("sessionInner.newFolder"))}"><i data-lucide="folder-plus" class="lucide lucide-xs"></i></button>
        <button class="si-editor-head-btn" data-act="refresh" data-tooltip="${this.esc(t("sessionInner.treeRefresh"))}"><i data-lucide="refresh-cw" class="lucide lucide-xs"></i></button>
        <button class="si-editor-head-btn" data-act="collapse-all" data-tooltip="${this.esc(t("sessionInner.treeCollapseAll"))}"><i data-lucide="chevrons-down" class="lucide lucide-xs"></i></button>
      </span>
      <div class="si-editor-tree" style="flex:1;overflow:auto;max-width:none;width:100%">
        <div class="si-editor-tree-body"></div>
      </div>
    </div>`;

    const treeBody = panel.querySelector(".si-editor-tree-body")! as HTMLElement;

    // Ensure the general-mode tree root (session files dir) resolves
    // immediately instead of waiting for the next backend push: request a
    // fresh config snapshot when the current session has none yet.
    const st0 = getState();
    if (!st0.activeWorkspace && !st0.sessionFilesDirs[st0.sessionId]) {
      void send({ type: "get_config" });
    }

    /* context menu for tree */
    const treeCtxMenu = document.createElement("div");
    treeCtxMenu.className = "context-menu hidden";
    treeCtxMenu.id = "editor-tree-ctx-menu";
    const treeApi = (window as any).electronAPI;
    document.body.appendChild(treeCtxMenu);

    const showTreeCtx = (ev: MouseEvent, path: string, isDir: boolean, target: HTMLElement | null) => {
      ev.preventDefault();
      // Without an active workspace there is no valid root to act on.
      if (!rootPath) return;
      // Visual affordance: the right-clicked entry becomes the single
      // selected row, so "the file I right-clicked" is always the file the
      // menu actions (delete/new/… ) visibly act on — never a stale click
      // selection from earlier.
      if (target) {
        treeBody.querySelectorAll(".si-tree-entry.selected").forEach((s) => s.classList.remove("selected"));
        target.classList.add("selected");
      }
      const api = treeApi;
      const ext = path.split(".").pop()?.toLowerCase() || "";
      const isMd = !isDir && ext === "md";
      const baseName = path.split(/[/\\]/).pop() || path;
      const sendToChat = () => {
        const att: AttachmentMeta = isDir
          ? { name: baseName, path, content: "", mime_type: "text/x-directory", size: 0, is_binary: false }
          : { name: baseName, path, content: "", mime_type: "", size: 0, is_binary: false };
        if (isDir) {
          addAttachments([att]);
          return;
        }
        void api?.readFile(path).then((result: any) => {
          if (!result) return;
          addAttachments([
            {
              name: baseName,
              path,
              content: result.content,
              mime_type: result.mime_type || "",
              size: result.size,
              is_binary: result.is_binary,
            },
          ]);
        });
      };
      const items: ContextMenuItemSpec[] = [];
      if (isDir) {
        // Folder: new file/folder, paste into it, send, delete.
        items.push({ action: "new-file", icon: "file-plus", label: t("sessionInner.newFile") });
        items.push({ action: "new-folder", icon: "folder-plus", label: t("sessionInner.newFolder") });
        items.push({ divider: true });
        items.push({ action: "paste", icon: "clipboard", label: t("sessionInner.pasteFile") });
        items.push({ divider: true });
        items.push({ action: "send-folder", icon: "send", label: t("sessionInner.termSendToChat"), onClick: sendToChat });
        items.push({ divider: true });
        items.push({ action: "delete", icon: "trash-2", danger: true, label: t("sessionInner.delete") });
      } else if (!path) {
        // Empty area (no file/folder under the cursor): create + paste into root.
        items.push({ action: "new-file", icon: "file-plus", label: t("sessionInner.newFile") });
        items.push({ action: "new-folder", icon: "folder-plus", label: t("sessionInner.newFolder") });
        items.push({ divider: true });
        items.push({ action: "paste", icon: "clipboard", label: t("sessionInner.pasteFile") });
      } else {
        // File: copy it, send to conversation, preview (.md), delete.
        items.push({ action: "copy", icon: "copy", label: t("sessionInner.copyFile") });
        items.push({ divider: true });
        items.push({ action: "send-file", icon: "send", label: t("sessionInner.termSendToChat"), onClick: sendToChat });
        if (isMd) {
          items.push({
            divider: true,
          });
          items.push({
            action: "preview",
            icon: "eye",
            label: t("sessionInner.preview"),
            onClick: () => {
              void api?.readFile(path).then((result: any) => {
                if (result) this.openMarkdownPreview(result.content, baseName, path);
              });
            },
          });
        }
        items.push({ divider: true });
        items.push({ action: "delete", icon: "trash-2", danger: true, label: t("sessionInner.delete") });
      }
      // Plugin-contributed items ("workspace_files" surface), spliced through
      // the one shared helper so plugin rows stay defined in a single place.
      items.push(...pluginMenuSpecs("workspace_files", { path, is_dir: isDir }));
      renderContextMenuItems(items, {
        container: treeCtxMenu,
        onAction: (spec) => void treeCtxAction(spec.action!, path, isDir),
      });
      showContextMenu(treeCtxMenu, ev.clientX, ev.clientY, { target });
    };

    const treeCtxAction = async (action: string, path: string, isDir: boolean): Promise<void> => {
      const api = treeApi;
      if (action === "send-file" && api) {
        const result = await api.readFile(path);
        if (result) {
          const name = path.split(/[/\\]/).pop() || path;
          addAttachments([
            {
              name,
              path,
              content: result.content,
              mime_type: result.mime_type || "",
              size: result.size,
              is_binary: result.is_binary,
            },
          ]);
        }
      } else if (action === "send-folder" && api) {
        const name = path.split(/[/\\]/).pop() || path;
        addAttachments([{ name, path, content: "", mime_type: "text/x-directory", size: 0, is_binary: false }]);
      } else if (action === "preview" && api) {
        const result = await api.readFile(path);
        if (result) {
          const name = path.split(/[/\\]/).pop() || path;
          this.openMarkdownPreview(result.content, name, path);
        }
      } else if (action === "copy" && api) {
        await api.copyFilesToClipboard([path]);
      } else if (action === "paste" && api) {
        const sources = await api.readFilesFromClipboard();
        if (sources && sources.length > 0) {
          // Same lock rule: paste target is the right-clicked directory, or
          // the parent of the right-clicked file — never a default root.
          const destDir = isDir ? path : (path.replace(/[/\\][^/\\]+$/, "") || rootPath);
          const res = await api.pasteFiles(sources, destDir);
          if (res && res.success) await renderTree();
        }
      } else if ((action === "new-file" || action === "new-folder") && api) {
        // Lock to the right-clicked entry: a directory receives the new item
        // itself, a file delegates to its own parent folder, empty space
        // means the tree root. Never fall back to the left-click selection.
        const parentDir = isDir ? path : (path.replace(/[/\\][^/\\]+$/, "") || rootPath);
        const label = action === "new-file" ? t("sessionInner.newFile") : t("sessionInner.newFolder");
        // Offer a default that does not collide with an existing sibling, so
        // the dialog never suggests the very name dedup would have to rename.
        const siblings = new Set((await fetchDir(parentDir)).map((e) => e.name.toLowerCase()));
        const bumpDefault = (base: string): string => {
          if (!siblings.size || !siblings.has(base.toLowerCase())) return base;
          const m = base.match(/^(.*?)(\.[^./\\]*)?$/);
          const stem = m?.[1] || base;
          const ext = m?.[2] || "";
          for (let i = 1; i < 1000; i++) {
            const cand = `${stem} (${i})${ext}`;
            if (!siblings.has(cand.toLowerCase())) return cand;
          }
          return base;
        };
        const defVal = bumpDefault(action === "new-file" ? "untitled.txt" : "untitled");
        const name = await Dialog.promptIn(
          this.el,
          label,
          action === "new-file" ? t("sessionInner.newFileHint") : t("sessionInner.newFolderHint"),
          defVal,
          { primary: t("dialog.save") },
        );
        if (name && name.trim()) {
          // Normalise CJK full-width brackets to ASCII: the auto-dedup
          // suffix ("name (1).ext") uses ASCII and the tree mixes both
          // conventions otherwise.
          const cleanedName = name.trim().replace(/（/g, "(").replace(/）/g, ")");
          // Never silently absorb a forced duplicate: tell the user the
          // backend will append a numbered suffix and let them cancel.
          if (siblings.has(cleanedName.toLowerCase())) {
            const cont = await Dialog.confirmIn(
              this.el,
              t("sessionInner.treeNameExists"),
              t("sessionInner.treeNameExistsMsg", { name: cleanedName }),
            );
            if (!cont) return;
          }
          const res = action === "new-file"
            ? await api.createFile(parentDir, cleanedName)
            : await api.createFolder(parentDir, cleanedName);
          if (res && res.success) {
            // The poller keeps dirCache warm: drop the parent's cached
            // listing or the new entry stays invisible until the cache is
            // invalidated elsewhere, and expand the parent so it shows.
            dirCache.delete(parentDir);
            expandedDirs.add(parentDir);
            persistExpandState();
            lastTreeHtml = "";
            await renderTree();
            if (action === "new-file" && res.path) {
              this.openFileInEditor(res.path);
            }
          }
        }
      } else if (action === "delete" && api) {
        // Show the path RELATIVE to the tree root in the confirm dialog:
        // long absolute paths are noise and hard to compare at a glance.
        // Folders get their own wording that states the deletion takes all
        // contained files/subfolders — a folder must never look like a
        // plain "file" being asked about.
        let relPath = path;
        if (rootPath) {
          const rootClean = rootPath.replace(/[/\\]+$/, "");
          if (path === rootClean) {
            relPath = path.split(/[/\\]/).pop() || path;
          } else if (path.toLowerCase().startsWith(rootClean.toLowerCase())) {
            relPath = path.slice(rootClean.length).replace(/^[/\\]+/, "").replace(/[\\/]/g, "/");
          }
        }
        const titleKey = isDir ? "sessionInner.confirmDeleteFolderTitle" : "sessionInner.confirmDeleteTitle";
        const msgKey = isDir ? "sessionInner.confirmDeleteFolder" : "sessionInner.confirmDelete";
        if (await Dialog.confirmIn(this.el, t(titleKey), t(msgKey, { name: relPath }))) {
          const res = await api.deletePath(path);
          if (res && res.success) {
            // Invalidate the containing directory's cached listing so the
            // removal shows on the next render instead of from the cache.
            dirCache.delete(path.replace(/[/\\][^/\\]+$/, "") || rootPath);
            lastTreeHtml = "";
            await renderTree();
          } else if (res && res.error) {
            Dialog.alert(t("dialog.error"), res.error, { container: this.el });
          }
        }
      }
    };
    document.addEventListener("click", (e) => {
      if (!treeCtxMenu.contains(e.target as Node)) {
        hideAnimated(treeCtxMenu);
      }
    });

    // Right-click on empty tree space (not a file/folder): paste into root.
    treeBody.addEventListener("contextmenu", (ev: MouseEvent) => {
      if ((ev.target as HTMLElement).closest(".si-tree-entry")) return;
      showTreeCtx(ev, "", false, null);
    });

    let rootPath = "";
    let configReqSid = "";
    let configReqAt = 0;
    let configReqCount = 0;
    const expandedDirs = new Set<string>();
    const dirCache = new Map<string, DirEntry[]>();
    let lastTreeHtml = "";
    /* Write-through snapshot of the expansion set: closing the tab rebuilds
       the panel with an empty instance set, so every fold/unfold must be
       persisted immediately — persisting only on root switch restored stale
       state on reopen. */
    const persistExpandState = () => {
      if (rootPath) SessionInner._editorExpandState.set(rootPath, new Set(expandedDirs));
    };

    const joinPath = (base: string, name: string) => {
      const sep = base.includes("\\") ? "\\" : "/";
      return base.replace(/[/\\]+$/, "") + sep + name;
    };

    const fetchDir = async (dirPath: string): Promise<DirEntry[]> => {
      if (dirCache.has(dirPath)) return dirCache.get(dirPath)!;
      if (!api) return [];
      try {
        const entries: DirEntry[] = await api.listDirectory(dirPath);
        dirCache.set(dirPath, entries);
        return entries;
      } catch {
        return [];
      }
    };

    const renderRecursive = async (dirPath: string, depth: number, out: string[]) => {
      const entries = await fetchDir(dirPath);
      const sorted = [...entries].sort((a, b) => {
        if (a.isDirectory && !b.isDirectory) return -1;
        if (!a.isDirectory && b.isDirectory) return 1;
        return a.name.localeCompare(b.name);
      });
      const indent = depth * 8;
      for (const entry of sorted) {
        const fullPath = joinPath(dirPath, entry.name);
        if (entry.isDirectory) {
          const isExp = expandedDirs.has(fullPath);
          out.push(`<div class="si-tree-entry" data-path="${this.esc(fullPath)}" data-dir="true" style="padding-left:${indent}px">
            <span class="si-tree-chevron${isExp ? " expanded" : ""}"><i data-lucide="chevron-right" class="lucide lucide-xs"></i></span>
            <span class="si-tree-icon"><img src="${getFolderIcon(entry.name, isExp)}" class="icon" style="width:14px;height:14px"></span>
            <span class="si-tree-name">${this.esc(entry.name)}</span>
          </div>`);
          if (isExp) {
            await renderRecursive(fullPath, depth + 1, out);
          }
        } else {
          out.push(`<div class="si-tree-entry" data-path="${this.esc(fullPath)}" data-file="true" style="padding-left:${indent}px">
            <span class="si-tree-chevron" style="visibility:hidden"><i data-lucide="chevron-right" class="lucide lucide-xs"></i></span>
            <span class="si-tree-icon"><img src="${getFileIcon(entry.name)}" class="icon" style="width:14px;height:14px"></span>
            <span class="si-tree-name">${this.esc(entry.name)}</span>
          </div>`);
        }
      }
    };

    const renderTree = async () => {
      const ws = getState().activeWorkspace;
      // General mode falls back to the CURRENT session's files directory
      // (backend-resolved via config_data, keyed by session id), so the
      // editor tree operates on the same per-session sandbox the agent
      // writes into — and never on another session's files.
      const root = ws || getState().sessionFilesDirs[getState().sessionId] || "";
      if (!root) {
        // No workspace and no files dir for this session yet: render the
        // same subtle empty-tree view an empty directory gets — never the
        // large empty-state placeholder. Ask the backend for a config
        // snapshot (once per session) so the tree appears promptly.
        rootPath = "";
        lastTreeHtml = "";
        const sid = getState().sessionId;
        if (sid && configReqSid !== sid) {
          configReqSid = sid;
          configReqAt = 0;
          configReqCount = 0;
        }
        if (sid) {
          if (!configReqSid) configReqSid = sid;
          // Bounded retry: a config snapshot can be dropped by the session
          // filters or a WS reconnect, and a one-shot request per session
          // used to leave the tree stuck on the empty view forever.
          const now = Date.now();
          if (configReqCount < 8 && now - configReqAt > 5000) {
            configReqAt = now;
            configReqCount++;
            void send({ type: "get_config" });
          }
        }
        if (!treeBody.querySelector(".si-empty-center")) {
          treeBody.innerHTML = `<div class="si-empty-center"><i data-lucide="folder-open" class="lucide"></i><span class="si-empty-title">${t("sessionInner.filesEmpty")}</span></div>`;
          if (typeof (window as any).lucide !== "undefined") {
            (window as any).lucide.createIcons({ root: treeBody });
          }
        }
        return;
      }
      if (root !== rootPath) {
        // Root changed (workspace switch or session switch): persist this
        // root's expansion snapshot (VSCode keeps per-explorer fold state),
        // restore the incoming root's own, then force a full re-render.
        if (rootPath) SessionInner._editorExpandState.set(rootPath, new Set(expandedDirs));
        expandedDirs.clear();
        const saved = SessionInner._editorExpandState.get(root);
        if (saved) {
          for (const p of saved) expandedDirs.add(p);
        }
        lastTreeHtml = "";
      }
      rootPath = root;
      dirCache.clear();
      // Root resolved: reset the get_config retry budget for future
      // post-switch empty-root rounds.
      configReqCount = 0;
      const out: string[] = [];
      await renderRecursive(rootPath, 0, out);
      const html = out.length ? out.join("") : `<div class="si-empty-center"><i data-lucide="folder-open" class="lucide"></i><span class="si-empty-title">${t("sessionInner.filesEmpty")}</span></div>`;
      if (html === lastTreeHtml) return;
      const prevScroll = treeBody.scrollTop;
      const selEl = treeBody.querySelector(".si-tree-entry.selected") as HTMLElement | null;
      const selPath = selEl ? selEl.dataset.path || "" : "";
      lastTreeHtml = html;
      treeBody.innerHTML = html;

      treeBody.querySelectorAll(".si-tree-entry[data-dir]").forEach((el) => {
        el.addEventListener("click", async () => {
          const path = (el as HTMLElement).dataset.path!;
          treeBody.querySelectorAll(".si-tree-entry.selected").forEach((s) => s.classList.remove("selected"));
          (el as HTMLElement).classList.add("selected");
          if (expandedDirs.has(path)) {
            expandedDirs.delete(path);
          } else {
            await fetchDir(path);
            expandedDirs.add(path);
          }
          persistExpandState();
          await renderTree();
        });
        (el as HTMLElement).addEventListener("contextmenu", (ev: MouseEvent) => {
          showTreeCtx(ev, (el as HTMLElement).dataset.path!, true, el as HTMLElement);
        });
      });

      treeBody.querySelectorAll(".si-tree-entry[data-file]").forEach((el) => {
        el.addEventListener("click", () => {
          treeBody.querySelectorAll(".si-tree-entry.selected").forEach((s) => s.classList.remove("selected"));
          (el as HTMLElement).classList.add("selected");
          const filePath = (el as HTMLElement).dataset.path!;
          this.openFileInEditor(filePath);
        });
        (el as HTMLElement).addEventListener("contextmenu", (ev: MouseEvent) => {
          showTreeCtx(ev, (el as HTMLElement).dataset.path!, false, el as HTMLElement);
        });
      });

      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: treeBody });
      }

      if (selPath) {
        const restored = treeBody.querySelector<HTMLElement>(`.si-tree-entry[data-path="${CSS.escape(selPath)}"]`);
        if (restored) restored.classList.add("selected");
      }
      treeBody.scrollTop = prevScroll;
    };

    /* Initial render (empty state or tree depending on active workspace). */
    await renderTree();

    // Expose an immediate refresh hook so switching back to this tab can
    // update the tree without waiting for the next poll tick.
    (panel as any)._siRefreshTree = () => { void renderTree(); };

    // Keep the file tree fresh: re-render every 2s while the sidebar is
    // connected and visible, so files created/removed on disk appear in real
    // time without manual refresh.  Deliberately not gated on the panel's
    // "active" class — panels rebuilt after a session/mode switch can lose
    // that class and silently stall the poller.
    const panelTimer = window.setInterval(() => {
      if (!panel.isConnected) return;
      if (document.hidden) return;
      // The child-window (ESD) shell never clears the #main-body
      // "sidebar-hidden" boot state (restoreSidebarVisibility early-returns
      // there) even while the panel is visible, so the gate below would
      // stall the poller forever.  Only honour it in the main window.
      if (!inChildWindow()) {
        const mainBody = document.getElementById("main-body");
        if (mainBody && mainBody.classList.contains("sidebar-hidden")) return;
      }
      // External changes must show up too: the warm dirCache would otherwise
      // hide files created/removed outside this panel forever.  Re-list from
      // disk every tick; tree HTML identical to lastFrame is still skipped
      // by the renderTree diff, so steady state costs only the listing.
      dirCache.clear();
      void renderTree();
    }, 2000);
    (panel as any)._siTreeTimer = panelTimer;

    /* Toolbar (VSCode-explorer style): new file / new folder / refresh /
       collapse-all.  All actions reuse the tree context-menu backend calls;
       they act on the resolved tree root and are no-ops without one. */
    panel.querySelectorAll<HTMLElement>(".si-editor-head-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!rootPath) return;
        const act = btn.dataset.act || "";
        if (act === "new-file" || act === "new-folder") {
          // Anchor to the selected tree entry, VSCode-style: a directory
          // receives the new entry directly, a file's parent directory does,
          // and with no selection at all we fall back to the tree root.
          const sel = treeBody.querySelector(".si-tree-entry.selected") as HTMLElement | null;
          const selPath = sel?.dataset.path || "";
          let target = rootPath;
          if (selPath) {
            target = sel!.dataset.dir ? selPath : (selPath.replace(/[/\\][^/\\]+$/, "") || rootPath);
          }
          await treeCtxAction(act, target, true);
        } else if (act === "refresh") {
          // Force a full re-read: the poller keeps dirCache warm, so a manual
          // refresh must invalidate it to pick up out-of-band disk changes.
          dirCache.clear();
          lastTreeHtml = "";
          await renderTree();
        } else if (act === "collapse-all") {
          expandedDirs.clear();
          persistExpandState();
          await renderTree();
        }
      });
    });

    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: panel });
    }
  }

  private static _imgExts = new Set(["png","jpg","jpeg","gif","webp","ico","bmp","svg"]);
  private static _videoExts = new Set(["mp4","avi","mov","wmv","flv","mkv","webm"]);
  private static _mediaExts = new Set([...SessionInner._imgExts, ...SessionInner._videoExts]);

  private static _officeExts = new Set(["doc","docx","xls","xlsx","ppt","pptx"]);

  private static _binaryExts = new Set([
    "exe","dll","so","dylib","bin","dat","iso","img","dmg",
    "zip","rar","7z","gz","tar","bz2","xz","zst",
    "pdf",
    "mp3","wav","flac","ogg",
    "ttf","otf","woff","woff2","eot",
    "pyc","class","o","obj","lib","a","lo",
  ]);

  private _isBinaryContent(content: string): boolean {
    return /\0/.test(content);
  }

  /** In-flight single-click opens, keyed by path: a double-fire click used
   *  to race two readFile→createTab chains into duplicate code tabs. */
  private _openFileLocks = new Set<string>();

  async openFileInEditor(filePath: string): Promise<void> {
    if (this._openFileLocks.has(filePath)) return;
    this._openFileLocks.add(filePath);
    try {
      await this._openFileInEditor(filePath);
    } finally {
      this._openFileLocks.delete(filePath);
    }
  }

  private async _openFileInEditor(filePath: string): Promise<void> {
    const api = (window as any).electronAPI;
    if (!api) return;
    const name = filePath.split(/[/\\]/).pop() || filePath;
    const ext = filePath.split(".").pop()?.toLowerCase() || "";

    if (SessionInner._mediaExts.has(ext)) {
      await this._openMediaPreview(name, filePath);
      return;
    }

    if (SessionInner._officeExts.has(ext)) {
      await this._openOfficePreview(name, filePath);
      return;
    }

    const result = await api.readFile(filePath);
    if (!result) return;

    if (result.is_binary || SessionInner._binaryExts.has(ext) || this._isBinaryContent(result.content)) {
      await this._openBinaryPreview(name, filePath);
      return;
    }

    // Re-use existing code tab for this file, or create a new one
    const existing = this.tabs.find(t => t.type === "code" && t.filePath === filePath);
    if (existing) {
      this.activeTab = existing.id;
      this.revealPanel();
      await this.renderTabs();
      return;
    }
    await this.createTab("code", { content: result.content, title: name, filePath });
  }

  private async _openBinaryPreview(name: string, filePath: string): Promise<void> {
    const existing = this.tabs.find(t => t.type === "binary" && t.filePath === filePath);
    if (existing) {
      this.activeTab = existing.id;
      await this.renderTabs();
      return;
    }
    const tabId = `binary-${Date.now()}`;
    this.tabs.push({
      id: tabId,
      type: "binary",
      label: name,
      closable: true,
      filePath,
    });
    this.activeTab = tabId;
    await this.renderTabs();
  }

  private async _openMediaPreview(name: string, filePath: string): Promise<void> {
    const existing = this.tabs.find(t => t.type === "media" && t.filePath === filePath);
    if (existing) {
      this.activeTab = existing.id;
      await this.renderTabs();
      return;
    }
    await this.createTab("media", { title: name, filePath });
  }

  private async _openOfficePreview(name: string, filePath: string): Promise<void> {
    const existing = this.tabs.find(t => t.type === "office" && t.filePath === filePath);
    if (existing) {
      this.activeTab = existing.id;
      await this.renderTabs();
      return;
    }
    await this.createTab("office", { title: name, filePath });
  }

  private setupMediaPanel(panel: HTMLElement, tabId: string, filePath: string, mediaType?: string): void {
    panel.style.overflow = "hidden";
    panel.style.background = "var(--bg-primary)";

    const existing = this.panelMediaViewers.get(tabId);
    if (existing) {
      if (existing.el.parentElement !== panel) {
        panel.appendChild(existing.el);
      }
      return;
    }

    const container = document.createElement("div");
    container.style.cssText = "width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:var(--bg-primary);overflow:hidden;";
    panel.appendChild(container);

    const ext = filePath.split(".").pop()?.toLowerCase() || "";
    const isVideo = mediaType
      ? mediaType === "video"
      : SessionInner._videoExts.has(ext);

    const mv = new MediaViewer(container, { type: isVideo ? "video" : "image", src: filePath });
    this.panelMediaViewers.set(tabId, mv);
  }

  private setupOfficePanel(panel: HTMLElement, tabId: string, filePath: string): void {
    panel.style.overflow = "hidden";
    panel.style.background = "var(--bg-primary)";

    const existing = this.panelOfficeControllers.get(tabId);
    if (existing) {
      if (existing.parentElement !== panel) panel.appendChild(existing);
      return;
    }

    const container = document.createElement("div");
    container.className = "si-office-viewer";
    panel.appendChild(container);
    this.panelOfficeControllers.set(tabId, container);

    const ext = filePath.split(".").pop()?.toLowerCase() || "";
    const api = (window as any).electronAPI;
    if (!api) return;

    container.innerHTML = "";
    // Branded loading state while the office file is being read and parsed.
    EALoader.mountCentered(container, "96px");

    api.readFileBase64(filePath).then(async (r: any) => {
      // Any outcome below replaces the loading state outright.
      EALoader.destroyMounted(container);
      if (!r || !r.data) {
        container.innerHTML = `<div class="si-empty-center"><i data-lucide="file-x-2" class="lucide"></i><span class="si-empty-title">${this.esc(t("sessionInner.binaryFileNotShown"))}</span></div>`;
        if (typeof (window as any).lucide !== "undefined") {
          (window as any).lucide.createIcons({ root: container });
        }
        return;
      }
      const bin = Uint8Array.from(atob(r.data), (c) => c.charCodeAt(0));
      const buffer = bin.buffer;
      try {
        if (ext === "doc" || ext === "docx") {
          await this._renderOfficeDocx(container, buffer);
        } else if (ext === "xls" || ext === "xlsx") {
          await this._renderOfficeXlsx(container, buffer);
        } else if (ext === "ppt" || ext === "pptx") {
          await this._renderOfficePptx(container, buffer);
        } else {
          container.innerHTML = `<div class="si-empty-center"><i data-lucide="file-x-2" class="lucide"></i><span class="si-empty-title">${this.esc(t("sessionInner.binaryFileNotShown"))}</span></div>`;
        }
      } catch (e) {
        container.innerHTML = `<div class="si-empty-center"><i data-lucide="alert-triangle" class="lucide"></i><span class="si-empty-title">${this.esc(String(e))}</span></div>`;
      }
      if (typeof (window as any).lucide !== "undefined") {
        (window as any).lucide.createIcons({ root: container });
      }
    });
  }

  private async _renderOfficeDocx(container: HTMLElement, buffer: ArrayBuffer): Promise<void> {
    // Lazy-load mammoth on first .docx preview.
    const mammoth = (await import("mammoth")).default;
    const result = await mammoth.convertToHtml({ arrayBuffer: buffer });
    container.innerHTML = `<div class="si-office-page docx">${result.value}</div>`;
  }

  private async _renderOfficeXlsx(container: HTMLElement, buffer: ArrayBuffer): Promise<void> {
    // Lazy-load SheetJS on first .xlsx preview.
    const XLSX = await import("xlsx");
    const wb = XLSX.read(buffer, { type: "array" });
    const sheet = wb.Sheets[wb.SheetNames[0]];
    const html = XLSX.utils.sheet_to_html(sheet, { id: "xlsx-table" });
    container.innerHTML = `<div class="si-office-page xlsx-wrap">${html}</div>`;
  }

  private async _renderOfficePptx(container: HTMLElement, buffer: ArrayBuffer): Promise<void> {
    // Lazy-load pptx-to-html on first .pptx preview.
    const { pptxToHtml } = await import("@jvmr/pptx-to-html");
    const slides = await pptxToHtml(buffer, { width: 960, height: 540, scaleToFit: true });
    const total = slides.length;

    const viewer = document.createElement("div");
    viewer.className = "si-office-ppt-viewer";

    const rail = document.createElement("div");
    rail.className = "si-office-ppt-rail";

    const stage = document.createElement("div");
    stage.className = "si-office-ppt-stage";

    const stageSlides: HTMLElement[] = [];

    slides.forEach((slideHtml, idx) => {
      const thumb = document.createElement("div");
      thumb.className = "si-office-ppt-thumb" + (idx === 0 ? " active" : "");
      thumb.dataset.idx = String(idx);
      thumb.innerHTML = `<div class="si-office-ppt-thumb-inner">${slideHtml}</div>`;
      rail.appendChild(thumb);

      const stageSlide = document.createElement("div");
      stageSlide.className = "si-office-ppt-stage-slide" + (idx === 0 ? " active" : "");
      stageSlide.innerHTML = `<div class="si-office-ppt-stage-inner">${slideHtml}</div>`;
      stage.appendChild(stageSlide);
      stageSlides.push(stageSlide);
    });

    const fitStage = () => {
      const active = stage.querySelector(".si-office-ppt-stage-slide.active");
      if (!active) return;
      const w = active.clientWidth;
      if (!w) return;
      const scale = w / 960;
      active.querySelectorAll<HTMLElement>(".si-office-ppt-stage-inner").forEach((inner) => {
        inner.style.transform = `scale(${scale})`;
      });
    };

    const selectSlide = (idx: number) => {
      rail.querySelectorAll(".si-office-ppt-thumb").forEach((el, i) => {
        el.classList.toggle("active", i === idx);
      });
      stageSlides.forEach((el, i) => {
        el.classList.toggle("active", i === idx);
      });
      updateCounter();
      fitStage();
    };

    rail.querySelectorAll(".si-office-ppt-thumb").forEach((el, idx) => {
      el.addEventListener("click", () => selectSlide(idx));
    });

    // Prev / next navigation.
    const nav = document.createElement("div");
    nav.className = "si-office-ppt-nav";
    const prev = document.createElement("button");
    prev.type = "button";
    prev.innerHTML = `<i data-lucide="chevron-left" class="lucide"></i>`;
    const next = document.createElement("button");
    next.type = "button";
    next.innerHTML = `<i data-lucide="chevron-right" class="lucide"></i>`;
    const counter = document.createElement("span");
    counter.className = "si-office-ppt-counter";
    const updateCounter = () => {
      const activeIdx = [...stageSlides].findIndex((el) => el.classList.contains("active"));
      counter.textContent = `${activeIdx + 1} / ${total}`;
    };
    prev.addEventListener("click", () => {
      const activeIdx = [...stageSlides].findIndex((el) => el.classList.contains("active"));
      if (activeIdx > 0) selectSlide(activeIdx - 1);
    });
    next.addEventListener("click", () => {
      const activeIdx = [...stageSlides].findIndex((el) => el.classList.contains("active"));
      if (activeIdx < total - 1) selectSlide(activeIdx + 1);
    });
    updateCounter();
    nav.appendChild(prev);
    nav.appendChild(counter);
    nav.appendChild(next);

    viewer.appendChild(rail);
    viewer.appendChild(stage);
    viewer.appendChild(nav);
    container.innerHTML = "";
    container.appendChild(viewer);

    requestAnimationFrame(fitStage);
    const ro = new ResizeObserver(() => fitStage());
    ro.observe(stage);
    (container as any)._officeCleanup = () => ro.disconnect();
  }

  private setupBinaryPanel(panel: HTMLElement): void {
    panel.innerHTML = `<div class="si-empty-center">
      <i data-lucide="file-x-2" class="lucide"></i>
      <span class="si-empty-title">${t("sessionInner.binaryFileNotShown")}</span>
    </div>`;
    if (typeof (window as any).lucide !== "undefined") {
      (window as any).lucide.createIcons({ root: panel });
    }
  }

  private async setupCodePanel(panel: HTMLElement, tabId: string, content: string, filePath: string): Promise<void> {
    panel.style.overflow = "hidden";
    panel.style.background = "var(--bg-primary)";

    const existing = this.panelCodeEditors.get(tabId);
    if (existing) {
      const container = existing._codeContainer;
      if (container && container.parentElement !== panel) {
        panel.appendChild(container);
      }
      return;
    }

    const container = document.createElement("div");
    container.style.cssText = "width:100%;height:100%;background:var(--bg-primary);";
    panel.appendChild(container);

    const ext = filePath.split(".").pop()?.toLowerCase() || "";
    const isDark = document.documentElement.getAttribute("data-theme") !== "light";

    // Branded loading state for the (first-open) Monaco cold load: without
    // it the panel is a blank rectangle for the entire ~3MB bundle fetch.
    // When Monaco is already warm, mount+resolve+destroy happen inside one
    // microtask run and never paint, so there is no flash.
    EALoader.mountCentered(container, "96px");
    let editor: typeof monaco.editor;
    try {
      editor = await getMonaco();
    } catch (err) {
      // Loader network failure / bundle timeout: never leave a blank tab.
      EALoader.destroyMounted(container);
      const msg = err instanceof Error ? err.message : String(err);
      console.error("[monaco] code panel load failed:", msg);
      const fail = document.createElement("div");
      fail.style.cssText =
        "display:flex;align-items:center;justify-content:center;height:100%;padding:24px;text-align:center;color:var(--text-secondary);font-size:13px;user-select:text;";
      fail.textContent = `${t("sessionInner.editorLoadFailed")}: ${msg}`;
      container.appendChild(fail);
      return;
    }
    EALoader.destroyMounted(container);
    const view = editor.create(container, {
      ...editorViewOptions(),
      value: content,
      language: monacoLang(ext),
      theme: isDark ? "encre-dark" : "encre-light",
      readOnly: false,
      automaticLayout: true,
      scrollBeyondLastLine: false,
      renderLineHighlight: "line",
      contextmenu: false,
      bracketPairColorization: { enabled: true },
      padding: { top: 8 },
    });

    // Selection strip for the code editor. Monaco keeps its own selection
    // model, so there is no DOM range to read here, and `.si-code-container`
    // is dead CSS — this anonymous div is the real host. A mouseup that ends
    // the drag is the only trigger, which is what keeps the strip one-shot.
    container.addEventListener("mouseup", (e: MouseEvent) => {
      // Left button only. Monaco reports the same selection for a right-press,
      // which used to make the strip appear together with the editor's own
      // context menu — visibly the same copy action twice.
      if (e.button !== 0) return;
      const selection = view.getSelection();
      const codeModel = view.getModel();
      if (!selection || !codeModel || selection.isEmpty()) return;
      const text = codeModel.getValueInRange(selection);
      if (!text.trim()) return;
      const host = container.getBoundingClientRect();
      const pos = view.getScrolledVisiblePosition(selection.getEndPosition());
      const top = pos ? host.top + pos.top : host.top;
      const height = pos ? pos.height : 16;
      showSelectionActionBarFor({
        text,
        kind: "code",
        rect: { left: host.left, top, right: host.right, bottom: top + height },
      });
    });

    const model = view.getModel();
    if (model && typeof model.onDidChangeContent === "function") {
      // Auto-save is a real write: the buffer goes to its own file a beat after
      // the last keystroke.  The tab guard is what keeps a pending timer from
      // landing on disk after the user closed the tab and chose "don't save".
      let saveTimer = 0;
      const scheduleAutoSave = () => {
        if (!filePath || !_settingBool("editor_auto_save", false)) return;
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => {
          if (this.panelCodeEditors.get(tabId) !== view) return;
          const value = model.getValue();
          if (value === content) return;
          void window.electronAPI?.writeFile?.(filePath, value).then((ok) => {
            if (!ok || this.panelCodeEditors.get(tabId) !== view) return;
            content = value;
            if (this._dirtyTabs.get(tabId)) {
              this._dirtyTabs.set(tabId, false);
              this._refreshTabClose(tabId);
            }
          });
        }, 600);
      };
      model.onDidChangeContent(() => {
        const dirty = model.getValue() !== content;
        if (dirty !== !!this._dirtyTabs.get(tabId)) {
          this._dirtyTabs.set(tabId, dirty);
          this._refreshTabClose(tabId);
        }
        scheduleAutoSave();
      });
    }

    (view as any)._codeContainer = container;
    registerEditor(view);
    this.panelCodeEditors.set(tabId, view);

    /* context menu (reuses the app's standard context-menu UI) */
    const ctxMenu = document.createElement("div");
    ctxMenu.className = "context-menu hidden";
    ctxMenu.setAttribute("id", `code-ctx-menu-${tabId}`);
    document.body.appendChild(ctxMenu);
    let ctxEditorTarget: any = null;

    container.addEventListener("contextmenu", (ev: MouseEvent) => {
      ev.preventDefault();
      ctxEditorTarget = view;
      renderContextMenuItems(
        [
          {
            action: "copy",
            icon: "copy",
            label: t("sessionInner.termCopy"),
            onClick: () => {
              const sel = ctxEditorTarget?.getSelection();
              const model = ctxEditorTarget?.getModel();
              const text = sel && model && !sel.isEmpty() ? model.getValueInRange(sel) : "";
              if (text) navigator.clipboard.writeText(text).catch(() => {});
            },
          },
          {
            action: "paste",
            icon: "clipboard",
            label: t("sessionInner.termPaste"),
            onClick: () => {
              navigator.clipboard.readText().then((text) => {
                if (!text || !ctxEditorTarget) return;
                const sel = ctxEditorTarget.getSelection();
                if (sel) {
                  ctxEditorTarget.executeEdits("ctx-paste", [{ range: sel, text, forceMoveMarkers: true }]);
                }
                ctxEditorTarget.focus();
              }).catch(() => {});
            },
          },
          { divider: true },
          {
            action: "send",
            icon: "send",
            label: t("sessionInner.termSendToChat"),
            onClick: () => void this._sendCodeToChat(ctxEditorTarget, filePath),
          },
          ...pluginMenuSpecs("code_editor", { path: filePath }),
        ],
        { container: ctxMenu },
      );
      showContextMenu(ctxMenu, ev.clientX, ev.clientY, { target: container });
    }, true);

    const outsideClick = (e: MouseEvent) => {
      if (!ctxMenu.contains(e.target as Node)) {
        hideAnimated(ctxMenu);
      }
    };
    document.addEventListener("click", outsideClick);

    (view as any)._ctxMenuCleanup = () => {
      ctxMenu.remove();
      document.removeEventListener("click", outsideClick);
    };
  }

  /** Add the (possibly modified) content of a code editor to the conversation. */
  private async _sendCodeToChat(view: any, filePath: string): Promise<void> {
    const api = (window as any).electronAPI;
    const name = filePath.split(/[/\\]/).pop() || filePath;
    let mime = "text/plain";
    if (api && filePath) {
      try {
        const r = await api.readFile(filePath);
        if (r) mime = r.mime_type || "text/plain";
      } catch {}
    }
    const content = view.getValue() || "";
    const att: AttachmentMeta = {
      name, path: filePath,
      content,
      mime_type: mime,
      size: content.length,
      is_binary: false,
    };
    addAttachments([att]);
  }

  private _reviewLoad: ((filePath?: string) => Promise<void>) | null = null;
  private _reviewMode: "git" | "artifact" = "git";
  private _reviewFilter: string = "all";
  private _reviewArtifact: ArtifactItem | null = null;
  private _reviewRequestSeq = 0;
  private _reviewDiffCache: Map<string, string> | null = null;
  private _reviewTreeCache = new Map<string, string>();
  // Review caches are bounded: the panel re-renders on every git
  // status/diff round, so without a cap these maps grow for the whole
  // renderer lifetime. The oldest entry is evicted first (Map insertion
  // order), which also keeps the hot entries (recent diffs) resident.
  private cachePut(cache: Map<string, string>, key: string, value: string, max = 300): void {
    if (cache.size >= max) {
      const oldest = cache.keys().next().value;
      if (oldest !== undefined) cache.delete(oldest);
    }
    cache.set(key, value);
  }
  // Review display toggles (set by the action menu).
  private _reviewWrap = false;        // word wrap
  private _reviewFullFile = true;     // false => truncate large diffs
  private _reviewWordDiff = false;     // inline word diff
  private _reviewHideWs = false;       // dim whitespace-only lines
  private _reviewSplitView = false;    // two-column split diff
  private _reviewCollapsed = false;    // collapse all diff bodies
  // Context key last used to render the review panel (workspace/session/mode).
  // Switching context resets per-panel view toggles so one session's collapse
  // /split state doesn't leak into another conversation or mode.
  private _reviewContextKey = "";
  // Selected git action for the commit/push/pr trigger.
  private _reviewGitAction: "commit" | "push" | "pull" = "commit";
  // How many side buttons are currently merged into the �?overflow menu.
  private _reviewOverflow = 0;
  private _reviewDiffEl: HTMLElement | null = null;  // ref for CSS class toggling
  // Live re-apply hook owned by the mounted review panel: it re-reads the two
  // Settings-owned toggles and re-renders when the split flag flips (see
  // `_applyReviewSettings`).  Null while no review panel is mounted.
  private _reviewApply: (() => void) | null = null;
  private _reviewSettingsSig = "";
  // Observers created by setupReviewPanel; disconnected before the next setup
  // so repeatedly rendering the review panel doesn't accumulate live
  // observers on detached panels.
  private _reviewObservers: Array<ResizeObserver | MutationObserver> = [];

  /** Public entry point called from App when a "View Changes" button is clicked. */
  /** Un-hide the panel so a tab opened from the chat is actually visible.
   *  A hidden panel still accepts tabs -- they just land where nobody can
   *  see them, which read as "clicking the file does nothing". */
  public revealPanel(): void {
    const panel = document.getElementById("session-inner-sidebar");
    const mainBody = document.getElementById("main-body");
    if (!panel || !mainBody || !panel.classList.contains("hidden")) return;
    panel.classList.remove("hidden");
    mainBody.classList.remove("sidebar-hidden");
    document.getElementById("app")?.classList.add("sidebar-collapsed");
    // Re-render info panel content since it was skipped while hidden
    this.renderForce();
    this.saveSidebarVisibility();
  }

  public showReviewTab(path: string, artifact?: ArtifactItem): void {
    // Ensure sidebar is visible when showing review
    this.revealPanel();
    if (artifact) {
      this._reviewFilter = "lastRound";
      this._reviewMode = "artifact";
      this._reviewArtifact = artifact;
    } else {
      // No artifact: review git changes when a workspace is open, otherwise
      // fall back to the session's artifacts (all changes).
      const hasWs = !!getState().activeWorkspace;
      this._reviewFilter = hasWs ? "all" : "lastRound";
      this._reviewMode = hasWs ? "git" : "artifact";
      this._reviewArtifact = null;
    }
    this.openReviewTab(path);
  }

  private closeReviewTab(): void {
    this.closeTab("review");
  }

  private openReviewTab(filePath?: string): void {
    const createNew = !this.tabs.some((t) => t.type === "review");
    if (createNew) {
      this.tabs.push({ id: "review", type: "review", label: tabLabel("review"), closable: true });
      this._reviewFilePending = filePath;
    }
    this.activeTab = "review";
    this.renderTabs();
    if (!createNew) {
      if (this._reviewLoad) this._reviewLoad(filePath);
    }
  }
  private _reviewFilePending: string | undefined;

  /* ── Tab Events ──────────────────────────────────────────────────── */

  private bindTabEvents(): void {
    // Close affordance for sidebar tabs is the icon-slot × (mode-chip
    // pattern); tooltips below carry data-tab from the
    // parent <button>. Also attach middle-click-to-close on the tab itself.
    const closeTab = (el: HTMLElement) => {
      const tabEl = el.closest(".tab") as HTMLElement;
      if (!tabEl) return;
      const id = tabEl.dataset.tab;
      if (id) this.closeTab(id);
    };
    this.tabList.addEventListener("wheel", (e) => {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        this.tabList.scrollLeft += e.deltaY;
      }
    }, { passive: false });
    this.tabList.querySelectorAll(".tab").forEach((tab, idx) => {
      const el = tab as HTMLElement;
      el.dataset.idx = String(idx);

      el.addEventListener("mousedown", (e) => {
        if (e.button === 1) e.preventDefault();
      });
      /* mode-chip pattern: the icon element carries the close click — it is
         the transparent-but-present click target behind the × pseudo-element
         that appears over it on hover.  Non-closable tabs keep plain icon
         behaviour (no handler, clicks fall through to activation). */
      const iconWrap = el.querySelector(".tab-icon-wrap");
      if (iconWrap && el.classList.contains("closable")) {
        iconWrap.addEventListener("click", (e) => {
          e.stopPropagation();
          closeTab(el);
        });
      }
      const onIcon = (e: Event) =>
        el.classList.contains("closable") && !!(e.target as HTMLElement).closest(".tab-icon-wrap");
      el.addEventListener("auxclick", (e) => {
        if ((e as MouseEvent).button === 1) {
          e.preventDefault();
          closeTab(el);
        }
      });

      /* Activate on click (unless dragged; icon click on closable tabs = close) */
      el.addEventListener("click", (e) => {
        if (onIcon(e)) return;
        if (this.wasDragged) { this.wasDragged = false; return; }
        const id = el.dataset.tab!;
        if (id !== this.activeTab) this.activateTab(id);
      });

      /* Drag start — only mark the element, DON'T apply .dragging yet so a
         plain click doesn't flash the drag visual. */
      el.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        if (onIcon(e)) return;
        this.wasDragged = false;
        this.dragEl = el;
        this.dragIdx = parseInt(el.dataset.idx ?? "-1");
        this.dragStartX = e.clientX;
        this.dragSlot = this.dragIdx;
        this.dragBarEl = null;
        e.preventDefault();
      });
    });

    if (!this.dragBound) {
      this.dragBound = true;
      document.addEventListener("mousemove", (e) => {
        if (!this.dragEl) return;
        const dx = e.clientX - this.dragStartX;
        /* Only consider it a drag if moved more than 5px */
        if (!this.wasDragged && Math.abs(dx) > 5) {
          this.wasDragged = true;
          /* Sample the untransformed geometry once, before any transform. */
          const base = this.dragEl.getBoundingClientRect();
          this.dragBaseLeft = base.left;
          this.dragBaseWidth = base.width;
          this.dragEl.classList.add("dragging");
          this._showDragOverlay();
        }
        if (!this.wasDragged) return;
        /* Clamp the slide to the strip's own extent: Edge never lets the
           dragged tab leave the bar.  Bounds come from the geometry sampled
           at drag start, not from the live rect — the live rect already
           contains the clamped transform and would drift the bounds. */
        const barRect = this.tabList.getBoundingClientRect();
        const cdx = Math.max(
          barRect.left - this.dragBaseLeft,
          Math.min(dx, barRect.right - this.dragBaseLeft - this.dragBaseWidth),
        );
        this.dragEl.style.transform = `translateX(${cdx}px)`;

        /* Slot-based insertion geometry: the slot counts how many non-dragged
           tabs' midpoints the pointer has passed.  Unlike per-rect half
           splitting this has no dead zones (gutters / the dragged tab's own
           translated body) and no edge flips inside no-op zones — the bar
           only appears where dropping would actually change the order:
             slot < dragIdx → bar on others[slot]           left edge
             slot > dragIdx → bar on others[slot - 1]       right edge
             slot == dragIdx → no bar (back to its own slot). */
        const others = ([...this.tabList.querySelectorAll(".tab")] as HTMLElement[]).filter((t) => t !== this.dragEl);
        const firstRect = others.length ? others[0].getBoundingClientRect() : null;
        const lastRect = others.length ? others[others.length - 1].getBoundingClientRect() : null;
        /* Tabs lay left-to-right in LTR and right-to-left in RTL (ar/he). */
        const ltr = !firstRect || !lastRect || firstRect.left <= lastRect.left;
        let slot = 0;
        for (const t of others) {
          const r = t.getBoundingClientRect();
          const mid = r.left + r.width / 2;
          if (ltr ? e.clientX > mid : e.clientX < mid) slot++;
        }
        /* Edge override: a pointer pinned at (or past) a strip edge means the
           extreme slot.  Midpoints of tabs scrolled out of view are
           unreachable, which made a drag-to-the-end land one position short —
           the bar stuck between the last two tabs instead of after the last
           one (and symmetrically at the left edge). */
        if (others.length) {
          if (ltr ? e.clientX >= barRect.right : e.clientX <= barRect.left) slot = others.length;
          else if (ltr ? e.clientX <= barRect.left : e.clientX >= barRect.right) slot = 0;
        }
        let barEl: HTMLElement | null = null;
        let after = false;
        if (others.length > 0 && slot !== this.dragIdx) {
          if (slot < this.dragIdx) {
            barEl = others[slot];
          } else {
            barEl = others[Math.min(slot, others.length) - 1];
            after = true;
          }
        }
        if (slot !== this.dragSlot) {
          if (this.dragBarEl) this.dragBarEl.classList.remove("drop-target", "drop-after");
          this.dragBarEl = barEl;
          this.dragSlot = slot;
          if (barEl) {
            barEl.classList.add("drop-target");
            if (after) barEl.classList.add("drop-after");
          }
        }
      });

      document.addEventListener("mouseup", () => {
        if (!this.dragEl) return;
        this.dragEl.style.transform = "";
        this.dragEl.classList.remove("dragging");
        this._hideDragOverlay();
        this.tabList.querySelectorAll(".drop-target").forEach((t) => t.classList.remove("drop-target", "drop-after"));

        if (this.wasDragged && this.dragSlot >= 0 && this.dragSlot !== this.dragIdx) {
          /* The slot counts non-dragged tabs; splicing the dragged tab out
             then back in at the slot lands it exactly between them. */
          const moved = this.tabs.splice(this.dragIdx, 1)[0];
          this.tabs.splice(this.dragSlot, 0, moved);
          _saveTabs(this.tabs);
          this.renderTabs();
        }

        this.dragEl = null;
        this.dragIdx = -1;
        this.dragSlot = -1;
        this.dragBarEl = null;
      });
    }
  }

  public activateTab(id: string): void {
    this.activeTab = id;
    this.tabList.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", (t as HTMLElement).dataset.tab === id);
    });
    // Remove the .si-home placeholder when switching to a real tab so it
    // doesn't remain visible underneath the .tab-panel.
    const homeEl = this.tabBody.querySelector(".si-home");
    if (homeEl && id !== "home") homeEl.remove();
    this.tabBody.querySelectorAll(".tab-panel").forEach((p) => {
      p.classList.toggle("active", (p as HTMLElement).dataset.panel === id);
    });
    if (id === "info") this.renderContent();
    if (id === "review" && getState().activeWorkspace && this._reviewFilter !== "all") {
      this._reviewFilter = "all";
      this._reviewMode = "git";
      this._reviewArtifact = null;
      if (this._reviewLoad) this._reviewLoad(undefined);
    }
    // Switching to a panel with a live-refreshing tree (editor) should show
    // the current disk state immediately instead of waiting for the next poll.
    const target = this.tabBody.querySelector(`.tab-panel[data-panel="${id}"]`) as HTMLElement | null;
    if (target && typeof (target as any)._siRefreshTree === "function") {
      (target as any)._siRefreshTree();
    }
  }

  /** Set the breathing light state for a tab by id. */
  public setTabState(tabId: string, state: "" | "running" | "awaiting_approval" | "permission"): void {
    const tab = this.tabs.find(t => t.id === tabId);
    if (tab) tab.state = state;
    const btn = this.tabList.querySelector(`.tab[data-tab="${tabId}"]`);
    if (!btn) return;
    const wrap = btn.querySelector(".tab-icon-wrap") as HTMLElement;
    if (!wrap) return;
    let dot = wrap.querySelector(".tab-indicator-pos") as HTMLElement;
    if (state) {
      const cls = state === "running" ? "session-running" : state === "awaiting_approval" ? "session-waiting" : "session-orange";
      if (!dot) {
        dot = document.createElement("span");
        dot.className = cls + " tab-indicator-pos";
        wrap.insertBefore(dot, wrap.firstChild);
      } else {
        dot.className = cls + " tab-indicator-pos";
      }
    } else {
      if (dot) dot.remove();
    }
  }

  private async closeTab(id: string): Promise<void> {
    const idx = this.tabs.findIndex((tab) => tab.id === id);
    if (idx < 0) return;
    const tab = this.tabs[idx];

    // Unsaved code editors: ask for confirmation inside the sidebar before closing.
    if (tab.type === "code" && this._dirtyTabs.get(id)) {
      const name = tab.label || (tab.filePath ? tab.filePath.split(/[/\\]/).pop() || tab.filePath : tab.id);
      const ok = await Dialog.confirmIn(
        this.el,
        t("sessionInner.closeUnsavedTitle"),
        t("sessionInner.closeUnsavedMessage", { name }),
        { primary: t("sessionInner.discardChanges") },
      );
      if (!ok) return;
    }
    if (tab.type === "terminal") {
      const terms = this.panelTerminals.get(id);
      if (terms) {
        for (const t of terms) {
          t.cleanup();
          t.resizeObs.disconnect();
          t.term.dispose();
          const api = (window as any).electronAPI;
          if (api) api.terminalKill(t.ptyId);
        }
        this.panelTerminals.delete(id);
        this.panelActiveTermIdx.delete(id);
      }
      const dd = document.querySelector(`#term-ctx-menu-${id}`);
      if (dd) dd.remove();
    }
    if (tab.type === "browser") {
      const bv = this.panelBrowsers.get(id);
      if (bv) {
        bv.destroy();
        this.panelBrowsers.delete(id);
      }
    }
    if (tab.type === "markdown") {
      const mp = this.panelMarkdownPreviews.get(id);
      if (mp) {
        mp.destroy();
        this.panelMarkdownPreviews.delete(id);
      }
    }
    if (tab.type === "code") {
      const editor = this.panelCodeEditors.get(id);
      if (editor) {
        if (typeof (editor as any)._ctxMenuCleanup === "function") {
          (editor as any)._ctxMenuCleanup();
        }
        unregisterEditor(editor);
        editor.dispose();
        this.panelCodeEditors.delete(id);
      }
      this._dirtyTabs.delete(id);
    }
    if (tab.type === "media") {
      const mv = this.panelMediaViewers.get(id);
      if (mv) {
        mv.destroy();
        this.panelMediaViewers.delete(id);
      }
    }
    if (tab.type === "office") {
      const c = this.panelOfficeControllers.get(id);
      if (c && typeof (c as any)._officeCleanup === "function") (c as any)._officeCleanup();
      this.panelOfficeControllers.delete(id);
    }
    const subapp = this.panelSubapps.get(id);
    if (subapp) {
      subapp();
      this.panelSubapps.delete(id);
    }
    this.tabs.splice(idx, 1);
    _saveTabs(this.tabs);
    if (this.tabs.length === 0) {
      // The subwindow exists for its views; with the last one closed there is
      // nothing left to keep it open.
      if (inChildWindow()) {
        void window.electronAPI?.windowClose?.();
        return;
      }
      this.tabs = [];
      this.activeTab = "";
    } else if (this.activeTab === id) {
      this.activeTab = this.tabs[0].id;
    }
    this.renderTabs();
  }

  /* ── Drag overlay ──────────────────────────────────────────────── */
  /* A full-window transparent overlay shown while dragging. Electron's
     <webview> runs as a separate process and swallows mouse events once the
     cursor enters the page, which would freeze document-level mousemove/
     mouseup handlers. Covering it with an overlay keeps events in the main
     document so drags (tab reorder / resize) keep working over a webpage. */

  private _showDragOverlay(): void {
    let ov = document.getElementById("si-drag-overlay") as HTMLElement | null;
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "si-drag-overlay";
      ov.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483647;background:transparent;";
      document.body.appendChild(ov);
    }
    ov.style.display = "block";
  }

  private _hideDragOverlay(): void {
    const ov = document.getElementById("si-drag-overlay");
    if (ov) ov.style.display = "none";
  }

  /* ── Resize ─────────────────────────────────────────────────────── */

  private bindResize(): void {
    const handle = this.el.querySelector(".si-resize-handle") as HTMLElement;
    if (!handle) return;

    handle.addEventListener("mousedown", (e) => {
      this.resizing = true;
      handle.classList.add("resizing");
      this.resizeStartX = e.clientX;
      this.resizeStartW = this.el.offsetWidth;
      this._showDragOverlay();
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
    });

    document.addEventListener("mousemove", (e) => {
      if (!this.resizing) return;
      const dx = this.resizeStartX - e.clientX;
      const newW = Math.min(800, Math.max(200, this.resizeStartW + dx));
      this.el.style.setProperty("--sidebar-w", newW + "px");
      document.getElementById("main-body")?.style.setProperty("--sidebar-w", newW + "px");
      this.el.classList.add("is-resizing");
    });

    document.addEventListener("mouseup", () => {
      if (!this.resizing) return;
      this.resizing = false;
      const handle = this.el.querySelector(".si-resize-handle") as HTMLElement;
      if (handle) handle.classList.remove("resizing");
      this._hideDragOverlay();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      this.el.classList.remove("is-resizing");
      this.sidebarWidth = this.el.offsetWidth;
      try { localStorage.setItem("session-sidebar-width", String(this.sidebarWidth)); } catch {}
    });
  }

  getSavedWidth(): number {
    return this.sidebarWidth;
  }

  saveWidth(): void {
    this.sidebarWidth = this.el.offsetWidth;
    try { localStorage.setItem("session-sidebar-width", String(this.sidebarWidth)); } catch {}
  }

  restoreWidth(): void {
    this.el.style.setProperty("--sidebar-w", this.sidebarWidth + "px");
    document.getElementById("main-body")?.style.setProperty("--sidebar-w", this.sidebarWidth + "px");
  }

  saveSidebarVisibility(): void {
    // The subwindow's tab layer is always the whole window; its visibility is
    // not the main window's sidebar state and must not be remembered as such.
    if (inChildWindow()) return;
    const panel = document.getElementById("session-inner-sidebar");
    if (!panel) return;
    this._sessionSidebarVisible.set(this._tabKey, !panel.classList.contains("hidden"));
  }

  /** Record whether the user manually collapsed the inner sidebar. Persisted
   *  so a browser tool call never auto-reopens the panel against the user's
   *  will — the flag survives restarts and is only cleared by a manual
   *  restore through the session menu button. */
  markUserSidebarCollapsed(collapsed: boolean): void {
    try {
      if (collapsed) localStorage.setItem("session-sidebar-user-collapsed", "1");
      else localStorage.removeItem("session-sidebar-user-collapsed");
    } catch {}
  }

  isUserSidebarCollapsed(): boolean {
    try {
      return localStorage.getItem("session-sidebar-user-collapsed") === "1";
    } catch {
      return false;
    }
  }

  restoreSidebarVisibility(): void {
    if (inChildWindow()) return;
    const panel = document.getElementById("session-inner-sidebar");
    const mainBody = document.getElementById("main-body");
    const appEl = document.getElementById("app");
    if (!panel || !mainBody) return;
    const visible = this._sessionSidebarVisible.get(this._tabKey) ?? false;
    if (visible) {
      this.restoreWidth();
      panel.classList.remove("hidden");
      mainBody.classList.remove("sidebar-hidden");
      appEl?.classList.add("sidebar-collapsed");
      this.renderForce();
    } else {
      panel.classList.add("hidden");
      mainBody.classList.add("sidebar-hidden");
      // The inner sidebar's open state collapsed the app sidebar; undo that
      // so switching to a session without the inner sidebar re-expands it.
      (window as any).__appRestoreSidebar?.();
    }
  }

  /* ── Collapsible Panel State ────────────────────────────────────── */
  private collapsedPanels = new Set<string>();

  private togglePanel(panelId: string): void {
    if (this.collapsedPanels.has(panelId)) {
      this.collapsedPanels.delete(panelId);
    } else {
      this.collapsedPanels.add(panelId);
    }
    this.renderContent();
  }

  /* ── Content Rendering ──────────────────────────────────────────── */

  private renderContent(): void {
    if (!this.infoBody) return;
    const st = getState();

    const panels: string[] = [];
    // Each panel is its own boundary: one bad artifact/reference/count must
    // not remove the other panels from the side area.
    const addPanel = (id: string, render: () => string) =>
      panels.push(guardHtml("session-panel", id, render, ""));
    addPanel("rules", () => this.renderProjectRulesPanel(st));
    addPanel("hooks", () => this.renderHooksPanel(st));
    addPanel("progress", () => this.renderProgressPanel(st));
    addPanel("artifacts", () => this.renderArtifactsPanel(st));
    addPanel("references", () => this.renderReferencesPanel(st));
    addPanel("canvas", () => this.renderCanvasPanel(st));

    const html = `<div class="si-panels">${panels.join("")}</div>`;
    // State mutated but the info panel content did not change (typical during
    // streaming / status churn): keep the existing DOM so the sidebar does
    // not flash on every emit.  Real content changes rebuild as usual.
    if (this._lastInfoHtml === html) return;
    this._lastInfoHtml = html;

    // Keep the reader's position: a tool boundary swaps the panel markup
    // several times per turn, and resetting to the top reads as the sidebar
    // jumping on its own.
    const prevScrollTop = this.infoBody.scrollTop;
    this.infoBody.innerHTML = html;
    this.infoBody.scrollTop = prevScrollTop;
    this.bindPanelToggles();
    this.bindReviewLink();
    safeCreateIcons(this.infoBody);
  }

  private bindPanelToggles(): void {
    this.infoBody.querySelectorAll(".si-panel-header").forEach((header) => {
      header.addEventListener("click", () => {
        const panelId = (header as HTMLElement).dataset.panel!;
        this.togglePanel(panelId);
      });
    });
  }

  private bindReviewLink(): void {
    const link = document.getElementById("si-view-all-changes");
    if (link) {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        this.showReviewTab("", undefined);
      });
    }
    this.infoBody.querySelectorAll(".si-diff-review").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const path = (el as HTMLElement).dataset.path!;
        const st = getState();
        const artifact = st.artifacts.find((a: ArtifactItem) => a.path === path);
        this.showReviewTab(path, artifact);
      });
    });
  }

  private panelHeader(id: string, label: string, icon: string, badge?: string): string {
    const collapsed = this.collapsedPanels.has(id) ? " si-panel-collapsed" : "";
    const chevron = this.collapsedPanels.has(id) ? "chevron-right" : "chevron-down";
    const badgeHTML = badge ? `<span class="si-panel-badge">${this.esc(badge)}</span>` : "";
    return `<div class="si-panel-header${collapsed}" data-panel="${id}">
      <i data-lucide="${icon}" class="lucide si-panel-header-icon"></i>
      <span class="si-panel-title">${label}</span>
      ${badgeHTML}
      <i data-lucide="${chevron}" class="lucide si-panel-chevron"></i>
    </div>`;
  }

  /* ── Progress Panel (Todo / Plan Items) ─────────────────────────── */

  private renderProgressPanel(st: ReturnType<typeof getState>): string {
    const items = st.planItems || [];
    let bodyHTML = "";

    if (items.length === 0) {
      bodyHTML = `<div class="si-empty-center"><i data-lucide="list-checks" class="lucide"></i><span class="si-empty-title">${t("sessionInner.noProgress")}</span></div>`;
    } else {
      bodyHTML = items.map((item) => {
        let iconClass = "si-todo-pending";
        let iconSvg = "circle";
        if (item.status === "done") { iconClass = "si-todo-done"; iconSvg = "check-circle-2"; }
        else if (item.status === "active") { iconClass = "si-todo-active"; iconSvg = "loader"; }
        return `<div class="si-todo-item ${iconClass}">
          <i data-lucide="${iconSvg}" class="lucide lucide-sm si-todo-icon"></i>
          <span class="si-todo-text">${this.esc(item.text)}</span>
        </div>`;
      }).join("");
    }

    return `<div class="si-panel">
      ${this.panelHeader("progress", t("sessionInner.progress"), "list-checks")}
      <div class="si-panel-body${this.collapsedPanels.has("progress") ? " hidden" : ""}"><div class="si-panel-inner">${bodyHTML}</div></div>
    </div>`;
  }

  /* ── Artifacts Panel (File Changes) ─────────────────────────────── */

  private renderArtifactsPanel(st: ReturnType<typeof getState>): string {
    const artifacts = st.artifacts;
    let bodyHTML = "";

    if (!artifacts || artifacts.length === 0) {
      bodyHTML = `<div class="si-empty-center"><i data-lucide="file-code-2" class="lucide"></i><span class="si-empty-title">${t("sessionInner.noArtifacts")}</span></div>`;
    } else {
      const countBadge = `${t("sessionInner.changedFiles")} ${artifacts.length}`;
      bodyHTML = `<div class="si-artifacts-header">
        <span>${countBadge}</span>
        <a class="si-artifacts-link" id="si-view-all-changes" href="#">${t("sessionInner.viewAllChanges")} <i data-lucide="arrow-up-right" class="lucide lucide-xs"></i></a>
      </div>
      <div class="si-diff-list">${artifacts.map((a) => {
        const extIcon = getFileIcon(a.name);
        return `<div class="si-diff-file" data-path="${this.esc(a.path)}">
          <img src="${extIcon}" class="si-diff-icon icon" style="width:14px;height:14px">
          <span class="si-diff-name">${this.esc(a.name)}</span>
          <span class="si-diff-stats">
            <span class="si-diff-add">+${a.diff_text ? (a.diff_text.match(/^\+/gm) || []).length : (a.size > 0 ? Math.min(a.size, 9999) : 0)}</span>
            <span class="si-diff-remove">-${a.diff_text ? (a.diff_text.match(/^-/gm) || []).length : 0}</span>
          </span>
          <a class="si-diff-review" href="#" data-path="${this.esc(a.path)}">${t("sessionInner.viewAllChanges")} <i data-lucide="file-search" class="lucide lucide-xs"></i></a>
        </div>`;
      }).join("")}</div>`;
    }

    return `<div class="si-panel">
      ${this.panelHeader("artifacts", t("sessionInner.artifacts"), "file-code-2")}
      <div class="si-panel-body${this.collapsedPanels.has("artifacts") ? " hidden" : ""}"><div class="si-panel-inner">${bodyHTML}</div></div>
    </div>`;
  }

  /* ── References Panel ───────────────────────────────────────────── */

  private renderReferencesPanel(st: ReturnType<typeof getState>): string {
    const refs = st.references || [];
    const filteredRefs = refs.filter((r: ReferenceItem) => {
      const t = r.tool.toLowerCase();
      return t.startsWith("mcp__") || t.startsWith("memory_") || t.startsWith("web_") || t.includes("profile");
    });

    if (filteredRefs.length === 0) {
      const bodyHTML = `<div class="si-empty-center"><i data-lucide="link-2" class="lucide"></i><span class="si-empty-title">${t("sessionInner.noRefs")}</span></div>`;
      return `<div class="si-panel">
        ${this.panelHeader("references", t("sessionInner.references"), "link-2")}
        <div class="si-panel-body${this.collapsedPanels.has("references") ? " hidden" : ""}"><div class="si-panel-inner">${bodyHTML}</div></div>
      </div>`;
    }

    // Show most recent references first, limit to 50
    const shown = filteredRefs.slice(-50).reverse();

    // Group references by broad category so each class can be collapsed.
    const groups: { key: string; label: string; icon: string; refs: ReferenceItem[] }[] = [
      { key: "ref-group-web", label: t("sessionInner.referencesWeb") || "Web", icon: "globe", refs: [] },
      { key: "ref-group-memory", label: t("sessionInner.referencesMemory") || "Memory", icon: "brain", refs: [] },
      { key: "ref-group-mcp", label: t("sessionInner.referencesMcp") || "MCP", icon: "cable", refs: [] },
    ];
    for (const r of shown) {
      const t = r.tool.toLowerCase();
      if (t.startsWith("mcp__")) {
        groups[2].refs.push(r);
      } else if (t.includes("memory") || t.includes("profile")) {
        groups[1].refs.push(r);
      } else {
        groups[0].refs.push(r);
      }
    }

    const bodyHTML = groups
      .filter((g) => g.refs.length > 0)
      .map((g) => {
        const listHTML = g.refs.map((r: ReferenceItem) => {
          const icon = r.icon || "";
          const toolLabel = r.tool.startsWith("mcp__") ? r.tool.split("__").slice(0, 2).join(":") : r.tool;
          return `<div class="si-ref-item">
            ${icon ? `<i data-lucide="${icon}" class="lucide si-ref-icon"></i>` : ""}
            <div class="si-ref-content">
              <span class="si-ref-tool">${this.esc(toolLabel)}</span>
              <span class="si-ref-summary">${this.esc(r.summary)}</span>
            </div>
          </div>`;
        }).join("");
        return `<div class="si-ref-group">
          ${this.panelHeader(g.key, g.label, g.icon, String(g.refs.length))}
          <div class="si-panel-body${this.collapsedPanels.has(g.key) ? " hidden" : ""}"><div class="si-panel-inner si-ref-list">${listHTML}</div></div>
        </div>`;
      }).join("");

    return `<div class="si-panel">
      ${this.panelHeader("references", t("sessionInner.references"), "link-2", String(filteredRefs.length))}
      <div class="si-panel-body${this.collapsedPanels.has("references") ? " hidden" : ""}"><div class="si-panel-inner">${bodyHTML}</div></div>
    </div>`;
  }

  /* ── Canvas Panel (Context / Tokens / Info) ─────────────────────── */

  private renderCanvasPanel(st: ReturnType<typeof getState>): string {
    const models = st.modelConfigs;
    const active = models[st.activeModelIndex];
    // Prefer the server-reported window: it tracks the ACTIVE model and
    // follows every switch (manual pick or pool fallback) in real time,
    // while modelConfigs[i] is a static snapshot that goes stale the
    // moment the active model changes (128K ↔ 1M bug).
    const contextLimit = st.contextWindow || (active ? (active.context_window || active.max_tokens || 0) : 0);
    const telemetry = st.telemetry;
    const tu = st.tokenUsage;

    // Context usage: use server-sent context_tokens (actual message count),
    // NOT output/usage tokens (which are minuscule vs context window).
    // Falls back to last compact event's new_tokens if not yet available.
    const lastCompact = (st.compactEvents || []).length > 0 ? st.compactEvents[st.compactEvents.length - 1] : null;
    const totalUsed = st.contextTokens || lastCompact?.new_tokens || 0;
    const usagePct = contextLimit > 0 ? ((totalUsed / contextLimit) * 100).toFixed(1) : null;
    const usageWarn = usagePct !== null && parseFloat(usagePct) > 80;
    const usageDanger = usagePct !== null && parseFloat(usagePct) > 95;

    let progressFillClass = "";
    if (usageDanger) progressFillClass = " danger";
    else if (usageWarn) progressFillClass = " warn";

    const rows: string[] = [];

    // Progress bar
    if (usagePct !== null) {
      rows.push(`<div class="si-canvas-progress-wrap">
        <div class="si-canvas-progress-track">
          <div class="si-canvas-progress-fill${progressFillClass}" style="width:${Math.min(parseFloat(usagePct), 100)}%"></div>
        </div>
      </div>`);
    }

    if (contextLimit > 0) {
      rows.push(`<div class="si-canvas-row">
        <span class="si-canvas-label">${t("sessionInner.contextWindow")}</span>
        <span class="si-canvas-value">${this.fmtNum(contextLimit)}</span>
      </div>`);
    }

    rows.push(`<div class="si-canvas-row">
      <span class="si-canvas-label">${t("sessionInner.contextUsed")}</span>
      <span class="si-canvas-value${usageWarn ? " si-canvas-warn" : ""}">${this.fmtNum(totalUsed)}${usagePct !== null ? ` <span class="si-canvas-pct">(${usagePct}%)</span>` : ""}</span>
    </div>`);

    if (tu) {
      rows.push(`<div class="si-canvas-row">
        <span class="si-canvas-label">${t("sessionInner.inputTokens")}</span>
        <span class="si-canvas-value">${this.fmtNum(tu.input_tokens)}</span>
      </div>`);
      rows.push(`<div class="si-canvas-row">
        <span class="si-canvas-label">${t("sessionInner.outputTokens")}</span>
        <span class="si-canvas-value">${this.fmtNum(tu.output_tokens)}</span>
      </div>`);
    }

    // Derive Compactions / Tool Calls from authoritative local state so the
    // canvas panel always shows real numbers, even if backend telemetry is
    // disabled or not yet streamed.
    const compactCount = st.compactEvents.length
      + (telemetry?.compactions ? Math.max(0, telemetry.compactions - st.compactEvents.length) : 0);
    const toolCallCount = st.messages.reduce(
      (sum, m) => sum + m.toolCalls.length, 0,
    );
    const hasCompactOrTools = compactCount > 0 || toolCallCount > 0 || telemetry;
    if (hasCompactOrTools) {
      rows.push(`<div class="si-canvas-divider"></div>`);
      rows.push(`<div class="si-canvas-row">
        <span class="si-canvas-label">${t("sessionInner.compactions")}</span>
        <span class="si-canvas-value">${compactCount}</span>
      </div>`);
      rows.push(`<div class="si-canvas-row">
        <span class="si-canvas-label">${t("sessionInner.toolCalls")}</span>
        <span class="si-canvas-value">${toolCallCount}</span>
      </div>`);
    }

    const turns = st.messages.filter((m) => m.role === "user").length;
    rows.push(`<div class="si-canvas-divider"></div>`);
    rows.push(`<div class="si-canvas-row">
      <span class="si-canvas-label">${t("sessionInner.turns")}</span>
      <span class="si-canvas-value">${turns}</span>
    </div>`);

    const bodyHTML = rows.length > 0
      ? `<div class="si-canvas-grid">${rows.join("")}</div>`
      : `<div class="si-empty-center"><i data-lucide="sliders" class="lucide"></i><span class="si-empty-title">${t("sessionInner.noData")}</span></div>`;

    return `<div class="si-panel">
      ${this.panelHeader("canvas", t("sessionInner.canvas"), "sliders")}
      <div class="si-panel-body${this.collapsedPanels.has("canvas") ? " hidden" : ""}"><div class="si-panel-inner">${bodyHTML}</div></div>
    </div>`;
  }

  private renderProjectRulesPanel(st: ReturnType<typeof getState>): string {
    if (st.workspaceMode !== "iwork") return "";
    const rules = st.projectRules || [];
    let bodyHTML: string;
    if (rules.length === 0) {
      bodyHTML = `<div class="si-empty-center"><i data-lucide="book-open-check" class="lucide"></i><span class="si-empty-title">${t("sessionInner.noProjectRules")}</span></div>`;
    } else {
      const rows = rules.map((r) => {
        const mod = new Date(r.modified * 1000).toLocaleString();
        return `<div class="si-rule-row">
          <img src="${getFileIcon(r.name)}" class="icon si-rule-icon" style="width:14px;height:14px;flex-shrink:0">
          <div class="si-rule-body">
            <div class="si-rule-name">${this.esc(r.name)}</div>
            <div class="si-rule-path">${this.esc(r.path)}</div>
          </div>
          <span class="si-rule-priority">p${r.priority}</span>
          <span class="si-rule-time">${this.esc(mod)}</span>
        </div>`;
      }).join("");
      bodyHTML = `<div class="si-rule-list">${rows}</div>`;
    }
    return `<div class="si-panel">
      ${this.panelHeader("project-rules", t("sidebar.projectRules"), "book-open-check")}
      <div class="si-panel-body${this.collapsedPanels.has("project-rules") ? " hidden" : ""}"><div class="si-panel-inner">${bodyHTML}</div></div>
    </div>`;
  }

  private renderHooksPanel(st: ReturnType<typeof getState>): string {
    if (st.workspaceMode !== "iwork") return "";
    const hooks = st.projectHooks || [];
    let bodyHTML: string;
    if (hooks.length === 0) {
      bodyHTML = `<div class="si-empty-center"><i data-lucide="webhook" class="lucide"></i><span class="si-empty-title">${t("sessionInner.noProjectHooks")}</span></div>`;
    } else {
      const rows = hooks.map((h) => {
        const evt = this.esc(h.event_type);
        const matcher = this.esc(h.matcher || "*");
        const cmd = this.esc(h.command || "");
        const src = this.esc(h.source_path || "");
        return `<div class="si-hook-row" data-tooltip="${src}">
          <i data-lucide="zap" class="lucide lucide-sm si-hook-icon"></i>
          <div class="si-hook-body">
            <div class="si-hook-event">${evt} <span class="si-hook-matcher">${matcher}</span></div>
            <div class="si-hook-cmd">${cmd}</div>
          </div>
        </div>`;
      }).join("");
      bodyHTML = `<div class="si-hook-list">${rows}</div>`;
    }
    return `<div class="si-panel">
      ${this.panelHeader("hooks", t("sidebar.hooks"), "webhook")}
      <div class="si-panel-body${this.collapsedPanels.has("hooks") ? " hidden" : ""}"><div class="si-panel-inner">${bodyHTML}</div></div>
    </div>`;
  }

  private fmtNum(n: number): string {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  private esc(s: string): string {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }
}

